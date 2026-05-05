# Project Launch Guide

## 1. Structure

Detected in project root:

- `app/` (backend FastAPI code)
- `alembic/` (migrations)
- `docker/` (`Dockerfile.api`, `Dockerfile.worker`)
- `frontend/` (Vite React app)
- `docker-compose.yml`
- `.env`, `.env.example`
- `tests/`

Not found:

- `run.ps1`
- `Makefile`

## 2. Prerequisites

PowerShell commands to check tools:

```powershell
docker --version
docker compose version
node --version
npm --version
```

Environment file:

```powershell
Test-Path .env
```

If `.env` is missing:

```powershell
Copy-Item .env.example .env
```

## 3. Backend Launch (REAL commands)

From project root (`C:\Dima\Projects\CURSOR\1t`):

```powershell
docker compose up -d
docker compose ps
```

Expected running services:

- `db` (PostgreSQL, `5432`)
- `redis` (`6379`)
- `api` (FastAPI, `8000`)
- `worker` (Celery worker)

One-shot migration service:

- `migrate` starts, applies Alembic, exits successfully.

Useful logs:

```powershell
docker compose logs api --tail 100
docker compose logs worker --tail 100
```

Stop backend stack:

```powershell
docker compose down
```

## 4. Frontend Launch (REAL commands)

From project root:

```powershell
Set-Location .\frontend
npm run dev
```

Detected dev URL:

- `http://localhost:5173`

If dependencies are not installed:

```powershell
npm install
npm run dev
```

## 5. Health Check

API container/port:

```powershell
docker compose ps
```

API health endpoint (actual working in this repo):

```powershell
Invoke-WebRequest -UseBasicParsing http://localhost:8000/healthz
```

Expected:

- HTTP `200`
- body `{"status":"ok"}`

Frontend availability:

```powershell
Invoke-WebRequest -UseBasicParsing http://localhost:5173
```

Expected:

- HTTP `200`

## 6. Common Issues

1) `GET /health` returns `404`

- Use `GET /healthz` for health checks.

2) API/worker do not start after `docker compose up -d`

```powershell
docker compose logs migrate --tail 200
docker compose logs api --tail 200
docker compose logs worker --tail 200
```

3) Port already used (`5432`, `6379`, `8000`, `5173`)

```powershell
netstat -ano | findstr :5432
netstat -ano | findstr :6379
netstat -ano | findstr :8000
netstat -ano | findstr :5173
```

4) Frontend `npm run dev` fails due to missing modules

```powershell
Set-Location .\frontend
npm install
npm run dev
```

## 7. Minimal Demo Flow

1. Start backend stack:

```powershell
Set-Location C:\Dima\Projects\CURSOR\1t
docker compose up -d
```

2. Check backend health:

```powershell
Invoke-WebRequest -UseBasicParsing http://localhost:8000/healthz
```

3. Start frontend (new PowerShell window):

```powershell
Set-Location C:\Dima\Projects\CURSOR\1t\frontend
npm run dev
```

4. Open:

- API docs: `http://localhost:8000/docs`
- Frontend: `http://localhost:5173`
