# Atlas AI — backend

FastAPI service. Code is organised by feature under `app/features/<feature>/`.

## Requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/)

## Commands

Run from `backend/`:

| Task | Command |
|---|---|
| Install dependencies | `uv sync` |
| Start the API (reload) | `uv run uvicorn app.main:app --reload` |
| Lint | `uv run ruff check .` |
| Check formatting | `uv run ruff format --check .` |
| Typecheck (strict) | `uv run mypy app tests migrations` |
| Tests | `uv run pytest` |

The API listens on <http://127.0.0.1:8000>:

- `GET /health` — liveness probe (outside the versioned contract, for orchestrators)
- `GET /api/v1/health` — health endpoint of the versioned API
- `GET /api/v1/health/db` — readiness probe: checks the PostgreSQL connection and that `pgvector` is enabled
- `GET /docs` — Swagger UI, `GET /openapi.json` — OpenAPI schema

## API contract

All business endpoints live under `/api/v1` (`app/api/v1/router.py`). Every error uses one format:

```json
{"error": {"code": "not_found", "message": "Recurso no encontrado.", "request_id": "…", "details": null}}
```

- `code` is a stable machine-readable string; `message` is user-facing (Spanish); `details` lists
  `{loc, message, type}` per field on `422` validation errors.
- Raise `AppError(status, code, message)` from `app.api.errors`; declare it in OpenAPI with
  `responses=error_responses(404, ...)`. Unhandled exceptions return `500` without leaking internals.
