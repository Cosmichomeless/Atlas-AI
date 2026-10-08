"""Contrato común de los proveedores de generación (LLM).

El dominio solo conoce `LLMProvider`, `Message` y `Completion`: ningún SDK ni formato de un
proveedor concreto sale de su adaptador. Cada respuesta lleva la `LLMSpec` (proveedor, modelo y
versión del adaptador) y los `LLMParams` con que se generó, para poder reproducir una evaluación.
"""

import math
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

Role = Literal["system", "user", "assistant"]
ROLES: tuple[Role, ...] = ("system", "user", "assistant")


class LLMError(Exception):
    """El proveedor no pudo generar la respuesta; el mensaje es seguro de registrar.

    `transient` indica que reintentar más tarde puede funcionar (red, límite de tasa, caída del
    servicio) y es lo que se asume por defecto; `False` se reserva a lo que esperar no arregla
    (clave inválida, petición rechazada, respuesta incomprensible).
    """

    def __init__(self, message: str, *, transient: bool = True) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True, slots=True)
class LLMSpec:
    """Qué modelo produjo una respuesta.

    `version` identifica el comportamiento del adaptador (formato de la petición, parámetros que
    envía…): se incrementa cuando el mismo modelo dejaría de ser comparable con respuestas antiguas.
    """

    provider: str
    model: str
    version: str

    def __post_init__(self) -> None:
        if not (self.provider and self.model and self.version):
            raise ValueError("proveedor, modelo y versión son obligatorios")

    @property
    def key(self) -> str:
        """Identificador estable, p. ej. `openai/gpt-4o-mini/v1`."""
        return f"{self.provider}/{self.model}/v{self.version}"


@dataclass(frozen=True, slots=True)
class LLMParams:
    """Parámetros de generación, configurables por entorno."""

    temperature: float = 0.0
    max_output_tokens: int = 512

    def __post_init__(self) -> None:
        if not (math.isfinite(self.temperature) and 0 <= self.temperature <= 2):
            raise ValueError("temperature debe estar entre 0 y 2")
        if self.max_output_tokens < 1:
            raise ValueError("max_output_tokens debe ser positivo")


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class Usage:
    """Tokens consumidos, tal como los informa el proveedor (None si no los informa)."""

    input_tokens: int
    output_tokens: int


@dataclass(frozen=True, slots=True)
class RawCompletion:
    """Lo que devuelve un adaptador antes de validarlo."""

    text: str
    usage: Usage | None = None
    finish_reason: str | None = None


@dataclass(frozen=True, slots=True)
class Completion:
    """Respuesta validada, con el modelo y los parámetros que la produjeron."""

    text: str
    spec: LLMSpec
    params: LLMParams
    usage: Usage | None = None
    finish_reason: str | None = None

    @property
    def truncated(self) -> bool:
        """El modelo se quedó sin tokens de salida: el texto puede estar cortado."""
        return self.finish_reason == "length"


class LLMProvider(ABC):
    """Base de los adaptadores: `complete` valida la entrada y la salida de cada implementación."""

    spec: LLMSpec
    params: LLMParams

    @abstractmethod
    def _complete(self, messages: Sequence[Message]) -> RawCompletion:
        """Genera la respuesta a una conversación ya validada."""

    def complete(self, messages: Sequence[Message]) -> Completion:
        """Genera la siguiente respuesta del asistente.

        Lanza `LLMError` si la conversación no es válida (vacía, con roles desconocidos, mensajes
        en blanco o sin ningún mensaje de usuario) o si el proveedor devuelve una respuesta vacía.
        """
        self._check(messages)
        raw = self._complete(messages)
        if not isinstance(raw.text, str) or not raw.text.strip():
            raise LLMError("El proveedor devolvió una respuesta vacía")
        return Completion(raw.text, self.spec, self.params, raw.usage, raw.finish_reason)

    @staticmethod
    def _check(messages: Sequence[Message]) -> None:
        if not messages:
            raise LLMError("La conversación no tiene mensajes")
        if any(message.role not in ROLES for message in messages):
            raise LLMError("La conversación tiene un rol desconocido")
        if any(not message.content.strip() for message in messages):
            raise LLMError("La conversación tiene un mensaje en blanco")
        if not any(message.role == "user" for message in messages):
            raise LLMError("La conversación no tiene ningún mensaje de usuario")
