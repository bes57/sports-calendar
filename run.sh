#!/usr/bin/env bash
# Run K-Cal locally.
# Usage: ./run.sh
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "Creating venv..."
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip --quiet
  .venv/bin/pip install -r requirements.txt --quiet
fi
if [ ! -f .env ]; then
  echo "Creating .env from .env.example (edit it to configure email/SMS)"
  cp .env.example .env
fi
exec .venv/bin/uvicorn app:app --host 127.0.0.1 --port 8765 --reload
