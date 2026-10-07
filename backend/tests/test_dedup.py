"""Reducción de resultados repetidos o contiguos: política determinista y medible."""

import uuid

import pytest
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.embeddings.store import SimilarChunk
from app.features.documents.models import Document, DocumentChunk
from app.retrieval.dedup import DedupPolicy, overlap, reduce_redundancy, shingles
from app.retrieval.question import PreparedQuestion, prepare_question
from app.retrieval.search import SearchLimits, search_chunks, search_with_report
from tests.factories import make_document
from tests.test_embedding_store import PROVIDER, index_document

POLICY = DedupPolicy(min_overlap=0.5, window=1, overfetch=3)
DB_URL = "postgresql+psycopg://x/y"


def words(start: int, stop: int) -> str:
    return " ".join(f"palabra{n}" for n in range(start, stop))


def hit(document: Document, ordinal: int, text: str, distance: float = 0.1) -> SimilarChunk:
    chunk = DocumentChunk(
        id=uuid.uuid4(), document_id=document.id, ordinal=ordinal, text=text, page=ordinal + 1
    )
    return SimilarChunk(chunk, document, distance)


@pytest.fixture
def doc() -> Document:
    return make_document(uuid.uuid4())


@pytest.fixture
def other_doc() -> Document:
    return make_document(uuid.uuid4())


def ordinals(hits: list[SimilarChunk]) -> list[int]:
    return [h.chunk.ordinal for h in hits]


# ── Medida de solape ────────────────────────────────────────────────────────


def test_overlap_measures_shared_word_sequences() -> None:
    assert overlap(words(0, 20), words(0, 20)) == 1.0
    assert overlap(words(0, 20), words(100, 120)) == 0.0
    assert overlap(words(0, 20), words(10, 30)) == pytest.approx(8 / 18)
    # Un texto contenido en otro mayor solapa por completo (se mide sobre el más corto).
    assert overlap(words(5, 10), words(0, 20)) == 1.0


def test_overlap_ignores_case_punctuation_and_spacing() -> None:
    assert overlap("Hola,   MUNDO cruel!", "hola mundo cruel") == 1.0


def test_overlap_handles_very_short_and_empty_texts() -> None:
    assert overlap("uno", "uno") == 1.0
    assert overlap("uno", "dos") == 0.0
    assert overlap("", "uno") == 0.0
    assert shingles("") == frozenset()


def test_the_policy_rejects_nonsense_parameters() -> None:
    for bad in (
        {"min_overlap": 0.0},
        {"min_overlap": 1.5},
        {"window": -1},
        {"overfetch": 0},
    ):
        with pytest.raises(ValueError, match=next(iter(bad))):
            DedupPolicy(**bad)


# ── Política ────────────────────────────────────────────────────────────────


def test_an_exact_repeat_in_the_same_document_is_dropped_wherever_it_is(doc: Document) -> None:
    hits = [hit(doc, 7, "Aviso legal"), hit(doc, 2, "otra cosa"), hit(doc, 0, "aviso   LEGAL.")]

    result = reduce_redundancy(hits, POLICY)

    assert ordinals(result.kept) == [7, 2]
    [dropped] = result.dropped
    assert (dropped.hit.chunk.ordinal, dropped.kept.chunk.ordinal) == (0, 7)
    assert (dropped.reason, dropped.overlap) == ("exact", 1.0)


def test_the_same_text_in_different_documents_is_never_merged(
    doc: Document, other_doc: Document
) -> None:
    hits = [hit(doc, 0, "texto igual"), hit(other_doc, 0, "texto igual")]

    assert reduce_redundancy(hits, POLICY).kept == hits


def test_a_contiguous_chunk_that_mostly_repeats_the_better_one_is_dropped(doc: Document) -> None:
    best = hit(doc, 4, words(0, 20), 0.1)
    neighbour = hit(doc, 5, words(8, 28), 0.2)  # 12 de 20 palabras compartidas ≈ 0.63

    result = reduce_redundancy([best, neighbour], POLICY)

    assert result.kept == [best]
    assert result.dropped[0].reason == "contiguous"
    assert result.dropped[0].overlap >= POLICY.min_overlap


def test_a_contiguous_chunk_with_mostly_new_text_is_kept(doc: Document) -> None:
    hits = [hit(doc, 4, words(0, 20)), hit(doc, 5, words(17, 40))]  # solapa ≈ 0.1

    assert reduce_redundancy(hits, POLICY).kept == hits


def test_overlapping_text_far_apart_is_kept_unless_the_window_reaches_it(doc: Document) -> None:
    hits = [hit(doc, 1, words(0, 20)), hit(doc, 3, words(2, 22))]

    assert reduce_redundancy(hits, POLICY).kept == hits
    assert ordinals(reduce_redundancy(hits, DedupPolicy(window=2)).kept) == [1]
    assert reduce_redundancy(hits, DedupPolicy(window=0)).kept == hits


def test_the_better_ranked_chunk_always_survives(doc: Document) -> None:
    worse_first = [hit(doc, 5, words(8, 28), 0.2), hit(doc, 4, words(0, 20), 0.1)]

    result = reduce_redundancy(worse_first, POLICY)

    # Se respeta el orden recibido (el de relevancia): el primero gana aunque sea el posterior.
    assert ordinals(result.kept) == [5]
    assert ordinals([d.hit for d in result.dropped]) == [4]


