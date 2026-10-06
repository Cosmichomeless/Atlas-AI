import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base
from app.core.types import created_at_column, uuid_pk


class User(Base):
    """Cuenta de usuario. El email se guarda normalizado (minúsculas) y es único."""

    __tablename__ = "users"
    __table_args__ = (
        # Garantiza en la base de datos que la unicidad no distingue mayúsculas de minúsculas.
        CheckConstraint("email = lower(email)", name="email_lowercase"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(String(254), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = created_at_column()

    def __repr__(self) -> str:  # nunca incluir password_hash
        return f"User(id={self.id!s}, email={self.email!r})"
