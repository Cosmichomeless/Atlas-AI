"""Recuperación ante fallos: reintentos de proveedores, BD caída y nada falsamente READY."""

import json
import threading
from collections.abc import Callable, Sequence
from datetime import timedelta

import httpx2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.retry import RetryPolicy, call_with_retries, is_transient
from app.embeddings.openai import OpenAIEmbeddingProvider
from app.embeddings.provider import EmbeddingError
from app.embeddings.registry import build_embedding_provider
from app.features.documents.models import Document
from app.features.documents.states import DocumentStatus
from app.features.documents.storage import LocalFileStorage
from app.ingestion import service, worker
from app.llm.openai import OpenAILLMProvider
from app.llm.provider import LLMError, LLMParams, Message
from app.llm.registry import build_llm_provider
from app.main import create_app
from tests.test_chunk_persistence import text_document
from tests.test_ingestion import LEASE, MAX_ATTEMPTS, T0, claim
from tests.test_ingestion_embeddings import TEXT, CountingProvider, chunks, ingest, vectors
from tests.test_llm import chat_response

FAST = RetryPolicy(attempts=3, base_delay=0.0, max_delay=0.0)


def status_of(document: Document) -> DocumentStatus:
    return document.status


def unavailable(request: httpx2.Request, status: int = 503) -> httpx2.Response:
    return httpx2.Response(status, text="sk-secreta")


class Script:
    """Transporte simulado: responde en orden con lo programado y cuenta las llamadas."""

    def __init__(self, *steps: Callable[[httpx2.Request], httpx2.Response]) -> None:
        self.steps = list(steps)
        self.calls = 0

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        step = self.steps[min(self.calls, len(self.steps) - 1)]
        self.calls += 1
        return step(request)


def embedding_ok(request: httpx2.Request) -> httpx2.Response:
    count = len(json.loads(request.content)["input"])
    data = [{"index": i, "embedding": [1.0, 0.0, 0.0]} for i in range(count)]
    return httpx2.Response(200, json={"data": data})


def embedder(script: Script, retry: RetryPolicy = FAST) -> OpenAIEmbeddingProvider:
    return OpenAIEmbeddingProvider(
        api_key="sk-secreta",
        model="text-embedding-3-small",
        dimensions=3,
        client=httpx2.Client(transport=httpx2.MockTransport(script)),
        retry=retry,
    )


def chat(script: Script, retry: RetryPolicy = FAST) -> OpenAILLMProvider:
    return OpenAILLMProvider(
        api_key="sk-secreta",
        model="gpt-4o-mini",
        params=LLMParams(0.0, 100),
        client=httpx2.Client(transport=httpx2.MockTransport(script)),
        retry=retry,
    )


def chat_ok(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, json=chat_response())


