"""Adaptador de embeddings: el fake evita llamadas de pago y cada vector lleva su especificación."""

import json
import math
from collections.abc import Sequence

import httpx2
import pytest

from app.core.config import get_settings
from app.embeddings.fake import FAKE_VERSION, FakeEmbeddingProvider
from app.embeddings.openai import OPENAI_VERSION, OpenAIEmbeddingProvider
from app.embeddings.provider import Embedding, EmbeddingError, EmbeddingProvider, EmbeddingSpec
from app.embeddings.registry import build_embedding_provider, get_embedding_provider
from tests.test_config import make


def cosine(a: Embedding, b: Embedding) -> float:
    return sum(x * y for x, y in zip(a.vector, b.vector, strict=True))


# ── Especificación ──────────────────────────────────────────────────────────


def test_spec_key_identifies_provider_model_dimension_and_version() -> None:
    spec = EmbeddingSpec("openai", "text-embedding-3-small", 1536, "1")

    assert spec.key == "openai/text-embedding-3-small/1536/v1"


@pytest.mark.parametrize(
    "args",
    [
        ("", "m", 8, "1"),
        ("p", "", 8, "1"),
        ("p", "m", 8, ""),
        ("p", "m", 0, "1"),
        ("p", "m", -3, "1"),
    ],
)
def test_spec_rejects_incomplete_values(args: tuple[str, str, int, str]) -> None:
    with pytest.raises(ValueError):
        EmbeddingSpec(*args)


# ── Fake ────────────────────────────────────────────────────────────────────


def test_fake_is_deterministic_and_normalised() -> None:
    provider = FakeEmbeddingProvider("fake-model", 64)

    first = provider.embed_one("El contrato vence en marzo")
    second = provider.embed_one("El contrato vence en marzo")

    assert first == second
    assert math.isclose(math.sqrt(sum(v * v for v in first.vector)), 1.0)


def test_fake_attaches_model_dimension_and_version_to_every_vector() -> None:
    provider = FakeEmbeddingProvider("fake-model", 32)

    embeddings = provider.embed(["uno", "dos", "tres"])

    assert len(embeddings) == 3
    for embedding in embeddings:
        assert len(embedding.vector) == 32
        assert embedding.spec == EmbeddingSpec("fake", "fake-model", 32, FAKE_VERSION)


def test_fake_keeps_the_input_order() -> None:
    provider = FakeEmbeddingProvider("m", 64)
    texts = ["alfa", "beta", "gamma"]

    assert provider.embed(texts) == [provider.embed_one(text) for text in texts]


def test_fake_places_texts_that_share_words_closer() -> None:
    provider = FakeEmbeddingProvider("m", 256)
    query = provider.embed_one("plazo de entrega del pedido")
    related = provider.embed_one("el plazo de entrega es de diez días")
    unrelated = provider.embed_one("receta de tortilla con cebolla")

    assert cosine(query, related) > cosine(query, unrelated)


def test_fake_handles_text_without_word_characters() -> None:
    embedding = FakeEmbeddingProvider("m", 16).embed_one("¿¡…!?")

    assert math.isclose(sum(v * v for v in embedding.vector), 1.0)


def test_fake_changing_the_dimension_changes_the_spec() -> None:
    small = FakeEmbeddingProvider("m", 8).embed_one("hola")
    large = FakeEmbeddingProvider("m", 16).embed_one("hola")

    assert small.spec != large.spec
    assert len(small.vector) == 8
    assert len(large.vector) == 16


# ── Validación común ────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
def test_blank_texts_are_rejected(text: str) -> None:
    with pytest.raises(EmbeddingError, match="blanco"):
        FakeEmbeddingProvider("m", 8).embed(["bueno", text])


def test_embedding_nothing_returns_nothing() -> None:
    assert FakeEmbeddingProvider("m", 8).embed([]) == []


def test_texts_are_sent_in_batches_without_losing_order() -> None:
    class Spy(FakeEmbeddingProvider):
        max_batch_size = 2

        def __init__(self) -> None:
            super().__init__("m", 8)
            self.batches: list[list[str]] = []

        def _embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
            self.batches.append(list(texts))
            return super()._embed_batch(texts)

    spy = Spy()
    texts = ["a", "b", "c", "d", "e"]

    result = spy.embed(texts)

    assert spy.batches == [["a", "b"], ["c", "d"], ["e"]]
    assert result == [FakeEmbeddingProvider("m", 8).embed_one(text) for text in texts]


class Broken(EmbeddingProvider):
    def __init__(self, vectors: list[list[float]]) -> None:
        self.spec = EmbeddingSpec("broken", "m", 3, "1")
        self.vectors = vectors

    def _embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        return self.vectors


def test_a_wrong_dimension_is_rejected() -> None:
    with pytest.raises(EmbeddingError, match="Dimensión 2"):
        Broken([[0.1, 0.2]]).embed(["x"])


def test_a_wrong_vector_count_is_rejected() -> None:
    with pytest.raises(EmbeddingError, match="2 vectores para 1"):
        Broken([[0.1, 0.2, 0.3], [0.1, 0.2, 0.3]]).embed(["x"])


