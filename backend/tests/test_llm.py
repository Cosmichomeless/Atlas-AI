"""Adaptador de LLM: el fake evita llamadas externas y cada respuesta lleva modelo y parámetros."""

import json
from collections.abc import Sequence

import httpx2
import pytest

from app.core.config import get_settings
from app.llm.fake import FAKE_VERSION, FakeLLMProvider
from app.llm.openai import OPENAI_VERSION, OpenAILLMProvider
from app.llm.provider import (
    Completion,
    LLMError,
    LLMParams,
    LLMProvider,
    LLMSpec,
    Message,
    RawCompletion,
)
from app.llm.registry import build_llm_provider, get_llm_provider
from tests.test_config import make

QUESTION = [Message("user", "¿cuándo vence la factura?")]

# ── Especificación y parámetros ─────────────────────────────────────────────


def test_spec_key_identifies_provider_model_and_version() -> None:
    assert LLMSpec("openai", "gpt-4o-mini", "1").key == "openai/gpt-4o-mini/v1"


@pytest.mark.parametrize("args", [("", "m", "1"), ("p", "", "1"), ("p", "m", "")])
def test_spec_rejects_incomplete_values(args: tuple[str, str, str]) -> None:
    with pytest.raises(ValueError, match="obligatorios"):
        LLMSpec(*args)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"temperature": -0.1},
        {"temperature": 2.1},
        {"temperature": float("nan")},
        {"max_output_tokens": 0},
    ],
)
def test_params_reject_nonsense(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError, match=next(iter(kwargs))):
        LLMParams(**kwargs)  # type: ignore[arg-type]


def test_params_default_to_reproducible_generation() -> None:
    assert LLMParams() == LLMParams(temperature=0.0, max_output_tokens=512)


# ── Fake ────────────────────────────────────────────────────────────────────


def test_the_fake_answers_deterministically_without_network() -> None:
    provider = FakeLLMProvider("fake-model")

    first, second = provider.complete(QUESTION), provider.complete(QUESTION)

    assert first == second
    assert first.text == "Respuesta simulada a: ¿cuándo vence la factura?"
    assert first.spec == LLMSpec("fake", "fake-model", FAKE_VERSION)


def test_every_completion_carries_the_model_and_parameters_that_produced_it() -> None:
    params = LLMParams(temperature=0.3, max_output_tokens=99)

    completion = FakeLLMProvider("m", params).complete(QUESTION)

    assert (completion.spec.model, completion.params) == ("m", params)
    assert completion.usage is not None
    assert completion.finish_reason == "stop"
    assert not completion.truncated


def test_the_fake_records_what_it_was_sent_and_accepts_a_custom_responder() -> None:
    def shout(messages: Sequence[Message]) -> str:
        return messages[-1].content.upper()

    provider = FakeLLMProvider("m", responder=shout)
    conversation = [Message("system", "sé breve"), *QUESTION]

    assert provider.complete(conversation).text == "¿CUÁNDO VENCE LA FACTURA?"
    assert provider.calls == [conversation]


def test_a_truncated_completion_is_flagged() -> None:
    completion = Completion("texto", LLMSpec("p", "m", "1"), LLMParams(), None, "length")

    assert completion.truncated


# ── Validación común ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "conversation",
    [
        pytest.param([], id="vacía"),
        pytest.param([Message("user", "   ")], id="mensaje-en-blanco"),
        pytest.param([Message("system", "reglas")], id="sin-usuario"),
        pytest.param([Message("tool", "x"), *QUESTION], id="rol-desconocido"),  # type: ignore[arg-type]
    ],
)
def test_invalid_conversations_are_rejected_before_reaching_the_provider(
    conversation: list[Message],
) -> None:
    provider = FakeLLMProvider("m")

    with pytest.raises(LLMError):
        provider.complete(conversation)

    assert provider.calls == []


class Silent(LLMProvider):
    spec = LLMSpec("silent", "m", "1")
    params = LLMParams()

    def _complete(self, messages: Sequence[Message]) -> RawCompletion:
        return RawCompletion("  \n")


def test_an_empty_answer_from_the_provider_is_an_error() -> None:
    with pytest.raises(LLMError, match="vacía"):
        Silent().complete(QUESTION)


# ── OpenAI (sin red: transporte simulado) ───────────────────────────────────


def chat_response(content: object = "Vence el día 15.", **extra: object) -> dict[str, object]:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 5},
        **extra,
    }


def openai_provider(
    handler: httpx2.MockTransport | None = None, params: LLMParams | None = None
) -> tuple[OpenAILLMProvider, list[httpx2.Request]]:
    requests: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(200, json=chat_response())

    provider = OpenAILLMProvider(
        api_key="sk-secreta",
        model="gpt-4o-mini",
        params=params or LLMParams(0.2, 300),
        base_url="https://example.test/v1/",
        client=httpx2.Client(transport=handler or httpx2.MockTransport(respond)),
    )
    return provider, requests


