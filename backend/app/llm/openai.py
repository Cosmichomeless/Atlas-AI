"""Adaptador de la API de chat compatible con OpenAI."""

from collections.abc import Sequence

import httpx2

from app.llm.provider import (
    LLMError,
    LLMParams,
    LLMProvider,
    LLMSpec,
    Message,
    RawCompletion,
    Usage,
)

OPENAI_VERSION = "1"


class OpenAILLMProvider(LLMProvider):
    """`POST {base_url}/chat/completions`. Los errores no incluyen clave ni contenido enviado."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        params: LLMParams,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 60.0,
        client: httpx2.Client | None = None,
    ) -> None:
        self.spec = LLMSpec("openai", model, OPENAI_VERSION)
        self.params = params
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._client = client or httpx2.Client(timeout=timeout_seconds)

    def _complete(self, messages: Sequence[Message]) -> RawCompletion:
        payload: dict[str, object] = {
            "model": self.spec.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": self.params.temperature,
            "max_tokens": self.params.max_output_tokens,
        }
        try:
            response = self._client.post(self._url, json=payload, headers=self._headers)
            response.raise_for_status()
            body = response.json()
            choice = body["choices"][0]
            text = choice["message"]["content"]
            if not isinstance(text, str):
                raise TypeError("contenido no textual")
            finish = choice.get("finish_reason")
            usage = body.get("usage")
            return RawCompletion(
                text,
                Usage(int(usage["prompt_tokens"]), int(usage["completion_tokens"]))
                if usage
                else None,
                finish if isinstance(finish, str) else None,
            )
        except httpx2.HTTPStatusError as error:
            raise LLMError(
                f"El servicio de generación respondió {error.response.status_code}"
            ) from None
        except httpx2.HTTPError as error:
            raise LLMError(
                f"No se pudo contactar con el servicio de generación ({type(error).__name__})"
            ) from None
        except (ValueError, KeyError, IndexError, TypeError):
            raise LLMError("Respuesta de generación con formato inesperado") from None
