"""Evaluación de la recuperación sobre el dataset versionado: precision@k, recall@k y fuente.

El corpus del dataset se indexa en un usuario temporal (extracción, fragmentos y embeddings reales
del pipeline), cada pregunta se busca con `search_with_report` y se compara lo recuperado con la
evidencia anotada. Un fragmento es *relevante* si pertenece a un documento con evidencia y recoge
al menos la mitad de sus secuencias de palabras: así una cita partida entre dos fragmentos cuenta
en ambos. Las métricas:

- `precision@k`: fragmentos relevantes / fragmentos devueltos (0 si no se devuelve ninguno).
- `recall@k`: evidencias cubiertas por algún fragmento devuelto / evidencias anotadas. En las
  preguntas ambiguas la evidencia es la unión de todas las lecturas.
- `source_success@k`: 1 si algún fragmento devuelto es de un documento con evidencia.
- `mrr`: inverso de la posición del primer fragmento relevante (0 si no hay).
- Sin respuesta: no hay evidencia que recuperar; se mide la puntuación del mejor fragmento y con
  qué frecuencia algo supera el umbral (cuanto más baja, menos riesgo de dar contexto engañoso).

El informe separa las métricas (deterministas: misma configuración y datos, mismo resultado) de los
tiempos, que varían entre ejecuciones. Con los proveedores falsos las cifras no representan a un
modelo real: sirven para vigilar regresiones del pipeline, no para comparar modelos de verdad.
"""

import argparse
import io
import json
import statistics
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.embeddings.provider import EmbeddingProvider
from app.embeddings.registry import build_embedding_provider
from app.embeddings.store import SimilarChunk
from app.evaluation.dataset import (
    DEFAULT_DATASET,
    Dataset,
    DatasetError,
    Evidence,
    Kind,
    Question,
    load_dataset,
)
from app.features.documents.models import Document
from app.features.documents.states import DocumentStatus
from app.features.documents.storage import LocalFileStorage
from app.features.users.models import User
from app.ingestion.chunking import ChunkPolicy
from app.ingestion.service import process
from app.retrieval.dedup import DedupPolicy, normalize, shingles
from app.retrieval.question import prepare_question
from app.retrieval.search import SearchLimits, search_with_report

REPORT_SCHEMA = 1
COVERAGE_THRESHOLD = 0.5
LEASE_SECONDS = 600
MAX_ATTEMPTS = 1
SCORE_DIGITS = 6
UNUSABLE_HASH = "!"  # nunca coincide con un hash real: nadie puede iniciar sesión
NAMESPACE = uuid.UUID("6f1b0e0e-5a52-4c43-9f7e-2d6f4c1d9a10")
POSITIVE_KINDS = (Kind.ANSWERABLE, Kind.AMBIGUOUS)


@dataclass(frozen=True, slots=True)
class EvalConfig:
    """Configuración evaluada: lo que debe quedar registrado para poder repetir el experimento."""

    embedder: EmbeddingProvider
    policy: ChunkPolicy
    top_k: int = 5
    min_score: float = 0.0
    dedup: DedupPolicy | None = field(default_factory=DedupPolicy)
    question_max_chars: int = 1000

    @classmethod
    def from_settings(cls) -> "EvalConfig":
        """La configuración con la que arranca la aplicación (`EMBEDDING_*`, `CHUNK_*`)"""
        settings = get_settings()
        limits = SearchLimits.from_settings(settings)
        return cls(
            embedder=build_embedding_provider(settings),
            policy=ChunkPolicy.from_settings(settings),
            top_k=limits.default_k,
            min_score=limits.min_score,
            dedup=limits.dedup,
            question_max_chars=settings.question_max_chars,
        )

    def limits(self) -> SearchLimits:
        return SearchLimits(
            default_k=self.top_k,
            max_k=max(self.top_k, 20),
            min_score=self.min_score,
            dedup=self.dedup,
        )

    def describe(self) -> dict[str, Any]:
        spec = self.embedder.spec
        dedup = self.dedup
        return {
            "embedding": {
                "key": spec.key,
                "provider": spec.provider,
                "model": spec.model,
                "dimensions": spec.dimensions,
            },
            "chunking": {
                "key": self.policy.key,
                "size": self.policy.size,
                "overlap": self.policy.overlap,
            },
            "top_k": self.top_k,
            "min_score": self.min_score,
            "dedup": None
            if dedup is None
            else {
                "min_overlap": dedup.min_overlap,
                "window": dedup.window,
                "overfetch": dedup.overfetch,
            },
        }


