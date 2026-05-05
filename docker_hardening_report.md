# Docker Hardening Report

## Current Images

- `docker/Dockerfile.api`: `python:3.11-slim` + `ffmpeg` via `apt-get`, app runs as `appuser`.
- `docker/Dockerfile.worker`: `python:3.11-slim` + `ffmpeg` via `apt-get`, app runs as `appuser`.
- Runtime context (`docker-compose.yml`):
  - `api`, `worker`, `migrate` mount project source (`./:/app`) -> dev-friendly hot reload style.
  - `db` = `postgres:16`, `redis` = `redis:7`.
  - `.env` injected directly for runtime config.

## Vulnerability Sources

1. **Base image CVE surface (`python:3.11-slim`)**
   - `slim` is reasonably small, but still inherits Debian package CVEs over time.
   - Not pinned by digest, so image drift can introduce unplanned changes.

2. **APT-installed `ffmpeg`**
   - `ffmpeg` and codec libs are frequent CVE carriers.
   - Installed from Debian repos at build time (moving target).

3. **Dependency install from project package**
   - `pip install .` resolves Python deps without explicit hash pinning in Dockerfile.
   - Supply-chain and reproducibility risks depend on lock strategy in project metadata.

4. **Dev bind mounts in compose**
   - `./:/app` in `api/worker/migrate` increases attack surface in production if reused as-is.
   - Runtime immutability is lost; container FS can be influenced by host state.

5. **Secrets handling**
   - `env_file: .env` convenient for local dev, but risky for production if secrets are long-lived and broadly shared.

## Safe Fixes

1. **Pin base image by digest** (safe, high value)
   - Benefit: reproducible builds, less supply-chain drift.
   - Risk: low (requires controlled update cadence).

2. **Regular base refresh cadence**
   - Pull patched `python:3.11-slim` (or pinned digest updates) weekly/biweekly.
   - Benefit: fast CVE reduction with minimal behavioral change.

3. **Keep current non-root runtime**
   - Already using `USER appuser` in both Dockerfiles.
   - Benefit: strong containment gain, no functional regression expected.

4. **APT minimality already mostly good**
   - `--no-install-recommends` and apt list cleanup are present.
   - Keep package list unchanged (`ffmpeg` only) per current requirement.

5. **Add image scanning in CI** (safe operational control)
   - e.g. Trivy/Grype with severity threshold and waiver mechanism.

## Risky Fixes

1. **Switch to distroless immediately**
   - Benefit: lower CVE surface.
   - Risk: medium/high (ffmpeg binary/runtime libs compatibility, debugging friction).

2. **Jump Python base major/minor aggressively**
   - e.g. 3.12/3.13 without compatibility validation.
   - Risk: medium (dependency and runtime behavior regressions).

3. **Remove `ffmpeg` or split media processing out blindly**
   - Benefit: surface reduction.
   - Risk: high for current pipeline because media preprocessing depends on ffmpeg.

4. **Turn on strict read-only FS/cap-drop without bake-in tests**
   - Benefit: hardening.
   - Risk: medium if temp dirs, cache, or runtime writes are required.

## Non-Root Feasibility

- **Status:** already implemented safely (`appuser` in both images).
- **Recommendation:** keep as-is.
- **Additional safe hardening (prod profile):**
  - `read_only: true` (if app writes only to explicit volumes/tmpfs)
  - `tmpfs: /tmp`
  - `cap_drop: ["ALL"]`
  - `security_opt: ["no-new-privileges:true"]`
- **Risk note:** enable only after runtime write-path validation (Celery temp files, ffmpeg temp usage).

## Base Image Upgrade Recommendation

1. **Short-term (safest):**
   - Stay on `python:3.11-slim`, pin digest, and update digest on schedule.
   - Rationale: minimal break risk, immediate reproducibility/security gain.

2. **Mid-term:**
   - Evaluate `python:3.11-slim-bookworm` pinned digest explicitly (if not already implied).
   - Run smoke pipeline with real audio before promotion.

3. **Do not recommend immediate major leap** without test matrix.

## Production Hardening Checklist

- [ ] Pin `FROM python:3.11-slim@sha256:...` in both Dockerfiles.
- [ ] Add CI image scan (fail on Critical/High unless waived).
- [ ] Separate compose profiles:
  - local-dev: keep bind mounts
  - production: remove `./:/app`, immutable image only.
- [ ] Move secrets from `.env` to production secret manager (Docker/K8s secrets).
- [ ] Add SBOM generation in CI for traceability.
- [ ] Add restart/backoff and health policy review for worker/api.
- [ ] Validate optional runtime hardening flags (`read_only`, `cap_drop`, `no-new-privileges`).
- [ ] Keep ffmpeg package; avoid extra apt packages.
- [ ] Maintain patch cadence for Debian and Python base.

## Final Recommendation

Текущие Dockerfiles уже на хорошем минимальном уровне для MVP (non-root, minimal apt packages, cleanup, retry config).  
Наиболее безопасный путь hardening без риска поломки:

1. **Не менять логику образов радикально.**
2. **Зафиксировать base image digest + регулярные обновления.**
3. **Внедрить CI vulnerability scanning и разделить dev/prod runtime-профили.**
4. **Сохранить ffmpeg и текущий runtime behavior.**

Это даст максимальное снижение critical/high рисков при минимальной вероятности нарушить рабочий pipeline.
