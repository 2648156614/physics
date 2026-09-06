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

echo "== Version check =="
echo "Git HEAD: $APP_VERSION"
echo -n "Container APP_VERSION: "
docker compose exec -T web printenv APP_VERSION
echo -n "Health: "
curl -fsS http://127.0.0.1/health
echo

echo "== Recent web logs =="
docker compose logs --tail=80 web
