#!/bin/bash
set -e

PROJECT_DIR="/root/physics_project"
cd "$PROJECT_DIR"

echo "== Fetching latest code =="
git fetch origin
git reset --hard origin/main

APP_VERSION="$(git rev-parse --short HEAD)"
APP_BUILD_TIME="$(date '+%Y-%m-%d %H:%M:%S %Z')"
export APP_VERSION
export APP_BUILD_TIME

echo "== Deploying version: $APP_VERSION =="
echo "== Build time: $APP_BUILD_TIME =="

docker compose up -d --build --force-recreate web

echo "== Container status =="
docker compose ps

echo "== Waiting for health check =="
HEALTH_BODY=""
for attempt in $(seq 1 30); do
    if HEALTH_BODY="$(curl -fsS http://127.0.0.1/health 2>/dev/null)"; then
        echo "Health check passed on attempt $attempt"
        break
    fi
    echo "Waiting for web container... ($attempt/30)"
    sleep 3
done

if [ -z "$HEALTH_BODY" ]; then
    echo "ERROR: web container did not pass /health within 90 seconds."
    docker compose ps
    docker compose logs --tail=160 web
    exit 1
fi

echo "== Version check =="
echo "Git HEAD: $APP_VERSION"
echo -n "Container APP_VERSION: "
docker compose exec -T web printenv APP_VERSION
echo "Health: $HEALTH_BODY"
echo

if ! echo "$HEALTH_BODY" | grep -q "\"version\":\"$APP_VERSION\""; then
    echo "ERROR: /health version does not match Git HEAD."
    exit 1
fi

echo "== Recent web logs =="
docker compose logs --tail=80 web
