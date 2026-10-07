"""Adaptador de la API de embeddings compatible con OpenAI."""

from collections.abc import Sequence

import httpx2

from app.embeddings.provider import EmbeddingError, EmbeddingProvider, EmbeddingSpec

OPENAI_VERSION = "1"
TIMEOUT_SECONDS = 30.0


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """`POST {base_url}/embeddings`. Los errores no incluyen la clave ni el texto enviado."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        dimensions: int,
        base_url: str = "https://api.openai.com/v1",
        client: httpx2.Client | None = None,
    ) -> None:
        self.spec = EmbeddingSpec("openai", model, dimensions, OPENAI_VERSION)
        self._url = f"{base_url.rstrip('/')}/embeddings"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._client = client or httpx2.Client(timeout=TIMEOUT_SECONDS)

    def _embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        payload: dict[str, object] = {
            "model": self.spec.model,
            "input": list(texts),
            "encoding_format": "float",
        }
        if self.spec.model.startswith("text-embedding-3"):
            payload["dimensions"] = self.spec.dimensions  # solo estos modelos lo admiten
        try:
            response = self._client.post(self._url, json=payload, headers=self._headers)
            response.raise_for_status()
            data = response.json()["data"]
            ordered = sorted(data, key=lambda item: item["index"])
            return [[float(value) for value in item["embedding"]] for item in ordered]
        except httpx2.HTTPStatusError as error:
            raise EmbeddingError(
                f"El servicio de embeddings respondió {error.response.status_code}"
            ) from None
        except httpx2.HTTPError as error:
            raise EmbeddingError(
                f"No se pudo contactar con el servicio de embeddings ({type(error).__name__})"
            ) from None
        except (ValueError, KeyError, TypeError):
            raise EmbeddingError("Respuesta de embeddings con formato inesperado") from None
