#!/bin/sh
set -eu

mkdir -p photos static templates data

echo "Running database migrations..."
alembic upgrade head

echo "Seeding demo catalog if empty..."
python3 -c "from main import maybe_seed_demo_marketplace; maybe_seed_demo_marketplace()"

echo "Starting uvicorn on 0.0.0.0:${PORT:-8000}"
exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
