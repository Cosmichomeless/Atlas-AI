"""Proveedor falso: determinista, sin red y sin coste, para desarrollo y tests."""

from collections.abc import Callable, Sequence

from app.llm.provider import LLMParams, LLMProvider, LLMSpec, Message, RawCompletion, Usage

FAKE_VERSION = "1"
Responder = Callable[[Sequence[Message]], str]


def _tokens(text: str) -> int:
    return len(text.split())


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
