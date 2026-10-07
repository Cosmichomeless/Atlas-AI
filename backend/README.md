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

All endpoints require a session (`401 unauthorized` otherwise) and only ever see the caller's documents.

- `GET /api/v1/documents?limit=20&offset=0&status=READY` — `{items, total, limit, offset}`, newest first
  (ties broken by id so pages never overlap). `limit` is 1–100, `offset` >= 0, `status` optional; invalid
  values return `422`. Items carry `id, filename, content_type, size_bytes, status, created_at, updated_at`.
- `GET /api/v1/documents/{id}` — the same plus `error_summary`, `attempts`, `processing_started_at` and
  `processed_at`. A missing id and another user's id return the identical `404 document_not_found`, so
  nothing reveals whether a document exists. Owner and worker lease are never exposed.
- `POST /api/v1/documents` — multipart upload (`file` field) of a PDF, TXT or Markdown file; creates a
  `UPLOADED` document and returns `201` with the detail. The type is deduced from the extension
  (`.pdf`, `.txt`, `.md`, `.markdown`) and checked against the real content (`%PDF-` header, or UTF-8
  text without NUL bytes); the client's `Content-Type` is ignored. Errors: `415 unsupported_media_type`,
  `422 empty_file` / `invalid_filename`, `413 file_too_large` (limit `MAX_UPLOAD_MB`, also checked early
  against `Content-Length`). A rejected upload leaves no database row and no file; if saving the row
  fails, the stored file is removed. The filename is sanitized metadata only.
- `DELETE /api/v1/documents/{id}` — removes the document with its stored file, chunks and vectors and
  returns `204`. Another user's id and a missing id both return `404 document_not_found`, and nothing is
  touched. While a worker holds the document (`PROCESSING` with a live lease) it answers
  `409 document_processing`; retry once it finishes. A `PROCESSING` document whose lease expired (dead
  worker) can be deleted. The file goes first (a failure keeps the document so the delete can be retried, and
  no unreachable file is left behind); chunks and vectors disappear with the row (`ON DELETE CASCADE`). If a
  worker's document vanishes mid-processing, the worker logs it and moves on.

### Ingestion worker

Uploading never waits for processing: the API only stores the file and leaves the document
`UPLOADED`. A separate process does the work:

```bash
uv run python -m app.ingestion.worker   # from backend/; run as many as you like
```

- The `documents` table is the queue. A worker claims the oldest pending document with
  `FOR UPDATE SKIP LOCKED` (workers never collide), marks it `PROCESSING` with a lease
  (`INGESTION_LEASE_SECONDS`) and commits the claim before doing any work.
- If a worker dies mid-document nobody renews the lease; once it expires another worker takes the
  document over and retries it. After `INGESTION_MAX_ATTEMPTS` attempts it ends `FAILED` with a
  readable cause instead of looping forever.
- A document that cannot be processed (no text, corrupt, encrypted) ends `FAILED` with its cause; an
  unexpected error puts it back to `UPLOADED` for another try (or `FAILED` once attempts run out).
- Extraction, chunking and indexing happen in **one transaction**: the worker embeds every chunk with the
  configured provider (`get_embedding_provider()`, batched) and only then marks the document `READY`, so a
  `READY` document always has all its chunks indexed. If the provider fails midway nothing is kept (no half-written
  vectors, previous chunks and vectors survive a failed reindex) and the document goes back to `UPLOADED` for
  another try, or `FAILED` once attempts run out. Reindexing replaces vectors, never duplicates them. A vector
  dimension the schema cannot hold (`EMBEDDING_DIMENSIONS` ≠ 1536) is a configuration error: the document fails
  immediately with a message naming the setting, without calling the provider. Error summaries never include the
  provider's message.
- `SIGINT`/`SIGTERM` finish the current document and exit. When the queue is empty the worker sleeps
  `INGESTION_POLL_SECONDS`.

### Text extraction

`app/features/documents/extraction.py` turns a stored original into `ExtractedBlock`s, each carrying its
`document_id` and where the text came from:

