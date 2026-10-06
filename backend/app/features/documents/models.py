import uuid
from datetime import datetime

from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base
from app.core.types import created_at_column, uuid_pk


class Document(Base):
    """Documento subido por un usuario. Metadatos y estados de ingestión se añaden en #12."""

    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = uuid_pk()
    # NOT NULL: ningún documento queda sin propietario; al borrar al usuario se borran los suyos.
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    created_at: Mapped[datetime] = created_at_column()
