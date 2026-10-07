"""Puerta de regresión: umbrales contrastados con una ejecución determinista y sin secretos."""

import json
import re
from collections.abc import Sequence
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.evaluation import gate
from app.evaluation.answers import extractive_responder
from app.evaluation.dataset import load_dataset
from app.evaluation.gate import (
    Check,
    GateError,
    Threshold,
    evaluate,
    load_spec,
    parse_spec,
    run_gate,
)
from app.llm.provider import Message

VALID: dict[str, Any] = {
    "schema": 1,
    "dataset": "atlas-qa-v1",
    "dataset_key": "atlas-qa@1.0.0",
    "config": {
        "chunk_size": 1000,
        "chunk_overlap": 150,
        "top_k": 5,
        "context_max_tokens": 3000,
        "rerank": {"weight": 0.5, "pool": 3},
    },
    "thresholds": {"retrieval.overall.recall": {"min": 0.9}},
}


def spec_with(**changes: Any) -> gate.GateSpec:
    return parse_spec({**VALID, **changes})


class TestSpecification:
    def test_the_versioned_thresholds_are_valid_and_match_the_dataset(self) -> None:
        spec = load_spec()
        assert spec.dataset_key == load_dataset(spec.dataset).key
        metrics = {t.metric for t in spec.thresholds}
        assert {"retrieval.overall.recall", "answers.citation_recall"} <= metrics
        assert {"answers.citation_precision", "answers.hallucination_rate"} <= metrics

    def test_the_configuration_can_only_use_fake_models(self) -> None:
        config = load_spec().eval_config()
        assert config.embedder.spec.provider == "fake"
        assert "provider" not in json.loads(gate.GATE_PATH.read_text())["config"]

    def test_every_problem_is_reported_at_once(self) -> None:
        with pytest.raises(GateError) as error:
            parse_spec(
                {
                    "schema": 2,
                    "dataset": "",
                    "config": {"top_k": "5", "rerank": {"weight": 3, "pool": 3}},
                    "thresholds": {
                        "latency.p95": {"min": 1},
                        "retrieval.overall.recall": {"min": "alto"},
                        "answers.correct_rate": {"min": 0.9, "max": 0.1},
                        "answers.faithfulness": {},
                    },
                }
            )
        text = "\n".join(error.value.problems)
        for expected in (
            "schema debe ser 1",
            "dataset_key",
            "config.top_k",
            "config.chunk_size",
            "config.rerank",
            "latency.p95",
            "retrieval.overall.recall: los límites",
            "answers.correct_rate: min no puede ser mayor",
            "answers.faithfulness: el umbral",
        ):
            assert expected in text

    def test_rerank_may_be_off(self) -> None:
        config = {**VALID["config"], "rerank": None}
        assert parse_spec({**VALID, "config": config}).rerank is None

    def test_an_unreadable_file_is_a_gate_error(self, tmp_path: Path) -> None:
        with pytest.raises(GateError, match="no se puede leer"):
            load_spec(tmp_path / "missing.json")
        (tmp_path / "bad.json").write_text("{")
        with pytest.raises(GateError, match="no se puede leer"):
            load_spec(tmp_path / "bad.json")


class TestEvaluation:
    spec = replace(
        spec_with(),
        thresholds=(
            Threshold("retrieval.overall.recall", minimum=0.9),
            Threshold("answers.hallucination_rate", maximum=0.05),
        ),
    )

    def measured(self, recall: float | None, hallucination: float) -> dict[str, Any]:
        return {
            "retrieval": {"overall": {"recall": recall}},
            "answers": {"hallucination_rate": hallucination},
        }

    def test_it_passes_when_every_metric_is_within_its_threshold(self) -> None:
        result = evaluate(self.spec, self.measured(0.95, 0.0))
        assert result.passed and not result.failures

    def test_the_limit_itself_is_acceptable(self) -> None:
        assert evaluate(self.spec, self.measured(0.9, 0.05)).passed

    def test_a_metric_below_its_minimum_fails_and_is_named(self) -> None:
        result = evaluate(self.spec, self.measured(0.89, 0.0))
        assert [c.metric for c in result.failures] == ["retrieval.overall.recall"]
        assert "FAIL retrieval.overall.recall = 0.8900" in result.render()
        assert "FAILED (1 below threshold)" in result.render()

    def test_a_metric_above_its_maximum_fails(self) -> None:
        result = evaluate(self.spec, self.measured(0.95, 0.06))
        assert [c.metric for c in result.failures] == ["answers.hallucination_rate"]

    def test_a_metric_that_cannot_be_measured_fails_instead_of_passing_silently(self) -> None:
        assert not evaluate(self.spec, self.measured(None, 0.0)).passed
        missing = evaluate(self.spec, {"retrieval": {}, "answers": {}})
        assert [c.value for c in missing.checks] == [None, None]
        assert not missing.passed
        assert "not measurable" in missing.render()

    def test_both_bounds_are_checked(self) -> None:
        assert Check("m", 0.5, 0.4, 0.6).passed
        assert not Check("m", 0.7, 0.4, 0.6).passed
        assert not Check("m", 0.3, 0.4, 0.6).passed

    def test_the_result_is_serializable(self) -> None:
        report = evaluate(self.spec, self.measured(0.8, 0.0)).to_dict()
        assert report["passed"] is False
        assert json.loads(json.dumps(report)) == report


