"""Comparación de dos configuraciones sobre el mismo dataset: la recuperación sin y con reranking.

Se indexa el corpus una sola vez y se ejecutan las dos configuraciones sobre el mismo índice, así
que toda diferencia viene de la configuración y no de la indexación. El informe recoge calidad
(recuperación y respuestas), latencia y coste, las preguntas que cambian y la decisión de
mantener o descartar la variante.

La regla de decisión se fija antes de ver los resultados (`DecisionRule`) y se escribe en el propio
informe, para que la decisión se pueda comprobar y no solo leer:

* no baja la recuperación (recall) ni el MRR;
* no baja la tasa de respuestas correctas ni sube la de alucinaciones;
* mejora al menos una de esas cuatro;
* el sobrecoste de latencia (p95 de recuperación) no supera `max_overhead_ms`.

Con 28 preguntas y un solo anotador una diferencia pequeña no es significativa: por eso se listan
las preguntas que cambian, que son lo que hay que mirar antes de fiarse de una media. Si el
experimento usa embeddings o un LLM falsos, el informe lo advierte: sus cifras comprueban el
procedimiento, no la calidad de los modelos reales.

La latencia se mide repitiendo cada configuración (`repeats`, más una pasada de calentamiento que
se descarta) y alternando el orden de las dos para que ninguna se beneficie de la caché.
"""

import argparse
import json
import statistics
import sys
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.evaluation.answers import (
    AnswerReport,
    build_provider,
    evaluate_answers,
)
from app.evaluation.dataset import DEFAULT_DATASET, Dataset, DatasetError, load_dataset
from app.evaluation.retrieval import (
    EvalConfig,
    RetrievalReport,
    _percentile,
    evaluate_retrieval,
    indexed_corpus,
)
from app.llm.provider import LLMProvider
from app.retrieval.rerank import RerankPolicy

REPORT_SCHEMA = 1
DIGITS = 6
DEFAULT_REPEATS = 5
DEFAULT_MAX_OVERHEAD_MS = 20.0

# Llamadas a un modelo que añade cada estrategia de reranking por pregunta. La léxica es una función
# pura del texto: no consume tokens ni llama a ninguna API.
MODEL_CALLS_PER_QUESTION = {"lexical": 0}

# (fuente, ruta, mayor_es_mejor)
METRICS: tuple[tuple[str, tuple[str, ...], bool], ...] = (
    ("retrieval", ("overall", "recall"), True),
    ("retrieval", ("overall", "mrr"), True),
    ("retrieval", ("overall", "precision"), True),
    ("retrieval", ("overall", "source_success"), True),
    ("retrieval", ("answerable", "recall"), True),
    ("retrieval", ("answerable", "mrr"), True),
    ("retrieval", ("ambiguous", "recall"), True),
    ("retrieval", ("ambiguous", "mrr"), True),
    ("answers", ("correct_rate",), True),
    ("answers", ("key_fact_recall",), True),
    ("answers", ("faithfulness",), True),
    ("answers", ("citation_precision",), True),
    ("answers", ("citation_recall",), True),
    ("answers", ("valid_abstention_rate",), True),
    ("answers", ("unnecessary_abstention_rate",), False),
    ("answers", ("hallucination_rate",), False),
    ("answers", ("injection_rate",), False),
)


@dataclass(frozen=True, slots=True)
class DecisionRule:
    """Criterio de decisión fijado de antemano."""

    max_overhead_ms: float = DEFAULT_MAX_OVERHEAD_MS

    def describe(self) -> dict[str, Any]:
        return {
            "no_regression": [
                "retrieval.overall.recall",
                "retrieval.overall.mrr",
                "answers.correct_rate",
                "answers.hallucination_rate",
            ],
            "at_least_one_improvement": True,
            "max_p95_retrieval_overhead_ms": self.max_overhead_ms,
        }


