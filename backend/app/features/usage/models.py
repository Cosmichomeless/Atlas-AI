import uuid
from datetime import date

from sqlalchemy import BigInteger, CheckConstraint, Date, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.core.base import Base


class UsageDay(Base):
    """Uso de un usuario en un día UTC: solo contadores, nunca preguntas, textos ni respuestas.

    Es la base de las cuotas diarias y de la contabilidad de coste. Se borra con el usuario.
    """

    __tablename__ = "usage_days"
    __table_args__ = (
        CheckConstraint(
            "questions >= 0 AND embedding_calls >= 0 AND llm_calls >= 0 "
            "AND input_tokens >= 0 AND output_tokens >= 0",
            name="non_negative",
        ),
    )

    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    questions: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    embedding_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    llm_calls: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    input_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
