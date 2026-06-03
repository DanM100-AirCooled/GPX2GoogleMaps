#!/usr/bin/env bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ ! -f "$SCRIPT_DIR/.env" ]; then
  echo "⚠  No .env found. Copying .env.example → .env"
  cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
  echo "   Edit .env with your UPS credentials before syncing data."
fi

cd "$SCRIPT_DIR/backend"
python app.py
