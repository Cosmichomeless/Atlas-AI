"""Proveedor falso: determinista, sin red y sin coste, para desarrollo y tests."""

import re
from collections.abc import Callable, Sequence

from app.answers.prompt import INSUFFICIENT_MARKER, SOURCES_CLOSE, SOURCES_OPEN
from app.llm.provider import LLMParams, LLMProvider, LLMSpec, Message, RawCompletion, Usage

FAKE_VERSION = "1"
Responder = Callable[[Sequence[Message]], str]


def _tokens(text: str) -> int:
    return len(text.split())


_WORD = re.compile(r"\w{4,}")
_SOURCE = re.compile(r"^\[(S\d+)\][^\n]*\n", re.MULTILINE)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def grounded_responder(messages: Sequence[Message]) -> str:
    """Responde con la frase de las fuentes que más palabras comparte con la pregunta y la cita.

    Si ninguna comparte palabras declara `SIN_EVIDENCIA`, como haría un modelo real. Es
    determinista y sin red: permite probar de extremo a extremo subida → pregunta → cita.
    """
    user = next(m.content for m in reversed(messages) if m.role == "user")
    sources, _, question = user.partition(SOURCES_CLOSE)
    sources = sources.removeprefix(SOURCES_OPEN)
    wanted = set(_WORD.findall(question.lower()))
    best: tuple[int, str, str] | None = None
    labels = list(_SOURCE.finditer(sources))
    for position, found in enumerate(labels):
        end = labels[position + 1].start() if position + 1 < len(labels) else len(sources)
        for sentence in _SENTENCE.split(sources[found.end() : end].strip()):
            overlap = len(wanted & set(_WORD.findall(sentence.lower())))
            if overlap and (best is None or overlap > best[0]):
                best = (overlap, found.group(1), sentence.strip())
    if best is None:
        return INSUFFICIENT_MARKER
    return f"{best[2].rstrip('.')} [{best[1]}]."


class FakeLLMProvider(LLMProvider):
    """Responde con `responder(messages)`; por defecto repite la última pregunta del usuario.

    Guarda cada conversación recibida en `calls`, para que los tests comprueben qué se le envió.
    """

    def __init__(
        self, model: str, params: LLMParams | None = None, responder: Responder | None = None
    ) -> None:
        self.spec = LLMSpec("fake", model, FAKE_VERSION)
        self.params = params or LLMParams()
        self._responder = responder or self._echo
        self.calls: list[list[Message]] = []

    @staticmethod
    def _echo(messages: Sequence[Message]) -> str:
        question = next(m.content for m in reversed(messages) if m.role == "user")
        return f"Respuesta simulada a: {question.strip()}"

    def _complete(self, messages: Sequence[Message]) -> RawCompletion:
        self.calls.append(list(messages))
        text = self._responder(messages)
        used = sum(_tokens(message.content) for message in messages)
        return RawCompletion(text, Usage(used, _tokens(text)), "stop")
