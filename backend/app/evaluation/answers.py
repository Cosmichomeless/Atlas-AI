"""Evaluación de las respuestas: fidelidad a las fuentes, calidad de las citas y abstención.

Cada pregunta del dataset recorre el pipeline completo (búsqueda → contexto → modelo → verificación
de citas) y se clasifica en uno de seis veredictos, que separan lo que una sola cifra mezclaría:

- `correct`: responde con todos los datos clave anotados y sin inventar.
- `partial`: responde, pero faltan datos clave (en una pregunta ambigua, alguna lectura).
- `incorrect`: responde sin ningún dato clave, aunque se apoye en las fuentes.
- `hallucination`: responde cuando no hay respuesta en los documentos, o la mayoría de lo que
  afirma no está respaldado por las fuentes que cita.
- `valid_abstention`: se abstiene ante una pregunta sin respuesta en el corpus.
- `unnecessary_abstention`: se abstiene aunque el corpus la responde (o la ambigua tiene lecturas).

Además de la etiqueta se miden, por respuesta:

- `key_fact_recall`: datos clave anotados que aparecen en el texto entregado.
- `faithfulness`: afirmaciones respaldadas / afirmaciones propias (las `external` no cuentan).
  Una afirmación está respaldada si lleva cita y al menos el 60 % de sus palabras con contenido
  aparece en los fragmentos citados; una sin cita no está respaldada.
- `cited_rate`: afirmaciones propias con al menos una cita verificada.
- `citation_precision`: citas verificadas cuyo fragmento recoge evidencia anotada.
- `citation_recall`: evidencias anotadas recogidas por algún fragmento citado.

La fidelidad es *léxica*: detecta contenido que no sale de las fuentes, no contradicciones sutiles
ni paráfrasis. Por eso el criterio se calibra con revisión humana de una muestra
(`--review-sample` y `calibrate`), que mide el acuerdo (exactitud y kappa de Cohen) entre el
veredicto automático y el humano.

Con el proveedor falso el modelo es un respondedor extractivo determinista
(`extractive_responder`): una línea base heurística, no un modelo real. Sus cifras sirven para
vigilar regresiones del pipeline y para probar la propia evaluación; no miden la calidad de un LLM.
"""

import argparse
import json
import random
import re
import statistics
import sys
import time
import uuid
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.answers.generate import Answer
from app.answers.grounding import EXTERNAL_MARKER, Statement
from app.answers.prompt import INSUFFICIENT_MARKER, PROMPT_FINGERPRINT, PROMPT_VERSION
from app.answers.service import Abstained, Answered, Outcome, answer_question
from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.evaluation.dataset import (
    DEFAULT_DATASET,
    Dataset,
    DatasetError,
    Kind,
    Question,
    load_dataset,
)
from app.evaluation.retrieval import EvalConfig, covers, indexed_corpus
from app.features.documents.states import DocumentStatus
from app.llm.fake import FakeLLMProvider
from app.llm.provider import LLMProvider, Message
from app.llm.registry import build_llm_provider, llm_params
from app.retrieval.dedup import normalize
from app.retrieval.question import prepare_question
from app.retrieval.search import search_with_report

REPORT_SCHEMA = 1
SUPPORT_THRESHOLD = 0.6
FAITHFULNESS_MIN = 0.5
MIN_WORD_CHARS = 4
EXTRACTIVE_MIN_OVERLAP = 0.5
DIGITS = 6
SAMPLE_SCHEMA = 1

_WORD = re.compile(r"\w+")
_LABEL_MARK = re.compile(r"\[\s*S\d+(?:\s*[,;]\s*S\d+)*\s*\]")
_SOURCE_HEADER = re.compile(r"(?m)^\[(S\d+)\] [^\n]*\n")
_SENTENCE = re.compile(r"(?<=[.!?…])\s+")
_QUESTION_LINE = re.compile(r"Pregunta: (.*)\Z", re.DOTALL)
_STOPWORDS = frozenset(
    [
        "cuál",
        "cuáles",
        "cuánto",
        "cuántos",
        "cuánta",
        "cuántas",
        "cómo",
        "dónde",
        "cuándo",
        "quién",
        "quiénes",
        "qué",
        "para",
        "como",
        "desde",
        "hasta",
        "sobre",
        "entre",
        "cada",
        "esta",
        "este",
        "estos",
        "estas",
        "tiene",
        "tienen",
        "puede",
        "pueden",
        "debe",
        "deben",
        "hacer",
        "persona",
        "personas",
    ]
)


