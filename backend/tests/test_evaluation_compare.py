"""Comparación de configuraciones: mismo índice, deltas, decisión fijada de antemano y limpieza."""

import json
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.embeddings.fake import FakeEmbeddingProvider
from app.evaluation import compare
from app.evaluation.answers import extractive_responder
from app.evaluation.compare import (
    DecisionRule,
    compare_configs,
    render_markdown,
)
from app.evaluation.retrieval import EvalConfig
from app.features.documents.models import Document, DocumentChunk
from app.features.users.models import User
from app.ingestion.chunking import ChunkPolicy
from app.llm.fake import FakeLLMProvider
from app.llm.provider import LLMParams
from app.retrieval.rerank import RerankPolicy

BASE = EvalConfig(
    embedder=FakeEmbeddingProvider("fake-model", 1536),
    policy=ChunkPolicy(size=1000, overlap=150),
    top_k=3,
)
RERANKED = replace(BASE, rerank=RerankPolicy(weight=0.5))


def provider() -> FakeLLMProvider:
    return FakeLLMProvider("m", LLMParams(), extractive_responder)


def run(db_session: Session, **kwargs: Any) -> compare.Comparison:
    return compare_configs(db_session, BASE, RERANKED, provider(), repeats=1, **kwargs)


def delta(report: dict[str, Any], metric: str) -> dict[str, Any]:
    return next(row for row in report["deltas"] if row["metric"] == metric)


class TestComparison:
    def test_it_is_reproducible_without_timing(self, db_session: Session) -> None:
        first, second = run(db_session), run(db_session)
        assert first.to_json(include_timing=False) == second.to_json(include_timing=False)

    def test_both_arms_share_dataset_and_index_and_differ_only_in_rerank(
        self, db_session: Session
    ) -> None:
        report = run(db_session).to_dict(include_timing=False)
        base, cand = report["arms"]["baseline"], report["arms"]["candidate"]
        assert base["config"]["rerank"] is None
        assert cand["config"]["rerank"]["key"] == "lexical/v1"
        assert {k: v for k, v in base["config"].items() if k != "rerank"} == {
            k: v for k, v in cand["config"].items() if k != "rerank"
        }
        assert report["dataset"]["questions"] == 32

    def test_deltas_are_candidate_minus_baseline_and_flagged_by_direction(
        self, db_session: Session
    ) -> None:
        report = run(db_session).to_dict(include_timing=False)
        for row in report["deltas"]:
            if row["delta"] is None:
                continue
            assert row["delta"] == pytest.approx(row["candidate"] - row["baseline"], abs=1e-6)
            if row["delta"] == 0:
                assert row["outcome"] == "same"
            else:
                assert row["outcome"] == (
                    "better" if (row["delta"] > 0) == row["higher_is_better"] else "worse"
                )
        assert delta(report, "answers.hallucination_rate")["higher_is_better"] is False

    def test_it_counts_the_questions_that_change(self, db_session: Session) -> None:
        questions = run(db_session).to_dict(include_timing=False)["questions"]
        counts = questions["retrieval"]
        assert counts["improved"] + counts["worsened"] + counts["unchanged"] == 24
        assert counts["improved"] + counts["worsened"] == len(questions["retrieval_changes"])
        for item in questions["retrieval_changes"]:
            assert item["recall"][0] != item["recall"][1] or (
                item["reciprocal_rank"][0] != item["reciprocal_rank"][1]
            )

    def test_the_reranker_costs_no_model_calls_or_tokens(self, db_session: Session) -> None:
        cost = run(db_session).to_dict(include_timing=False)["cost"]
        assert cost["rerank_model_calls_per_question"] == 0
        assert cost["baseline"].keys() == cost["candidate"].keys()

    def test_the_report_warns_when_the_models_are_fake(self, db_session: Session) -> None:
        caveats = run(db_session).to_dict(include_timing=False)["caveats"]
        assert any("Fake embeddings" in item for item in caveats)
        assert any("Fake LLM" in item for item in caveats)

    def test_timing_is_measured_per_arm_and_alternating(self, db_session: Session) -> None:
        comparison = compare_configs(db_session, BASE, RERANKED, provider(), repeats=2)
        timing = comparison.to_dict()["timing"]
        assert timing["repeats"] == 2
        assert timing["samples_per_arm"] == 2 * 32
        assert (
            timing["baseline"]["retrieval_ms"]["p95"] >= timing["baseline"]["retrieval_ms"]["p50"]
        )
        assert "timing" not in comparison.to_dict(include_timing=False)

    def test_it_rejects_configurations_that_would_use_another_index(
        self, db_session: Session
    ) -> None:
        other = replace(RERANKED, policy=ChunkPolicy(size=500, overlap=50))
        with pytest.raises(ValueError, match="embedder y chunking"):
            compare_configs(db_session, BASE, other, provider(), repeats=1)

    def test_it_leaves_no_trace(self, db_session: Session) -> None:
        models = (User, Document, DocumentChunk)
        before = [db_session.scalar(select(func.count()).select_from(m)) for m in models]
        run(db_session)
        assert [db_session.scalar(select(func.count()).select_from(m)) for m in models] == before