class TestTheGate:
    """Ejecuciones reales con la base de pruebas, sin ningún proveedor externo."""

    def test_the_current_pipeline_clears_the_agreed_thresholds(self, db_session: Session) -> None:
        result = run_gate(db_session)
        assert result.passed, result.render()

    def test_it_is_deterministic(self, db_session: Session) -> None:
        assert run_gate(db_session).to_dict() == run_gate(db_session).to_dict()

    def test_it_needs_no_provider_credentials(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Si la puerta construyera un proveedor real desde el entorno, `Settings` fallaría aquí.
        monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("OPENAI_API_KEY", "")
        get_settings.cache_clear()
        try:
            assert run_gate(db_session).passed
        finally:
            get_settings.cache_clear()

    def test_it_ignores_the_environment_configuration(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SEARCH_DEFAULT_K", "1")
        monkeypatch.setenv("CHUNK_SIZE_CHARS", "200")
        get_settings.cache_clear()
        try:
            assert run_gate(db_session).passed
        finally:
            get_settings.cache_clear()

    def test_it_fails_when_retrieval_gets_worse(self, db_session: Session) -> None:
        worse = replace(load_spec(), top_k=1)
        result = run_gate(db_session, worse)
        assert not result.passed
        assert "retrieval.overall.recall" in {c.metric for c in result.failures}

    def test_it_fails_when_the_ranking_loses_the_reranker(self, db_session: Session) -> None:
        result = run_gate(db_session, replace(load_spec(), rerank=None))
        assert "retrieval.overall.mrr" in {c.metric for c in result.failures}

    def test_it_fails_when_citations_get_worse(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def without_citations(messages: Sequence[Message]) -> str:
            return re.sub(r"\s*\[[^\]]*\]\s*$", "", extractive_responder(messages))

        monkeypatch.setattr(gate, "extractive_responder", without_citations)
        failed = {c.metric for c in run_gate(db_session).failures}
        assert {"answers.citation_recall", "answers.faithfulness"} <= failed

    def test_thresholds_for_another_dataset_version_are_refused(self, db_session: Session) -> None:
        stale = replace(load_spec(), dataset_key="atlas-qa@0.9.0")
        with pytest.raises(GateError, match="atlas-qa@0.9.0"):
            run_gate(db_session, stale)

    def test_it_leaves_no_trace(self, db_session: Session) -> None:
        from sqlalchemy import func, select

        from app.features.documents.models import Document, DocumentChunk
        from app.features.users.models import User

        models = (User, Document, DocumentChunk)
        before = [db_session.scalar(select(func.count()).select_from(m)) for m in models]
        run_gate(db_session)
        assert [db_session.scalar(select(func.count()).select_from(m)) for m in models] == before


class TestCommandLine:
    @pytest.fixture(autouse=True)
    def use_test_session(self, db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(gate, "get_sessionmaker", lambda: lambda: nullcontext(db_session))

    def test_it_exits_zero_and_writes_the_result_when_the_gate_passes(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        output = tmp_path / "gate.json"
        assert gate.main(["--output", str(output)]) == 0
        assert "Gate passed." in capsys.readouterr().out
        assert json.loads(output.read_text())["passed"] is True

    def test_it_exits_one_and_names_what_failed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        strict = {**VALID, "thresholds": {"retrieval.overall.recall": {"min": 1.01}}}
        spec = tmp_path / "strict.json"
        spec.write_text(json.dumps(strict))
        assert gate.main(["--spec", str(spec)]) == 1
        out = capsys.readouterr().out
        assert "FAIL retrieval.overall.recall" in out
        assert "Gate FAILED" in out

    def test_an_invalid_definition_exits_two_with_a_message(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        spec = tmp_path / "bad.json"
        spec.write_text(json.dumps({**VALID, "thresholds": {}}))
        assert gate.main(["--spec", str(spec)]) == 2
        assert "thresholds" in capsys.readouterr().err

    def test_an_unknown_dataset_exits_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        spec = tmp_path / "other.json"
        spec.write_text(json.dumps({**VALID, "dataset": "no-existe"}))
        assert gate.main(["--spec", str(spec)]) == 2
        assert capsys.readouterr().err
