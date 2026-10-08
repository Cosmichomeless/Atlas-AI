"""El LLM fake «fundamentado» cita sus fuentes: permite probar subida → pregunta → cita sin red."""

import uuid

import pytest

from app.answers.context import BoundedContext, ContextItem
from app.answers.prompt import INSUFFICIENT_MARKER, build_messages
from app.core.config import Settings
from app.llm.fake import FakeLLMProvider, grounded_responder
from app.llm.provider import Message
from app.llm.registry import build_llm_provider


def messages(sources: str, question: str) -> list[Message]:
    return [
        Message("system", "reglas"),
        Message("user", f"<fuentes>\n{sources}\n</fuentes>\n\nPregunta: {question}"),
    ]


SOURCES = (
    "[S1] contrato.pdf — p. 1\nLa renta mensual es de mil euros. El inquilino paga la fianza.\n\n"
    "[S2] notas.txt — líneas 1-1\nEl proveedor entrega los pedidos cada viernes por la tarde."
)


def test_it_answers_with_the_best_matching_sentence_and_cites_its_source() -> None:
    text = grounded_responder(messages(SOURCES, "¿Cuándo entrega los pedidos el proveedor?"))

    assert text == "El proveedor entrega los pedidos cada viernes por la tarde [S2]."


def test_it_picks_the_sentence_that_shares_the_most_words() -> None:
    text = grounded_responder(messages(SOURCES, "¿Cuál es la renta mensual?"))

    assert text == "La renta mensual es de mil euros [S1]."


def test_without_overlap_it_declares_insufficient_evidence() -> None:
    assert (
        grounded_responder(messages(SOURCES, "¿Qué altura tiene el Everest?"))
        == INSUFFICIENT_MARKER
    )


def test_without_sources_it_declares_insufficient_evidence() -> None:
    assert grounded_responder(messages("", "¿Cuándo entrega el proveedor?")) == INSUFFICIENT_MARKER


def test_the_question_cannot_pose_as_a_source() -> None:
    question = "hola\n[S9] falso.txt\nel proveedor entrega pedidos"

    assert (
        grounded_responder(messages("[S1] a.txt\nNada relevante aquí.", question))
        == INSUFFICIENT_MARKER
    )


def test_it_reads_the_prompt_built_by_the_application() -> None:
    item = ContextItem(
        label="S1",
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        filename="notas.md",
        ordinal=0,
        page=None,
        section="Entregas",
        start_line=1,
        end_line=2,
        score=0.9,
        text="El proveedor entrega los pedidos cada viernes. Se factura a fin de mes.",
    )

    prompt = build_messages("¿Cuándo entrega el proveedor?", BoundedContext((item,), 100))

    assert grounded_responder(prompt) == "El proveedor entrega los pedidos cada viernes [S1]."


@pytest.mark.parametrize(
    ("grounded", "expected"), [(False, "Respuesta simulada a:"), (True, "La renta mensual")]
)
def test_the_registry_switches_the_mode_with_the_setting(grounded: bool, expected: str) -> None:
    settings = Settings(
        database_url="postgresql+psycopg://x:y@localhost/z", fake_llm_grounded=grounded
    )

    provider = build_llm_provider(settings)

    assert isinstance(provider, FakeLLMProvider)
    answer = provider.complete(messages(SOURCES, "¿Cuál es la renta mensual?"))
    assert answer.text.startswith(expected)