def deltas(**outcomes: str) -> list[dict[str, Any]]:
    """Filas de deltas con el resultado dado para las métricas protegidas; el resto, `same`."""
    names = {
        "recall": "retrieval.overall.recall",
        "mrr": "retrieval.overall.mrr",
        "correct": "answers.correct_rate",
        "hallucination": "answers.hallucination_rate",
    }
    return [
        {"metric": metric, "outcome": outcomes.get(key, "same"), "delta": 0.1}
        for key, metric in names.items()
    ]


class TestDecision:
    rule = DecisionRule(max_overhead_ms=20.0)

    def timing(self, p95: float) -> dict[str, Any]:
        return {"overhead_ms": {"retrieval_ms": {"p95": p95}}}

    def test_keep_when_nothing_regresses_something_improves_and_it_is_fast_enough(self) -> None:
        decision = compare._decision(self.rule, deltas(mrr="better"), self.timing(5.0))
        assert decision["keep"] is True

    def test_drop_when_a_guarded_metric_regresses_even_if_another_improves(self) -> None:
        decision = compare._decision(
            self.rule, deltas(mrr="better", hallucination="worse"), self.timing(1.0)
        )
        assert decision["keep"] is False
        failed = [c["check"] for c in decision["checks"] if not c["passed"]]
        assert failed == ["answers.hallucination_rate does not regress"]

    def test_drop_when_nothing_improves(self) -> None:
        assert compare._decision(self.rule, deltas(), self.timing(1.0))["keep"] is False

    def test_drop_when_it_is_too_slow(self) -> None:
        decision = compare._decision(self.rule, deltas(recall="better"), self.timing(20.5))
        assert decision["keep"] is False

    def test_without_timing_the_latency_check_is_absent_and_said_so(self) -> None:
        decision = compare._decision(self.rule, deltas(recall="better"), None)
        assert decision["latency_checked"] is False
        assert all(
            "latency" not in c["check"] and "overhead" not in c["check"] for c in decision["checks"]
        )

    def test_regressions_outside_the_rule_are_listed_but_do_not_decide(self) -> None:
        rows = [*deltas(mrr="better"), {"metric": "answers.faithfulness", "outcome": "worse"}]
        decision = compare._decision(self.rule, rows, self.timing(1.0))
        assert decision["keep"] is True
        assert decision["unguarded_regressions"] == ["answers.faithfulness"]


class TestMarkdown:
    def test_it_renders_the_decision_the_caveats_and_the_changes(self, db_session: Session) -> None:
        report = compare_configs(db_session, BASE, RERANKED, provider(), repeats=1).to_dict()
        text = render_markdown(report)
        assert "# Baseline vs reranked retrieval" in text
        assert "## Caveats" in text and "Fake embeddings" in text
        assert "## Latency" in text
        assert ("**Keep the reranking.**" in text) == report["decision"]["keep"]
        assert "retrieval.overall.mrr" in text

    def test_without_timing_there_is_no_latency_section(self, db_session: Session) -> None:
        report = run(db_session).to_dict(include_timing=False)
        assert "## Latency" not in render_markdown(report)
        assert "not measured" in render_markdown(report)


class TestCommandLine:
    def test_it_writes_a_reproducible_report_and_a_markdown_summary(
        self, db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(compare, "get_sessionmaker", lambda: lambda: nullcontext(db_session))
        monkeypatch.setattr(compare, "build_provider", provider)
        paths = [tmp_path / "a.json", tmp_path / "b.json"]
        for path in paths:
            code = compare.main(
                ["--no-timing", "--output", str(path), "--markdown", str(tmp_path / "r.md")]
            )
            assert code == 0
        assert paths[0].read_text() == paths[1].read_text()
        report = json.loads(paths[0].read_text())
        assert "timing" not in report
        assert report["arms"]["candidate"]["config"]["rerank"] is not None
        assert (tmp_path / "r.md").read_text().startswith("# Baseline vs reranked")

    def test_an_unknown_dataset_fails_with_a_message(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert compare.main(["--dataset", "no-existe"]) == 1
        assert capsys.readouterr().err
