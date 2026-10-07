"""Construye el proveedor configurado (`EMBEDDING_PROVIDER`)."""

from functools import lru_cache

from app.core.config import Settings, get_settings
from app.embeddings.fake import FakeEmbeddingProvider
from app.embeddings.openai import OpenAIEmbeddingProvider
from app.embeddings.provider import EmbeddingProvider


def build_embedding_provider(settings: Settings) -> EmbeddingProvider:
    if settings.embedding_provider == "openai":
        if settings.openai_api_key is None:
            raise ValueError("OPENAI_API_KEY es obligatoria para el proveedor 'openai'")
        return OpenAIEmbeddingProvider(
            api_key=settings.openai_api_key.get_secret_value(),
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            base_url=settings.openai_base_url,
        )
    return FakeEmbeddingProvider(settings.embedding_model, settings.embedding_dimensions)


@lru_cache
def get_embedding_provider() -> EmbeddingProvider:
    """Proveedor configurado; dependencia sustituible en las pruebas."""
    return build_embedding_provider(get_settings())
