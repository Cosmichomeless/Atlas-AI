import io

from pypdf import PdfWriter


def make_pdf(pages: list[str | None]) -> bytes:
    """PDF mínimo válido: una página por elemento; `None` deja la página sin texto."""
    objects: list[bytes] = []
    page_ids = [4 + 2 * i for i in range(len(pages))]
    kids = " ".join(f"{i} 0 R" for i in page_ids)
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, text in enumerate(pages):
        content_id = page_ids[i] + 1
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
            f"/Resources << /Font << /F1 3 0 R >> >> >>".encode()
        )
        escaped = (text or "").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode() if text else b""
        objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream))

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def make_encrypted_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(612, 792)
    writer.encrypt("clave-de-prueba")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
