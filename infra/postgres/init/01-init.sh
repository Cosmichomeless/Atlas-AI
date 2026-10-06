#!/usr/bin/env bash
# Se ejecuta solo la primera vez que se inicializa el volumen de datos.
# Crea la base de pruebas y habilita pgvector en ambas bases.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<SQL
CREATE DATABASE "${POSTGRES_TEST_DB}";
SQL

for db in "$POSTGRES_DB" "$POSTGRES_TEST_DB"; do
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$db" \
    -c "CREATE EXTENSION IF NOT EXISTS vector;"
done