@dataclass(frozen=True, slots=True)
class HitResult:
    document: str
    ordinal: int
    score: float
    relevant: bool


@dataclass(frozen=True, slots=True)
class QuestionResult:
    """Resultado de una pregunta; las métricas de evidencia son `None` si no la tiene."""

    id: str
    kind: Kind
    hits: tuple[HitResult, ...]
    precision: float | None
    recall: float | None
    source_success: float | None
    reciprocal_rank: float | None
    top_score: float | None
    latency_ms: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "precision": self.precision,
            "recall": self.recall,
            "source_success": self.source_success,
            "reciprocal_rank": self.reciprocal_rank,
            "top_score": self.top_score,
            "hits": [
                {
                    "document": hit.document,
                    "ordinal": hit.ordinal,
                    "score": hit.score,
                    "relevant": hit.relevant,
                }
                for hit in self.hits
            ],
        }


def covers(chunk_text: str, evidence: Evidence, chunk_document: str) -> bool:
    """Si el fragmento recoge la evidencia (del mismo documento): íntegra o al menos la mitad."""
    if chunk_document != evidence.document:
        return False
    if normalize(evidence.quote) in normalize(chunk_text):
        return True
    wanted = shingles(evidence.quote)
    return bool(wanted) and len(wanted & shingles(chunk_text)) / len(wanted) >= COVERAGE_THRESHOLD


def score_question(
    question: Question, hits: Sequence[SimilarChunk], *, latency_ms: float
) -> QuestionResult:
    """Compara lo recuperado con la evidencia anotada y calcula las métricas de la pregunta."""
    marked = [
        (
            hit,
            any(covers(hit.chunk.text, ev, hit.document.filename) for ev in question.evidence),
        )
        for hit in hits
    ]
    results = tuple(
        HitResult(
            hit.document.filename, hit.chunk.ordinal, round(hit.score, SCORE_DIGITS), relevant
        )
        for hit, relevant in marked
    )
    top_score = round(hits[0].score, SCORE_DIGITS) if hits else 0.0
    if question.kind is Kind.UNANSWERABLE:
        return QuestionResult(
            question.id, question.kind, results, None, None, None, None, top_score, latency_ms
        )

    sources = {ev.document for ev in question.evidence}
    covered = sum(
        any(covers(hit.chunk.text, ev, hit.document.filename) for hit in hits)
        for ev in question.evidence
    )
    relevant_ranks = [rank for rank, (_, relevant) in enumerate(marked, start=1) if relevant]
    return QuestionResult(
        question.id,
        question.kind,
        results,
        precision=round(len(relevant_ranks) / len(hits), SCORE_DIGITS) if hits else 0.0,
        recall=round(covered / len(question.evidence), SCORE_DIGITS),
        source_success=1.0 if any(hit.document.filename in sources for hit in hits) else 0.0,
        reciprocal_rank=round(1 / relevant_ranks[0], SCORE_DIGITS) if relevant_ranks else 0.0,
        top_score=top_score,
        latency_ms=latency_ms,
    )


def _mean(values: Sequence[float]) -> float:
    return round(statistics.fmean(values), SCORE_DIGITS) if values else 0.0


