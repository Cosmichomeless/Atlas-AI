"""Puerta de regresión de calidad: falla si la recuperación o las citas caen bajo el umbral.

Es un subconjunto *determinista* de la evaluación, pensado para ejecutarse en cada cambio, en local
o en CI, sin secretos ni red:

- Los modelos son los falsos: embeddings por hashing de palabras y el respondedor extractivo. No
  hay clave de ningún proveedor ni llamada externa; el resultado depende solo del código y de los
  datos, así que el mismo commit da siempre las mismas cifras (no hay latencias ni muestreo).
- La configuración está fijada en `evaluation/gate.json`, no sale del `.env`: un cambio de entorno
  no mueve la puerta.
- Los umbrales viven en ese mismo archivo, versionado. Bajarlos (o subirlos tras una mejora) es un
  cambio visible en la revisión del PR, con su justificación; nunca un efecto lateral.

Qué detecta y qué no: vigila el pipeline (búsqueda, reranking, contexto, citas) con modelos
falsos. Una mala elección de modelo real o de prompts no se ve aquí; para eso están los informes
completos (`retrieval`, `answers`, `compare`) y la revisión humana.

    uv run python -m app.evaluation.gate            # 0 si pasa, 1 si algún umbral falla
"""

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.db import get_sessionmaker
from app.embeddings.fake import FakeEmbeddingProvider
from app.evaluation.answers import evaluate_answers, extractive_responder
from app.evaluation.dataset import Dataset, DatasetError, load_dataset
from app.evaluation.retrieval import EvalConfig, evaluate_retrieval, indexed_corpus
from app.ingestion.chunking import ChunkPolicy
from app.llm.fake import FakeLLMProvider
from app.llm.provider import LLMParams
from app.retrieval.rerank import RerankPolicy

SPEC_SCHEMA = 1
GATE_PATH = Path(__file__).resolve().parents[2] / "evaluation" / "gate.json"
EMBEDDING_MODEL = "fake-model"
EMBEDDING_DIMENSIONS = 1536
SOURCES = ("retrieval", "answers")
DIGITS = 6


