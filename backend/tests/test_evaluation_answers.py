"""Evaluación de las respuestas: veredictos, fidelidad, citas y calibración con revisión humana."""

import json
import uuid
from collections.abc import Sequence
from contextlib import nullcontext
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.answers.citations import Citation, VerifiedAnswer
from app.answers.context import BoundedContext, ContextItem
from app.answers.generate import Answer, Provenance
from app.answers.grounding import split_statements
from app.answers.prompt import INSUFFICIENT_MARKER, PROMPT_VERSION, build_messages
from app.answers.service import Abstained, Answered
from app.embeddings.fake import FakeEmbeddingProvider
from app.evaluation import answers
from app.evaluation.answers import (
    Verdict,
    calibrate,
    coverage,
    extractive_responder,
    review_sample,
    run_answer_eval,
    score_outcome,
)
from app.evaluation.dataset import Evidence, Kind, Question, load_dataset
from app.evaluation.retrieval import EvalConfig
from app.features.documents.models import Document, DocumentChunk
from app.features.users.models import User
from app.ingestion.chunking import ChunkPolicy
from app.llm.fake import FakeLLMProvider
from app.llm.provider import Completion as LLMCompletion
from app.llm.provider import LLMParams, LLMSpec, Message

QUOTE = "Cada empleada o empleado puede teletrabajar hasta tres días por semana."
EVIDENCE = Evidence("teletrabajo.md", QUOTE)
OTHER = "El jueves es día de presencia obligatoria en la oficina central."


def config() -> EvalConfig:
    return EvalConfig(
        embedder=FakeEmbeddingProvider("fake-model", 1536),
        policy=ChunkPolicy(size=1000, overlap=150),
        top_k=3,
    )


def question(
    kind: Kind = Kind.ANSWERABLE,
    facts: tuple[str, ...] = ("tres días",),
    evidence: tuple[Evidence, ...] = (EVIDENCE,),
) -> Question:
    return Question("q001", kind, "¿Cuántos días?", evidence, facts, (), "")


def item(label: str, text: str, filename: str = "teletrabajo.md") -> ContextItem:
    return ContextItem(
        label, uuid.uuid4(), uuid.uuid4(), filename, 0, None, None, None, None, 0.8, text
    )


def answered(text: str, *items: ContextItem, cited: Sequence[str] = ()) -> Answered:
    """Una respuesta ya verificada con las citas `cited` (etiquetas del contexto)."""
    context = BoundedContext(tuple(items), 3000)
    spec = LLMSpec("fake", "m", "1")
    completion = LLMCompletion(text, spec, LLMParams())
    provenance = Provenance(PROMPT_VERSION, "f", spec.key, 0.0, 512, 10, ())
    answer = Answer("¿?", text, context, completion, provenance, split_statements(text))
    citations = tuple(
        Citation.from_item(found) for label in cited if (found := context.item(label)) is not None
    )
    verified = VerifiedAnswer(answer, text, text, citations, (), split_statements(text))
    return Answered(verified)


def abstained(reason: str = "insufficient_evidence") -> Abstained:
    return Abstained("¿?", reason, BoundedContext((), 3000))  # type: ignore[arg-type]


def score(q: Question, outcome: Answered | Abstained) -> answers.AnswerResult:
    return score_outcome(q, outcome, latency_ms=1.0, input_tokens=10, output_tokens=5)


