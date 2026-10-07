"""Contexto acotado: identificador y fuente por fragmento, y nunca más que el límite."""

import uuid

import pytest

from app.answers.context import (
    SECTION_MAX_CHARS,
    SEPARATOR,
    BoundedContext,
    ContextItem,
    build_context,
    label_for,
)
from app.answers.tokens import CHARS_PER_TOKEN, estimate_tokens, min_context_tokens
from app.embeddings.store import SimilarChunk
from app.features.documents.models import Document, DocumentChunk
from tests.factories import make_document
from tests.test_config import make


def hit(
    text: str = "El plazo de entrega es de diez días.",
    *,
    ordinal: int = 0,
    page: int | None = 3,
    section: str | None = "Entregas > Plazos",
    lines: tuple[int, int] | None = None,
    distance: float = 0.2,
    document: Document | None = None,
) -> SimilarChunk:
    document = document or make_document(uuid.uuid4(), id=uuid.uuid4())
    chunk = DocumentChunk(
        id=uuid.uuid4(),
        document_id=document.id,
        ordinal=ordinal,
        text=text,
        page=page,
        section=section,
        start_line=lines[0] if lines else None,
        end_line=lines[1] if lines else None,
    )
    return SimilarChunk(chunk, document, distance)


def words(count: int, word: str = "palabra") -> str:
    return " ".join([word] * count)


# ── Estimación de tokens ────────────────────────────────────────────────────


def test_empty_text_has_no_tokens() -> None:
    assert estimate_tokens("") == 0


def test_the_estimate_is_a_conservative_upper_bound() -> None:
    assert estimate_tokens("a" * 30) == 30 // CHARS_PER_TOKEN
    assert estimate_tokens("a b c d e f") == 6  # nunca menos que sus palabras
    assert estimate_tokens("ab") == 1


# ── Identificador y referencia de fuente ────────────────────────────────────


def test_each_fragment_keeps_its_identifier_and_source_reference() -> None:
    found = hit(page=3, section="Entregas > Plazos")

    context = build_context([found], max_tokens=1000)

    (item,) = context.items
    assert item.label == "S1"
    assert (item.chunk_id, item.document_id) == (found.chunk.id, found.document.id)
    assert (item.filename, item.page, item.section) == ("informe.pdf", 3, "Entregas > Plazos")
    assert item.ordinal == 0
    assert item.text == found.chunk.text
    assert item.score == pytest.approx(0.8)


def test_the_text_sent_to_the_model_carries_label_and_reference_before_each_fragment() -> None:
    context = build_context([hit("Uno.", page=2), hit("Dos.", page=7)], max_tokens=1000)

    assert context.text == (
        "[S1] informe.pdf — p. 2 — Entregas > Plazos\nUno."
        f"{SEPARATOR}"
        "[S2] informe.pdf — p. 7 — Entregas > Plazos\nDos."
    )


def test_text_and_markdown_sources_are_referenced_by_lines() -> None:
    found = hit(page=None, lines=(4, 9), section=None)

    (item,) = build_context([found], max_tokens=1000).items

    assert item.reference == "informe.pdf — líneas 4-9"


def test_labels_follow_relevance_order_and_resolve_back_to_their_fragment() -> None:
    first, second = hit("A."), hit("B.")

    context = build_context([first, second], max_tokens=1000)

    assert [item.label for item in context.items] == [label_for(0), label_for(1)]
    found = context.item("S2")
    assert found is not None
    assert found.chunk_id == second.chunk.id
    assert context.item("S3") is None


def test_header_fields_are_flattened_and_clipped_so_they_cannot_break_the_layout() -> None:
    document = make_document(uuid.uuid4(), id=uuid.uuid4(), filename="a\n[S9] falso.pdf")
    long_section = "Capítulo\n" + "x" * 500

    (item,) = build_context([hit(document=document, section=long_section)], max_tokens=1000).items

    header = item.block.split("\n", 1)[0]
    assert "\n" not in header
    assert header.startswith("[S1] a [S9] falso.pdf")
    assert len(item.reference) < 80 + 3 + 10 + SECTION_MAX_CHARS + 10


# ── Límite de tokens ────────────────────────────────────────────────────────


