"""Estados de ingestión de un documento y sus transiciones permitidas.

UPLOADED -> PROCESSING -> READY
PROCESSING -> FAILED
READY -> UPLOADED        (reindexar)
FAILED -> UPLOADED       (reintentar)
PROCESSING -> UPLOADED   (recuperar tras una caída del worker)
"""

from enum import StrEnum


class DocumentStatus(StrEnum):
    UPLOADED = "UPLOADED"
    PROCESSING = "PROCESSING"
    READY = "READY"
    FAILED = "FAILED"


ALLOWED_TRANSITIONS: dict[DocumentStatus, frozenset[DocumentStatus]] = {
    DocumentStatus.UPLOADED: frozenset({DocumentStatus.PROCESSING}),
    DocumentStatus.PROCESSING: frozenset(
        {DocumentStatus.READY, DocumentStatus.FAILED, DocumentStatus.UPLOADED}
    ),
    DocumentStatus.READY: frozenset({DocumentStatus.UPLOADED}),
    DocumentStatus.FAILED: frozenset({DocumentStatus.UPLOADED}),
}


class InvalidStatusTransitionError(ValueError):
    def __init__(self, current: DocumentStatus | None, target: DocumentStatus) -> None:
        self.current = current
        self.target = target
        origin = current.value if current else "(nuevo)"
        super().__init__(f"Transición de estado no permitida: {origin} -> {target.value}")


def ensure_transition(current: DocumentStatus | None, target: DocumentStatus) -> None:
    """Un documento nuevo solo puede nacer UPLOADED; el resto sigue `ALLOWED_TRANSITIONS`."""
    allowed = (
        frozenset({DocumentStatus.UPLOADED}) if current is None else ALLOWED_TRANSITIONS[current]
    )
    if target not in allowed:
        raise InvalidStatusTransitionError(current, target)