@dataclass(frozen=True, slots=True)
class Arm:
    """Una configuración evaluada: sus informes y las muestras de latencia por pregunta."""

    name: str
    config: EvalConfig
    retrieval: RetrievalReport
    answers: AnswerReport
    retrieval_ms: tuple[float, ...]
    answer_ms: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class Comparison:
    dataset: Dataset
    baseline: Arm
    candidate: Arm
    rule: DecisionRule
    repeats: int

    def to_dict(self, *, include_timing: bool = True) -> dict[str, Any]:
        """El informe; sin tiempos es idéntico en cada ejecución con la misma configuración."""
        manifest = self.dataset.manifest
        deltas = _deltas(self.baseline, self.candidate)
        timing = _timing(self) if include_timing else None
        report: dict[str, Any] = {
            "schema": REPORT_SCHEMA,
            "dataset": {
                "key": self.dataset.key,
                "name": manifest.name,
                "version": manifest.version,
                "content_sha256": manifest.content_sha256,
                "questions": len(self.dataset.questions),
            },
            "arms": {
                arm.name: {
                    "config": {**arm.config.describe(), "llm": arm.answers.llm},
                    "retrieval": arm.retrieval.metrics,
                    "answers": arm.answers.metrics,
                }
                for arm in (self.baseline, self.candidate)
            },
            "deltas": deltas,
            "questions": _question_changes(self.baseline, self.candidate),
            "cost": _cost(self.baseline, self.candidate),
            "caveats": _caveats(self.baseline),
        }
        if timing is not None:
            report["timing"] = timing
        report["decision"] = _decision(self.rule, deltas, timing)
        return report

    def to_json(self, *, include_timing: bool = True) -> str:
        return json.dumps(
            self.to_dict(include_timing=include_timing),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )


def _dig(metrics: dict[str, Any], path: Sequence[str]) -> float | None:
    value: Any = metrics
    for key in path:
        value = value[key]
    return None if value is None else float(value)


def _deltas(baseline: Arm, candidate: Arm) -> list[dict[str, Any]]:
    sources = {
        "retrieval": (baseline.retrieval.metrics, candidate.retrieval.metrics),
        "answers": (baseline.answers.metrics, candidate.answers.metrics),
    }
    rows = []
    for source, path, higher_is_better in METRICS:
        before = _dig(sources[source][0], path)
        after = _dig(sources[source][1], path)
        change = None if before is None or after is None else round(after - before, DIGITS)
        if change is None or change == 0:
            outcome = "same"
        else:
            outcome = "better" if (change > 0) == higher_is_better else "worse"
        rows.append(
            {
                "metric": ".".join((source, *path)),
                "baseline": before,
                "candidate": after,
                "delta": change,
                "higher_is_better": higher_is_better,
                "outcome": outcome,
            }
        )
    return rows


def _question_changes(baseline: Arm, candidate: Arm) -> dict[str, Any]:
    """Preguntas cuya recuperación o veredicto cambian entre las dos configuraciones."""
    before = {r.id: r for r in baseline.retrieval.results}
    after = {r.id: r for r in candidate.retrieval.results}
    verdicts_before = {r.id: r for r in baseline.answers.results}
    verdicts_after = {r.id: r for r in candidate.answers.results}

    retrieval = {"improved": 0, "worsened": 0, "unchanged": 0}
    changed = []
    for question_id in sorted(before):
        old, new = before[question_id], after[question_id]
        if old.recall is None or new.recall is None:
            continue  # sin evidencia anotada no hay qué comparar
        old_key = (old.recall, old.reciprocal_rank or 0.0)
        new_key = (new.recall, new.reciprocal_rank or 0.0)
        if new_key == old_key:
            retrieval["unchanged"] += 1
            continue
        retrieval["improved" if new_key > old_key else "worsened"] += 1
        changed.append(
            {
                "id": question_id,
                "kind": old.kind.value,
                "recall": [old.recall, new.recall],
                "reciprocal_rank": [old.reciprocal_rank, new.reciprocal_rank],
            }
        )
    verdicts = [
        {
            "id": question_id,
            "kind": verdicts_before[question_id].kind.value,
            "verdict": [
                verdicts_before[question_id].verdict.value,
                verdicts_after[question_id].verdict.value,
            ],
        }
        for question_id in sorted(verdicts_before)
        if verdicts_before[question_id].verdict is not verdicts_after[question_id].verdict
    ]
    return {"retrieval": retrieval, "retrieval_changes": changed, "verdict_changes": verdicts}


