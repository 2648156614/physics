#!/bin/sh
set -eu

PROJECT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$PROJECT_DIR"

test -f deploy/env.primary || {
    echo "缺少 deploy/env.primary，请先从 env.primary.example 复制并填写。"
    exit 1
}

APP_VERSION="$(git rev-parse --short HEAD)"
APP_BUILD_TIME="$(date '+%Y-%m-%d %H:%M:%S %Z')"
export APP_VERSION APP_BUILD_TIME

docker compose --env-file deploy/env.primary -f deploy/docker-compose.primary.yml up -d --build
docker compose --env-file deploy/env.primary -f deploy/docker-compose.primary.yml ps
curl -fsS "http://127.0.0.1:${PRIMARY_WEB_PORT:-5001}/health" || true
