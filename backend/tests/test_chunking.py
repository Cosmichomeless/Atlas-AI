import itertools
import random
import uuid
from collections.abc import Iterable

import pytest

from app.core.config import Settings
from app.features.documents.extraction import ExtractedBlock
from app.ingestion.chunking import Chunk, ChunkPolicy, chunk_blocks
from tests.test_config import make

DOC = uuid.uuid4()
SMALL = ChunkPolicy(size=100, overlap=20)


def block(text: str, **where: int | str | None) -> ExtractedBlock:
    return ExtractedBlock(DOC, text, **where)  # type: ignore[arg-type]


def words(n: int, prefix: str = "palabra") -> str:
    return " ".join(f"{prefix}{i}" for i in range(n))


def texts(chunks: list[Chunk]) -> list[str]:
    return [c.text for c in chunks]


# ── Política ────────────────────────────────────────────────────────────────


def test_default_policy_comes_from_settings_and_is_valid() -> None:
    settings: Settings = make()

    policy = ChunkPolicy.from_settings(settings)

    assert (policy.size, policy.overlap) == (1000, 150)


@pytest.mark.parametrize(("size", "overlap"), [(0, 0), (100, -1), (100, 51), (10, 10)])
def test_invalid_policies_are_rejected(size: int, overlap: int) -> None:
    with pytest.raises(ValueError, match="debe"):
        ChunkPolicy(size, overlap)


def test_settings_reject_an_overlap_above_half_the_size() -> None:
    with pytest.raises(ValueError, match="CHUNK_OVERLAP_CHARS"):
        make(chunk_size_chars=200, chunk_overlap_chars=101)


def test_size_and_overlap_are_configurable() -> None:
    settings = make(chunk_size_chars=400, chunk_overlap_chars=40)

    assert (settings.chunk_size_chars, settings.chunk_overlap_chars) == (400, 40)


# ── Sin vacíos, en orden, sin pérdidas ──────────────────────────────────────


def test_no_input_gives_no_chunks() -> None:
    assert chunk_blocks([], SMALL) == []


def test_blank_blocks_never_become_chunks() -> None:
    result = chunk_blocks([block("  \n\t "), block("Hola"), block(""), block("\n\n")], SMALL)

    assert texts(result) == ["Hola"]


def test_short_text_is_a_single_chunk_with_provenance() -> None:
    result = chunk_blocks([block("Hola mundo", page=3)], SMALL)

    assert result == [Chunk(DOC, 0, "Hola mundo", page=3)]


def test_chunks_are_ordered_numbered_and_never_exceed_the_size() -> None:
    result = chunk_blocks([block(words(200))], SMALL)

    assert len(result) > 5
    assert [c.ordinal for c in result] == list(range(len(result)))
    assert all(0 < len(c.text) <= SMALL.size for c in result)
    assert all(c.text == c.text.strip() for c in result)


def test_chunks_appear_in_document_order_and_lose_no_words() -> None:
    original = words(300)

    result = chunk_blocks([block(original)], SMALL)

    cursor = 0
    for chunk in result:
        position = original.find(chunk.text, cursor)
        assert position >= cursor  # en orden y literal: ningún carácter inventado
        cursor = position + 1
    assert set(" ".join(texts(result)).split()) == set(original.split())


def test_consecutive_chunks_overlap_by_at_most_the_configured_amount() -> None:
    original = words(300)

    result = chunk_blocks([block(original)], SMALL)

    for previous, current in zip(result, result[1:], strict=False):
        shared = _shared_suffix_prefix(previous.text, current.text)
        assert 0 < len(shared) <= SMALL.overlap


def _shared_suffix_prefix(a: str, b: str) -> str:
    for length in range(min(len(a), len(b)), 0, -1):
        if a.endswith(b[:length]):
            return b[:length]
    return ""


def test_zero_overlap_repeats_nothing() -> None:
    original = words(200)

    result = chunk_blocks([block(original)], ChunkPolicy(size=100, overlap=0))

    assert " ".join(texts(result)).split() == original.split()


def test_chunking_is_deterministic() -> None:
    blocks = [block(words(150), page=1), block(words(90, "otra"), page=2)]

    assert chunk_blocks(blocks, SMALL) == chunk_blocks(blocks, SMALL)


# ── Dónde se corta ──────────────────────────────────────────────────────────


def test_cuts_prefer_paragraph_ends_then_sentences_then_words() -> None:
    paragraph_a = "Primera idea completa. " * 3 + "Fin del primero."
    paragraph_b = "Segunda idea completa. " * 3 + "Fin del segundo."
    text = f"{paragraph_a}\n\n{paragraph_b}"

    result = chunk_blocks([block(text)], ChunkPolicy(size=len(paragraph_a) + 30, overlap=0))

    assert texts(result) == [paragraph_a, paragraph_b]


def test_a_word_is_not_split_when_a_space_is_available() -> None:
    result = chunk_blocks([block(words(100))], SMALL)

    original_words = set(words(100).split())
    assert all(word in original_words for chunk in result for word in chunk.text.split())


