"""Cuotas diarias y contabilidad de uso: el exceso no llama al proveedor; solo hay contadores."""

import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.embeddings.provider import EmbeddingProvider
from app.embeddings.registry import get_embedding_provider
from app.features.usage.models import UsageDay
from app.features.usage.service import (
    Tally,
    UsageLimitExceeded,
    UsageLimits,
    ensure_within_limits,
    get_usage,
    record_usage,
    utc_day,
)
from app.features.users.models import User
from app.llm.provider import LLMError, Message
from tests.test_document_flow import ALICE, BOB
from tests.test_documents_api import sign_in
from tests.test_embedding_store import PROVIDER
from tests.test_questions_api import ANSWER, URL, ask, embedder, model  # noqa: F401
from tests.test_search_api import FACTURA, ready_document

USAGE_URL = "/api/v1/usage"
NOON = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def set_quotas(
    monkeypatch: pytest.MonkeyPatch, questions: int = 100, tokens: int = 300_000
) -> None:
    monkeypatch.setenv("USAGE_DAILY_QUESTIONS", str(questions))
    monkeypatch.setenv("USAGE_DAILY_TOKENS", str(tokens))
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def fresh_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class CountingProvider(EmbeddingProvider):
    """Embeddings reales del fake, contando cuántas veces se le llama."""

    spec = PROVIDER.spec

    def __init__(self) -> None:
        self.batches = 0

    def _embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        self.batches += 1
        return PROVIDER._embed_batch(texts)


def today(client: TestClient) -> dict[str, int]:
    usage: dict[str, int] = client.get(USAGE_URL).json()
    return usage


@pytest.fixture
def user_id(db_session: Session) -> uuid.UUID:
    user = User(email="usage@example.com", password_hash="not-a-real-hash")
    db_session.add(user)
    db_session.flush()
    return user.id


# ── Servicio ────────────────────────────────────────────────────────────────


def test_an_empty_tally_writes_nothing(db_session: Session, user_id: uuid.UUID) -> None:
    record_usage(db_session, user_id, Tally(), now=NOON)

    assert db_session.scalars(select(UsageDay)).all() == []


def test_recording_accumulates_across_calls(db_session: Session, user_id: uuid.UUID) -> None:
    record_usage(db_session, user_id, Tally(1, 1, 1, 100, 20), now=NOON)
    record_usage(db_session, user_id, Tally(1, 1, 0, 0, 0), now=NOON + timedelta(hours=3))

    (row,) = db_session.scalars(select(UsageDay)).all()
    assert (row.questions, row.embedding_calls, row.llm_calls) == (2, 2, 1)
    assert (row.input_tokens, row.output_tokens) == (100, 20)


def test_each_utc_day_has_its_own_counters(db_session: Session, user_id: uuid.UUID) -> None:
    record_usage(db_session, user_id, Tally(questions=3), now=NOON)
    tomorrow = NOON + timedelta(days=1)

    assert get_usage(db_session, user_id, now=NOON).questions == 3
    assert get_usage(db_session, user_id, now=tomorrow).questions == 0
    assert utc_day(datetime(2026, 10, 7, 23, 59, tzinfo=UTC)) != utc_day(
        datetime(2026, 10, 8, 0, 0, tzinfo=UTC)
    )


def test_the_question_quota_blocks_and_resets_the_next_day(
    db_session: Session, user_id: uuid.UUID
) -> None:
    limits = UsageLimits(daily_questions=2, daily_tokens=1_000_000)
    record_usage(db_session, user_id, Tally(questions=1), now=NOON)
    ensure_within_limits(db_session, user_id, limits, now=NOON)

    record_usage(db_session, user_id, Tally(questions=1), now=NOON)
    with pytest.raises(UsageLimitExceeded, match="2 preguntas"):
        ensure_within_limits(db_session, user_id, limits, now=NOON)

    ensure_within_limits(db_session, user_id, limits, now=NOON + timedelta(days=1))


def test_the_token_quota_adds_input_and_output(db_session: Session, user_id: uuid.UUID) -> None:
    limits = UsageLimits(daily_questions=100, daily_tokens=500)
    record_usage(db_session, user_id, Tally(input_tokens=300, output_tokens=199), now=NOON)
    ensure_within_limits(db_session, user_id, limits, now=NOON)

    record_usage(db_session, user_id, Tally(output_tokens=1), now=NOON)
    with pytest.raises(UsageLimitExceeded, match="uso diario del modelo"):
        ensure_within_limits(db_session, user_id, limits, now=NOON)


# ── /questions ──────────────────────────────────────────────────────────────


def test_an_answer_records_calls_and_the_tokens_the_provider_reports(
    client: TestClient, db_session: Session
) -> None:
    alice = sign_in(client, ALICE)
    ready_document(db_session, alice)
    provider = model(client)

    assert ask(client).status_code == 200

    (messages,) = provider.calls
    prompt_words = sum(len(m.content.split()) for m in messages)
    usage = today(client)
    assert (usage["questions"], usage["embedding_calls"], usage["llm_calls"]) == (1, 1, 1)
    assert usage["input_tokens"] == prompt_words
    assert usage["output_tokens"] == len(ANSWER.split())


