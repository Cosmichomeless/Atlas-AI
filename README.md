# Atlas AI — Document Intelligence

Plataforma para subir PDFs, apuntes o documentación y hacer preguntas sobre ellos, obteniendo respuestas
**con citas al contenido original** (RAG: recuperación aumentada por generación). No es un envoltorio de un
LLM: la respuesta se construye solo a partir de fragmentos recuperados de los documentos del usuario.

```
frontend/ (Next.js)  ──HTTP/JSON──▶  backend/ (FastAPI)  ──SQL──▶  PostgreSQL + pgvector
```

Los detalles de arquitectura y el flujo RAG están en [docs/architecture.md](docs/architecture.md).
El enunciado original del proyecto está en [docs/project-brief.md](docs/project-brief.md).
La metodología de evaluación, los resultados reproducibles y sus límites están en
[docs/evaluation.md](docs/evaluation.md).

## Estructura del repositorio

| Ruta | Contenido |
| --- | --- |
| `frontend/` | Aplicación Next.js 16 (App Router, React 19, TypeScript) y su `Dockerfile`. [README](frontend/README.md) |
| `backend/` | API FastAPI (Python 3.12, uv), SQLAlchemy, Alembic. [README](backend/README.md) |
| `infra/postgres/init/` | Script de inicialización de la base de datos (crea `atlas_test`, habilita `vector`) |
| `docker-compose.yml` | PostgreSQL 16 + pgvector para desarrollo local |
| `.env.example` | Variables de entorno documentadas (se copia a `.env`) |
| `docs/` | Arquitectura, [evaluación](docs/evaluation.md) y notas del proyecto |

## Requisitos

- [Docker](https://docs.docker.com/get-docker/) con Compose v2
- Python 3.12+ y [uv](https://docs.astral.sh/uv/)
- Node.js 20.19+ (probado con 22) y npm

## Puesta en marcha desde cero

Todos los comandos se ejecutan desde la raíz del repositorio salvo que se indique lo contrario.

```bash
# 1. Configuración (los valores por defecto sirven para desarrollo; .env no se sube a git)
cp .env.example .env

# 2. Base de datos PostgreSQL + pgvector (puerto 127.0.0.1:5433)
docker compose up -d db
docker compose ps                        # el servicio debe aparecer como "healthy"

# 3. Backend: dependencias, migraciones y API en http://localhost:8000
cd backend
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload     # déjalo corriendo en esta terminal

# 4. Frontend (otra terminal): http://localhost:3000
cd frontend
cp .env.example .env.local               # NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
npm install
npm run dev
```

### Comprobar que todo funciona

```bash
curl localhost:8000/api/v1/health        # {"status":"ok"}
curl localhost:8000/api/v1/health/db     # {"status":"ok","pgvector_version":"…"}
```

Documentación interactiva de la API: <http://localhost:8000/docs>.

### Puertos

| Servicio | Puerto | Cómo cambiarlo |
| --- | --- | --- |
| PostgreSQL | 5433 | `POSTGRES_PORT` y `DATABASE_URL` en `.env` |
| API | 8000 | `--port` de uvicorn (y `NEXT_PUBLIC_API_BASE_URL`, `FRONTEND_ORIGIN`) |
| Frontend | 3000 | `PORT=3100 npm run dev` (y `FRONTEND_ORIGIN` en `.env`) |

## Comprobaciones de calidad

```bash
# backend/ (los tests usan la base atlas_test; la base de datos debe estar levantada)
uv run ruff check . && uv run ruff format --check . && uv run mypy app tests migrations && uv run pytest

# frontend/
npm run lint && npm run typecheck && npm test && npm run build
```

## Contrato de la API

El backend publica su contrato en `backend/openapi.json` (versionado en git) y el frontend genera tipos
a partir de él. Tras cambiar endpoints o esquemas:

```bash
cd backend  && uv run python -m app.openapi_export
cd frontend && npm run api:types
```

## Configuración y secretos

Toda la configuración se lee de variables de entorno (o `.env`). Por defecto los proveedores de IA son
`fake` (deterministas y sin red), de modo que el proyecto funciona sin claves. Para usar un proveedor real,
define `EMBEDDING_PROVIDER=openai` / `LLM_PROVIDER=openai` y `OPENAI_API_KEY` en tu `.env`. En producción
`SECRET_KEY` es obligatoria (≥ 32 caracteres). Nunca subas `.env` ni claves al repositorio.

## Detener y reiniciar

```bash
docker compose down        # detiene la base de datos (los datos se conservan)
docker compose down -v     # además borra los datos
```
