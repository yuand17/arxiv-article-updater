#!/bin/sh
set -eu

backup_dir=${1:-./backups}
mkdir -p "$backup_dir"
stamp=$(date -u +%Y%m%dT%H%M%SZ)
output="$backup_dir/arxiv_updater_$stamp.sql.gz"
docker compose exec -T db pg_dump -U arxiv -d arxiv_updater | gzip > "$output"
echo "$output"
