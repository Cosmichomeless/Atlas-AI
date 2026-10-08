"""Construye el proveedor configurado (`LLM_PROVIDER`)."""

from functools import lru_cache

from app.core.config import Settings, get_settings
from app.llm.fake import FakeLLMProvider, grounded_responder
from app.llm.openai import OpenAILLMProvider
from app.llm.provider import LLMParams, LLMProvider


def llm_params(settings: Settings) -> LLMParams:
    return LLMParams(settings.llm_temperature, settings.llm_max_output_tokens)


def build_llm_provider(settings: Settings) -> LLMProvider:
    params = llm_params(settings)
    if settings.llm_provider == "openai":
        if settings.openai_api_key is None:
            raise ValueError("OPENAI_API_KEY es obligatoria para el proveedor 'openai'")
        return OpenAILLMProvider(
            api_key=settings.openai_api_key.get_secret_value(),
            model=settings.llm_model,
            params=params,
            base_url=settings.openai_base_url,
            timeout_seconds=settings.llm_timeout_seconds,
            retry=settings.provider_retry,
        )
    responder = grounded_responder if settings.fake_llm_grounded else None
    return FakeLLMProvider(settings.llm_model, params, responder)


@lru_cache
def get_llm_provider() -> LLMProvider:
    """Proveedor configurado; dependencia sustituible en las pruebas."""
    return build_llm_provider(get_settings())
