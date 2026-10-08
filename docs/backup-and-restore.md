# Persistencia, copias de seguridad y restauración

Cómo se guardan los datos de Atlas AI cuando se ejecuta en local y cómo copiarlos y recuperarlos (issue #67).

## Qué datos hay y dónde viven

| Dato | Dónde | Cómo se protege |
| --- | --- | --- |
| Usuarios (hash Argon2id), documentos, fragmentos y vectores | PostgreSQL con pgvector, volumen `atlas_pgdata` | Puerto publicado solo en `127.0.0.1`; el índice HNSW coseno lo crea la migración 0007 |
| Archivos originales subidos | Directorio `STORAGE_DIR` (volumen `atlas_data` en Compose, montado en `/data`) | Directorios `0700` y archivos `0600`; claves opacas `<usuario>/<documento>/original`; la API no sirve el original por HTTP |

Garantías comprobadas por tests: pgvector se habilita en la migración inicial y existe el índice HNSW de coseno
(`test_embedding_store.py`); los archivos se guardan con esos permisos y fuera de la base (`test_storage.py`); un
documento solo se lee con su propietario. **Los datos son privados mientras la máquina lo sea**: no hay cifrado en
disco propio; usa el cifrado de disco del sistema.

## Copia de seguridad

```bash
scripts/backup.sh                      # ./backups/atlas-<fecha UTC>/, conserva las últimas 7
scripts/backup.sh --out /mnt/disco --keep 14
```

Crea una carpeta con `db.dump` (`pg_dump`, formato personalizado), `storage.tar.gz` y `MANIFEST.txt` (revisión del
esquema, recuentos de usuarios/documentos/fragmentos y sha256 de cada archivo). Detecta solo si usar **Compose**
(`docker compose exec`/`run`) o el **modo host** (`pg_dump` local con `DATABASE_URL` y `STORAGE_DIR`); se fuerza con
`ATLAS_MODE=compose|host`. En modo host hacen falta `pg_dump`, `pg_restore` y `psql` del cliente de PostgreSQL
(versión igual o superior a la del servidor).

> **La copia contiene los documentos de los usuarios y los hashes de sus contraseñas, sin cifrar.** Se crea con
> permisos `700`; `backups/` está en `.gitignore`. Guárdala en un disco privado y, si sale de tu equipo, cífrala
> (por ejemplo `age -p` o `gpg -c` sobre una copia comprimida).

### Política de retención

- **Copias**: las 7 últimas por defecto (`--keep`); `--keep 0` no borra nunca. La limpieza solo toca carpetas
  `atlas-*` dentro de `--out`.
- **Frecuencia sugerida**: una al día mientras uses el sistema con datos que te importen, y una antes de cada
  actualización. Para automatizarlo con cron: `0 3 * * * cd /ruta/atlas && scripts/backup.sh >> backups/cron.log 2>&1`.
- **Datos borrados**: un documento que el usuario elimina **seguirá existiendo en las copias** hasta que estas
  caduquen por la rotación. Si lo prometes en una política de privacidad, ajusta `--keep` en consecuencia.
- **Qué no cubre**: una copia en el mismo disco no protege de un fallo del disco. Copia `backups/` a otro medio.

## Restauración

```bash
scripts/restore.sh backups/atlas-20261008T031500Z          # solo describe lo que haría (código de salida 2)
scripts/restore.sh backups/atlas-20261008T031500Z --yes    # lo hace
```

1. Verifica los sha256 del `MANIFEST.txt`: una copia dañada se rechaza **antes** de tocar nada.
2. Hace una copia de lo que hay ahora en `backups/pre-restore-<fecha>` (por si restauras la equivocada).
3. **Sustituye la base de datos** por la de la copia (`pg_restore --clean`) y **aparta** los archivos actuales en
   `storage.before-restore-<fecha>` en vez de borrarlos.
4. Aplica las migraciones pendientes (útil si la copia es de una versión anterior).
5. Compara los recuentos con el manifiesto y ejecuta la reconciliación.
6. En modo Compose para API, worker y frontend antes y los vuelve a arrancar al final.

Si algo no cuadra termina con código 1 y lo explica; los datos previos siguen en `pre-restore-*` y
`storage.before-restore-*`.

### Reconciliación base de datos ↔ archivos

```bash
uv run python -m app.reconcile                  # modo host, desde backend/
docker compose exec api python -m app.reconcile  # Compose
```

Solo lee. Lista los documentos cuyo **original falta** (se puede seguir preguntando y citando, porque fragmentos y
vectores están en la base, pero no se podrán reprocesar) y los **archivos huérfanos** (seguros de borrar a mano).
Código de salida 0 si todo cuadra, 1 si hay diferencias.

### Ensayo

Una copia que nunca se ha restaurado no es una copia. `test_backup_restore.py` ejecuta el ciclo completo (copia →
perder base y archivos → restaurar → comprobar datos y archivo → reconciliar) en modo host contra una base temporal,
y comprueba la protección sin `--yes`, la detección de una copia dañada y la rotación. Ensáyalo tú también con tus
datos antes de fiarte, y anota el tiempo que tarda.

## Límites conocidos

- **Modo Compose sin probar aquí**: el entorno donde se desarrolló no tiene Docker. Usa los mismos comandos
  (`docker compose exec/run`) que el modo host verificado, pero conviene que lo ensayes una vez.
- **Sin copia en caliente consistente**: base y archivos se copian uno tras otro. Si se sube un documento entre
  ambos pasos, la reconciliación lo mostrará. Para una copia limpia, para el worker y la API antes
  (`docker compose stop api ingestion`).
- **Sin restauración a un punto en el tiempo**: solo se recupera el estado de cada copia (RPO = tiempo desde la última).
- **Sin cifrado ni copia remota integrados**: ver la advertencia de arriba.
