#!/bin/sh
set -e

# Telethon session and SQLite must survive restarts and redeploys.
# Render mounts the persistent disk at /var/data; the app expects
# ./sessions and ./data, so point both at the disk.
DISK="${PERSIST_DIR:-/var/data}"

mkdir -p "$DISK/sessions" "$DISK/data"

rm -rf /app/sessions /app/data
ln -s "$DISK/sessions" /app/sessions
ln -s "$DISK/data" /app/data

exec python -m app.main