- PDF (`pypdf`): one block per page with text, `page` starting at 1 (pages without text are skipped but
  keep their real number).
- TXT: one block per paragraph, with `start_line`/`end_line`. Markdown: the same plus `section`, the heading
  path (`Guide > Install`); headings inside code fences are ignored and a lone heading joins the paragraph
  that follows.
- When there is nothing to extract, `ExtractionError` carries a user-readable cause (scanned/image-only PDF,
  password-protected or damaged PDF, empty or non-UTF-8 text). `extract_or_fail(document, storage)` stores that
  cause in `error_summary` and moves the `PROCESSING` document to `FAILED`; the worker owns the commit.

### Chunking policy

`app/ingestion/chunking.py` turns the extracted blocks into the fragments that get embedded
(`chunk_blocks(blocks, ChunkPolicy)`). It is pure and deterministic: the same input and policy always
give the same chunks. Configuration (in characters, validated at startup):

| Setting | Default | Why |
|---|---|---|
| `CHUNK_SIZE_CHARS` | 1000 (min 100) | ~200-250 tokens: far below any embedding model limit, short enough to cover one idea (precise retrieval) yet long enough to give the LLM useful context |
| `CHUNK_OVERLAP_CHARS` | 150 (max half the size) | 15 %: a sentence cut at a boundary appears whole in one of the two chunks at little extra storage; capping it at half the size guarantees each chunk adds new text and the split always advances |

- A chunk never crosses a PDF page or a Markdown section, so its citation (`page`, `section`,
  `start_line`/`end_line`) is exact. Consecutive paragraphs of the same page/section are merged.
- Cuts prefer, in order: paragraph end, line end, sentence end, word boundary; only text without
  spaces is cut mid-word.
- No empty chunks are emitted and `ordinal` follows document order (0, 1, 2…).

### Reindexing

Changing the embedding model, its adapter `version`, the chunk size/overlap or the chunker itself
(`CHUNKER_VERSION` in `app/ingestion/chunking.py`) leaves earlier documents indexed with something else.
Each `READY` document remembers how it was indexed (`documents.index_embedding` = `EmbeddingSpec.key`,
`documents.index_chunking` = `ChunkPolicy.key`; both are cleared in any other state and are never exposed by the
API). A `READY` document whose fingerprint differs from the current configuration is **stale**, and so is one with
no fingerprint (it predates the column).

```bash
uv run python -m app.ingestion.reindex --dry-run   # from backend/: counts only, changes nothing
uv run python -m app.ingestion.reindex             # queues the stale documents; the worker does the rest
```

- Reindexing never rewrites vectors itself: it moves stale documents `READY → UPLOADED`, resets `attempts` (they
  belong to the previous pass) and clears the error. The worker then reprocesses them like any upload.
- Old and new are never mixed: `replace_chunks` swaps chunks and vectors in the same transaction that sets
  `READY`, so a document always has one complete index of one version. If the provider fails midway the previous
  index survives untouched and the document retries. Retrieval only compares vectors of the query's spec, so during
  a partial rollout migrated documents answer with the new model and the rest wait their turn.
- Idempotent: queued documents are no longer `READY`, so a second run finds nothing (`0 documents queued`) and a
  finished run leaves no duplicate chunks or vectors. `FAILED` and already queued documents are left alone;
  documents locked by another process (being deleted) are skipped and picked up by the next run.
- `index_coverage()` returns how many documents are current, stale, pending (`UPLOADED`/`PROCESSING`) and failed;
  `request_reindex(owner_id=...)` can be limited to one owner.

### Persisted chunks

`app/ingestion/chunk_store.py` stores the chunks in `document_chunks`; `replace_chunks(session, document_id,
chunks)` is the only writer.

- Each row keeps its provenance: `document_id` (FK, `ON DELETE CASCADE`), `ordinal`, `text`, and the optional
  `page`, `section`, `start_line`/`end_line` copied from the extracted blocks, so any chunk can be traced back to
  the original (`documents.storage_key`). `CHECK`s reject blank text, negative ordinals, `page < 1` and
  half-filled or inverted line ranges; `(document_id, ordinal)` is unique.
