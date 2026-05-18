# Architecture Audit Report

## 1. System Overview

Architecture style: **modular monolith + async worker** (not microservices).

Detected components:

- **API**: FastAPI app with single router (`app/main.py`, `app/api/routes.py`)
- **Worker**: Celery worker (`app/workers/tasks.py`)
- **DB**: PostgreSQL (docker-compose `db`)
- **Queue/Broker**: Redis (docker-compose `redis`)
- **Frontend**: React + Vite (`frontend/`)

Data flow (actual):

1. `POST /api/v1/meetings/upload` saves file to `storage/`, creates `meetings` + `processing_jobs`, sends Celery task.
2. Worker executes pipeline: media prep → STT → diarization → segment align/cleanup → summary (OpenAI) → tasks (OpenAI).
3. API exposes status and artifacts by meeting id (`/summary`, `/tasks`, `/transcript`, `/segments`).
4. Frontend polls status every 3 seconds and renders artifacts.

## 2. Strengths

1. **Clear vertical slice implemented end-to-end**  
   Upload → async processing → retrieval endpoints is fully wired.

2. **Operational baseline exists in compose**  
   `db`, `redis`, `migrate`, `api`, `worker` with restart and healthchecks.

3. **Schema + migration chain is coherent**  
   `0001` → `0002` → `0003` aligns with SQLAlchemy models (tasks confidence/speaker label, segment timestamps as float).

4. **Input limits exist for upload**  
   Extension/content-type allowlist + hard max size + cleanup on oversize.

5. **API ownership isolation exists**  
   Meeting data is scoped by owner (`owner_id`) and checked in all read endpoints.

6. **Integration tests cover key happy path**  
   Upload + processing + endpoint retrieval are tested with monkeypatched heavy services.

## 3. Critical Issues (P0)

1. **Secrets are committed in `.env` (hard blocker for production)**  
   Real-looking `OPENAI_API_KEY` and `PYANNOTE_AUTH_TOKEN` are present in repo.  
   Impact: immediate credential leakage risk and external account abuse.

2. **Auth model is not production-safe and not self-service-ready**  
   Only static API key + `X-User-Email` header check; user must already exist in DB, but no user management endpoint/seed flow for runtime.
   This blocks real multi-user demo onboarding and secure production access.

3. **Health contract inconsistency between docs and implementation**  
   README says `GET /health`, actual API exposes `GET /healthz` and `GET /readyz`.  
   Impact: broken runbooks, false monitoring alarms, failed external checks.

4. **No guaranteed idempotency for reprocessing same meeting**  
   Worker creates new transcript records on each run and commits stage-by-stage; retries can create inconsistent historical state and duplicate artifacts.

## 4. High Risks (P1)

1. **Celery routing mismatch**  
   `task_routes` routes pipeline task to `meetings` queue, but `send_task` uses queue `celery`.  
   Today it works because worker consumes default queues broadly, but queue hardening later can silently break processing.

2. **Pipeline reliability is weak under transient failures**  
   No Celery retry policy (`autoretry_for`, backoff, max_retries), no dead-letter strategy, no circuit breaking for provider failures.

3. **No explicit transaction boundaries per pipeline stage**  
   Multiple commits inside one task without compensations; failures between commits leave partial state requiring manual cleanup.

4. **Frontend/back contracts are partially divergent**  
   Frontend normalizes many alternate payload shapes and has fallback parsing logic, indicating unstable/implicit contract ownership.

5. **Container hardening incomplete**  
   API/worker run as non-root (good), but bind-mount full repo (`./:/app`) in runtime and load `.env` directly, increasing exposure surface.

## 5. Medium Issues (P2)

1. **Observability is minimal**  
   Mostly warning logs in services, no structured JSON logs, no trace IDs, no metrics, no queue lag visibility.

2. **Readiness endpoint checks DB and Redis but no timeout/guard pattern**  
   Potential slow/blocking behavior under degraded dependencies.

3. **Upload validation is mime/extension-based only**  
   No content sniffing/magic-byte verification, no malware scanning, no storage quarantine flow.

4. **Due date is persisted as string**  
   Task `due_date` type is text/string, reducing queryability and SLA/reporting quality.

5. **No API versioning governance beyond path prefix**  
   Current `/api/v1` exists, but no compatibility policy or explicit deprecation strategy.

## 6. Architecture Gaps

1. **Contracts gap**
   - No dedicated error envelope model (currently plain `HTTPException` details).
   - No global exception handler for consistent API error shape.
   - Health endpoint naming and docs are inconsistent.

2. **Platform gap**
   - No production-grade secret management (vault/CI secret injection).
   - No immutable deployment profile (dev bind mounts used in compose runtime).

3. **Async gap**
   - No deterministic idempotency key for processing jobs.
   - No retry/backoff policy for STT/diarization/OpenAI network failures.

4. **Product gap**
   - No runtime user provisioning/auth flow despite user-scoped access checks.
   - No explicit admin/demo bootstrap path in deployment guide.

## 7. Production Readiness Score

**4.8 / 10**

Reasoning:

- **+** Working vertical slice, migrations, compose orchestration, healthchecks, integration tests.
- **−** Credential leakage in repo, weak auth model, weak retry/idempotency story, contract inconsistency, limited observability.
- **−** Architecture can demo value, but is not yet safe or stable for production workloads.

## 8. MVP Gap

To be sellable as MVP, missing critical productization pieces:

1. Secure secret handling and key rotation process.
2. Real authentication (token/session) and user onboarding path.
3. Stable API/error contracts documented and enforced.
4. Reliable async processing guarantees (retry + idempotency).
5. Basic ops telemetry (structured logs + metrics + alerts).

## 9. Fastest Path to Demo (1–3 days)

1. Remove leaked secrets from repo, rotate all exposed keys.
2. Align docs and checks on real health endpoints (`/healthz`, `/readyz`).
3. Ship one scripted demo user bootstrap (seed user + known API key in dev only).
4. Lock demo flow:
   - upload sample media
   - poll status
   - show summary/tasks/transcript/segments in frontend.

## 10. Fastest Path to MVP (7–14 days)

1. Replace static header auth with proper auth (JWT/session) + user lifecycle.
2. Add Celery reliability profile:
   - retries with exponential backoff
   - idempotency guard per meeting/job
   - failure classification and terminal states.
3. Introduce consistent error response model and global handlers.
4. Add baseline observability:
   - structured logs
   - request/task correlation IDs
   - queue depth + processing latency metrics.
5. Harden runtime deployment profile:
   - no source bind mounts in production compose/profile
   - secret injection only through environment/secret store.

## 11. Recommended Architecture Evolution

Recommended evolution: keep **modular monolith + worker** for MVP, do not split into microservices yet.

Near-term target architecture:

- FastAPI API boundary with strict schemas and unified errors.
- Celery worker with explicit retry/idempotency policies.
- PostgreSQL as source of truth.
- Redis for broker/result + queue telemetry.
- Frontend consuming a stable typed API contract.

Why this path:

- Lowest delivery risk and fastest time-to-MVP.
- Existing code already matches this shape; improvements are mainly hardening and contract discipline, not rewrite.
