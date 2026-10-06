import uuid
from typing import Any

from app.features.documents.models import Document


def make_document(owner_id: uuid.UUID | None, **overrides: Any) -> Document:
    """Documento válido con metadatos de ejemplo; `overrides` pisa cualquier campo."""
    fields: dict[str, Any] = {
        "owner_id": owner_id,
        "filename": "informe.pdf",
        "content_type": "application/pdf",
        "size_bytes": 1024,
    }
    return Document(**{**fields, **overrides})
