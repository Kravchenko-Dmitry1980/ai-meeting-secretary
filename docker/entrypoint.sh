#!/bin/sh
set -e

# Named volumes mount over image paths and are often root-owned; app runs as appuser.
if [ "$(id -u)" = "0" ]; then
  mkdir -p /app/.cache/huggingface
  chown -R appuser:appuser /app/.cache
  exec runuser -u appuser -g appuser -- "$@"
fi

exec "$@"
