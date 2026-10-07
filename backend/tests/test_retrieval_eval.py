"""Evaluación de la recuperación de punta a punta: similitud, ámbito y procedencia.

Un corpus pequeño y conocido de tres usuarios se sube por la API, lo procesa el worker (extracción,
fragmentos, embeddings) y se consulta por `POST /search` contra Postgres real. Las preguntas de
muestra tienen una fuente esperada, así que la calidad se mide (acierto@k y MRR), no se supone.
"""

import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import Session

from app.embeddings.registry import get_embedding_provider
from app.features.documents.models import DocumentChunk
from app.features.documents.storage import LocalFileStorage
from tests.pdfs import make_pdf
from tests.test_document_flow import ALICE, BOB, ingest_all, upload
from tests.test_documents_api import sign_in
from tests.test_embedding_store import PROVIDER

SEARCH = "/api/v1/search"
CAROL = "carol@example.com"
TOP_K = 3

# Manual de la comunidad: párrafos de dos líneas separados por una en blanco (el párrafo `i`
# ocupa las líneas 3i+1 y 3i+2), con tamaño suficiente para partirse en varios fragmentos.
MANUAL_TOPICS = [
    "La calefacción central se enciende el uno de noviembre y se apaga el treinta de abril.",
    "El ascensor tiene revisión técnica obligatoria cada seis meses por una empresa autorizada.",
    "El garaje comunitario cierra a medianoche y las motos aparcan junto a la rampa.",
    "La portería recoge los paquetes de mensajería y los entrega con una firma del vecino.",
    "La piscina abre en junio y exige ducha previa y gorro de baño a todos los bañistas.",
    "El contador del agua se lee el primer lunes de cada trimestre en el cuarto de máquinas.",
    "Las obras de reforma solo se permiten de lunes a viernes entre las nueve y las seis.",
    "El tablón de anuncios del portal publica las actas de las juntas de propietarios.",
]
FILLER = "Consulte el reglamento interno de la comunidad para más detalles sobre este punto."
MANUAL = "\n\n".join(f"{topic}\n{FILLER}" for topic in MANUAL_TOPICS).encode()

FACTURAS = (
    "# Facturas\n\n## Luz\n\nLa factura de la luz vence el día quince de cada mes y se paga "
    "por domiciliación bancaria.\n\n## Agua\n\nLa factura del agua se paga cada trimestre "
    "en la oficina municipal.\n"
).encode()
FACTURAS_BOB = (
    "# Facturas\n\n## Luz\n\nLa factura de la luz de Bob vence el día cinco de cada mes.\n"
).encode()
RECETAS = make_pdf(
    [
        "receta de paella valenciana con arroz bomba azafran y pollo",
        "tortilla de patatas con huevos cebolla y aceite de oliva",
        "gazpacho andaluz con tomate pepino pimiento y ajo",
    ]
)
COFRE = b"la clave del cofre de Bob es secreta y nadie mas debe conocerla"
RECETAS_CAROL = b"receta de paella valenciana con arroz bomba azafran y pollo"


@dataclass(frozen=True)
class Corpus:
    """Ids de los documentos subidos, por usuario y nombre de archivo."""

    ids: dict[str, dict[str, str]]

    def owner_of(self, document_id: str) -> str:
        return next(user for user, docs in self.ids.items() if document_id in docs.values())

    def id(self, user: str, filename: str) -> str:
        return self.ids[user][filename]


@pytest.fixture(autouse=True)
def embedder(raw_client: TestClient) -> None:
    """La API vectoriza las preguntas igual que el worker vectorizó los documentos."""
    raw_client.app.dependency_overrides[get_embedding_provider] = lambda: PROVIDER  # type: ignore[attr-defined]


@pytest.fixture
def corpus(client: TestClient, db_session: Session, storage: LocalFileStorage) -> Corpus:
    files = {
        ALICE: {
            "manual.txt": MANUAL,
            "facturas.md": FACTURAS,
            "recetas.pdf": RECETAS,
        },
        BOB: {"facturas.md": FACTURAS_BOB, "cofre.txt": COFRE},
        CAROL: {"recetas.txt": RECETAS_CAROL},
    }
    ids: dict[str, dict[str, str]] = {}
    for user, documents in files.items():
        sign_in(client, user)
        ids[user] = {}
        for filename, content in documents.items():
            response = upload(client, filename, content)
            assert response.status_code == 201, response.text
            ids[user][filename] = response.json()["id"]
    assert ingest_all(db_session, storage) == sum(len(docs) for docs in files.values())
    return Corpus(ids)


def search(client: TestClient, user: str, question: str, **fields: Any) -> list[dict[str, Any]]:
    sign_in(client, user)
    response = client.post(SEARCH, json={"question": question, **fields})
    assert response.status_code == 200, response.text
    results: list[dict[str, Any]] = response.json()["results"]
    return results


# ── Preguntas de muestra: la fuente esperada aparece en el top-k ────────────


