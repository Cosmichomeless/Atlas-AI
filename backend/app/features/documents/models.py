import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Enum, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.core.base import Base
from app.core.types import created_at_column, uuid_pk
from app.features.documents.states import DocumentStatus, ensure_transition
from app.features.documents.storage import MAX_KEY_LENGTH, document_key

ERROR_SUMMARY_MAX_LENGTH = 500


class Document(Base):
    """Documento subido por un usuario y su progreso de ingestión.

    El estado solo cambia por transiciones válidas (`transition_to`); asignar `status` directamente
    también se valida, así ningún código puede saltarse el ciclo de vida.
    """

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="size_non_negative"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    # NOT NULL: ningún documento queda sin propietario; al borrar al usuario se borran los suyos.
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(127), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    # Referencia al archivo original en el almacenamiento privado; la base nunca guarda binarios.
    storage_key: Mapped[str] = mapped_column(String(MAX_KEY_LENGTH), unique=True, nullable=False)

    status: Mapped[DocumentStatus] = mapped_column(
        Enum(
            DocumentStatus,
            name="document_status",
            native_enum=False,
            create_constraint=True,
            length=16,
        ),
        index=True,
        nullable=False,
    )
    error_summary: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(default=0, server_default="0", nullable=False)

    created_at: Mapped[datetime] = created_at_column()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Arrendamiento del worker: si vence con el documento en PROCESSING, otro puede recuperarlo.
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("id", uuid.uuid4())
        if kwargs.get("owner_id") is not None:
            kwargs.setdefault("storage_key", document_key(kwargs["owner_id"], kwargs["id"]))
        kwargs.setdefault("status", DocumentStatus.UPLOADED)
        kwargs.setdefault("attempts", 0)
        super().__init__(**kwargs)

    @validates("status")
    def _validate_status(self, _key: str, target: DocumentStatus) -> DocumentStatus:
        ensure_transition(self.status, target)
        return target

    def transition_to(
        self,
        target: DocumentStatus,
        *,
        error_summary: str | None = None,
        lease_expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> None:
        """Cambia de estado y registra fechas, intentos y error; lanza si la transición no vale."""
        ensure_transition(self.status, target)
        if target is DocumentStatus.FAILED and not (error_summary and error_summary.strip()):
            raise ValueError("Pasar a FAILED exige un error resumido.")
        now = now or datetime.now(UTC)

        self.status = target
        match target:
            case DocumentStatus.PROCESSING:
                self.attempts += 1
                self.processing_started_at = now
                self.processed_at = None
                self.error_summary = None
                self.lease_expires_at = lease_expires_at
            case DocumentStatus.READY:
                self.processed_at = now
                self.error_summary = None
                self.lease_expires_at = None
            case DocumentStatus.FAILED:
                self.processed_at = now
                self.error_summary = (error_summary or "").strip()[:ERROR_SUMMARY_MAX_LENGTH]
                self.lease_expires_at = None
            case DocumentStatus.UPLOADED:
                # Vuelve a la cola; el último error se conserva hasta el próximo intento
                self.processing_started_at = None
                self.processed_at = None
                self.lease_expires_at = None