class TestVerdicts:
    def test_a_complete_supported_answer_is_correct(self) -> None:
        outcome = answered(f"{QUOTE} [S1]", item("S1", QUOTE), cited=["S1"])
        result = score(question(), outcome)
        assert result.verdict is Verdict.CORRECT
        assert (result.key_fact_recall, result.faithfulness, result.cited_rate) == (1.0, 1.0, 1.0)

    def test_missing_facts_make_it_partial_or_incorrect(self) -> None:
        facts = ("tres días", "viernes")
        outcome = answered(f"{QUOTE} [S1]", item("S1", QUOTE), cited=["S1"])
        assert score(question(facts=facts), outcome).verdict is Verdict.PARTIAL
        wrong = answered(f"{OTHER} [S1]", item("S1", OTHER), cited=["S1"])
        assert score(question(), wrong).verdict is Verdict.INCORRECT

    def test_an_answer_with_nothing_behind_it_is_a_hallucination(self) -> None:
        # Dice el dato correcto, pero lo atribuye a una fuente que no lo contiene.
        outcome = answered(f"{QUOTE} [S1]", item("S1", OTHER), cited=["S1"])
        result = score(question(), outcome)
        assert result.faithfulness == 0.0
        assert result.verdict is Verdict.HALLUCINATION

    def test_an_uncited_claim_is_not_supported(self) -> None:
        outcome = answered(QUOTE, item("S1", QUOTE))
        result = score(question(), outcome)
        assert (result.faithfulness, result.cited_rate) == (0.0, 0.0)
        assert result.verdict is Verdict.HALLUCINATION

    def test_external_sentences_do_not_count_against_faithfulness(self) -> None:
        text = f"{QUOTE} [S1] (No consta en los documentos) Suele pactarse con el equipo."
        outcome = answered(text, item("S1", QUOTE), cited=["S1"])
        result = score(question(), outcome)
        assert result.faithfulness == 1.0
        assert result.verdict is Verdict.CORRECT

    def test_answering_something_unanswerable_is_a_hallucination(self) -> None:
        outcome = answered(f"{QUOTE} [S1]", item("S1", QUOTE), cited=["S1"])
        result = score(question(Kind.UNANSWERABLE, (), ()), outcome)
        assert result.verdict is Verdict.HALLUCINATION
        assert result.citation_precision is None

    def test_abstaining_is_valid_only_when_there_is_no_answer(self) -> None:
        valid = score(question(Kind.UNANSWERABLE, (), ()), abstained())
        needless = score(question(), abstained("no_relevant_chunks"))
        assert valid.verdict is Verdict.VALID_ABSTENTION
        assert needless.verdict is Verdict.UNNECESSARY_ABSTENTION
        assert needless.abstention == "no_relevant_chunks"
        assert needless.faithfulness is None

    def test_citation_quality_is_measured_against_the_annotated_evidence(self) -> None:
        good, bad = item("S1", QUOTE), item("S2", OTHER)
        outcome = answered(f"{QUOTE} [S1] {OTHER} [S2]", good, bad, cited=["S1", "S2"])
        result = score(question(), outcome)
        assert result.citation_precision == 0.5
        assert result.citation_recall == 1.0
        assert result.cited == ("S1", "S2")

    def test_recall_counts_each_piece_of_evidence(self) -> None:
        other = Evidence("teletrabajo.md", OTHER)
        outcome = answered(f"{QUOTE} [S1]", item("S1", QUOTE), cited=["S1"])
        result = score(question(evidence=(EVIDENCE, other)), outcome)
        assert result.citation_recall == 0.5
        assert result.citation_precision == 1.0


class TestSupport:
    def test_coverage_counts_content_words(self) -> None:
        assert coverage("Puedes teletrabajar tres días", [QUOTE]) == 0.75
        assert coverage("Sí.", [QUOTE]) == 1.0  # sin palabras con contenido

    def test_citation_labels_and_the_external_marker_are_ignored(self) -> None:
        assert coverage(f"{QUOTE} [S1]", [QUOTE]) == 1.0
        assert coverage("(No consta en los documentos)", []) == 1.0


class TestExtractiveResponder:
    def sources(self, question_text: str) -> list[Message]:
        context = BoundedContext(
            (
                item("S1", f"# Días\n\n{QUOTE} Los viernes no se teletrabaja."),
                item("S2", OTHER, "otro.md"),
            ),
            3000,
        )
        return build_messages(question_text, context)

    def test_it_cites_the_closest_sentence_without_headings(self) -> None:
        reply = extractive_responder(self.sources("¿Cuántos días puedo teletrabajar?"))
        assert reply == f"{QUOTE} [S1]"

    def test_it_declares_insufficient_evidence_without_overlap(self) -> None:
        reply = extractive_responder(self.sources("¿Cuántas vacaciones tengo al año?"))
        assert reply == INSUFFICIENT_MARKER

    def test_it_is_deterministic(self) -> None:
        messages = self.sources("¿Cuántos días puedo teletrabajar?")
        assert extractive_responder(messages) == extractive_responder(messages)