@dataclass(frozen=True)
class Case:
    user: str
    question: str
    filename: str  # fuente esperada, del propio usuario


CASES = [
    Case(ALICE, "¿cuándo vence la factura de la luz?", "facturas.md"),
    Case(ALICE, "¿cada cuánto se paga la factura del agua en la oficina municipal?", "facturas.md"),
    Case(ALICE, "receta de paella con arroz bomba y azafran", "recetas.pdf"),
    Case(ALICE, "tortilla de patatas con huevos y cebolla", "recetas.pdf"),
    Case(ALICE, "gazpacho andaluz con tomate y pepino", "recetas.pdf"),
    Case(ALICE, "¿cuándo se enciende la calefacción central?", "manual.txt"),
    Case(ALICE, "revisión técnica del ascensor cada seis meses", "manual.txt"),
    Case(ALICE, "¿a qué hora cierra el garaje comunitario?", "manual.txt"),
    Case(ALICE, "actas de las juntas de propietarios en el tablón de anuncios", "manual.txt"),
    Case(BOB, "¿cuándo vence la factura de la luz?", "facturas.md"),
    Case(BOB, "¿cuál es la clave del cofre?", "cofre.txt"),
    Case(CAROL, "receta de paella con arroz bomba y azafran", "recetas.txt"),
]


def rank_of(client: TestClient, corpus: Corpus, case: Case, k: int = TOP_K) -> int | None:
    """Posición (desde 1) de la fuente esperada entre los `k` primeros resultados, o None."""
    expected = corpus.id(case.user, case.filename)
    results = search(client, case.user, case.question, k=k, min_score=0)
    for position, result in enumerate(results, start=1):
        if result["source"]["document_id"] == expected:
            return position
    return None


@pytest.mark.parametrize("case", CASES, ids=lambda case: f"{case.user[:5]}-{case.question[:30]}")
def test_the_expected_source_is_in_the_top_k(
    client: TestClient, corpus: Corpus, case: Case
) -> None:
    assert rank_of(client, corpus, case) is not None


def test_the_sample_questions_meet_the_quality_bar(client: TestClient, corpus: Corpus) -> None:
    ranks = [rank_of(client, corpus, case) for case in CASES]

    hit_rate = sum(rank is not None for rank in ranks) / len(ranks)
    mrr = sum(1 / rank for rank in ranks if rank) / len(ranks)

    # El embedder de pruebas es una bolsa de palabras: un fragmento con varios temas (el manual)
    # diluye su vector y puede quedar tercero. Es una red contra regresiones, no una promesa de
    # calidad del modelo real: lo exigible es que la fuente esperada esté siempre en el top-k.
    assert hit_rate == 1.0
    assert mrr >= 0.8, f"MRR {mrr:.2f}; posiciones: {ranks}"


def test_the_expected_source_ranks_first_for_a_verbatim_question(
    client: TestClient, corpus: Corpus
) -> None:
    results = search(client, CAROL, RECETAS_CAROL.decode(), k=1)

    assert results[0]["source"]["document_id"] == corpus.id(CAROL, "recetas.txt")
    assert results[0]["score"] == pytest.approx(1.0, abs=1e-3)


# ── Similitud ───────────────────────────────────────────────────────────────


def test_scores_are_ordered_and_bounded(client: TestClient, corpus: Corpus) -> None:
    results = search(client, ALICE, "factura de la luz", k=10, min_score=0)

    scores = [result["score"] for result in results]
    assert len(scores) >= 2
    assert scores == sorted(scores, reverse=True)
    assert all(0 <= score <= 1 for score in scores)


def test_an_unrelated_question_finds_nothing_above_a_strict_threshold(
    client: TestClient, corpus: Corpus
) -> None:
    assert search(client, ALICE, "teoría cuántica de campos", min_score=0.6) == []


def test_the_search_is_deterministic(client: TestClient, corpus: Corpus) -> None:
    first = search(client, ALICE, "factura de la luz", k=10, min_score=0)
    second = search(client, ALICE, "factura de la luz", k=10, min_score=0)

    assert first == second


# ── Procedencia ─────────────────────────────────────────────────────────────


def test_a_pdf_result_cites_its_page(client: TestClient, corpus: Corpus) -> None:
    for page, question in enumerate(
        [
            "receta de paella con arroz bomba",
            "tortilla de patatas con huevos",
            "gazpacho andaluz con tomate",
        ],
        start=1,
    ):
        [top] = search(client, ALICE, question, k=1)

        assert top["source"]["document_id"] == corpus.id(ALICE, "recetas.pdf")
        assert top["source"]["filename"] == "recetas.pdf"
        assert top["source"]["page"] == page


def test_a_markdown_result_cites_its_section(client: TestClient, corpus: Corpus) -> None:
    [luz] = search(client, ALICE, "¿cuándo vence la factura de la luz?", k=1)
    [agua] = search(client, ALICE, "la factura del agua se paga cada trimestre", k=1)

    assert luz["source"]["section"] == "Facturas > Luz"
    assert agua["source"]["section"] == "Facturas > Agua"
    assert luz["source"]["filename"] == agua["source"]["filename"] == "facturas.md"