- Every response carries `X-Request-ID` (the client's value is kept if it matches `[A-Za-z0-9._-]{1,64}`).
- CORS only allows `FRONTEND_ORIGIN`, with credentials.

The contract is exported to `backend/openapi.json` and committed. After changing endpoints or schemas,
regenerate it (a test fails if it drifts) and then the frontend types:

```bash
uv run python -m app.openapi_export        # from backend/
npm run api:types                          # from frontend/
```

## Accounts

`POST /api/v1/auth/register` (`{email, password}`) creates an account and returns `{id, email, created_at}`
— never the hash. Emails are trimmed and lowercased; passwords must be 10–128 characters and are stored
as Argon2id hashes (`app/core/security.py`). A repeated email returns `409 email_already_registered`;
invalid data returns `422` with per-field `details` (the submitted password is never echoed back).

### Sessions

`POST /api/v1/auth/login` validates credentials and creates a server-side session (`sessions` table; only
the SHA-256 of the random token is stored). The token travels in the `atlas_session` cookie (HttpOnly,
SameSite=Lax, Secure in production). `POST /api/v1/auth/logout` deletes the session and clears the cookie;
`GET /api/v1/auth/me` returns the current user. Sessions last `SESSION_TTL_HOURS` (default 168).

Protect a route with the `CurrentUser` dependency (`app.features.auth.dependencies`): anonymous or invalid
sessions get `401 unauthorized`. A wrong password and an unknown email return the same
`401 invalid_credentials`.

### Cookies, CSRF and CORS

Cookies (`atlas_session`, `atlas_csrf`) are always HttpOnly. `COOKIE_SAMESITE` (`lax` default, `strict`,
`none`) and `COOKIE_SECURE` (empty = Secure only when `APP_ENV=production`) control the other attributes;
`none` requires Secure, and production refuses non-Secure cookies.

Every mutating request (POST/PUT/PATCH/DELETE under `/api/v1`, login and logout included) must pass
`app/api/csrf.py`, applied once on the API router so new routes are protected by default:

1. `GET /api/v1/auth/csrf` returns `{csrf_token}` and sets it in the HttpOnly `atlas_csrf` cookie (signed
   double-submit: `nonce.HMAC(SECRET_KEY, nonce)`; without `SECRET_KEY` outside production a random
   per-process key is used).
2. The client sends the token in `X-CSRF-Token`. It must equal the cookie and carry a valid signature,
   otherwise `403 csrf_failed` (checked before body validation).
3. If the browser sends `Origin`, it must be exactly `FRONTEND_ORIGIN` (otherwise `403 csrf_failed`).

CORS only allows `FRONTEND_ORIGIN` with credentials (headers `Content-Type`, `X-CSRF-Token`,
`X-Request-ID`). Tests use the `client` fixture (already sends the token) or `raw_client` (does not).

## Documents

Both endpoints require a session (`401 unauthorized` otherwise) and only ever see the caller's documents.

- `GET /api/v1/documents?limit=20&offset=0&status=READY` — `{items, total, limit, offset}`, newest first
  (ties broken by id so pages never overlap). `limit` is 1–100, `offset` >= 0, `status` optional; invalid
  values return `422`. Items carry `id, filename, content_type, size_bytes, status, created_at, updated_at`.
- `GET /api/v1/documents/{id}` — the same plus `error_summary`, `attempts`, `processing_started_at` and
  `processed_at`. A missing id and another user's id return the identical `404 document_not_found`, so
  nothing reveals whether a document exists. Owner and worker lease are never exposed.

### File storage

Uploaded files never live in PostgreSQL: the `documents` table keeps only a reference
(`storage_key`, unique) and the bytes go to `STORAGE_DIR` (git-ignored, never served statically).
`app/features/documents/storage.py` is the single boundary:

- `FileStorage` is a protocol (`save`, `open`, `exists`, `delete`); `LocalFileStorage` is the disk
  implementation and `get_storage()` is the FastAPI dependency, replaced by a temp directory in tests.
- Keys are opaque and app-generated: `<owner_id>/<document_id>/original` (derivatives such as extracted
  text use another last segment). Users' filenames are metadata only and never become part of a path.
- Every key is validated (segments of `[A-Za-z0-9._-]`, no leading dot, no empty/`..`/absolute/NUL/
  backslash segments) and every resolved path must stay inside the root, symlinks included.
- Writes are atomic (temp file + rename), can enforce a size limit and leave nothing behind on failure.
  Directories are `0700` and files `0600`.

## Configuration

Settings (`app/core/config.py`) come from environment variables, then from a `.env` file at the repository
root, then from defaults. Copy `.env.example` to `.env` to start; `.env` is ignored by git, so real secrets
never reach the repository. The example documents the API, database, storage and AI provider variables.

- `EMBEDDING_PROVIDER` / `LLM_PROVIDER` default to `fake` (deterministic, no network). Setting either to
  `openai` requires `OPENAI_API_KEY`.
- `APP_ENV=production` requires `SECRET_KEY` (>= 32 characters) and Secure cookies.
- Secrets are `SecretStr`: they are masked in `repr()` and logs.
- The test suite forces `APP_ENV=test` and `fake` providers regardless of `.env`.

## Database

PostgreSQL 16 with [pgvector](https://github.com/pgvector/pgvector) runs locally through Docker Compose
(from the repository root):

```bash
docker compose up -d db     # starts PostgreSQL on 127.0.0.1:5433
docker compose down         # stops it (data is kept in the `atlas_pgdata` volume)
docker compose down -v      # stops it and deletes the data
```

The first start creates the `atlas` and `atlas_test` databases and enables the `vector` extension in both
(`infra/postgres/init/01-init.sh`). The backend reads its connection from the environment:

```bash
export DATABASE_URL="postgresql+psycopg://atlas:atlas_dev_password@localhost:5433/atlas"
```

### Migrations (Alembic)

The schema is versioned with Alembic (`migrations/`). The URL comes from `DATABASE_URL`.

| Task | Command |
|---|---|
| Apply all migrations (idempotent) | `uv run alembic upgrade head` |
| Current revision | `uv run alembic current` |
| New migration | `uv run alembic revision --autogenerate -m "message"` |
| Roll back one revision | `uv run alembic downgrade -1` |
| Detect model/schema drift | `uv run alembic check` |

An empty database is created by running `uv run alembic upgrade head`; running it again changes nothing.
Models inherit from `app.core.base.Base` (deterministic constraint naming) and must be imported in
`migrations/env.py` so autogenerate sees them.

### Data model

- `users` — UUID id, unique lowercase `email` (a `CHECK` makes uniqueness case-insensitive), `password_hash`,
  `created_at`. Emails must be normalised to lowercase before insert.
- `documents` — UUID id, `owner_id` (`NOT NULL`, FK to `users`, `ON DELETE CASCADE`), metadata (`filename`,
  `content_type`, `size_bytes >= 0`), ingestion `status`, `attempts`, `error_summary` (max 500 chars), and the
  dates `created_at`, `updated_at`, `processing_started_at`, `processed_at`, plus `lease_expires_at` for the
  worker lease. `status` is `UPLOADED | PROCESSING | READY | FAILED` (varchar + `CHECK`, indexed).

  Lifecycle (`app/features/documents/states.py`): `UPLOADED → PROCESSING → READY`, `PROCESSING → FAILED`, and
  back to `UPLOADED` from `READY` (reindex), `FAILED` (retry) or `PROCESSING` (worker crash recovery). Anything
  else raises `InvalidStatusTransitionError`, also when assigning `document.status` directly, and a document
  can only be born `UPLOADED`. Use `document.transition_to(status, error_summary=..., lease_expires_at=...)`:
  it records dates, increments `attempts` on `PROCESSING`, requires an error summary for `FAILED` and clears
  the lease when leaving `PROCESSING`.
- Always query documents through `app.features.documents.queries` (`list_owned_page`, `get_owned`): they
  require the owner id, and a foreign or missing id both return `None`.

Tests get an isolated session (`db_session` fixture) on `atlas_test` migrated to `head`; each test is
rolled back.

Verify the extension: `curl localhost:8000/api/v1/health/db` → `{"status":"ok","pgvector_version":"0.8.7"}`.

Tests use the `atlas_test` database (override with `TEST_DATABASE_URL`); start the database before `uv run pytest`.