@pytest.mark.parametrize("bad", [math.nan, math.inf])
def test_non_finite_values_are_rejected(bad: float) -> None:
    with pytest.raises(EmbeddingError, match="no finitos"):
        Broken([[0.1, bad, 0.3]]).embed(["x"])


# ── OpenAI (sin red: transporte simulado) ───────────────────────────────────


def openai_provider(
    handler: httpx2.MockTransport | None = None, *, model: str = "text-embedding-3-small"
) -> tuple[OpenAIEmbeddingProvider, list[httpx2.Request]]:
    requests: list[httpx2.Request] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        inputs = json.loads(request.content)["input"]
        # Devuelve los elementos desordenados para comprobar que se reordenan por `index`.
        data = [
            {"index": index, "embedding": [float(index), 0.0, 1.0]}
            for index in reversed(range(len(inputs)))
        ]
        return httpx2.Response(200, json={"data": data})

    transport = handler or httpx2.MockTransport(respond)
    provider = OpenAIEmbeddingProvider(
        api_key="sk-secreta",
        model=model,
        dimensions=3,
        base_url="https://example.test/v1/",
        client=httpx2.Client(transport=transport),
    )
    return provider, requests


def test_openai_posts_to_the_embeddings_endpoint_with_the_key() -> None:
    provider, requests = openai_provider()

    provider.embed(["hola"])

    request = requests[0]
    assert str(request.url) == "https://example.test/v1/embeddings"
    assert request.headers["Authorization"] == "Bearer sk-secreta"
    body = json.loads(request.content)
    assert body["model"] == "text-embedding-3-small"
    assert body["dimensions"] == 3


def test_openai_does_not_send_dimensions_to_models_that_do_not_support_it() -> None:
    provider, requests = openai_provider(model="text-embedding-ada-002")

    provider.embed(["hola"])

    assert "dimensions" not in json.loads(requests[0].content)


def test_openai_orders_vectors_by_index_and_records_the_spec() -> None:
    provider, _ = openai_provider()

    embeddings = provider.embed(["a", "b", "c"])

    assert [e.vector[0] for e in embeddings] == [0.0, 1.0, 2.0]
    assert all(
        e.spec == EmbeddingSpec("openai", "text-embedding-3-small", 3, OPENAI_VERSION)
        for e in embeddings
    )


def test_openai_splits_large_inputs_into_batches() -> None:
    provider, requests = openai_provider()
    provider.max_batch_size = 2

    provider.embed(["a", "b", "c"])

    assert [len(json.loads(r.content)["input"]) for r in requests] == [2, 1]


@pytest.mark.parametrize("status", [401, 429, 500])
def test_openai_http_errors_become_embedding_errors_without_leaking_the_key(status: int) -> None:
    transport = httpx2.MockTransport(lambda request: httpx2.Response(status, text="sk-secreta"))
    provider, _ = openai_provider(transport)

    with pytest.raises(EmbeddingError) as error:
        provider.embed(["hola"])

    assert str(status) in str(error.value)
    assert "sk-secreta" not in str(error.value)


def test_openai_network_failures_become_embedding_errors() -> None:
    def fail(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("sin red", request=request)

    provider, _ = openai_provider(httpx2.MockTransport(fail))

    with pytest.raises(EmbeddingError, match="No se pudo contactar"):
        provider.embed(["hola"])


@pytest.mark.parametrize("body", [{"unexpected": 1}, {"data": [{"index": 0}]}, {"data": "x"}])
def test_openai_malformed_responses_become_embedding_errors(body: dict[str, object]) -> None:
    transport = httpx2.MockTransport(lambda request: httpx2.Response(200, json=body))
    provider, _ = openai_provider(transport)

    with pytest.raises(EmbeddingError, match="formato inesperado"):
        provider.embed(["hola"])


def test_openai_rejects_a_dimension_that_does_not_match_the_configuration() -> None:
    transport = httpx2.MockTransport(
        lambda request: httpx2.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})
    )
    provider, _ = openai_provider(transport)

    with pytest.raises(EmbeddingError, match="Dimensión 1"):
        provider.embed(["hola"])


# ── Selección por configuración ─────────────────────────────────────────────


def test_registry_builds_the_fake_by_default() -> None:
    provider = build_embedding_provider(make(embedding_model="m", embedding_dimensions=24))

    assert isinstance(provider, FakeEmbeddingProvider)
    assert provider.spec == EmbeddingSpec("fake", "m", 24, FAKE_VERSION)


def test_registry_builds_openai_from_settings() -> None:
    settings = make(
        embedding_provider="openai",
        openai_api_key="sk-x",
        embedding_dimensions=512,
        openai_base_url="https://example.test/v1",
    )

    provider = build_embedding_provider(settings)

    assert isinstance(provider, OpenAIEmbeddingProvider)
    assert provider.spec.key == "openai/text-embedding-3-small/512/v1"


def test_the_test_suite_uses_the_fake_provider() -> None:
    get_embedding_provider.cache_clear()
    try:
        assert get_embedding_provider().spec.provider == "fake"
        assert get_embedding_provider().spec.dimensions == get_settings().embedding_dimensions
    finally:
        get_embedding_provider.cache_clear()