class Verdict(StrEnum):
    CORRECT = "correct"
    PARTIAL = "partial"
    INCORRECT = "incorrect"
    HALLUCINATION = "hallucination"
    VALID_ABSTENTION = "valid_abstention"
    UNNECESSARY_ABSTENTION = "unnecessary_abstention"


def _content_words(text: str) -> frozenset[str]:
    plain = _LABEL_MARK.sub(" ", text).replace(EXTERNAL_MARKER, " ")
    return frozenset(
        word
        for word in _WORD.findall(plain.lower())
        if len(word) >= MIN_WORD_CHARS or any(ch.isdigit() for ch in word)
    )


def coverage(statement: str, sources: Sequence[str]) -> float:
    """Fracción de las palabras con contenido de `statement` presentes en `sources`."""
    wanted = _content_words(statement)
    if not wanted:
        return 1.0
    available: set[str] = set()
    for source in sources:
        available |= _content_words(source)
    return len(wanted & available) / len(wanted)


def _sentences(text: str) -> list[str]:
    """Frases de un fragmento; los títulos Markdown (`# ...`) no son frases."""
    body = " ".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    return [s.strip() for s in _SENTENCE.split(" ".join(body.split())) if s.strip()]


def extractive_responder(messages: Sequence[Message]) -> str:
    """Modelo falso determinista: cita la frase de las fuentes más parecida a la pregunta.

    Si ninguna frase comparte al menos la mitad de las palabras con contenido de la pregunta,
    declara que no hay evidencia. Es una línea base deliberadamente simple (umbral fijado de
    antemano, sin ajustar al dataset): no entiende la pregunta, solo busca solapamiento léxico.
    """
    user = next(m.content for m in reversed(messages) if m.role == "user")
    match = _QUESTION_LINE.search(user)
    wanted = _content_words(match.group(1) if match else user) - _STOPWORDS
    sources = user.split("</fuentes>")[0]
    parts = _SOURCE_HEADER.split(sources)
    best: tuple[float, str, str] | None = None
    for label, text in zip(parts[1::2], parts[2::2], strict=False):
        for sentence in _sentences(text):
            score = len(wanted & _content_words(sentence)) / len(wanted) if wanted else 0.0
            if best is None or score > best[0]:
                best = (score, label, sentence)
    if best is None or best[0] < EXTRACTIVE_MIN_OVERLAP:
        return INSUFFICIENT_MARKER
    _, label, sentence = best
    return f"{sentence} [{label}]"


def build_provider() -> LLMProvider:
    """El proveedor configurado; con `LLM_PROVIDER=fake`, el respondedor extractivo."""
    settings = get_settings()
    if settings.llm_provider == "fake":
        return FakeLLMProvider(settings.llm_model, llm_params(settings), extractive_responder)
    return build_llm_provider(settings)


@dataclass(frozen=True, slots=True)
class AnswerResult:
    """Resultado de una pregunta: lo que hizo el sistema, su veredicto y las métricas."""

    id: str
    kind: Kind
    verdict: Verdict
    abstention: str | None
    answer: str
    cited: tuple[str, ...]
    key_fact_recall: float | None
    faithfulness: float | None
    cited_rate: float | None
    citation_precision: float | None
    citation_recall: float | None
    input_tokens: int
    output_tokens: int
    latency_ms: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "verdict": self.verdict.value,
            "abstention": self.abstention,
            "answer": self.answer,
            "cited": list(self.cited),
            "key_fact_recall": self.key_fact_recall,
            "faithfulness": self.faithfulness,
            "cited_rate": self.cited_rate,
            "citation_precision": self.citation_precision,
            "citation_recall": self.citation_recall,
        }


def _round(value: float) -> float:
    return round(value, DIGITS)


def _own_statements(statements: Sequence[Statement]) -> list[Statement]:
    return [s for s in statements if s.kind != "external"]


def _faithfulness(answer: Answer, statements: Sequence[Statement]) -> float:
    """Afirmaciones propias respaldadas por los fragmentos que citan; 1 si no hay ninguna."""
    own = _own_statements(statements)
    if not own:
        return 1.0
    supported = 0
    for statement in own:
        sources = [
            item.text
            for label in statement.labels
            if (item := answer.context.item(label)) is not None
        ]
        if sources and coverage(statement.text, sources) >= SUPPORT_THRESHOLD:
            supported += 1
    return supported / len(own)


def _key_fact_recall(question: Question, text: str) -> float:
    if not question.key_facts:
        return 1.0
    plain = normalize(_LABEL_MARK.sub(" ", text))
    found = sum(normalize(fact) in plain for fact in question.key_facts)
    return found / len(question.key_facts)