class GateError(Exception):
    """La definición de la puerta no es válida; `problems` lista todo lo encontrado."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("Puerta de calidad no válida:\n- " + "\n- ".join(problems))


@dataclass(frozen=True, slots=True)
class Threshold:
    metric: str
    minimum: float | None = None
    maximum: float | None = None


@dataclass(frozen=True, slots=True)
class GateSpec:
    dataset: str
    dataset_key: str
    chunk_size: int
    chunk_overlap: int
    top_k: int
    context_max_tokens: int
    rerank: RerankPolicy | None
    thresholds: tuple[Threshold, ...]

    def eval_config(self) -> EvalConfig:
        """Siempre con embeddings falsos: la definición ni siquiera admite otro proveedor."""
        return EvalConfig(
            embedder=FakeEmbeddingProvider(EMBEDDING_MODEL, EMBEDDING_DIMENSIONS),
            policy=ChunkPolicy(size=self.chunk_size, overlap=self.chunk_overlap),
            top_k=self.top_k,
            rerank=self.rerank,
        )


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _threshold(metric: str, raw: Any, problems: list[str]) -> Threshold | None:
    if metric.split(".", 1)[0] not in SOURCES or "." not in metric:
        problems.append(f"{metric}: la métrica debe empezar por `retrieval.` o `answers.`")
        return None
    if not isinstance(raw, dict) or not raw or set(raw) - {"min", "max"}:
        problems.append(f"{metric}: el umbral debe tener `min` y/o `max`")
        return None
    bounds = {key: raw[key] for key in ("min", "max") if key in raw}
    if not all(_number(value) for value in bounds.values()):
        problems.append(f"{metric}: los límites deben ser números")
        return None
    low, high = bounds.get("min"), bounds.get("max")
    if low is not None and high is not None and low > high:
        problems.append(f"{metric}: min no puede ser mayor que max")
        return None
    return Threshold(metric, low, high)


def parse_spec(raw: Any) -> GateSpec:
    problems: list[str] = []
    if not isinstance(raw, dict):
        raise GateError(["el archivo debe ser un objeto JSON"])
    if raw.get("schema") != SPEC_SCHEMA:
        problems.append(f"schema debe ser {SPEC_SCHEMA}")
    for key in ("dataset", "dataset_key"):
        if not isinstance(raw.get(key), str) or not raw[key]:
            problems.append(f"falta `{key}`")
    config = raw.get("config")
    config = config if isinstance(config, dict) else {}
    numbers = ("chunk_size", "chunk_overlap", "top_k", "context_max_tokens")
    for key in numbers:
        if not isinstance(config.get(key), int) or isinstance(config.get(key), bool):
            problems.append(f"config.{key} debe ser un entero")
    rerank_raw = config.get("rerank")
    rerank: RerankPolicy | None = None
    if rerank_raw is not None:
        try:
            rerank = RerankPolicy(weight=rerank_raw["weight"], pool=rerank_raw["pool"])
        except (KeyError, TypeError, ValueError) as error:
            problems.append(f"config.rerank no es válido: {error}")
    thresholds = []
    raw_thresholds = raw.get("thresholds")
    if not isinstance(raw_thresholds, dict) or not raw_thresholds:
        problems.append("falta `thresholds`")
        raw_thresholds = {}
    for metric, bounds in raw_thresholds.items():
        if (threshold := _threshold(metric, bounds, problems)) is not None:
            thresholds.append(threshold)
    if problems:
        raise GateError(problems)
    return GateSpec(
        dataset=raw["dataset"],
        dataset_key=raw["dataset_key"],
        chunk_size=config["chunk_size"],
        chunk_overlap=config["chunk_overlap"],
        top_k=config["top_k"],
        context_max_tokens=config["context_max_tokens"],
        rerank=rerank,
        thresholds=tuple(thresholds),
    )


def load_spec(path: Path = GATE_PATH) -> GateSpec:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise GateError([f"no se puede leer {path}: {error}"]) from error
    return parse_spec(raw)


@dataclass(frozen=True, slots=True)
class Check:
    metric: str
    value: float | None
    minimum: float | None
    maximum: float | None

    @property
    def passed(self) -> bool:
        """Sin valor medible no se puede dar por bueno: cuenta como fallo."""
        if self.value is None:
            return False
        below = self.minimum is not None and self.value < self.minimum
        above = self.maximum is not None and self.value > self.maximum
        return not (below or above)

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "value": self.value,
            "min": self.minimum,
            "max": self.maximum,
            "passed": self.passed,
        }


@dataclass(frozen=True, slots=True)
class GateResult:
    dataset_key: str
    checks: tuple[Check, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def failures(self) -> tuple[Check, ...]:
        return tuple(check for check in self.checks if not check.passed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SPEC_SCHEMA,
            "dataset": self.dataset_key,
            "passed": self.passed,
            "checks": [check.to_dict() for check in self.checks],
        }

    def render(self) -> str:
        lines = [f"Quality gate on {self.dataset_key}"]
        for check in self.checks:
            bound = f">= {check.minimum}" if check.minimum is not None else f"<= {check.maximum}"
            if check.minimum is not None and check.maximum is not None:
                bound = f"[{check.minimum}, {check.maximum}]"
            value = "not measurable" if check.value is None else f"{check.value:.4f}"
            lines.append(
                f"  {'ok  ' if check.passed else 'FAIL'} {check.metric} = {value} ({bound})"
            )
        verdict = "passed" if self.passed else f"FAILED ({len(self.failures)} below threshold)"
        lines.append(f"Gate {verdict}.")
        return "\n".join(lines)


def _lookup(sources: Mapping[str, Mapping[str, Any]], metric: str) -> float | None:
    source, *path = metric.split(".")
    value: Any = sources[source]
    for key in path:
        if not isinstance(value, Mapping) or key not in value:
            return None
        value = value[key]
    return round(float(value), DIGITS) if _number(value) else None


def evaluate(spec: GateSpec, sources: Mapping[str, Mapping[str, Any]]) -> GateResult:
    """Contrasta las métricas medidas con los umbrales (función pura, sin base de datos)."""
    checks = tuple(
        Check(t.metric, _lookup(sources, t.metric), t.minimum, t.maximum) for t in spec.thresholds
    )
    return GateResult(spec.dataset_key, checks)


def run_gate(
    session: Session, spec: GateSpec | None = None, dataset: Dataset | None = None
) -> GateResult:
    """Indexa el corpus una vez, mide recuperación y respuestas y contrasta con los umbrales."""
    spec = spec or load_spec()
    dataset = dataset or load_dataset(spec.dataset)
    if dataset.key != spec.dataset_key:
        raise GateError(
            [f"los umbrales son de {spec.dataset_key} pero el dataset es {dataset.key}"]
        )
    config = spec.eval_config()
    provider = FakeLLMProvider(EMBEDDING_MODEL, LLMParams(), extractive_responder)
    with indexed_corpus(session, dataset, config) as owner_id:
        retrieval = evaluate_retrieval(session, dataset, config, owner_id)
        answers = evaluate_answers(
            session,
            dataset,
            config,
            owner_id,
            provider,
            max_context_tokens=spec.context_max_tokens,
        )
    return evaluate(spec, {"retrieval": retrieval.metrics, "answers": answers.metrics})


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.evaluation.gate",
        description="Falla (código 1) si la recuperación o las citas caen bajo los umbrales.",
    )
    parser.add_argument("--spec", type=Path, default=GATE_PATH, help="umbrales y configuración")
    parser.add_argument("--output", type=Path, help="guarda el resultado en JSON")
    args = parser.parse_args(argv)
    try:
        spec = load_spec(args.spec)
        with get_sessionmaker()() as session:
            result = run_gate(session, spec)
    except (GateError, DatasetError) as error:
        print(error, file=sys.stderr)
        return 2
    print(result.render())
    if args.output:
        args.output.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
