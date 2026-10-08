#!/usr/bin/env bash
# Copia de seguridad de Atlas AI: base de datos (usuarios, documentos, fragmentos y vectores) + archivos subidos.
#
#   scripts/backup.sh [--out DIR] [--keep N]
#
# Crea DIR/atlas-<fecha UTC>/ con db.dump (pg_dump, formato personalizado), storage.tar.gz, y MANIFEST.txt
# (revisión del esquema, recuentos y sha256 de cada archivo). Por defecto DIR=./backups (ignorado por git) y
# se conservan las últimas 7 copias (--keep 0 desactiva la limpieza).
#
# CUIDADO: la copia contiene los documentos de los usuarios y los hashes de sus contraseñas, SIN cifrar.
# Se crea con permisos 700; guárdala en un disco privado y cífrala (p. ej. con `age` o `gpg`) si sale de tu equipo.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

OUT="$ROOT/backups"; KEEP=7
while [ $# -gt 0 ]; do
  case "$1" in
    --out) OUT="$2"; shift 2;;
    --keep) KEEP="$2"; shift 2;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    *) die "Opción desconocida: $1 (usa --help)";;
  esac
done
[[ "$KEEP" =~ ^[0-9]+$ ]] || die "--keep debe ser un número"

detect_mode
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DEST="$OUT/atlas-$STAMP"
umask 077
mkdir -p "$DEST"

echo "Modo: $MODE · destino: $DEST"
db_dump > "$DEST/db.dump"
[ -s "$DEST/db.dump" ] || die "El volcado de la base de datos salió vacío."
storage_pack > "$DEST/storage.tar.gz"
tar -tzf "$DEST/storage.tar.gz" >/dev/null || die "El archivo de almacenamiento no es un tar.gz válido."

read -r USERS DOCS CHUNKS <<<"$(table_counts)"
{
  echo "created_at=$STAMP"
  echo "mode=$MODE"
  echo "alembic_revision=$(alembic_revision)"
  echo "users=$USERS"
  echo "documents=$DOCS"
  echo "chunks=$CHUNKS"
  echo "storage_files=$(tar -tzf "$DEST/storage.tar.gz" | grep -vc '/$' || true)"
  (cd "$DEST" && sha256sum db.dump storage.tar.gz | sed 's/^/sha256 /')
} > "$DEST/MANIFEST.txt"

echo "Copia creada: $USERS usuarios, $DOCS documentos, $CHUNKS fragmentos."

if [ "$KEEP" -gt 0 ]; then
  # Los nombres llevan la fecha: ordenados alfabéticamente, los más antiguos van primero.
  # Sin `mapfile`: el bash 3.2 de macOS no lo tiene.
  ALL=()
  while IFS= read -r dir; do ALL+=("$dir"); done < <(find "$OUT" -maxdepth 1 -type d -name 'atlas-*' | sort)
  EXTRA=$(( ${#ALL[@]} - KEEP ))
  if [ "$EXTRA" -gt 0 ]; then
    for old in "${ALL[@]:0:EXTRA}"; do echo "Eliminando copia antigua: $old"; rm -rf -- "$old"; done
  fi
fi
