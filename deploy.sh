#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

echo ">>> Pulling latest code..."
git pull origin main

echo ">>> Building image..."
docker compose build

echo ">>> Recreating container if image changed..."
docker compose up -d

echo ">>> Cleaning up unused images..."
docker image prune -f

echo ">>> Done. Service status:"
docker compose ps
