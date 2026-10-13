#!/bin/sh
# Nightly database backup. Usage: DATABASE_URL_PG=postgresql://user:pw@host/db BACKUP_DIR=/backups scripts/backup.sh
# Keeps 14 days. Copy the files off the server too (object storage, another machine): a backup on the same disk is not a backup.
set -eu
: "${DATABASE_URL_PG:?set DATABASE_URL_PG (a plain postgresql:// URL, not postgresql+psycopg2://)}"
DIR="${BACKUP_DIR:-./backups}"; mkdir -p "$DIR"
FILE="$DIR/careersniper-$(date -u +%Y%m%dT%H%M%SZ).sql.gz"
pg_dump --no-owner --no-privileges "$DATABASE_URL_PG" | gzip -9 > "$FILE"
[ -s "$FILE" ] || { echo "backup is empty, failing" >&2; rm -f "$FILE"; exit 1; }
find "$DIR" -name 'careersniper-*.sql.gz' -mtime +14 -delete
echo "wrote $FILE ($(du -h "$FILE" | cut -f1))"