def test_an_abstention_without_context_costs_a_question_but_no_model_call(
    client: TestClient,
) -> None:
    sign_in(client, ALICE)
    provider = model(client)

    ask(client)

    usage = today(client)
    assert (usage["questions"], usage["embedding_calls"], usage["llm_calls"]) == (1, 1, 0)
    assert (usage["input_tokens"], usage["output_tokens"]) == (0, 0)
    assert provider.calls == []


def test_a_failing_model_call_is_still_counted(client: TestClient, db_session: Session) -> None:
    alice = sign_in(client, ALICE)
    ready_document(db_session, alice)

    def fail(messages: Sequence[Message]) -> str:
        raise LLMError("el servicio no responde")

    model(client, fail)

    assert ask(client).status_code == 503

    usage = today(client)
    assert (usage["questions"], usage["embedding_calls"], usage["llm_calls"]) == (1, 1, 1)


def test_a_failing_embedding_call_is_still_counted(client: TestClient) -> None:
    from tests.test_questions_api import BrokenProvider  # noqa: PLC0415

    sign_in(client, ALICE)
    model(client)
    client.app.dependency_overrides[get_embedding_provider] = BrokenProvider  # type: ignore[attr-defined]

    assert ask(client).status_code == 503

    usage = today(client)
    assert (usage["questions"], usage["embedding_calls"], usage["llm_calls"]) == (0, 1, 0)


def test_an_invalid_question_consumes_no_quota(client: TestClient) -> None:
    sign_in(client, ALICE)
    provider = model(client)

    assert ask(client, "   ").status_code == 422

    assert today(client)["questions"] == 0
    assert provider.calls == []


def test_exceeding_the_question_quota_is_a_429_and_calls_no_provider(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_quotas(monkeypatch, questions=2)
    alice = sign_in(client, ALICE)
    ready_document(db_session, alice)
    provider = model(client)
    counter = CountingProvider()
    client.app.dependency_overrides[get_embedding_provider] = lambda: counter  # type: ignore[attr-defined]

    assert ask(client).status_code == 200
    assert ask(client).status_code == 200
    calls_before, embeds_before = len(provider.calls), counter.batches

    response = ask(client)

    assert response.status_code == 429
    error = response.json()["error"]
    assert error["code"] == "usage_limit_exceeded"
    assert "2 preguntas" in error["message"]
    assert len(provider.calls) == calls_before
    assert counter.batches == embeds_before
    assert today(client)["questions"] == 2


def test_exceeding_the_token_quota_is_a_429_and_does_not_call_the_model(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_quotas(monkeypatch, tokens=10)
    alice = sign_in(client, ALICE)
    ready_document(db_session, alice)
    provider = model(client)

    assert ask(client).status_code == 200  # el primer consumo rebasa los 10 tokens
    assert len(provider.calls) == 1

    response = ask(client)

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "usage_limit_exceeded"
    assert len(provider.calls) == 1


def test_quotas_are_per_user(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_quotas(monkeypatch, questions=1)
    sign_in(client, ALICE)
    model(client)
    assert ask(client).status_code == 200
    assert ask(client).status_code == 429

    client.post("/api/v1/auth/logout")
    token = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    client.headers["X-CSRF-Token"] = token
    sign_in(client, BOB)

    assert ask(client).status_code == 200


# ── Privacidad ──────────────────────────────────────────────────────────────


def test_only_counters_are_stored_never_the_content(
    client: TestClient, db_session: Session
) -> None:
    alice = sign_in(client, ALICE)
    ready_document(db_session, alice)
    secret = "pregunta-secreta-zorro-violeta"
    model(client, f"{ANSWER} respuesta-secreta-{secret}")

    ask(client, f"{FACTURA} {secret}")

    columns = {c.name for c in UsageDay.__table__.columns}
    assert columns == {
        "owner_id",
        "day",
        "questions",
        "embedding_calls",
        "llm_calls",
        "input_tokens",
        "output_tokens",
    }
    dumped = " ".join(str(row) for row in db_session.execute(text("SELECT * FROM usage_days")))
    assert secret not in dumped
    assert FACTURA not in dumped


# ── GET /usage ──────────────────────────────────────────────────────────────


def test_anonymous_users_cannot_read_usage(client: TestClient) -> None:
    assert client.get(USAGE_URL).status_code == 401


def test_usage_reports_zero_counters_and_the_quotas(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_quotas(monkeypatch, questions=7, tokens=1234)
    sign_in(client, ALICE)

    body = client.get(USAGE_URL).json()

    assert body["day"] == utc_day().isoformat()
    assert (body["questions"], body["llm_calls"], body["input_tokens"]) == (0, 0, 0)
    assert (body["daily_questions"], body["daily_tokens"]) == (7, 1234)


def test_usage_is_per_user(client: TestClient, db_session: Session) -> None:
    sign_in(client, ALICE)
    model(client)
    ask(client)
    assert today(client)["questions"] == 1

    client.post("/api/v1/auth/logout")
    client.headers["X-CSRF-Token"] = client.get("/api/v1/auth/csrf").json()["csrf_token"]
    sign_in(client, BOB)

    assert today(client)["questions"] == 0


def test_deleting_the_user_deletes_the_usage(db_session: Session, user_id: uuid.UUID) -> None:
    record_usage(db_session, user_id, Tally(questions=1), now=NOON)
    assert db_session.scalars(select(UsageDay)).all()

    db_session.delete(db_session.get(User, user_id))
    db_session.flush()
    db_session.expire_all()

    assert db_session.scalars(select(UsageDay)).all() == []
