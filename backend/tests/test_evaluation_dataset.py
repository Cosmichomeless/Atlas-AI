"""El dataset de evaluación es válido, está versionado y detecta cambios y anotaciones rotas."""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from app.evaluation import dataset as ds
from app.evaluation.dataset import DatasetError, Kind, load_dataset

NAME = ds.DEFAULT_DATASET


@pytest.fixture
def copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Copia editable del dataset, para romperlo sin tocar el real."""
    shutil.copytree(ds.dataset_path(NAME), tmp_path / NAME)
    monkeypatch.setattr(ds, "DATASETS_ROOT", tmp_path)
    return tmp_path / NAME


def edit_questions(root: Path, change: Any, *, reseal: bool = True) -> None:
    path = root / ds.QUESTIONS
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data["questions"])
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if reseal:
        manifest_path = root / ds.MANIFEST
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["content_sha256"] = ds.content_sha256(root)
        manifest["counts"] = {
            **{k.value: sum(1 for q in data["questions"] if q["kind"] == k) for k in Kind},
            "documents": manifest["counts"]["documents"],
        }
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")


def problems_of(name: str = NAME) -> list[str]:
    with pytest.raises(DatasetError) as error:
        load_dataset(name)
    return error.value.problems


def find(questions: list[dict[str, Any]], qid: str) -> dict[str, Any]:
    return next(q for q in questions if q["id"] == qid)


# ── El dataset publicado ────────────────────────────────────────────────────


def test_the_published_dataset_is_valid_and_versioned() -> None:
    dataset = load_dataset()

    assert dataset.key == "atlas-qa@1.1.0"
    assert dataset.manifest.license == "MIT"
    assert dataset.manifest.usage
    assert dataset.manifest.language == "es"
    assert (dataset.root / dataset.manifest.annotation_guide).is_file()
    assert dataset.manifest.content_sha256 == ds.content_sha256(dataset.root)


def test_it_includes_answerable_ambiguous_and_unanswerable_questions() -> None:
    dataset = load_dataset()

    assert len(dataset.of_kind(Kind.ANSWERABLE)) == 18
    assert len(dataset.of_kind(Kind.AMBIGUOUS)) == 6
    assert len(dataset.of_kind(Kind.UNANSWERABLE)) == 8
    assert len(dataset.questions) == dataset.manifest.counts["answerable"] + 14


def test_answerable_questions_carry_evidence_and_key_facts() -> None:
    for question in load_dataset().of_kind(Kind.ANSWERABLE):
        assert question.evidence, question.id
        assert question.key_facts, question.id
        assert not question.interpretations


def test_ambiguous_questions_have_several_distinct_readings() -> None:
    for question in load_dataset().of_kind(Kind.AMBIGUOUS):
        assert len(question.interpretations) >= 2, question.id
        assert len({r.reading for r in question.interpretations}) == len(question.interpretations)
        assert question.rationale


def test_unanswerable_questions_have_no_evidence() -> None:
    for question in load_dataset().of_kind(Kind.UNANSWERABLE):
        assert not question.evidence
        assert not question.key_facts
        assert question.rationale


def test_every_quote_is_literal_text_of_its_document() -> None:
    dataset = load_dataset()
    for question in dataset.questions:
        for evidence in question.evidence:
            document = ds.normalize_text(dataset.documents[evidence.document])
            assert ds.normalize_text(evidence.quote) in document


def test_the_validator_cli_summarises_a_valid_dataset(capsys: pytest.CaptureFixture[str]) -> None:
    assert ds.main([]) == 0
    assert "atlas-qa@1.1.0" in capsys.readouterr().out


# ── Cambios y anotaciones rotas ─────────────────────────────────────────────


def test_changing_content_without_a_new_version_is_rejected(copy: Path) -> None:
    document = copy / ds.DOCUMENTS / "guia-gastos.md"
    document.write_text(document.read_text(encoding="utf-8") + "\nUna línea nueva.\n")

    assert any("content_sha256" in p for p in problems_of())


def test_a_quote_that_is_not_in_the_document_is_rejected(copy: Path) -> None:
    def change(questions: list[dict[str, Any]]) -> None:
        find(questions, "q001")["evidence"][0]["quote"] = "Se puede teletrabajar cinco días."

    edit_questions(copy, change)

    assert any("q001" in p and "literalmente" in p for p in problems_of())


def test_a_key_fact_missing_from_the_evidence_is_rejected(copy: Path) -> None:
    edit_questions(copy, lambda qs: find(qs, "q001").update(key_facts=["siete días"]))

    assert any("q001" in p and "dato clave" in p for p in problems_of())


def test_an_unknown_document_is_rejected(copy: Path) -> None:
    def change(questions: list[dict[str, Any]]) -> None:
        find(questions, "q002")["evidence"][0]["document"] = "no-existe.md"

    edit_questions(copy, change)

    assert any("q002" in p and "no existe" in p for p in problems_of())


def test_an_answerable_question_without_evidence_is_rejected(copy: Path) -> None:
    edit_questions(copy, lambda qs: find(qs, "q003").update(evidence=[]))

    assert any("q003" in p and "evidencia" in p for p in problems_of())


def test_an_unanswerable_question_with_evidence_is_rejected(copy: Path) -> None:
    def change(questions: list[dict[str, Any]]) -> None:
        find(questions, "u001")["evidence"] = find(questions, "q001")["evidence"]

    edit_questions(copy, change)

    assert any("u001" in p for p in problems_of())


def test_an_ambiguous_question_needs_two_readings(copy: Path) -> None:
    def change(questions: list[dict[str, Any]]) -> None:
        find(questions, "a001")["interpretations"].pop()

    edit_questions(copy, change)

    assert any("a001" in p and "dos lecturas" in p for p in problems_of())


def test_duplicate_ids_and_questions_are_rejected(copy: Path) -> None:
    def change(questions: list[dict[str, Any]]) -> None:
        questions[1]["id"] = questions[0]["id"]
        questions[3]["question"] = questions[2]["question"]

    edit_questions(copy, change)

    found = problems_of()
    assert any("identificador duplicado" in p for p in found)
    assert any("pregunta duplicada" in p for p in found)


def test_a_dataset_must_include_every_kind(copy: Path) -> None:
    edit_questions(
        copy, lambda qs: qs.__setitem__(slice(None), [q for q in qs if q["id"][0] != "u"])
    )

    assert any("unanswerable" in p for p in problems_of())


def test_manifest_counts_must_match_the_questions(copy: Path) -> None:
    manifest_path = copy / ds.MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["counts"]["answerable"] = 99
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert any("recuentos" in p for p in problems_of())


def test_the_version_must_be_semantic(copy: Path) -> None:
    manifest_path = copy / ds.MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = "uno"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert any("semántica" in p for p in problems_of())


def test_the_manifest_must_declare_license_and_usage(copy: Path) -> None:
    manifest_path = copy / ds.MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    del manifest["license"]
    manifest["usage"] = " "
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    found = problems_of()
    assert any("license" in p for p in found)


def test_all_problems_are_reported_at_once(copy: Path) -> None:
    def change(questions: list[dict[str, Any]]) -> None:
        find(questions, "q001")["key_facts"] = ["x"]
        find(questions, "q002")["evidence"] = []

    edit_questions(copy, change)

    found = problems_of()
    assert any("q001" in p for p in found)
    assert any("q002" in p for p in found)


def test_an_unreadable_dataset_raises_a_clear_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ds, "DATASETS_ROOT", tmp_path)

    assert any("No se pueden leer" in p for p in problems_of("inexistente"))


def test_the_published_dataset_has_adversarial_probes_with_canaries() -> None:
    dataset = ds.load_dataset()
    probes = [q for q in dataset.questions if q.canaries]

    assert {q.kind for q in probes} == {Kind.ANSWERABLE, Kind.UNANSWERABLE}
    assert len(probes) >= 4
    assert all(c.startswith("CANARIO-") for q in probes for c in q.canaries)


def test_a_canary_missing_from_the_corpus_is_rejected(copy: Path) -> None:
    edit_questions(copy, lambda qs: find(qs, "u007").update(canaries=["CANARIO-NO-EXISTE-0000"]))

    assert any("u007" in p and "no aparece en ningún documento" in p for p in problems_of())


def test_a_canary_that_belongs_to_a_correct_answer_is_rejected(copy: Path) -> None:
    def change(questions: list[dict[str, Any]]) -> None:
        find(questions, "q017")["key_facts"].append("CANARIO-ALFA-7731")

    edit_questions(copy, change)

    assert any("q017" in p and "forma parte de una respuesta correcta" in p for p in problems_of())


@pytest.mark.parametrize("canaries", [[], "CANARIO-ALFA-7731", [""], [7]])
def test_canaries_must_be_a_non_empty_list_of_strings(copy: Path, canaries: Any) -> None:
    edit_questions(copy, lambda qs: find(qs, "u007").update(canaries=canaries))

    assert any("u007" in p and "canaries" in p for p in problems_of())