def _cost(baseline: Arm, candidate: Arm) -> dict[str, Any]:
    def tokens(arm: Arm) -> dict[str, int]:
        return {
            "answer_input_tokens": sum(r.input_tokens for r in arm.answers.results),
            "answer_output_tokens": sum(r.output_tokens for r in arm.answers.results),
        }

    policy = candidate.config.rerank
    calls = 0 if policy is None else MODEL_CALLS_PER_QUESTION[policy.strategy]
    return {
        "baseline": tokens(baseline),
        "candidate": tokens(candidate),
        "rerank_model_calls_per_question": calls,
    }


def _caveats(arm: Arm) -> list[str]:
    caveats = [
        "Single annotator and a small dataset: differences of a single question are not "
        "statistically meaningful; read the per-question changes."
    ]
    if arm.config.embedder.spec.provider == "fake":
        caveats.append(
            "Fake embeddings: the vector score is noise, so the reranking effect is overstated "
            "and says nothing about a real embedding model."
        )
    if arm.answers.llm["key"].startswith("fake"):
        caveats.append(
            "Fake LLM (extractive baseline): faithfulness is trivially 1.0 and the answer metrics "
            "describe the heuristic, not a real model."
        )
    return caveats


def _stats(samples: Sequence[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "mean": round(statistics.fmean(ordered), 3) if ordered else 0.0,
        "p50": round(_percentile(ordered, 0.5), 3),
        "p95": round(_percentile(ordered, 0.95), 3),
    }


def _timing(comparison: Comparison) -> dict[str, Any]:
    base, cand = comparison.baseline, comparison.candidate
    stats = {
        arm.name: {
            "retrieval_ms": _stats(arm.retrieval_ms),
            "answer_ms": _stats(arm.answer_ms),
        }
        for arm in (base, cand)
    }
    return {
        "repeats": comparison.repeats,
        "samples_per_arm": len(base.retrieval_ms),
        **stats,
        "overhead_ms": {
            kind: {
                key: round(stats[cand.name][kind][key] - stats[base.name][kind][key], 3)
                for key in ("mean", "p50", "p95")
            }
            for kind in ("retrieval_ms", "answer_ms")
        },
    }


def _decision(
    rule: DecisionRule, deltas: list[dict[str, Any]], timing: dict[str, Any] | None
) -> dict[str, Any]:
    by_metric = {row["metric"]: row for row in deltas}
    guarded = rule.describe()["no_regression"]
    checks = [
        {
            "check": f"{metric} does not regress",
            "passed": by_metric[metric]["outcome"] != "worse",
            "delta": by_metric[metric]["delta"],
        }
        for metric in guarded
    ]
    improved = [metric for metric in guarded if by_metric[metric]["outcome"] == "better"]
    checks.append({"check": "at least one guarded metric improves", "passed": bool(improved)})
    if timing is not None:
        overhead = timing["overhead_ms"]["retrieval_ms"]["p95"]
        checks.append(
            {
                "check": f"p95 retrieval overhead <= {rule.max_overhead_ms} ms",
                "passed": overhead <= rule.max_overhead_ms,
                "delta": overhead,
            }
        )
    return {
        "rule": rule.describe(),
        "checks": checks,
        # Peores en métricas que la regla no protege: no deciden, pero se muestran.
        "unguarded_regressions": [
            row["metric"]
            for row in deltas
            if row["outcome"] == "worse" and row["metric"] not in guarded
        ],
        "latency_checked": timing is not None,
        "keep": all(check["passed"] for check in checks),
    }


# --- Experimento ----------------------------------------------------------------------------------


def compare_configs(
    session: Session,
    baseline: EvalConfig,
    candidate: EvalConfig,
    provider: LLMProvider,
    dataset: Dataset | None = None,
    *,
    repeats: int = DEFAULT_REPEATS,
    max_context_tokens: int | None = None,
    rule: DecisionRule | None = None,
) -> Comparison:
    """Evalúa las dos configuraciones sobre un único índice y las compara.

    Ambas deben compartir embedder y chunking (si no, el índice no sería el mismo): solo puede
    cambiar lo que se aplica en la búsqueda.
    """
    if repeats < 1:
        raise ValueError("repeats debe ser al menos 1")
    if (baseline.embedder.spec, baseline.policy) != (candidate.embedder.spec, candidate.policy):
        raise ValueError("Las dos configuraciones deben compartir embedder y chunking")
    dataset = dataset or load_dataset()
    budget = max_context_tokens or get_settings().answer_context_max_tokens
    configs = {"baseline": baseline, "candidate": candidate}

    with indexed_corpus(session, dataset, baseline) as owner_id:
        runs: dict[str, list[tuple[RetrievalReport, AnswerReport]]] = {n: [] for n in configs}
        # La primera pasada calienta cachés y conexiones: se descarta. El orden se alterna para que
        # ninguna configuración dependa de ir antes o después.
        for repeat in range(repeats + 1):
            order = list(configs) if repeat % 2 == 0 else list(reversed(configs))
            for name in order:
                config = configs[name]
                retrieval = evaluate_retrieval(session, dataset, config, owner_id)
                answers = evaluate_answers(
                    session, dataset, config, owner_id, provider, max_context_tokens=budget
                )
                if repeat > 0:
                    runs[name].append((retrieval, answers))

    def arm(name: str) -> Arm:
        collected = runs[name]
        first_retrieval, first_answers = collected[0]
        return Arm(
            name,
            configs[name],
            first_retrieval,
            first_answers,
            tuple(r.latency_ms for report, _ in collected for r in report.results),
            tuple(r.latency_ms for _, report in collected for r in report.results),
        )

    return Comparison(dataset, arm("baseline"), arm("candidate"), rule or DecisionRule(), repeats)


# --- Informe legible ------------------------------------------------------------------------------


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def _signed(value: float | None) -> str:
    return "n/a" if value is None else f"{value:+.4f}"


def render_markdown(report: dict[str, Any]) -> str:
    """El informe en Markdown; solo muestra lo que está en `report`, sin recalcular nada."""
    base, cand = report["arms"]["baseline"], report["arms"]["candidate"]
    rerank = cand["config"]["rerank"]
    lines = [
        "# Baseline vs reranked retrieval",
        "",
        f"Dataset `{report['dataset']['key']}` ({report['dataset']['questions']} questions, "
        f"sha256 `{report['dataset']['content_sha256'][:12]}`). Both arms share one index "
        f"(embedding `{base['config']['embedding']['key']}`, chunking "
        f"`{base['config']['chunking']['key']}`, top-k {base['config']['top_k']}). "
        f"The candidate adds `{rerank['key']}` (weight {rerank['weight']}, pool {rerank['pool']}).",
        "",
    ]
    lines += ["## Caveats", "", *[f"- {item}" for item in report["caveats"]], ""]
    lines += [
        "## Quality",
        "",
        "| Metric | Baseline | Reranked | Delta | |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    marks = {"better": "better", "worse": "**worse**", "same": ""}
    for row in report["deltas"]:
        lines.append(
            f"| `{row['metric']}` | {_fmt(row['baseline'])} | {_fmt(row['candidate'])} | "
            f"{_signed(row['delta'])} | {marks[row['outcome']]} |"
        )
    questions = report["questions"]
    counts = questions["retrieval"]
    lines += [
        "",
        f"Retrieval per question: {counts['improved']} improved, {counts['worsened']} worsened, "
        f"{counts['unchanged']} unchanged.",
        "",
    ]
    if questions["retrieval_changes"]:
        lines += ["| Question | Kind | Recall | Reciprocal rank |", "| --- | --- | --- | --- |"]
        for item in questions["retrieval_changes"]:
            lines.append(
                f"| {item['id']} | {item['kind']} | {item['recall'][0]} → {item['recall'][1]} | "
                f"{item['reciprocal_rank'][0]} → {item['reciprocal_rank'][1]} |"
            )
        lines.append("")
    if questions["verdict_changes"]:
        lines += ["| Question | Kind | Verdict |", "| --- | --- | --- |"]
        for item in questions["verdict_changes"]:
            lines.append(
                f"| {item['id']} | {item['kind']} | {item['verdict'][0]} → {item['verdict'][1]} |"
            )
        lines.append("")
    else:
        lines += ["No answer verdict changes.", ""]

    cost = report["cost"]
    lines += [
        "## Cost",
        "",
        "| | Baseline | Reranked |",
        "| --- | ---: | ---: |",
        f"| Answer input tokens | {cost['baseline']['answer_input_tokens']} | "
        f"{cost['candidate']['answer_input_tokens']} |",
        f"| Answer output tokens | {cost['baseline']['answer_output_tokens']} | "
        f"{cost['candidate']['answer_output_tokens']} |",
        "",
        f"Extra model calls per question for reranking: {cost['rerank_model_calls_per_question']}.",
        "",
    ]
    timing = report.get("timing")
    if timing:
        lines += [
            "## Latency",
            "",
            f"{timing['repeats']} repeats after a warm-up pass, "
            f"{timing['samples_per_arm']} samples per arm, alternating order. Milliseconds, "
            "measured on one machine: they change from run to run, only the order of magnitude "
            "is stable.",
            "",
            "| | Baseline mean / p50 / p95 | Reranked mean / p50 / p95 | Overhead p95 |",
            "| --- | --- | --- | ---: |",
        ]
        for kind, label in (("retrieval_ms", "Retrieval"), ("answer_ms", "End to end")):
            b, c = timing["baseline"][kind], timing["candidate"][kind]
            lines.append(
                f"| {label} | {b['mean']} / {b['p50']} / {b['p95']} | "
                f"{c['mean']} / {c['p50']} / {c['p95']} | "
                f"{timing['overhead_ms'][kind]['p95']:+} |"
            )
        lines.append("")
    decision = report["decision"]
    lines += ["## Decision", "", f"**{'Keep' if decision['keep'] else 'Drop'} the reranking.**", ""]
    lines += [
        f"- [{'x' if check['passed'] else ' '}] {check['check']}"
        + ("" if check.get("delta") is None else f" (delta {check['delta']:+})")
        for check in decision["checks"]
    ]
    if decision["unguarded_regressions"]:
        lines += [
            "",
            "Metrics outside the rule that got worse (they did not decide, but look at them): "
            + ", ".join(f"`{metric}`" for metric in decision["unguarded_regressions"])
            + ".",
        ]
    if not decision["latency_checked"]:
        lines += ["", "Latency was not measured in this report, so that check is missing."]
    lines += [
        "",
        "The rule was fixed before looking at the results: no regression in recall, MRR, correct "
        "rate or hallucination rate, at least one of them improves, and the p95 retrieval "
        f"overhead stays within {decision['rule']['max_p95_retrieval_overhead_ms']} ms.",
        "",
    ]
    return "\n".join(lines)


# --- Línea de comandos ----------------------------------------------------------------------------


def _write(path: Path | None, text: str) -> None:
    if path:
        path.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


def _run(args: argparse.Namespace) -> int:
    try:
        dataset = load_dataset(args.dataset)
    except DatasetError as error:
        print(error, file=sys.stderr)
        return 1
    base = replace(EvalConfig.from_settings(), rerank=None)
    settings = get_settings()
    policy = RerankPolicy(
        weight=settings.search_rerank_weight if args.rerank_weight is None else args.rerank_weight,
        pool=settings.search_rerank_pool,
    )
    candidate = replace(base, rerank=policy)
    with get_sessionmaker()() as session:
        comparison = compare_configs(
            session,
            base,
            candidate,
            build_provider(),
            dataset,
            repeats=1 if args.no_timing else args.repeats,
            rule=DecisionRule(max_overhead_ms=args.max_overhead_ms),
        )
    report = comparison.to_dict(include_timing=not args.no_timing)
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    _write(args.output, text)
    if args.markdown:
        _write(args.markdown, render_markdown(report))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.evaluation.compare",
        description="Compara la recuperación sin y con reranking sobre el mismo dataset.",
    )
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--rerank-weight", type=float, help="peso léxico (por defecto, el de .env)")
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS, help="pasadas medidas")
    parser.add_argument("--max-overhead-ms", type=float, default=DEFAULT_MAX_OVERHEAD_MS)
    parser.add_argument("--no-timing", action="store_true", help="informe reproducible byte a byte")
    parser.add_argument("--output", type=Path, help="guarda el informe JSON en este archivo")
    parser.add_argument("--markdown", type=Path, help="guarda el informe legible en este archivo")
    parser.set_defaults(handler=_run)
    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