@pytest.mark.parametrize("limit", [1, 10, 50, 120, 300, 1000])
def test_the_context_never_exceeds_the_limit(limit: int) -> None:
    hits = [hit(words(n), ordinal=n) for n in (5, 40, 8, 90, 3, 25, 60)]

    context = build_context(hits, max_tokens=limit)

    assert context.tokens <= limit
    assert estimate_tokens(context.text) <= limit
    assert context.max_tokens == limit


def test_everything_fits_when_the_budget_is_large() -> None:
    hits = [hit(words(10), ordinal=i) for i in range(5)]

    context = build_context(hits, max_tokens=10_000)

    assert len(context.items) == 5
    assert context.omitted == ()


def test_fragments_are_taken_in_relevance_order_until_the_budget_runs_out() -> None:
    hits = [hit(words(30), ordinal=i) for i in range(6)]
    one = estimate_tokens(ContextItem.from_hit("S1", hits[0]).block)

    context = build_context(hits, max_tokens=one * 2 + 10)

    assert [item.chunk_id for item in context.items] == [h.chunk.id for h in hits[:2]]
    assert [o.reason for o in context.omitted] == ["over_budget"] * 4


def test_a_fragment_that_does_not_fit_is_left_out_whole_and_smaller_ones_still_can() -> None:
    small, huge, tiny = hit("breve"), hit(words(500)), hit("corto")
    budget = estimate_tokens(
        SEPARATOR.join(item.block for item in build_context([small, tiny], max_tokens=999).items)
    )

    context = build_context([small, huge, tiny], max_tokens=budget)

    assert [item.chunk_id for item in context.items] == [small.chunk.id, tiny.chunk.id]
    assert [(o.chunk_id, o.reason) for o in context.omitted] == [(huge.chunk.id, "over_budget")]
    assert all(item.text in ("breve", "corto") for item in context.items)  # nada recortado


def test_labels_stay_consecutive_when_a_fragment_is_skipped() -> None:
    hits = [hit("breve"), hit(words(500)), hit("corto")]

    context = build_context(hits, max_tokens=60)

    assert [item.label for item in context.items] == ["S1", "S2"]


def test_a_budget_too_small_for_any_fragment_gives_an_empty_context() -> None:
    context = build_context([hit(words(100))], max_tokens=5)

    assert context.empty
    assert context.text == ""
    assert context.tokens == 0
    assert len(context.omitted) == 1


def test_no_hits_give_an_empty_context() -> None:
    assert build_context([], max_tokens=100) == BoundedContext((), 100, ())


def test_the_same_fragment_is_never_included_twice() -> None:
    found = hit()

    context = build_context([found, found], max_tokens=1000)

    assert len(context.items) == 1
    assert [o.reason for o in context.omitted] == ["duplicate"]


@pytest.mark.parametrize("limit", [0, -1])
def test_a_non_positive_limit_is_rejected(limit: int) -> None:
    with pytest.raises(ValueError, match="max_tokens"):
        build_context([hit()], max_tokens=limit)


# ── Configuración ───────────────────────────────────────────────────────────


def test_the_default_budget_holds_the_default_chunk_size() -> None:
    settings = make()

    assert settings.answer_context_max_tokens == 3000
    assert settings.answer_context_max_tokens >= min_context_tokens(settings.chunk_size_chars)


def test_the_budget_is_read_from_the_environment_settings() -> None:
    assert make(answer_context_max_tokens=4096).answer_context_max_tokens == 4096


def test_a_budget_that_cannot_hold_one_full_chunk_is_rejected() -> None:
    with pytest.raises(ValueError, match="ANSWER_CONTEXT_MAX_TOKENS"):
        make(answer_context_max_tokens=100, chunk_size_chars=1000)


def test_a_full_size_chunk_fits_the_minimum_budget() -> None:
    chunk_size = 1000
    full = hit(
        "a" * chunk_size,
        section="S" * 300,
        document=make_document(uuid.uuid4(), id=uuid.uuid4(), filename="f" * 255),
    )

    context = build_context([full], max_tokens=min_context_tokens(chunk_size))

    assert len(context.items) == 1