class TestExperiment:
    def run(self, db_session: Session, responder: object = None) -> answers.AnswerReport:
        provider = FakeLLMProvider("m", LLMParams(), responder or extractive_responder)  # type: ignore[arg-type]
        return run_answer_eval(db_session, config(), provider)

    def test_it_is_reproducible(self, db_session: Session) -> None:
        first, second = self.run(db_session), self.run(db_session)
        assert first.to_json(include_timing=False) == second.to_json(include_timing=False)

    def test_the_report_records_what_is_needed_to_repeat_it(self, db_session: Session) -> None:
        report = self.run(db_session).to_dict()
        llm = report["config"]["llm"]
        assert llm["key"] == "fake/m/v1"
        assert llm["prompt_version"] == PROMPT_VERSION
        assert llm["prompt_fingerprint"]
        assert (llm["temperature"], llm["max_output_tokens"]) == (0.0, 512)
        assert report["config"]["embedding"]["key"] == "fake/fake-model/1536/v1"
        assert report["dataset"]["key"] == "atlas-qa@1.0.0"
        assert report["cost"]["input_tokens"] > 0
        assert len(report["questions"]) == len(load_dataset().questions)

    def test_it_tells_correct_answers_abstentions_and_hallucinations_apart(
        self, db_session: Session
    ) -> None:
        metrics = self.run(db_session).metrics
        verdicts = metrics["verdicts"]
        assert verdicts["correct"] >= 8
        assert verdicts["valid_abstention"] >= 3
        assert sum(verdicts.values()) == metrics["questions"] == 28
        assert metrics["by_kind"]["unanswerable"]["correct"] == 0

    def test_a_model_that_never_abstains_hallucinates_on_unanswerable_questions(
        self, db_session: Session
    ) -> None:
        report = self.run(db_session, lambda messages: "Treinta días laborables. [S1]")
        unanswerable = report.metrics["by_kind"]["unanswerable"]
        assert unanswerable["hallucination"] == 6
        assert unanswerable["valid_abstention"] == 0
        assert report.metrics["hallucination_rate"] >= 6 / 28

    def test_a_model_that_always_abstains_is_never_wrong_but_never_useful(
        self, db_session: Session
    ) -> None:
        metrics = self.run(db_session, lambda messages: INSUFFICIENT_MARKER).metrics
        assert metrics["valid_abstention_rate"] == 1.0
        assert metrics["correct_rate"] == 0.0
        assert metrics["unnecessary_abstention_rate"] == 1.0
        assert metrics["faithfulness"] is None

    def test_invented_citations_are_not_counted_as_support(self, db_session: Session) -> None:
        report = self.run(db_session, lambda messages: "Tres días por semana. [S9]")
        assert report.metrics["verdicts"]["unnecessary_abstention"] > 0
        assert report.metrics["citation_precision"] is None

    def test_it_leaves_no_trace(self, db_session: Session) -> None:
        models = (User, Document, DocumentChunk)
        before = [db_session.scalar(select(func.count()).select_from(m)) for m in models]
        self.run(db_session)
        assert [db_session.scalar(select(func.count()).select_from(m)) for m in models] == before


def report_for(db_session: Session) -> answers.AnswerReport:
    provider = FakeLLMProvider("m", LLMParams(), extractive_responder)
    return run_answer_eval(db_session, config(), provider)


