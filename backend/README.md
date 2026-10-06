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
| Typecheck (strict) | `uv run mypy app tests` |
| Tests | `uv run pytest` |

The API listens on <http://127.0.0.1:8000>:

- `GET /health` — liveness probe
- `GET /health/db` — readiness probe: checks the PostgreSQL connection and that `pgvector` is enabled
- `GET /docs` — Swagger UI, `GET /openapi.json` — OpenAPI schema

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

Verify the extension: `curl localhost:8000/health/db` → `{"status":"ok","pgvector_version":"0.8.7"}`.

Tests use the `atlas_test` database (override with `TEST_DATABASE_URL`); start the database before `uv run pytest`.