def test_a_chain_is_compared_with_what_was_kept_not_with_what_was_dropped(doc: Document) -> None:
    a, b, c = words(0, 20), words(8, 28), words(16, 36)
    hits = [hit(doc, 0, a), hit(doc, 1, b), hit(doc, 2, c)]

    result = reduce_redundancy(hits, POLICY)

    assert ordinals(result.kept) == [0, 2]  # b repite a; c solapa b pero no a
    assert ordinals([d.hit for d in result.dropped]) == [1]


def test_the_reduction_is_deterministic_and_idempotent(doc: Document, other_doc: Document) -> None:
    hits = [
        hit(doc, 0, words(0, 20)),
        hit(other_doc, 3, words(0, 20)),
        hit(doc, 1, words(8, 28)),
        hit(doc, 9, "aviso"),
        hit(doc, 2, "aviso"),
    ]

    first = reduce_redundancy(hits, POLICY)
    second = reduce_redundancy(hits, POLICY)
    again = reduce_redundancy(first.kept, POLICY)

    assert first == second
    assert again.kept == first.kept
    assert again.dropped == []


def test_the_redundancy_rate_measures_how_much_was_redundant(doc: Document) -> None:
    assert reduce_redundancy([], POLICY).redundancy_rate == 0.0
    clean = [hit(doc, 0, words(0, 20)), hit(doc, 5, words(50, 70))]
    assert reduce_redundancy(clean, POLICY).redundancy_rate == 0.0

    noisy = [*clean, hit(doc, 1, words(8, 28)), hit(doc, 6, words(58, 78))]
    result = reduce_redundancy(noisy, POLICY)

    assert result.redundancy_rate == pytest.approx(0.5)
    assert reduce_redundancy(result.kept, POLICY).redundancy_rate == 0.0


# ── Dentro de la búsqueda ───────────────────────────────────────────────────

FACTURA = "la factura de la luz vence el día quince de cada mes"
NEAR = f"{FACTURA} sin falta"
UNRELATED = ["el contrato de alquiler dura doce meses", "receta de paella con arroz y azafrán"]


def ask(text: str = FACTURA) -> PreparedQuestion:
    return prepare_question(text, PROVIDER, max_chars=1000)


def limits(dedup: DedupPolicy | None) -> SearchLimits:
    return SearchLimits(default_k=3, max_k=5, min_score=0.0, dedup=dedup)


def test_redundant_neighbours_do_not_displace_other_relevant_sources(db_session: Session) -> None:
    document, chunks = index_document(db_session, [FACTURA, NEAR, *UNRELATED])
    other, _ = index_document(
        db_session, ["resumen: la factura de la luz vence pronto"], owner_id=document.owner_id
    )

    plain = search_chunks(db_session, ask(), owner_id=document.owner_id, limits=limits(None), k=2)
    reduced = search_chunks(
        db_session, ask(), owner_id=document.owner_id, limits=limits(POLICY), k=2
    )

    assert {h.chunk.id for h in plain} == {chunks[0].id, chunks[1].id}  # el vecino repetido cuela
    assert reduced[0].chunk.id == chunks[0].id
    assert reduced[1].document.id == other.id  # la otra fuente ocupa su plaza


def test_the_report_tells_what_was_dropped(db_session: Session) -> None:
    document, chunks = index_document(db_session, [FACTURA, NEAR, *UNRELATED])

    outcome = search_with_report(
        db_session, ask(), owner_id=document.owner_id, limits=limits(POLICY), k=3
    )

    assert outcome.reduction is not None
    assert [d.hit.chunk.id for d in outcome.reduction.dropped] == [chunks[1].id]
    assert chunks[1].id not in {h.chunk.id for h in outcome.hits}
    assert len(outcome.hits) == 3
    assert outcome.reduction.redundancy_rate > 0


def test_without_a_policy_nothing_is_reduced_and_there_is_no_report(db_session: Session) -> None:
    document, _ = index_document(db_session, [FACTURA, NEAR])

    outcome = search_with_report(
        db_session, ask(), owner_id=document.owner_id, limits=limits(None), k=2
    )

    assert outcome.reduction is None
    assert len(outcome.hits) == 2


def test_the_final_list_never_exceeds_k(db_session: Session) -> None:
    document, _ = index_document(db_session, [f"{FACTURA} {n}{n}" for n in range(8)] + UNRELATED)

    hits = search_chunks(
        db_session, ask(), owner_id=document.owner_id, limits=limits(DedupPolicy(window=8)), k=2
    )

    assert len(hits) <= 2


def test_reduction_runs_after_the_owner_filter(db_session: Session) -> None:
    mine, _ = index_document(db_session, [FACTURA])
    index_document(db_session, [FACTURA, NEAR])

    hits = search_chunks(db_session, ask(), owner_id=mine.owner_id, limits=limits(POLICY), k=5)

    assert {h.document.id for h in hits} == {mine.id}


# ── Configuración ───────────────────────────────────────────────────────────


def test_the_policy_comes_from_the_settings() -> None:
    settings = Settings(
        database_url=DB_URL, search_dedup_overlap=0.8, search_dedup_window=2, search_overfetch=4
    )

    assert SearchLimits.from_settings(settings).dedup == DedupPolicy(0.8, 2, 4)


@pytest.mark.parametrize(
    "fields",
    [
        {"search_dedup_overlap": 0},
        {"search_dedup_overlap": 1.1},
        {"search_dedup_window": -1},
        {"search_overfetch": 0},
    ],
)
def test_invalid_dedup_settings_are_rejected(fields: dict[str, float]) -> None:
    with pytest.raises(ValueError, match=next(iter(fields))):
        Settings(database_url=DB_URL, **fields)  # type: ignore[arg-type]