def test_text_without_spaces_is_hard_cut_without_looping() -> None:
    result = chunk_blocks([block("x" * 1000)], SMALL)

    assert all(len(c.text) <= SMALL.size for c in result)
    assert len(result) > 10
    assert "".join(c.text for c in result).count("x") >= 1000


def test_unicode_text_is_kept_intact() -> None:
    original = "Ñandú señalizó el camión — ¿qué más? " * 40

    result = chunk_blocks([block(original)], SMALL)

    assert all(c.text.strip() == c.text for c in result)
    assert "".join(texts(result)).replace(" ", "").count("Ñandú") >= 40


# ── Procedencia ─────────────────────────────────────────────────────────────


def test_chunks_never_cross_a_page() -> None:
    blocks = [block("Final de la uno.", page=1), block("Inicio de la dos.", page=2)]

    result = chunk_blocks(blocks, ChunkPolicy(size=1000, overlap=0))

    assert [(c.page, c.text) for c in result] == [
        (1, "Final de la uno."),
        (2, "Inicio de la dos."),
    ]


def test_long_pages_split_within_the_page_keeping_its_number() -> None:
    result = chunk_blocks([block(words(120), page=7), block("Otra", page=8)], SMALL)

    assert [c.page for c in result[:-1]] == [7] * (len(result) - 1)
    assert result[-1].page == 8


def test_chunks_never_cross_a_markdown_section() -> None:
    blocks = [
        block("Texto A", section="Uno", start_line=1, end_line=1),
        block("Texto B", section="Uno > Dos", start_line=3, end_line=3),
    ]

    result = chunk_blocks(blocks, ChunkPolicy(size=1000, overlap=0))

    assert [(c.section, c.text) for c in result] == [("Uno", "Texto A"), ("Uno > Dos", "Texto B")]


def test_text_paragraphs_are_merged_and_keep_their_line_range() -> None:
    blocks = [
        block("Uno\ndos", start_line=1, end_line=2),
        block("Tres", start_line=4, end_line=4),
        block("Cuatro\ncinco", start_line=7, end_line=8),
    ]

    result = chunk_blocks(blocks, ChunkPolicy(size=1000, overlap=0))

    assert len(result) == 1
    assert result[0].text == "Uno\ndos\n\nTres\n\nCuatro\ncinco"
    assert (result[0].start_line, result[0].end_line) == (1, 8)


def test_line_ranges_of_split_chunks_stay_inside_their_source_lines() -> None:
    lines = [f"linea {i} " + "contenido " * 6 for i in range(1, 31)]
    paragraph = "\n".join(lines)

    result = chunk_blocks([block(paragraph, start_line=10, end_line=39)], SMALL)

    assert len(result) > 3
    assert result[0].start_line == 10
    assert result[-1].end_line == 39
    for chunk in result:
        assert chunk.start_line is not None and chunk.end_line is not None
        assert 10 <= chunk.start_line <= chunk.end_line <= 39
        # la primera línea del fragmento es realmente de esa línea del original
        assert chunk.text.splitlines()[0].strip() in lines[chunk.start_line - 10] + lines[0]


def test_pdf_chunks_have_no_line_numbers() -> None:
    result = chunk_blocks([block("Texto", page=1)], SMALL)

    assert (result[0].start_line, result[0].end_line) == (None, None)


# ── Propiedades con entradas aleatorias reproducibles ───────────────────────


@pytest.mark.parametrize("seed", range(25))
def test_invariants_hold_for_random_documents(seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311  (datos de prueba reproducibles)
    policy = ChunkPolicy(size=rng.randint(100, 400), overlap=rng.randint(0, 50))
    alphabet = ["a", "bb", "ccc", "Ñ", ".", ",", "\n", "\n\n", " ", "  ", "\t"]
    blocks = [
        block(
            "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 900))),
            page=rng.choice([1, 1, 2, 3]),
        )
        for _ in range(rng.randint(0, 6))
    ]

    result = chunk_blocks(blocks, policy)

    assert [c.ordinal for c in result] == list(range(len(result)))
    for chunk in result:
        assert chunk.text and chunk.text == chunk.text.strip()
        assert len(chunk.text) <= policy.size
    # El orden de las páginas de salida es el de entrada (sin repeticiones consecutivas)
    assert _collapse(c.page for c in result) == _collapse(b.page for b in blocks if b.text.strip())


def _collapse(values: Iterable[int | None]) -> list[int | None]:
    return [value for value, _ in itertools.groupby(values)]


def test_a_chunk_ending_with_its_block_reports_the_blocks_last_line() -> None:
    # El bloque omite la línea en blanco entre el título y el párrafo (líneas 1 a 3)
    result = chunk_blocks([block("# Guía\nIntro.", start_line=1, end_line=3)], SMALL)

    assert [(c.start_line, c.end_line) for c in result] == [(1, 3)]
