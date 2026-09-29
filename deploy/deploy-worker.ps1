$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

if (-not (Test-Path "deploy/env.worker")) {
    throw "缺少 deploy/env.worker，请先从 env.worker.example 复制并填写。"
}

$env:APP_VERSION = (git rev-parse --short HEAD).Trim()
$env:APP_BUILD_TIME = Get-Date -Format "yyyy-MM-dd HH:mm:ss K"

docker compose --env-file deploy/env.worker -f deploy/docker-compose.worker.yml up -d --build
docker compose --env-file deploy/env.worker -f deploy/docker-compose.worker.yml ps
Invoke-RestMethod "http://127.0.0.1:5000/health"
