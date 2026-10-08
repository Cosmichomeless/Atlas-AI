# Imagen del backend de Atlas AI: la usan la API y el worker de ingestión (mismo código y migraciones).
#   docker build -t atlas-backend backend/
#   docker run --rm -p 8000:8000 -e DATABASE_URL=... atlas-backend          # API
#   docker run --rm -e DATABASE_URL=... atlas-backend alembic upgrade head   # migraciones
# No lleva secretos ni datos: todo llega por variables de entorno y el volumen de /data.

FROM python:3.12-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11.6 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /srv
# Primero solo las dependencias, para aprovechar la caché de capas al cambiar el código.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --frozen --no-dev

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/srv/.venv/bin:$PATH" \
    STORAGE_DIR=/data/storage \
    APP_PORT=8000
RUN useradd --system --uid 10001 --home-dir /srv --shell /usr/sbin/nologin atlas \
    && mkdir -p /data/storage \
    && chown -R atlas:atlas /data
WORKDIR /srv
COPY --from=builder --chown=atlas:atlas /srv /srv
USER atlas
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=5 \
    CMD ["python", "-c", "import os,urllib.request as u; u.urlopen(f'http://127.0.0.1:{os.environ[\"APP_PORT\"]}/health', timeout=2)"]
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port \"$APP_PORT\""]