class TestHumanReview:
    def test_the_sample_is_stratified_and_reproducible(self, db_session: Session) -> None:
        report = report_for(db_session)
        first = review_sample(report, size=10, seed=7)
        again = review_sample(report, size=10, seed=7)
        other = review_sample(report, size=10, seed=8)
        assert first == again
        assert first != other
        assert len(first["items"]) == 10
        present = {r.verdict.value for r in report.results}
        assert {i["automatic_verdict"] for i in first["items"]} == present
        assert all(i["human_verdict"] is None for i in first["items"])

    def test_the_sample_gives_the_reviewer_what_they_need(self, db_session: Session) -> None:
        item = review_sample(report_for(db_session), size=28, seed=0)["items"][0]
        assert {"question", "expected_key_facts", "answer", "abstention", "notes"} <= item.keys()

    def test_a_sample_cannot_be_bigger_than_the_dataset_or_empty(self, db_session: Session) -> None:
        report = report_for(db_session)
        assert len(review_sample(report, size=500, seed=0)["items"]) == 28
        with pytest.raises(ValueError, match="size"):
            review_sample(report, size=0, seed=0)

    def labelled(self, pairs: list[tuple[str, str | None]]) -> dict[str, object]:
        return {
            "items": [
                {"id": f"q{i}", "automatic_verdict": auto, "human_verdict": human}
                for i, (auto, human) in enumerate(pairs)
            ]
        }

    def test_calibration_measures_agreement_and_lists_disagreements(self) -> None:
        sample = self.labelled(
            [
                ("correct", "correct"),
                ("correct", "partial"),
                ("valid_abstention", "valid_abstention"),
                ("hallucination", "hallucination"),
                ("partial", None),
            ]
        )
        result = calibrate(sample)
        assert (result["labelled"], result["unlabelled"]) == (4, 1)
        assert result["accuracy"] == 0.75
        assert result["disagreements"] == [{"id": "q1", "human": "partial", "automatic": "correct"}]
        assert result["confusion"]["partial"] == {"correct": 1}
        # Acuerdo esperado por azar = (2·1 + 1·0 + 1·1 + 1·1)/16 = 4/16 → kappa = (0.75-0.25)/0.75
        assert result["kappa"] == pytest.approx(0.666667, abs=1e-6)

    def test_perfect_agreement_has_kappa_one(self) -> None:
        result = calibrate(self.labelled([("correct", "correct")] * 3))
        assert (result["accuracy"], result["kappa"]) == (1.0, 1.0)

    def test_an_unknown_human_verdict_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="desconocido"):
            calibrate(self.labelled([("correct", "bien")]))

    def test_an_unlabelled_sample_cannot_be_calibrated(self) -> None:
        with pytest.raises(ValueError, match="ninguna fila etiquetada"):
            calibrate(self.labelled([("correct", None)]))


class TestCommandLine:
    @pytest.fixture(autouse=True)
    def session(self, db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(answers, "get_sessionmaker", lambda: lambda: nullcontext(db_session))
        monkeypatch.setattr(EvalConfig, "from_settings", staticmethod(config))

    def test_it_writes_a_reproducible_report_and_a_review_sample(self, tmp_path: Path) -> None:
        paths = [tmp_path / "a.json", tmp_path / "b.json"]
        sample = tmp_path / "sample.json"
        for path in paths:
            args = ["run", "--no-timing", "--output", str(path)]
            assert answers.main([*args, "--review-sample", str(sample), "--sample-size", "5"]) == 0
        assert paths[0].read_text() == paths[1].read_text()
        assert "timing" not in json.loads(paths[0].read_text())
        assert len(json.loads(sample.read_text())["items"]) == 5

    def test_calibrate_prints_the_agreement(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        sample = tmp_path / "sample.json"
        sample.write_text(
            json.dumps(
                {
                    "items": [
                        {"id": "q1", "automatic_verdict": "correct", "human_verdict": "correct"}
                    ]
                }
            )
        )
        assert answers.main(["calibrate", str(sample)]) == 0
        assert json.loads(capsys.readouterr().out)["accuracy"] == 1.0

    def test_calibrate_reports_a_broken_sample(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        sample = tmp_path / "sample.json"
        sample.write_text(json.dumps({"items": []}))
        assert answers.main(["calibrate", str(sample)]) == 1
        assert "No se puede calibrar" in capsys.readouterr().err
        assert answers.main(["calibrate", str(tmp_path / "missing.json")]) == 1

    def test_an_unknown_dataset_fails_with_a_message(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert answers.main(["run", "--dataset", "no-existe"]) == 1
        assert capsys.readouterr().err
