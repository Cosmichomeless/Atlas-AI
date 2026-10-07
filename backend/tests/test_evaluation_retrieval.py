"""Evaluación de la recuperación: métricas calculadas a mano y un experimento reproducible."""

import json
from contextlib import nullcontext
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.embeddings.fake import FakeEmbeddingProvider
from app.embeddings.store import SimilarChunk
from app.evaluation import retrieval
from app.evaluation.dataset import Evidence, Kind, Question, load_dataset
from app.evaluation.retrieval import (
    EvalConfig,
    aggregate,
    covers,
    run_retrieval_eval,
    score_question,
)
from app.features.documents.models import Document, DocumentChunk
from app.features.documents.states import DocumentStatus
from app.features.users.models import User
from app.ingestion.chunking import ChunkPolicy
from app.retrieval.dedup import DedupPolicy
from app.retrieval.rerank import RerankPolicy

QUOTE = "Cada empleada o empleado puede teletrabajar hasta tres días por semana."
EVIDENCE = Evidence("teletrabajo.md", QUOTE)


def config(**overrides: object) -> EvalConfig:
    fields: dict[str, object] = {
        "embedder": FakeEmbeddingProvider("fake-model", 1536),
        "policy": ChunkPolicy(size=1000, overlap=150),
        "top_k": 3,
    }
    return EvalConfig(**{**fields, **overrides})  # type: ignore[arg-type]


def hit(
    text: str, document: str = "teletrabajo.md", ordinal: int = 0, score: float = 0.8
) -> SimilarChunk:
    chunk = DocumentChunk(text=text, ordinal=ordinal)
    return SimilarChunk(chunk, Document(filename=document), 1 - score)


def question(kind: Kind, evidence: tuple[Evidence, ...] = (EVIDENCE,)) -> Question:
    return Question("q001", kind, "¿Cuántos días?", evidence, ("tres días",), (), "")


class TestRelevance:
    def test_a_chunk_containing_the_quote_covers_it(self) -> None:
        text = f"Antes del dato. {QUOTE} Después del dato."
        assert covers(text, EVIDENCE, "teletrabajo.md")

    def test_whitespace_and_case_do_not_matter(self) -> None:
        assert covers(f"  {QUOTE.upper()}\n", EVIDENCE, "teletrabajo.md")

    def test_a_quote_split_between_chunks_counts_in_both_halves(self) -> None:
        words = QUOTE.split()
        first, second = " ".join(words[:7]), " ".join(words[4:])
        assert covers(first, EVIDENCE, "teletrabajo.md")
        assert covers(second, EVIDENCE, "teletrabajo.md")

    def test_a_chunk_with_a_sliver_of_the_quote_does_not(self) -> None:
        assert not covers(
            "Cada empleada o empleado cobra una nómina mensual.", EVIDENCE, "teletrabajo.md"
        )

    def test_the_same_text_in_another_document_does_not_count(self) -> None:
        assert not covers(QUOTE, EVIDENCE, "otro.md")


class TestQuestionScores:
    def test_hand_computed_metrics(self) -> None:
        # Posiciones: 1 irrelevante, 2 relevante, 3 de otro documento.
        hits = [
            hit("Texto sin relación alguna con el asunto.", ordinal=0),
            hit(QUOTE, ordinal=1),
            hit(QUOTE, document="otro.md", ordinal=0),
        ]
        result = score_question(question(Kind.ANSWERABLE), hits, latency_ms=1.0)
        assert result.precision == pytest.approx(1 / 3, abs=1e-6)
        assert result.recall == 1.0
        assert result.source_success == 1.0
        assert result.reciprocal_rank == 0.5
        assert [h.relevant for h in result.hits] == [False, True, False]

    def test_recall_counts_each_piece_of_evidence(self) -> None:
        other = Evidence(
            "teletrabajo.md", "La silla ergonómica se solicita al departamento de Prevención."
        )
        result = score_question(
            question(Kind.AMBIGUOUS, (EVIDENCE, other)), [hit(QUOTE)], latency_ms=1.0
        )
        assert result.recall == 0.5
        assert result.precision == 1.0

    def test_source_success_without_relevant_text(self) -> None:
        result = score_question(
            question(Kind.ANSWERABLE), [hit("Otra sección del mismo documento.")], latency_ms=1.0
        )
        assert (result.source_success, result.precision, result.recall) == (1.0, 0.0, 0.0)
        assert result.reciprocal_rank == 0.0

    def test_no_hits_scores_zero_without_dividing_by_zero(self) -> None:
        result = score_question(question(Kind.ANSWERABLE), [], latency_ms=1.0)
        assert (result.precision, result.recall, result.source_success) == (0.0, 0.0, 0.0)

    def test_unanswerable_reports_only_the_top_score(self) -> None:
        result = score_question(
            question(Kind.UNANSWERABLE, ()),
            [hit("Algo.", score=0.4), hit("Otro.", score=0.3)],
            latency_ms=1.0,
        )
        assert result.precision is None and result.recall is None
        assert result.top_score == 0.4

    def test_aggregate_averages_per_kind(self) -> None:
        good = score_question(question(Kind.ANSWERABLE), [hit(QUOTE)], latency_ms=1.0)
        bad = score_question(question(Kind.ANSWERABLE), [], latency_ms=1.0)
        none = score_question(
            question(Kind.UNANSWERABLE, ()), [hit("Algo.", score=0.5)], latency_ms=1.0
        )
        metrics = aggregate([good, bad, none], min_score=0.0)
        assert metrics["answerable"] == {
            "questions": 2,
            "precision": 0.5,
            "recall": 0.5,
            "source_success": 0.5,
            "mrr": 0.5,
        }
        assert metrics["overall"]["questions"] == 2
        assert metrics["unanswerable"]["top_score_max"] == 0.5
        assert metrics["unanswerable"]["with_hits_rate"] == 1.0