def connect_error(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ConnectError("sin red", request=request)


def timeout(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ReadTimeout("lento", request=request)


QUESTION = [Message("user", "¿cuándo vence?")]


# ── Política de reintentos ──────────────────────────────────────────────────


def test_the_wait_doubles_up_to_the_cap() -> None:
    policy = RetryPolicy(attempts=6, base_delay=1.0, max_delay=5.0)

    assert [policy.delay(n) for n in (1, 2, 3, 4)] == [1.0, 2.0, 4.0, 5.0]


def test_retry_after_lengthens_the_wait_but_never_beyond_the_cap() -> None:
    policy = RetryPolicy(attempts=3, base_delay=1.0, max_delay=5.0)

    assert policy.delay(1, retry_after=3.0) == 3.0
    assert policy.delay(1, retry_after=600.0) == 5.0


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_these_http_statuses_are_transient(status: int) -> None:
    error = httpx2.HTTPStatusError(
        "x", request=httpx2.Request("POST", "https://x"), response=httpx2.Response(status)
    )

    assert is_transient(error)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_client_errors_are_not_transient(status: int) -> None:
    error = httpx2.HTTPStatusError(
        "x", request=httpx2.Request("POST", "https://x"), response=httpx2.Response(status)
    )

    assert not is_transient(error)


def test_call_with_retries_waits_between_attempts_and_returns_the_first_success() -> None:
    waits: list[float] = []
    failures: list[Exception] = [httpx2.ConnectError("a"), httpx2.ReadTimeout("b")]

    def call() -> str:
        if failures:
            raise failures.pop(0)
        return "listo"

    result = call_with_retries(call, RetryPolicy(3, 0.5, 8.0), what="Prueba", sleep=waits.append)

    assert result == "listo"
    assert waits == [0.5, 1.0]


def test_call_with_retries_gives_up_after_the_attempt_limit() -> None:
    calls: list[int] = []
    waits: list[float] = []

    def call() -> str:
        calls.append(1)
        raise httpx2.ConnectError("sin red")

    with pytest.raises(httpx2.ConnectError):
        call_with_retries(call, RetryPolicy(4, 0.1, 1.0), what="Prueba", sleep=waits.append)

    assert len(calls) == 4
    assert len(waits) == 3


# ── Embeddings ──────────────────────────────────────────────────────────────


def test_embeddings_recover_from_a_transient_outage() -> None:
    script = Script(unavailable, lambda r: unavailable(r, 429), embedding_ok)

    result = embedder(script).embed(["hola"])

    assert script.calls == 3
    assert [item.vector for item in result] == [(1.0, 0.0, 0.0)]


@pytest.mark.parametrize("failure", [connect_error, timeout])
def test_embeddings_retry_network_failures(
    failure: Callable[[httpx2.Request], httpx2.Response],
) -> None:
    script = Script(failure, embedding_ok)

    embedder(script).embed(["hola"])

    assert script.calls == 2


def test_embeddings_stop_retrying_at_the_limit_and_report_a_transient_error() -> None:
    script = Script(unavailable)

    with pytest.raises(EmbeddingError, match="503") as caught:
        embedder(script).embed(["hola"])

    assert script.calls == FAST.attempts
    assert caught.value.transient
    assert "sk-secreta" not in str(caught.value)


def test_embeddings_do_not_retry_a_rejected_request() -> None:
    script = Script(lambda r: unavailable(r, 401), embedding_ok)

    with pytest.raises(EmbeddingError, match="401") as caught:
        embedder(script).embed(["hola"])

    assert script.calls == 1
    assert not caught.value.transient


def test_embeddings_do_not_retry_a_malformed_answer() -> None:
    script = Script(lambda r: httpx2.Response(200, json={"nada": 1}), embedding_ok)

    with pytest.raises(EmbeddingError, match="formato") as caught:
        embedder(script).embed(["hola"])

    assert script.calls == 1
    assert not caught.value.transient


def test_without_a_policy_embeddings_call_once() -> None:
    script = Script(unavailable, embedding_ok)
    provider = OpenAIEmbeddingProvider(
        api_key="k",
        model="text-embedding-3-small",
        dimensions=3,
        client=httpx2.Client(transport=httpx2.MockTransport(script)),
    )

    with pytest.raises(EmbeddingError):
        provider.embed(["hola"])

    assert script.calls == 1


# ── Generación ──────────────────────────────────────────────────────────────


def test_the_model_recovers_from_a_transient_outage() -> None:
    script = Script(unavailable, connect_error, chat_ok)

    completion = chat(script).complete(QUESTION)

    assert script.calls == 3
    assert completion.text == "Vence el día 15."


def test_the_model_stops_retrying_at_the_limit_and_reports_a_transient_error() -> None:
    script = Script(lambda r: unavailable(r, 429))

    with pytest.raises(LLMError, match="429") as caught:
        chat(script).complete(QUESTION)

    assert script.calls == FAST.attempts
    assert caught.value.transient


def test_the_model_does_not_retry_a_rejected_request() -> None:
    script = Script(lambda r: unavailable(r, 400), chat_ok)

    with pytest.raises(LLMError, match="400") as caught:
        chat(script).complete(QUESTION)

    assert script.calls == 1
    assert not caught.value.transient


# ── Configuración ───────────────────────────────────────────────────────────


def settings(**overrides: object) -> Settings:
    return Settings(
        database_url="postgresql+psycopg://x:y@localhost/z",
        embedding_provider="openai",
        llm_provider="openai",
        openai_api_key="sk-secreta",  # type: ignore[arg-type]
        **overrides,  # type: ignore[arg-type]
    )


def test_the_retry_policy_comes_from_the_settings() -> None:
    configured = settings(
        provider_retry_attempts=5, provider_retry_base_seconds=0.25, provider_retry_max_seconds=2
    )

    assert configured.provider_retry == RetryPolicy(5, 0.25, 2.0)
    embeddings = build_embedding_provider(configured)
    model = build_llm_provider(configured)
    assert isinstance(embeddings, OpenAIEmbeddingProvider)
    assert isinstance(model, OpenAILLMProvider)
    assert embeddings._retry == model._retry == configured.provider_retry


def test_the_attempt_limit_is_bounded() -> None:
    with pytest.raises(ValueError, match="provider_retry_attempts"):
        settings(provider_retry_attempts=0)
    with pytest.raises(ValueError, match="provider_retry_attempts"):
        settings(provider_retry_attempts=50)


# ── Ingestión: nunca falsamente READY ───────────────────────────────────────


class Rejecting(CountingProvider):
    """Falso que rechaza la petición de forma permanente (p. ej. clave inválida)."""

    def _embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        raise EmbeddingError("El servicio de embeddings respondió 401", transient=False)


def test_a_transient_embedding_failure_requeues_and_the_next_run_completes(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = text_document(db_session, storage, TEXT)
    ingest(db_session, storage, CountingProvider(fail_from=2))

    assert status_of(document) is DocumentStatus.UPLOADED
    assert document.error_summary == service.EMBEDDING_RETRY
    assert vectors(db_session, document) == 0
    assert chunks(db_session, document) == 0

    ingest(db_session, storage, CountingProvider())

    assert status_of(document) is DocumentStatus.READY
    assert document.error_summary is None
    assert vectors(db_session, document) == chunks(db_session, document) > 0


def test_a_rejected_embedding_request_fails_at_once_with_a_clear_cause(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = text_document(db_session, storage, TEXT)

    ingest(db_session, storage, Rejecting())

    assert status_of(document) is DocumentStatus.FAILED
    assert document.error_summary == service.EMBEDDING_REJECTED
    assert document.attempts == 1 < MAX_ATTEMPTS
    assert vectors(db_session, document) == 0
    assert chunks(db_session, document) == 0


def test_a_persistently_failing_provider_ends_failed_and_never_ready(
    db_session: Session, storage: LocalFileStorage
) -> None:
    document = text_document(db_session, storage, TEXT)
    provider = CountingProvider(fail_from=1)

    for _ in range(MAX_ATTEMPTS):
        ingest(db_session, storage, provider)

    assert status_of(document) is DocumentStatus.FAILED
    assert document.error_summary == service.EMBEDDING_FAILURE
    assert vectors(db_session, document) == 0
    ready = db_session.scalars(select(Document).where(Document.status == DocumentStatus.READY))
    assert document not in ready.all()


def test_a_database_failure_while_recording_the_error_leaves_the_document_recoverable(
    db_session: Session, storage: LocalFileStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    def database_down(*args: object, **kwargs: object) -> None:
        raise OperationalError("UPDATE documents", {}, Exception("connection refused"))

    document = text_document(db_session, storage, TEXT)
    monkeypatch.setattr(service, "_retry_or_fail", database_down)

    with pytest.raises(OperationalError):
        ingest(db_session, storage, CountingProvider(fail_from=2))
    monkeypatch.undo()

    # La reserva ya estaba confirmada: nunca READY, y al vencer el arrendamiento se recupera.
    assert status_of(document) is DocumentStatus.PROCESSING
    assert vectors(db_session, document) == 0
    recovered = claim(db_session, now=T0 + 2 * timedelta(seconds=LEASE))
    assert recovered is document
    assert document.attempts == 2


# ── Worker ──────────────────────────────────────────────────────────────────


def test_idle_wait_grows_with_consecutive_failures_up_to_a_cap() -> None:
    assert worker.idle_wait(2.0, 0) == 2.0
    assert worker.idle_wait(2.0, 1) == 4.0
    assert worker.idle_wait(2.0, 3) == 16.0
    assert worker.idle_wait(2.0, 50) == worker.MAX_BACKOFF_SECONDS


def test_the_worker_survives_a_database_outage_and_keeps_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INGESTION_POLL_SECONDS", "0.01")
    get_settings.cache_clear()
    stop = threading.Event()
    calls: list[int] = []

    def flaky(*args: object, **kwargs: object) -> bool:
        calls.append(1)
        if len(calls) <= 2:
            raise OperationalError("SELECT 1", {}, Exception("connection refused"))
        stop.set()
        return False

    monkeypatch.setattr(worker, "run_once", flaky)
    try:
        thread = threading.Thread(target=worker.run, args=(stop,))
        thread.start()
        thread.join(timeout=10)
    finally:
        stop.set()
        get_settings.cache_clear()

    assert not thread.is_alive()
    assert len(calls) == 3  # dos fallos de la BD y el worker siguió hasta recuperarse


# ── API: la base de datos caída es un 503 claro ─────────────────────────────


def app_raising(error: Exception) -> FastAPI:
    app = create_app()

    @app.get("/boom")
    def boom() -> None:
        raise error

    return app


def test_a_database_outage_is_a_503_with_a_stable_code() -> None:
    error = OperationalError("SELECT 1", {}, Exception("password=secreta host=10.0.0.5"))
    client = TestClient(app_raising(error))

    response = client.get("/boom")

    assert response.status_code == 503
    body = response.json()["error"]
    assert body["code"] == "database_unavailable"
    assert body["request_id"]
    assert response.headers["Retry-After"] == "5"
    assert "secreta" not in response.text
    assert "10.0.0.5" not in response.text


def test_an_invalidated_connection_is_also_a_503() -> None:
    error = DBAPIError("SELECT 1", {}, Exception("server closed"), connection_invalidated=True)

    response = TestClient(app_raising(error)).get("/boom")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "database_unavailable"


def test_other_database_errors_stay_internal_errors_without_detail() -> None:
    error = DBAPIError("INSERT INTO x", {}, Exception("violación de restricción interna"))
    client = TestClient(app_raising(error), raise_server_exceptions=False)

    response = client.get("/boom")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "restricción" not in response.text
