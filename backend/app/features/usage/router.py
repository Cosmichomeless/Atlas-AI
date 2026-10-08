from datetime import date

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.api.errors import error_responses
from app.core.config import get_settings
from app.features.auth.dependencies import CurrentUser, SessionDep
from app.features.usage.service import get_usage

router = APIRouter(prefix="/usage", tags=["usage"], responses=error_responses(401))


class UsageToday(BaseModel):
    """Uso de hoy (día UTC) y cuotas. Solo contadores: nunca el contenido de las preguntas."""

    day: date
    questions: int
    embedding_calls: int
    llm_calls: int
    input_tokens: int
    output_tokens: int
    daily_questions: int = Field(description="Preguntas máximas al día.")
    daily_tokens: int = Field(description="Tokens máximos (entrada + salida) al día.")


@router.get("", summary="Mi uso de hoy y mis cuotas")
def my_usage(user: CurrentUser, session: SessionDep) -> UsageToday:
    settings = get_settings()
    usage = get_usage(session, user.id)
    return UsageToday(
        day=usage.day,
        questions=usage.questions,
        embedding_calls=usage.embedding_calls,
        llm_calls=usage.llm_calls,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        daily_questions=settings.usage_daily_questions,
        daily_tokens=settings.usage_daily_tokens,
    )