def test_openai_posts_to_the_chat_endpoint_with_model_params_and_key() -> None:
    provider, requests = openai_provider()

    provider.complete([Message("system", "reglas"), *QUESTION])

    request = requests[0]
    assert str(request.url) == "https://example.test/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer sk-secreta"
    assert json.loads(request.content) == {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "reglas"},
            {"role": "user", "content": "¿cuándo vence la factura?"},
        ],
        "temperature": 0.2,
        "max_tokens": 300,
    }


def test_openai_returns_text_usage_and_finish_reason() -> None:
    provider, _ = openai_provider()

    completion = provider.complete(QUESTION)

    assert completion.text == "Vence el día 15."
    assert (completion.usage.input_tokens, completion.usage.output_tokens) == (11, 5)  # type: ignore[union-attr]
    assert completion.spec == LLMSpec("openai", "gpt-4o-mini", OPENAI_VERSION)
    assert completion.params == LLMParams(0.2, 300)


def test_openai_tolerates_a_response_without_usage() -> None:
    body = {"choices": [{"message": {"content": "hola"}}]}
    provider, _ = openai_provider(httpx2.MockTransport(lambda r: httpx2.Response(200, json=body)))

    completion = provider.complete(QUESTION)

    assert (completion.usage, completion.finish_reason) == (None, None)


@pytest.mark.parametrize("status", [401, 429, 500])
def test_openai_http_errors_become_llm_errors_without_leaking_secrets(status: int) -> None:
    transport = httpx2.MockTransport(lambda r: httpx2.Response(status, text="sk-secreta ¿cuándo"))
    provider, _ = openai_provider(transport)

    with pytest.raises(LLMError) as error:
        provider.complete(QUESTION)

    assert str(status) in str(error.value)
    assert "sk-secreta" not in str(error.value)
    assert "cuándo" not in str(error.value)


def test_openai_network_failures_become_llm_errors() -> None:
    def fail(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("sk-secreta no se pudo conectar")

    provider, _ = openai_provider(httpx2.MockTransport(fail))

    with pytest.raises(LLMError, match="ConnectError") as error:
        provider.complete(QUESTION)

    assert "sk-secreta" not in str(error.value)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({}, id="sin-choices"),
        pytest.param({"choices": []}, id="choices-vacío"),
        pytest.param(chat_response(content=None), id="contenido-nulo"),
        pytest.param(chat_response(content=["a"]), id="contenido-no-textual"),
        pytest.param({"choices": [{"message": {"content": "x"}}], "usage": {"a": 1}}, id="uso"),
    ],
)
def test_openai_malformed_responses_are_llm_errors(body: dict[str, object]) -> None:
    provider, _ = openai_provider(httpx2.MockTransport(lambda r: httpx2.Response(200, json=body)))

    with pytest.raises(LLMError, match="formato inesperado"):
        provider.complete(QUESTION)


def test_openai_not_json_is_an_llm_error() -> None:
    provider, _ = openai_provider(
        httpx2.MockTransport(lambda r: httpx2.Response(200, text="<html>"))
    )

    with pytest.raises(LLMError, match="formato inesperado"):
        provider.complete(QUESTION)


# ── Configuración por entorno ───────────────────────────────────────────────


def test_registry_builds_the_fake_by_default_with_the_configured_parameters() -> None:
    provider = build_llm_provider(
        make(llm_model="m", llm_temperature=0.4, llm_max_output_tokens=77)
    )

    assert isinstance(provider, FakeLLMProvider)
    assert provider.spec == LLMSpec("fake", "m", FAKE_VERSION)
    assert provider.params == LLMParams(0.4, 77)


def test_registry_builds_openai_from_settings() -> None:
    settings = make(
        llm_provider="openai",
        openai_api_key="sk-x",
        llm_model="gpt-4.1",
        llm_temperature=1,
        llm_max_output_tokens=900,
        openai_base_url="https://example.test/v1",
    )

    provider = build_llm_provider(settings)

    assert isinstance(provider, OpenAILLMProvider)
    assert provider.spec.key == "openai/gpt-4.1/v1"
    assert provider.params == LLMParams(1, 900)


def test_default_settings_are_reproducible() -> None:
    settings = make()

    assert (settings.llm_temperature, settings.llm_max_output_tokens) == (0.0, 512)
    assert settings.llm_timeout_seconds == 60.0


@pytest.mark.parametrize(
    "fields",
    [
        {"llm_temperature": -1},
        {"llm_temperature": 3},
        {"llm_max_output_tokens": 0},
        {"llm_timeout_seconds": 0},
    ],
)
def test_invalid_llm_settings_are_rejected(fields: dict[str, float]) -> None:
    with pytest.raises(ValueError, match=next(iter(fields))):
        make(**fields)


def test_an_openai_llm_requires_its_key() -> None:
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        make(llm_provider="openai")


def test_the_test_suite_uses_the_fake_llm() -> None:
    get_llm_provider.cache_clear()
    try:
        assert get_llm_provider().spec.provider == "fake"
        assert get_llm_provider().spec.model == get_settings().llm_model
    finally:
        get_llm_provider.cache_clear()
