#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  echo "usage: scripts/restore.sh BACKUP.sql.gz" >&2
  exit 2
fi
if [ ! -f "$1" ]; then
  echo "backup not found: $1" >&2
  exit 2
fi

echo "Restore replaces all data in the arxiv_updater database. Type RESTORE to continue:"
read confirmation
if [ "$confirmation" != "RESTORE" ]; then
  echo "cancelled"
  exit 1
fi

docker compose stop web worker
docker compose exec -T db dropdb -U arxiv --if-exists arxiv_updater
docker compose exec -T db createdb -U arxiv -O arxiv arxiv_updater
gzip -dc "$1" | docker compose exec -T db psql -v ON_ERROR_STOP=1 -U arxiv -d arxiv_updater
docker compose run --rm migrate
docker compose up -d web worker
