#!/usr/bin/env bash
# Restaura una copia creada con scripts/backup.sh.
#
#   scripts/restore.sh BACKUP_DIR --yes
#
# DESTRUCTIVO para la base de datos: la sustituye por la de la copia (pg_restore --clean). Por eso exige --yes y,
# antes de tocar nada, hace una copia de lo que hay ahora en backups/pre-restore-<fecha>. Los archivos subidos
# actuales no se borran: se apartan en storage.before-restore-<fecha>.
# Después aplica las migraciones pendientes, comprueba los recuentos contra MANIFEST.txt y ejecuta la
# reconciliación (python -m app.reconcile), cuyo resultado se muestra pero no detiene el proceso.
# En modo compose para y vuelve a arrancar la API y el worker.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

BACKUP=""; CONFIRM=0
while [ $# -gt 0 ]; do
  case "$1" in
    --yes) CONFIRM=1; shift;;
    -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    -*) die "Opción desconocida: $1 (usa --help)";;
    *) [ -z "$BACKUP" ] || die "Solo se admite una copia"; BACKUP="$1"; shift;;
  esac
done
[ -n "$BACKUP" ] || die "Indica la carpeta de la copia: scripts/restore.sh backups/atlas-<fecha> --yes"
[ -f "$BACKUP/MANIFEST.txt" ] && [ -f "$BACKUP/db.dump" ] && [ -f "$BACKUP/storage.tar.gz" ] \
  || die "'$BACKUP' no parece una copia de Atlas AI (faltan MANIFEST.txt, db.dump o storage.tar.gz)."

detect_mode
manifest() { grep -E "^$1=" "$BACKUP/MANIFEST.txt" | head -n1 | cut -d= -f2-; }

echo "Verificando la integridad de la copia…"
(cd "$BACKUP" && grep '^sha256 ' MANIFEST.txt | sed 's/^sha256 //' | sha256sum -c --quiet -) \
  || die "La copia está dañada: los sha256 no coinciden con MANIFEST.txt."

if [ "$CONFIRM" -ne 1 ]; then
  echo "Se restauraría '$BACKUP' (creada $(manifest created_at); $(manifest users) usuarios, $(manifest documents) documentos)"
  echo "en modo $MODE, SUSTITUYENDO la base de datos actual. Vuelve a ejecutar con --yes para continuar."
  exit 2
fi

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
if [ "$MODE" = compose ]; then compose stop api ingestion frontend >/dev/null; fi
echo "Copia de seguridad previa en backups/pre-restore-$STAMP"
"$(dirname "${BASH_SOURCE[0]}")/backup.sh" --out "$ROOT/backups/pre-restore-$STAMP" --keep 0 >/dev/null

echo "Restaurando la base de datos…"
db_restore < "$BACKUP/db.dump"
echo "Restaurando los archivos…"
storage_unpack "$STAMP" < "$BACKUP/storage.tar.gz"
echo "Aplicando migraciones pendientes…"
run_migrations >/dev/null

read -r USERS DOCS CHUNKS <<<"$(table_counts)"
echo "Tras restaurar: $USERS usuarios, $DOCS documentos, $CHUNKS fragmentos."
STATUS=0
for pair in "users:$USERS" "documents:$DOCS" "chunks:$CHUNKS"; do
  key="${pair%%:*}"; got="${pair#*:}"; want="$(manifest "$key")"
  if [ "$got" != "$want" ]; then echo "ATENCIÓN: $key = $got, la copia tenía $want" >&2; STATUS=1; fi
done

echo "Reconciliación base de datos ↔ archivos:"
run_reconcile || echo "(hay diferencias o no se pudo comprobar; revisa el informe de arriba)"

if [ "$MODE" = compose ]; then compose up -d api ingestion frontend >/dev/null; fi
[ "$STATUS" -eq 0 ] && echo "Restauración completada." || { echo "Restauración terminada con avisos." >&2; exit 1; }
