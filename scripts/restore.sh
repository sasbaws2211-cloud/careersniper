#!/bin/sh
# Restore a backup into an EMPTY database. Usage: scripts/restore.sh backups/careersniper-....sql.gz postgresql://user:pw@host/newdb
# Test this on a scratch database regularly: an untested backup is a hope, not a backup.
set -eu
[ $# -eq 2 ] || { echo "usage: $0 <backup.sql.gz> <postgresql://target-db-url>" >&2; exit 2; }
gunzip -c "$1" | psql --set ON_ERROR_STOP=1 "$2"
