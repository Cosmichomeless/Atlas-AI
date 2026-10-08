"""Contabilidad de uso y cuotas diarias por usuario.

Solo se guardan contadores (preguntas, llamadas y tokens por día UTC): jamás el texto de una
pregunta, de un fragmento ni de una respuesta. La cuota se comprueba *antes* de llamar a ningún
proveedor, de modo que un exceso no cuesta nada; el consumo se anota *después*, con los tokens que
informa el proveedor (o una estimación prudente si no los informa).

La comprobación y la anotación no son una sola operación atómica: con peticiones simultáneas un
usuario puede rebasar la cuota en, como mucho, el número de peticiones en curso. Los contadores
sí se incrementan de forma atómica, así que no se pierde ninguna anotación.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.features.usage.models import UsageDay


class UsageLimitExceeded(Exception):
    """El usuario agotó su cuota de hoy; el mensaje está pensado para mostrarse tal cual."""


@dataclass(frozen=True, slots=True)
class UsageLimits:
    daily_questions: int
    daily_tokens: int


@dataclass(slots=True)
class Tally:
    """Consumo de una petición, que se anota de una vez al terminar (también si falla)."""

    questions: int = 0
    embedding_calls: int = 0
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def empty(self) -> bool:
        return not (
            self.questions
            or self.embedding_calls
            or self.llm_calls
            or self.input_tokens
            or self.output_tokens
        )


def utc_day(now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(UTC).date()


def get_usage(session: Session, owner_id: uuid.UUID, *, now: datetime | None = None) -> UsageDay:
    """Uso de hoy; si el usuario aún no ha hecho nada, un registro a cero que no se guarda."""
    row = session.get(UsageDay, (owner_id, utc_day(now)))
    return row or UsageDay(
        owner_id=owner_id,
        day=utc_day(now),
        questions=0,
        embedding_calls=0,
        llm_calls=0,
        input_tokens=0,
        output_tokens=0,
    )


def ensure_within_limits(
    session: Session, owner_id: uuid.UUID, limits: UsageLimits, *, now: datetime | None = None
) -> None:
    """Lanza `UsageLimitExceeded` si el usuario agotó hoy su cuota de preguntas o de tokens."""
    usage = get_usage(session, owner_id, now=now)
    if usage.questions >= limits.daily_questions:
        raise UsageLimitExceeded(
            f"Has alcanzado el máximo de {limits.daily_questions} preguntas al día. "
            "Se restablece a medianoche (UTC)."
        )
    if usage.input_tokens + usage.output_tokens >= limits.daily_tokens:
        raise UsageLimitExceeded(
            "Has alcanzado el máximo de uso diario del modelo. Se restablece a medianoche (UTC)."
        )


def record_usage(
    session: Session, owner_id: uuid.UUID, tally: Tally, *, now: datetime | None = None
) -> None:
    """Suma `tally` al día de hoy con un único `INSERT … ON CONFLICT` atómico y confirma."""
    if tally.empty:
        return
    values = {
        "questions": tally.questions,
        "embedding_calls": tally.embedding_calls,
        "llm_calls": tally.llm_calls,
        "input_tokens": tally.input_tokens,
        "output_tokens": tally.output_tokens,
    }
    statement = insert(UsageDay).values(owner_id=owner_id, day=utc_day(now), **values)
    session.execute(
        statement.on_conflict_do_update(
            index_elements=[UsageDay.owner_id, UsageDay.day],
            set_={name: getattr(UsageDay, name) + value for name, value in values.items()},
        )
    )
    session.commit()