def _verdict(question: Question, outcome: Outcome, faithfulness: float, recall: float) -> Verdict:
    if isinstance(outcome, Abstained):
        if question.kind is Kind.UNANSWERABLE:
            return Verdict.VALID_ABSTENTION
        return Verdict.UNNECESSARY_ABSTENTION
    if question.kind is Kind.UNANSWERABLE or faithfulness < FAITHFULNESS_MIN:
        return Verdict.HALLUCINATION
    if recall >= 1.0:
        return Verdict.CORRECT
    return Verdict.PARTIAL if recall > 0 else Verdict.INCORRECT


def score_outcome(
    question: Question,
    outcome: Outcome,
    *,
    latency_ms: float,
    input_tokens: int,
    output_tokens: int,
) -> AnswerResult:
    """Clasifica lo que hizo el sistema ante la pregunta y mide la respuesta si la dio."""
    if isinstance(outcome, Abstained):
        return AnswerResult(
            question.id,
            question.kind,
            _verdict(question, outcome, 1.0, 0.0),
            outcome.reason,
            "",
            (),
            None,
            None,
            None,
            None,
            None,
            input_tokens,
            output_tokens,
            latency_ms,
        )

    verified = outcome.verified
    faithfulness = _faithfulness(verified.answer, verified.statements)
    recall = _key_fact_recall(question, verified.text)
    own = _own_statements(verified.statements)
    cited = sum(1 for s in own if s.labels) / len(own) if own else 0.0

    precision = citation_recall = None
    if question.evidence:
        chunks = [verified.answer.context.item(c.label) for c in verified.citations]
        texts = [
            (c.filename, item.text)
            for c, item in zip(verified.citations, chunks, strict=True)
            if item is not None
        ]
        relevant = sum(
            any(covers(text, ev, name) for ev in question.evidence) for name, text in texts
        )
        covered = sum(
            any(covers(text, ev, name) for name, text in texts) for ev in question.evidence
        )
        precision = relevant / len(texts) if texts else 0.0
        citation_recall = covered / len(question.evidence)

    return AnswerResult(
        question.id,
        question.kind,
        _verdict(question, outcome, faithfulness, recall),
        None,
        verified.text,
        tuple(c.label for c in verified.citations),
        _round(recall),
        _round(faithfulness),
        _round(cited),
        None if precision is None else _round(precision),
        None if citation_recall is None else _round(citation_recall),
        input_tokens,
        output_tokens,
        latency_ms,
    )


def _mean(values: Sequence[float]) -> float | None:
    return _round(statistics.fmean(values)) if values else None


def aggregate(results: Sequence[AnswerResult]) -> dict[str, Any]:
    """Veredictos por tipo de pregunta y medias de las métricas de las respuestas dadas."""
    counts = Counter(r.verdict for r in results)
    total = len(results)
    with_answer = [r for r in results if r.faithfulness is not None]
    positive = [r for r in results if r.kind is not Kind.UNANSWERABLE]
    unanswerable = [r for r in results if r.kind is Kind.UNANSWERABLE]

    def rate(count: int, over: int) -> float | None:
        return _round(count / over) if over else None

    def of(field: str, subset: Sequence[AnswerResult]) -> float | None:
        values = [getattr(r, field) for r in subset if getattr(r, field) is not None]
        return _mean(values)

    return {
        "questions": total,
        "verdicts": {v.value: counts.get(v, 0) for v in Verdict},
        "by_kind": {
            kind.value: {
                v.value: sum(1 for r in results if r.kind is kind and r.verdict is v)
                for v in Verdict
            }
            for kind in Kind
        },
        "correct_rate": rate(counts[Verdict.CORRECT], len(positive)),
        "answered_rate": rate(sum(1 for r in positive if r.abstention is None), len(positive)),
        "unnecessary_abstention_rate": rate(counts[Verdict.UNNECESSARY_ABSTENTION], len(positive)),
        "valid_abstention_rate": rate(counts[Verdict.VALID_ABSTENTION], len(unanswerable)),
        "hallucination_rate": rate(counts[Verdict.HALLUCINATION], total),
        "key_fact_recall": of("key_fact_recall", [r for r in with_answer if r in positive]),
        "faithfulness": of("faithfulness", with_answer),
        "cited_rate": of("cited_rate", with_answer),
        "citation_precision": of("citation_precision", with_answer),
        "citation_recall": of("citation_recall", with_answer),
    }