- Reprocessing replaces: the worker deletes the document's old chunks and inserts the new ones in the same
  transaction that sets the final status, so there are never duplicates or a mix of old and new content. If
  anything fails the transaction rolls back and the previous chunks stay. A document that ends `FAILED`
  (no text any more) keeps no stale chunks.
- The owner is not copied into the chunks: retrieval joins `documents` and filters by `owner_id`.

### Embedding provider

`app/embeddings` hides the embedding service behind `EmbeddingProvider` (`embed(texts) -> list[Embedding]`),
chosen by `EMBEDDING_PROVIDER` through `get_embedding_provider()`:

- `fake` (default, also used by the tests): deterministic hashed bag-of-words vectors of unit length; the same
  text always gives the same vector and texts sharing words land closer, so retrieval can be tested without any
  paid call.
- `openai`: `POST {OPENAI_BASE_URL}/embeddings` with `OPENAI_API_KEY` (`dimensions` is sent only to
  `text-embedding-3-*`). Errors become `EmbeddingError` without the key or the text; tests use a mocked transport.
- Every `Embedding` carries its `EmbeddingSpec`: provider, model, dimension and `version`
  (`spec.key` → `openai/text-embedding-3-small/1536/v1`). The version belongs to the adapter and is bumped when
  its output stops being comparable with older vectors; vectors from different specs must never be compared,
  so whoever stores a vector stores its spec with it.
- `embed` always validates: no blank texts, one vector per text, the configured dimension and finite values.
  Large inputs are sent in batches of `max_batch_size`, keeping order.

### Vector storage

`app/embeddings/store.py` persists vectors in `chunk_embeddings` (pgvector) next to the chunk they describe:

- One row per `(chunk_id, provider, model, dimensions, version)`; the vector is stored with its spec, so
  vectors from several specs can coexist while a reindex is in progress and are never compared with each other.
- The column is a fixed `vector(1536)` (`VECTOR_DIMENSIONS`), which lets Postgres build an HNSW cosine index.
  The dimension is validated on persist: `save_embeddings` raises `EmbeddingError` before writing when a
  vector does not fit (change `EMBEDDING_DIMENSIONS` or migrate the column and reindex), and the database
  backs it with `CHECK (dimensions = vector_dims(embedding))`.
- `save_embeddings(session, chunks, embeddings)` replaces the rows of the same chunks and spec, so saving
  twice never duplicates. Rows are deleted with their chunk or document (`ON DELETE CASCADE`).
- `nearest_chunks(session, query, owner_id=..., limit=5)` returns `SimilarChunk(chunk, document, distance)`
  ordered by cosine distance, restricted to the owner's documents and to the query's spec; page, section and
  lines of the source stay reachable through `chunk`.

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
- `document_chunks` — UUID id, `document_id` (`NOT NULL`, FK to `documents`, `ON DELETE CASCADE`, indexed),
  `ordinal` (unique per document), `text`, optional `page`, `section`, `start_line`, `end_line`, `created_at`.
- `chunk_embeddings` — UUID id, `chunk_id` (`NOT NULL`, FK to `document_chunks`, `ON DELETE CASCADE`), the
  spec (`provider`, `model`, `dimensions`, `version`), `embedding vector(1536)` with an HNSW cosine index,
  `created_at`. Unique per chunk and spec.
- Always query documents through `app.features.documents.queries` (`list_owned_page`, `get_owned`): they
  require the owner id, and a foreign or missing id both return `None`.

Tests get an isolated session (`db_session` fixture) on `atlas_test` migrated to `head`; each test is
rolled back.

Verify the extension: `curl localhost:8000/api/v1/health/db` → `{"status":"ok","pgvector_version":"0.8.7"}`.

Tests use the `atlas_test` database (override with `TEST_DATABASE_URL`); start the database before `uv run pytest`.
