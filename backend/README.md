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
- `GET /docs` — Swagger UI, `GET /openapi.json` — OpenAPI schema
