#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo ">>> Pulling latest code..."
git pull origin main

echo ">>> Building and restarting..."
docker compose up -d --build

echo ">>> Cleaning up unused images..."
docker image prune -f

echo ">>> Done. Service status:"
docker compose ps
