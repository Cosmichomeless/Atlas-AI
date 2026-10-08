# Utilidades comunes de backup.sh y restore.sh (se cargan con `source`; no se ejecuta solo).
#
# Dos modos, elegidos con ATLAS_MODE=compose|host (por defecto: compose si el servicio `db` de Docker
# Compose está en marcha, y host en caso contrario):
#   compose  usa `docker compose exec/run` sobre los contenedores del proyecto.
#   host     usa pg_dump/pg_restore/psql del equipo y las variables DATABASE_URL y STORAGE_DIR.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Lee KEY de `.env` (sin ejecutarlo); el entorno del proceso tiene prioridad.
envval() {
  local key="$1" fallback="${2:-}"
  if [ -n "${!key:-}" ]; then printf '%s' "${!key}"; return; fi
  local line
  line="$(grep -E "^${key}=" "$ROOT/.env" 2>/dev/null | tail -n1 || true)"
  if [ -n "$line" ]; then
    line="${line#*=}"; line="${line%%#*}"; printf '%s' "$(printf '%s' "$line" | sed -E 's/[[:space:]]+$//')"
  else
    printf '%s' "$fallback"
  fi
}

die() { echo "ERROR: $*" >&2; exit 1; }

detect_mode() {
  if [ -n "${ATLAS_MODE:-}" ]; then MODE="$ATLAS_MODE"
  elif command -v docker >/dev/null 2>&1 && [ -n "$(cd "$ROOT" && docker compose ps -q db 2>/dev/null)" ]; then MODE=compose
  else MODE=host; fi
  case "$MODE" in compose|host) ;; *) die "ATLAS_MODE debe ser compose o host (es '$MODE')";; esac

  if [ "$MODE" = host ]; then
    DB_URL="$(envval DATABASE_URL)"
    [ -n "$DB_URL" ] || die "Falta DATABASE_URL (en el entorno o en .env)."
    PGURL="${DB_URL/postgresql+psycopg:/postgresql:}"          # psql/pg_dump no entienden el driver
    STORAGE="${ATLAS_STORAGE_DIR:-$(envval STORAGE_DIR storage)}"
    case "$STORAGE" in /*) ;; *) STORAGE="$ROOT/backend/$STORAGE";; esac   # relativa al backend
    PY=(${ATLAS_PYTHON:-uv run python})
    command -v pg_dump >/dev/null && command -v pg_restore >/dev/null && command -v psql >/dev/null \
      || die "Hacen falta pg_dump, pg_restore y psql (cliente de PostgreSQL) en modo host."
  else
    command -v docker >/dev/null || die "Modo compose: docker no está instalado."
  fi
}

compose() { (cd "$ROOT" && docker compose "$@"); }

db_dump() {   # volcado en formato personalizado a stdout
  if [ "$MODE" = compose ]; then
    compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" --format=custom --no-owner "$POSTGRES_DB"'
  else pg_dump --format=custom --no-owner --dbname "$PGURL"; fi
}

db_restore() {  # lee un volcado de stdin
  if [ "$MODE" = compose ]; then
    compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" --clean --if-exists --no-owner --exit-on-error -d "$POSTGRES_DB"'
  else pg_restore --clean --if-exists --no-owner --exit-on-error --dbname "$PGURL"; fi
}

db_scalar() {   # una consulta que devuelve un valor
  if [ "$MODE" = compose ]; then
    printf '%s\n' "$1" | compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tA'
  else psql "$PGURL" -tA -c "$1"; fi
}

storage_pack() {  # tar.gz del almacenamiento a stdout
  if [ "$MODE" = compose ]; then
    compose run --rm --no-deps -T api sh -c 'mkdir -p /data/storage && tar -C /data -czf - storage'
  else
    mkdir -p "$STORAGE"
    tar -C "$(dirname "$STORAGE")" -czf - "$(basename "$STORAGE")"
  fi
}

storage_unpack() {  # lee un tar.gz de stdin; aparta el almacenamiento actual, no lo borra
  local stamp="$1"
  if [ "$MODE" = compose ]; then
    compose run --rm --no-deps -T api sh -c \
      "[ -d /data/storage ] && mv /data/storage /data/storage.before-restore-$stamp; tar -C /data -xzf -"
  else
    local parent base; parent="$(dirname "$STORAGE")"; base="$(basename "$STORAGE")"
    [ -d "$STORAGE" ] && mv "$STORAGE" "$parent/$base.before-restore-$stamp"
    mkdir -p "$parent"; tar -C "$parent" -xzf -
  fi
}

table_counts() {  # "usuarios documentos fragmentos"; "0 0 0" si la base está vacía o sin esquema
  db_scalar "SELECT (SELECT count(*) FROM users) || ' ' || (SELECT count(*) FROM documents) || ' ' || (SELECT count(*) FROM document_chunks)" 2>/dev/null || echo "0 0 0"
}

alembic_revision() {  # "none" si la base no tiene esquema
  db_scalar "SELECT version_num FROM alembic_version" 2>/dev/null | head -n1 | grep . || echo none
}

run_migrations() {
  if [ "$MODE" = compose ]; then compose run --rm --no-deps -T api alembic upgrade head
  else (cd "$ROOT/backend" && DATABASE_URL="$DB_URL" "${PY[@]}" -m alembic upgrade head); fi
}

run_reconcile() {
  if [ "$MODE" = compose ]; then compose run --rm --no-deps -T api python -m app.reconcile
  else (cd "$ROOT/backend" && DATABASE_URL="$DB_URL" STORAGE_DIR="$STORAGE" "${PY[@]}" -m app.reconcile); fi
}