@dataclass(frozen=True, slots=True)
class AnswerReport:
    dataset: Dataset
    config: EvalConfig
    llm: dict[str, Any]
    results: tuple[AnswerResult, ...]

    @property
    def metrics(self) -> dict[str, Any]:
        return aggregate(self.results)

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
            "config": {**self.config.describe(), "llm": self.llm},
            "metrics": self.metrics,
            "cost": {
                "input_tokens": sum(r.input_tokens for r in self.results),
                "output_tokens": sum(r.output_tokens for r in self.results),
            },
            "questions": [r.to_dict() for r in self.results],
        }
        if include_timing:
            latencies = sorted(r.latency_ms for r in self.results)
            report["timing"] = {
                "answer_ms_mean": round(statistics.fmean(latencies), 3) if latencies else 0.0,
                "answer_ms_p95": latencies[int(0.95 * len(latencies))] if latencies else 0.0,
            }
        return report

    def to_json(self, *, include_timing: bool = True) -> str:
        return json.dumps(
            self.to_dict(include_timing=include_timing),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )


def describe_llm(provider: LLMProvider, max_context_tokens: int) -> dict[str, Any]:
    return {
        "key": provider.spec.key,
        "temperature": provider.params.temperature,
        "max_output_tokens": provider.params.max_output_tokens,
        "prompt_version": PROMPT_VERSION,
        "prompt_fingerprint": PROMPT_FINGERPRINT,
        "context_max_tokens": max_context_tokens,
    }


def evaluate_answers(
    session: Session,
    dataset: Dataset,
    config: EvalConfig,
    owner_id: uuid.UUID,
    provider: LLMProvider,
    *,
    max_context_tokens: int,
) -> AnswerReport:
    """Responde cada pregunta del dataset con el índice de `owner_id` y puntúa la respuesta."""
    limits = config.limits()
    results = []
    for question in dataset.questions:
        started = time.perf_counter()
        prepared = prepare_question(
            question.text, config.embedder, max_chars=config.question_max_chars
        )
        hits = search_with_report(
            session, prepared, owner_id=owner_id, limits=limits, status=DocumentStatus.READY
        ).hits
        outcome = answer_question(
            session,
            prepared.text,
            hits,
            owner_id=owner_id,
            provider=provider,
            max_context_tokens=max_context_tokens,
        )
        latency_ms = (time.perf_counter() - started) * 1000
        answer = outcome.verified.answer if isinstance(outcome, Answered) else outcome.answer
        usage = answer.completion.usage if answer is not None else None
        results.append(
            score_outcome(
                question,
                outcome,
                latency_ms=round(latency_ms, 3),
                input_tokens=usage.input_tokens if usage else 0,
                output_tokens=usage.output_tokens if usage else 0,
            )
        )
    return AnswerReport(dataset, config, describe_llm(provider, max_context_tokens), tuple(results))


def run_answer_eval(
    session: Session,
    config: EvalConfig,
    provider: LLMProvider,
    dataset: Dataset | None = None,
    *,
    max_context_tokens: int | None = None,
) -> AnswerReport:
    """Indexa el corpus, responde todas las preguntas y limpia; un experimento completo."""
    dataset = dataset or load_dataset()
    budget = max_context_tokens or get_settings().answer_context_max_tokens
    with indexed_corpus(session, dataset, config) as owner_id:
        return evaluate_answers(
            session, dataset, config, owner_id, provider, max_context_tokens=budget
        )


# --- Revisión humana de una muestra ---------------------------------------------------------------


def review_sample(report: AnswerReport, *, size: int, seed: int) -> dict[str, Any]:
    """Muestra estratificada por veredicto automático, para que una persona etiquete a ciegas.

    Se reparte el cupo entre los veredictos presentes (al menos uno por veredicto mientras quepan)
    y se elige con `random.Random(seed)` sobre preguntas ordenadas por id: misma semilla, misma
    muestra. `automatic_verdict` va aparte para poder ocultarlo al revisar.
    """
    if size < 1:
        raise ValueError("size debe ser al menos 1")
    questions = {q.id: q for q in report.dataset.questions}
    by_verdict: dict[Verdict, list[AnswerResult]] = {}
    for result in sorted(report.results, key=lambda r: r.id):
        by_verdict.setdefault(result.verdict, []).append(result)

    rng = random.Random(seed)  # noqa: S311 - muestreo reproducible, no criptografía
    chosen: list[AnswerResult] = []
    pools = {v: list(rs) for v, rs in sorted(by_verdict.items(), key=lambda kv: kv[0].value)}
    for pool in pools.values():
        rng.shuffle(pool)
    while len(chosen) < min(size, len(report.results)):
        for pool in pools.values():
            if pool and len(chosen) < size:
                chosen.append(pool.pop())
    chosen.sort(key=lambda r: r.id)

    items = []
    for result in chosen:
        question = questions[result.id]
        items.append(
            {
                "id": result.id,
                "kind": result.kind.value,
                "question": question.text,
                "expected_key_facts": list(question.key_facts),
                "answer": result.answer or None,
                "abstention": result.abstention,
                "automatic_verdict": result.verdict.value,
                "human_verdict": None,
                "notes": "",
            }
        )
    return {
        "schema": SAMPLE_SCHEMA,
        "dataset": report.dataset.key,
        "llm": report.llm["key"],
        "seed": seed,
        "verdicts": [v.value for v in Verdict],
        "items": items,
    }