@pytest.mark.parametrize("index", range(len(MANUAL_TOPICS)))
def test_a_text_result_cites_the_lines_that_contain_the_answer(
    client: TestClient, corpus: Corpus, index: int
) -> None:
    [top] = search(client, ALICE, MANUAL_TOPICS[index], k=1)

    source = top["source"]
    first, last = 3 * index + 1, 3 * index + 2
    assert source["document_id"] == corpus.id(ALICE, "manual.txt")
    assert source["start_line"] <= first <= last <= source["end_line"]
    assert MANUAL_TOPICS[index][:40] in MANUAL.decode()


def test_the_cited_chunk_is_the_stored_chunk(
    client: TestClient, corpus: Corpus, db_session: Session
) -> None:
    for result in search(client, ALICE, "factura de la luz", k=5, min_score=0):
        source = result["source"]
        chunk = db_session.get(DocumentChunk, uuid.UUID(source["chunk_id"]))

        assert chunk is not None
        assert str(chunk.document_id) == source["document_id"]
        assert (chunk.ordinal, chunk.page, chunk.section) == (
            source["ordinal"],
            source["page"],
            source["section"],
        )
        assert (chunk.start_line, chunk.end_line) == (source["start_line"], source["end_line"])
        assert result["snippet"].rstrip("…").strip()[:30] in " ".join(chunk.text.split())


# ── Propietario ─────────────────────────────────────────────────────────────


def test_every_result_belongs_to_the_asker(client: TestClient, corpus: Corpus) -> None:
    questions = [case.question for case in CASES] + ["factura", "receta", "clave secreta"]
    for user in (ALICE, BOB, CAROL):
        for question in questions:
            for result in search(client, user, question, k=20, min_score=0):
                assert corpus.owner_of(result["source"]["document_id"]) == user


def test_the_same_question_returns_each_users_own_document(
    client: TestClient, corpus: Corpus
) -> None:
    question = "¿cuándo vence la factura de la luz?"

    alice = search(client, ALICE, question, k=1)
    bob = search(client, BOB, question, k=1)

    assert alice[0]["source"]["document_id"] == corpus.id(ALICE, "facturas.md")
    assert bob[0]["source"]["document_id"] == corpus.id(BOB, "facturas.md")
    assert "Bob" in bob[0]["snippet"]
    assert "Bob" not in alice[0]["snippet"]


def test_another_users_better_match_never_takes_a_slot(client: TestClient, corpus: Corpus) -> None:
    """El filtro se aplica en la consulta, antes del límite: el mejor global no tapa lo propio."""
    question = RECETAS_CAROL.decode()  # coincidencia exacta con el documento de Carol

    [top] = search(client, ALICE, question, k=1, min_score=0)

    assert top["source"]["document_id"] == corpus.id(ALICE, "recetas.pdf")


def test_a_user_with_no_documents_gets_nothing(client: TestClient, corpus: Corpus) -> None:
    assert search(client, "dave@example.com", "factura de la luz", min_score=0) == []


def test_selecting_another_users_documents_returns_nothing(
    client: TestClient, corpus: Corpus
) -> None:
    foreign = [corpus.id(ALICE, "facturas.md"), corpus.id(CAROL, "recetas.txt")]

    results = search(client, BOB, "factura de la luz", document_ids=foreign, min_score=0)

    assert results == []


def test_a_selection_narrows_the_search_to_the_chosen_documents(
    client: TestClient, corpus: Corpus
) -> None:
    chosen = [corpus.id(ALICE, "manual.txt")]

    results = search(client, ALICE, "factura de la luz", document_ids=chosen, k=10, min_score=0)

    assert results
    assert {r["source"]["document_id"] for r in results} == set(chosen)


def test_the_owner_filter_is_part_of_the_sql_query(
    client: TestClient, corpus: Corpus, db_session: Session
) -> None:
    """Se captura el SQL real enviado a Postgres: filtra por propietario con el id del usuario."""
    bob = sign_in(client, BOB)
    captured: list[tuple[str, Any]] = []

    def record(_conn: Any, _cursor: Any, statement: str, parameters: Any, *_: Any) -> None:
        captured.append((statement, parameters))

    bind = db_session.get_bind()
    event.listen(bind, "before_cursor_execute", record)
    try:
        search(client, BOB, "factura de la luz", min_score=0)
    finally:
        event.remove(bind, "before_cursor_execute", record)

    [(statement, parameters)] = [
        item for item in captured if "chunk_embeddings" in item[0] and "<=>" in item[0]
    ]
    where = statement.split("WHERE", 1)[1].split("ORDER BY", 1)[0]
    assert "documents.owner_id" in where
    assert bob in _flatten(parameters)


def _flatten(parameters: Any) -> list[Any]:
    values = parameters.values() if isinstance(parameters, dict) else parameters
    return list(values)
