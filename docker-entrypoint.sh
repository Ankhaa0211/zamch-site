#!/bin/sh
set -eu

mkdir -p photos static templates data

echo "Running database migrations..."
alembic upgrade head

echo "Starting uvicorn on 0.0.0.0:${PORT:-8000}"
exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