def calibrate(sample: dict[str, Any]) -> dict[str, Any]:
    """Acuerdo entre el veredicto automático y el humano en las filas ya etiquetadas.

    Devuelve exactitud, kappa de Cohen (acuerdo corregido por azar), la matriz de confusión
    (humano → automático) y los desacuerdos con su pregunta, que son lo que hay que revisar para
    ajustar el criterio. Un veredicto humano fuera del vocabulario es un error, no se ignora.
    """
    allowed = {v.value for v in Verdict}
    pairs: list[tuple[str, str, str]] = []
    for item in sample["items"]:
        human = item.get("human_verdict")
        if human is None:
            continue
        if human not in allowed:
            raise ValueError(f"{item['id']}: veredicto humano desconocido {human!r}")
        pairs.append((item["id"], human, item["automatic_verdict"]))
    if not pairs:
        raise ValueError("La muestra no tiene ninguna fila etiquetada (human_verdict)")

    n = len(pairs)
    agreed = sum(1 for _, human, auto in pairs if human == auto)
    humans = Counter(h for _, h, _ in pairs)
    autos = Counter(a for _, _, a in pairs)
    expected = sum(humans[v] * autos[v] for v in allowed) / (n * n)
    observed = agreed / n
    kappa = 1.0 if expected == 1 else (observed - expected) / (1 - expected)
    confusion: dict[str, dict[str, int]] = {}
    for _, human, auto in pairs:
        confusion.setdefault(human, {}).setdefault(auto, 0)
        confusion[human][auto] += 1
    return {
        "labelled": n,
        "unlabelled": len(sample["items"]) - n,
        "accuracy": _round(observed),
        "kappa": _round(kappa),
        "confusion": {h: dict(sorted(row.items())) for h, row in sorted(confusion.items())},
        "disagreements": [
            {"id": i, "human": human, "automatic": auto}
            for i, human, auto in pairs
            if human != auto
        ],
    }


# --- Línea de comandos ----------------------------------------------------------------------------


def _run(args: argparse.Namespace) -> int:
    try:
        dataset = load_dataset(args.dataset)
    except DatasetError as error:
        print(error, file=sys.stderr)
        return 1
    config = EvalConfig.from_settings()
    provider = build_provider()
    with get_sessionmaker()() as session:
        report = run_answer_eval(session, config, provider, dataset)
    text = report.to_json(include_timing=not args.no_timing)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    if args.review_sample:
        sample = review_sample(report, size=args.sample_size, seed=args.seed)
        args.review_sample.write_text(
            json.dumps(sample, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return 0


def _calibrate(args: argparse.Namespace) -> int:
    try:
        sample = json.loads(args.sample.read_text(encoding="utf-8"))
        result = calibrate(sample)
    except (OSError, ValueError, KeyError) as error:
        print(f"No se puede calibrar: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.evaluation.answers",
        description="Evalúa fidelidad y citas de las respuestas y calibra el criterio con humanos.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="ejecuta el experimento sobre el dataset")
    run.add_argument("--dataset", default=DEFAULT_DATASET)
    run.add_argument("--output", type=Path, help="guarda el informe JSON en este archivo")
    run.add_argument("--no-timing", action="store_true", help="informe reproducible byte a byte")
    run.add_argument("--review-sample", type=Path, help="escribe aquí la muestra de revisión")
    run.add_argument("--sample-size", type=int, default=12)
    run.add_argument("--seed", type=int, default=0)
    run.set_defaults(handler=_run)

    cal = commands.add_parser("calibrate", help="acuerdo entre veredicto automático y humano")
    cal.add_argument("sample", type=Path, help="muestra con `human_verdict` rellenado")
    cal.set_defaults(handler=_calibrate)

    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