def aggregate(results: Sequence[QuestionResult], min_score: float) -> dict[str, Any]:
    """Medias por tipo de pregunta y global de las que tienen evidencia."""

    def positives(subset: Sequence[QuestionResult]) -> dict[str, Any]:
        return {
            "questions": len(subset),
            "precision": _mean([r.precision or 0.0 for r in subset]),
            "recall": _mean([r.recall or 0.0 for r in subset]),
            "source_success": _mean([r.source_success or 0.0 for r in subset]),
            "mrr": _mean([r.reciprocal_rank or 0.0 for r in subset]),
        }

    with_evidence = [r for r in results if r.kind in POSITIVE_KINDS]
    unanswerable = [r for r in results if r.kind is Kind.UNANSWERABLE]
    top_scores = [r.top_score or 0.0 for r in unanswerable]
    return {
        "overall": positives(with_evidence),
        "answerable": positives([r for r in results if r.kind is Kind.ANSWERABLE]),
        "ambiguous": positives([r for r in results if r.kind is Kind.AMBIGUOUS]),
        "unanswerable": {
            "questions": len(unanswerable),
            "top_score_mean": _mean(top_scores),
            "top_score_max": max(top_scores, default=0.0),
            "with_hits_rate": _mean([1.0 if r.hits else 0.0 for r in unanswerable]),
            "min_score": min_score,
        },
    }


@dataclass(frozen=True, slots=True)
class RetrievalReport:
    dataset: Dataset
    config: EvalConfig
    results: tuple[QuestionResult, ...]

    @property
    def metrics(self) -> dict[str, Any]:
        return aggregate(self.results, self.config.min_score)

    def to_dict(self, *, include_timing: bool = True) -> dict[str, Any]:
        """El informe; sin tiempos es idéntico en cada ejecución con la misma configuración."""
        manifest = self.dataset.manifest
        report: dict[str, Any] = {
            "schema": REPORT_SCHEMA,
            "dataset": {
                "key": self.dataset.key,
                "name": manifest.name,
                "version": manifest.version,
                "content_sha256": manifest.content_sha256,
            },
            "config": self.config.describe(),
            "metrics": self.metrics,
            "questions": [r.to_dict() for r in self.results],
        }
        if include_timing:
            latencies = sorted(r.latency_ms for r in self.results)
            report["timing"] = {
                "retrieval_ms_mean": round(statistics.fmean(latencies), 3) if latencies else 0.0,
                "retrieval_ms_p95": round(_percentile(latencies, 0.95), 3),
            }
        return report

    def to_json(self, *, include_timing: bool = True) -> str:
        return json.dumps(
            self.to_dict(include_timing=include_timing),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )


def _percentile(sorted_values: Sequence[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    return sorted_values[min(len(sorted_values) - 1, int(q * len(sorted_values)))]


@contextmanager
def indexed_corpus(session: Session, dataset: Dataset, config: EvalConfig) -> Iterator[uuid.UUID]:
    """Indexa los documentos del dataset en un usuario temporal y lo borra al terminar.

    Procesa solo los documentos propios con el mismo `process` del worker (nunca reserva trabajo
    ajeno de la cola). Devuelve el id del usuario propietario; al salir se eliminan usuario,
    documentos, fragmentos y vectores, así que se puede ejecutar contra una base con datos reales.
    """
    # Hash inválido a propósito: la cuenta existe solo para ser propietaria de los documentos.
    user = User(email=f"eval-{uuid.uuid4().hex}@atlas.invalid", password_hash=UNUSABLE_HASH)
    session.add(user)
    session.commit()
    try:
        with tempfile.TemporaryDirectory(prefix="atlas-eval-") as tmp:
            storage = LocalFileStorage(Path(tmp))
            for filename, text in sorted(dataset.documents.items()):
                _ingest(session, storage, user.id, filename, text, config, dataset.key)
            yield user.id
    finally:
        session.rollback()
        session.execute(delete(User).where(User.id == user.id))
        session.commit()


def _ingest(
    session: Session,
    storage: LocalFileStorage,
    owner_id: uuid.UUID,
    filename: str,
    text: str,
    config: EvalConfig,
    dataset_key: str,
) -> None:
    """Indexa un documento con un id derivado de su nombre.

    Los empates de similitud se resuelven por id de documento: con ids aleatorios el orden de los
    empatados (típicos en los fragmentos sin relación) cambiaría en cada ejecución.
    """
    data = text.encode()
    content_type = "text/markdown" if filename.endswith(".md") else "text/plain"
    document = Document(
        id=uuid.uuid5(NAMESPACE, f"{dataset_key}/{filename}"),
        owner_id=owner_id,
        filename=filename,
        content_type=content_type,
        size_bytes=len(data),
    )
    storage.save(document.storage_key, io.BytesIO(data))
    session.add(document)
    session.commit()
    document.transition_to(
        DocumentStatus.PROCESSING,
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS),
    )
    session.commit()
    process(
        session,
        storage,
        document,
        policy=config.policy,
        embedder=config.embedder,
        max_attempts=MAX_ATTEMPTS,
    )
    session.refresh(document)
    if document.status is not DocumentStatus.READY:
        raise RuntimeError(
            f"No se pudo indexar {filename}: {document.status.value} ({document.error_summary})"
        )


def evaluate_retrieval(
    session: Session, dataset: Dataset, config: EvalConfig, owner_id: uuid.UUID
) -> RetrievalReport:
    """Busca cada pregunta del dataset en el índice de `owner_id` y puntúa lo recuperado."""
    limits = config.limits()
    results = []
    for question in dataset.questions:
        started = time.perf_counter()
        prepared = prepare_question(
            question.text, config.embedder, max_chars=config.question_max_chars
        )
        outcome = search_with_report(
            session, prepared, owner_id=owner_id, limits=limits, status=DocumentStatus.READY
        )
        latency_ms = (time.perf_counter() - started) * 1000
        results.append(score_question(question, outcome.hits, latency_ms=round(latency_ms, 3)))
    return RetrievalReport(dataset, config, tuple(results))


def run_retrieval_eval(
    session: Session, config: EvalConfig, dataset: Dataset | None = None
) -> RetrievalReport:
    """Indexa el corpus, evalúa todas las preguntas y limpia; un experimento completo."""
    dataset = dataset or load_dataset()
    with indexed_corpus(session, dataset, config) as owner_id:
        return evaluate_retrieval(session, dataset, config, owner_id)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.evaluation.retrieval",
        description="Mide precision@k, recall@k y éxito de fuente sobre el dataset de evaluación.",
    )
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--top-k", type=int, help="k evaluado (por defecto SEARCH_DEFAULT_K)")
    parser.add_argument("--min-score", type=float, help="umbral (por defecto SEARCH_MIN_SCORE)")
    parser.add_argument("--no-dedup", action="store_true", help="sin reducción de redundancia")
    parser.add_argument("--output", type=Path, help="guarda el informe JSON en este archivo")
    parser.add_argument(
        "--no-timing",
        action="store_true",
        help="omite los tiempos (informe reproducible byte a byte)",
    )
    args = parser.parse_args(argv)

    try:
        dataset = load_dataset(args.dataset)
    except DatasetError as error:
        print(error, file=sys.stderr)
        return 1
    base = EvalConfig.from_settings()
    config = EvalConfig(
        embedder=base.embedder,
        policy=base.policy,
        top_k=args.top_k if args.top_k is not None else base.top_k,
        min_score=args.min_score if args.min_score is not None else base.min_score,
        dedup=None if args.no_dedup else base.dedup,
        question_max_chars=base.question_max_chars,
    )
    with get_sessionmaker()() as session:
        report = run_retrieval_eval(session, config, dataset)
    text = report.to_json(include_timing=not args.no_timing)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