class TestExperiment:
    def test_it_is_reproducible(self, db_session: Session) -> None:
        first = run_retrieval_eval(db_session, config())
        second = run_retrieval_eval(db_session, config())
        assert first.to_json(include_timing=False) == second.to_json(include_timing=False)

    def test_the_report_records_what_is_needed_to_repeat_it(self, db_session: Session) -> None:
        dataset = load_dataset()
        report = run_retrieval_eval(db_session, config(top_k=4, min_score=0.1), dataset).to_dict()
        assert report["dataset"] == {
            "key": "atlas-qa@1.1.0",
            "name": "atlas-qa",
            "version": "1.1.0",
            "content_sha256": dataset.manifest.content_sha256,
        }
        assert report["config"]["embedding"] == {
            "key": "fake/fake-model/1536/v1",
            "provider": "fake",
            "model": "fake-model",
            "dimensions": 1536,
        }
        assert report["config"]["chunking"]["key"] == ChunkPolicy(1000, 150).key
        assert report["config"]["top_k"] == 4
        assert report["config"]["min_score"] == 0.1
        assert report["config"]["dedup"] == {"min_overlap": 0.5, "window": 1, "overfetch": 3}
        assert len(report["questions"]) == len(dataset.questions)
        assert "timing" in report
        assert "timing" not in json.loads(
            run_retrieval_eval(db_session, config(), dataset).to_json(include_timing=False)
        )

    def test_the_configuration_changes_the_result_and_the_record(self, db_session: Session) -> None:
        small = run_retrieval_eval(db_session, config(policy=ChunkPolicy(300, 50), dedup=None))
        default = run_retrieval_eval(db_session, config())
        assert small.to_dict()["config"]["chunking"]["size"] == 300
        assert small.to_dict()["config"]["dedup"] is None
        assert small.to_json(include_timing=False) != default.to_json(include_timing=False)

    def test_reranking_is_recorded_with_sources_and_scores_to_compare(
        self, db_session: Session
    ) -> None:
        plain = run_retrieval_eval(db_session, config()).to_dict()
        reranked = run_retrieval_eval(db_session, config(rerank=RerankPolicy(weight=0.5))).to_dict()
        assert plain["config"]["rerank"] is None
        assert all("rerank" not in q for q in plain["questions"])
        assert reranked["config"]["rerank"]["key"] == "lexical/v1"
        entries = reranked["questions"][0]["rerank"]
        assert {"filename", "original_rank", "final_rank", "vector_score", "lexical_score"} <= (
            entries[0].keys()
        )
        # Los mismos candidatos, en otro orden: el contrato de cada resultado no cambia.
        assert reranked["questions"][0]["hits"][0].keys() == plain["questions"][0]["hits"][0].keys()

    def test_it_finds_the_sources_of_the_questions(self, db_session: Session) -> None:
        metrics = run_retrieval_eval(db_session, config(top_k=5)).metrics
        assert metrics["overall"]["source_success"] >= 0.8
        assert metrics["answerable"]["recall"] >= 0.6
        assert metrics["unanswerable"]["questions"] == 8

    def test_it_leaves_no_trace(self, db_session: Session) -> None:
        before = [
            db_session.scalar(select(func.count()).select_from(model))
            for model in (User, Document, DocumentChunk)
        ]
        run_retrieval_eval(db_session, config())
        after = [
            db_session.scalar(select(func.count()).select_from(model))
            for model in (User, Document, DocumentChunk)
        ]
        assert after == before

    def test_it_does_not_touch_other_peoples_documents(self, db_session: Session) -> None:
        owner = User(email="real@example.com", password_hash="x")
        db_session.add(owner)
        db_session.flush()
        pending = Document(
            owner_id=owner.id, filename="mio.md", content_type="text/markdown", size_bytes=1
        )
        db_session.add(pending)
        db_session.commit()
        run_retrieval_eval(db_session, config())
        db_session.refresh(pending)
        assert pending.status is DocumentStatus.UPLOADED


class TestCommandLine:
    def test_it_writes_a_reproducible_report(
        self, db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(retrieval, "get_sessionmaker", lambda: lambda: nullcontext(db_session))
        paths = [tmp_path / "a.json", tmp_path / "b.json"]
        for path in paths:
            code = retrieval.main(["--top-k", "3", "--no-timing", "--output", str(path)])
            assert code == 0
        assert paths[0].read_text() == paths[1].read_text()
        report = json.loads(paths[0].read_text())
        assert report["config"]["top_k"] == 3
        assert "timing" not in report

    def test_an_unknown_dataset_fails_with_a_message(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert retrieval.main(["--dataset", "no-existe"]) == 1
        assert capsys.readouterr().err

    def test_no_dedup_is_recorded(
        self, db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(retrieval, "get_sessionmaker", lambda: lambda: nullcontext(db_session))
        path = tmp_path / "report.json"
        assert retrieval.main(["--no-dedup", "--no-timing", "--output", str(path)]) == 0
        assert json.loads(path.read_text())["config"]["dedup"] is None
        assert DedupPolicy().overfetch == 3
