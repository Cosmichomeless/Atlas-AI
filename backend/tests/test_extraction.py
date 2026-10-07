import io
import uuid

import pytest
from sqlalchemy.orm import Session

from app.features.documents.extraction import (
    ExtractedBlock,
    ExtractionError,
    extract_blocks,
    extract_or_fail,
)
from app.features.documents.models import Document
from app.features.documents.states import DocumentStatus
from app.features.documents.storage import LocalFileStorage
from app.features.users.models import User
from tests.factories import make_document
from tests.pdfs import make_encrypted_pdf, make_pdf

DOC = uuid.uuid4()
PDF = "application/pdf"
TXT = "text/plain"
MD = "text/markdown"


def blocks(content: bytes, content_type: str) -> list[ExtractedBlock]:
    return extract_blocks(DOC, io.BytesIO(content), content_type)


def test_pdf_yields_one_block_per_page_with_page_numbers() -> None:
    result = blocks(make_pdf(["Primera pagina", "Segunda pagina", "Tercera"]), PDF)

    assert [(b.page, b.text) for b in result] == [
        (1, "Primera pagina"),
        (2, "Segunda pagina"),
        (3, "Tercera"),
    ]
    assert {b.document_id for b in result} == {DOC}
    assert all(b.section is None and b.start_line is None for b in result)


def test_pdf_keeps_real_page_numbers_when_some_pages_have_no_text() -> None:
    result = blocks(make_pdf([None, "Solo la segunda", None, "Y la cuarta"]), PDF)

    assert [(b.page, b.text) for b in result] == [(2, "Solo la segunda"), (4, "Y la cuarta")]


def test_pdf_without_extractable_text_raises_a_clear_error() -> None:
    with pytest.raises(ExtractionError, match="no contiene texto extraíble"):
        blocks(make_pdf([None, None]), PDF)


def test_encrypted_pdf_raises_a_clear_error() -> None:
    with pytest.raises(ExtractionError, match="contraseña"):
        blocks(make_encrypted_pdf(), PDF)


@pytest.mark.parametrize("content", [b"%PDF-1.4 esto no es un pdf", b"%PDF-", b"%PDF-1.7\n" * 50])
def test_corrupt_pdf_raises_a_clear_error(content: bytes) -> None:
    with pytest.raises(ExtractionError, match="dañado|no contiene texto"):
        blocks(content, PDF)


def test_plain_text_blocks_keep_line_ranges() -> None:
    content = b"Uno\ndos\n\nTres\n\n\n\nCuatro\ncinco\nseis\n"

    result = blocks(content, TXT)

    assert [(b.text, b.start_line, b.end_line) for b in result] == [
        ("Uno\ndos", 1, 2),
        ("Tres", 4, 4),
        ("Cuatro\ncinco\nseis", 8, 10),
    ]
    assert all(b.page is None and b.section is None and b.document_id == DOC for b in result)


def test_plain_text_handles_bom_crlf_and_accents() -> None:
    result = blocks("﻿Año nuevo\r\n\r\nÑandú".encode(), TXT)

    assert [b.text for b in result] == ["Año nuevo", "Ñandú"]


def test_markdown_blocks_carry_the_heading_path() -> None:
    content = (
        b"Intro sin titulo\n\n"
        b"# Guia\n\n"
        b"Texto de la guia.\n\n"
        b"## Instalacion\n"
        b"Pasos de instalacion.\n\n"
        b"### Linux\n\n"
        b"Detalles Linux.\n\n"
        b"## Uso\n\n"
        b"Como usarlo.\n\n"
        b"# Anexo\n\n"
        b"Notas.\n"
    )

    result = blocks(content, MD)

    assert [(b.section, b.text.splitlines()[-1], b.start_line) for b in result] == [
        (None, "Intro sin titulo", 1),
        ("Guia", "Texto de la guia.", 3),
        ("Guia > Instalacion", "Pasos de instalacion.", 7),
        ("Guia > Instalacion > Linux", "Detalles Linux.", 10),
        ("Guia > Uso", "Como usarlo.", 14),
        ("Anexo", "Notas.", 18),
    ]
    assert result[1].text.startswith("# Guia")
    assert (result[1].start_line, result[1].end_line) == (3, 5)


def test_markdown_ignores_headings_inside_code_fences() -> None:
    content = b"# Real\n\ntexto\n\n```\n# no es un titulo\n\nsigue el codigo\n```\n"

    result = blocks(content, MD)

    assert {b.section for b in result} == {"Real"}


@pytest.mark.parametrize("content_type", [TXT, MD])
@pytest.mark.parametrize("content", [b"   \n\n\t\n", b"\xef\xbb\xbf"])
def test_blank_text_files_raise(content: bytes, content_type: str) -> None:
    with pytest.raises(ExtractionError, match="no contiene texto"):
        blocks(content, content_type)


def test_non_utf8_text_raises() -> None:
    with pytest.raises(ExtractionError, match="UTF-8"):
        blocks("acción".encode("latin-1"), TXT)


def test_unknown_content_type_raises() -> None:
    with pytest.raises(ExtractionError, match="no compatible"):
        blocks(b"x", "image/png")


# -- extract_or_fail: el documento queda FAILED con la causa -------------------------------------


def processing_document(
    session: Session, storage: LocalFileStorage, content: bytes | None, content_type: str
) -> Document:
    user = User(email=f"{uuid.uuid4()}@example.com", password_hash="x")
    session.add(user)
    session.flush()
    document = make_document(user.id, content_type=content_type)
    session.add(document)
    session.flush()
    if content is not None:
        storage.save(document.storage_key, io.BytesIO(content))
    document.transition_to(DocumentStatus.PROCESSING)
    return document


def test_extract_or_fail_returns_blocks_and_keeps_the_document_processing(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = processing_document(db_session, storage, make_pdf(["Hola", "Mundo"]), PDF)

    result = extract_or_fail(document, storage)

    assert [(b.document_id, b.page) for b in result] == [(document.id, 1), (document.id, 2)]
    assert document.status is DocumentStatus.PROCESSING


def test_pdf_without_text_leaves_the_document_failed_with_a_readable_cause(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = processing_document(db_session, storage, make_pdf([None]), PDF)

    result = extract_or_fail(document, storage)
    db_session.flush()

    assert result == []
    assert document.status is DocumentStatus.FAILED
    assert document.error_summary is not None
    assert "no contiene texto extraíble" in document.error_summary
    assert document.lease_expires_at is None
    assert document.processed_at is not None


def test_missing_stored_file_fails_the_document(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = processing_document(db_session, storage, None, TXT)

    assert extract_or_fail(document, storage) == []

    assert document.status is DocumentStatus.FAILED
    assert document.error_summary == "No se encontró el archivo original."
