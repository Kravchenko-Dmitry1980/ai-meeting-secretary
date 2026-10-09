"""Sanitized, read-only diagnostics for the local Secretary runtime."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import sqlite3

from secretary.infrastructure.storage_space import MINIMUM_FREE_SPACE_BYTES


HEARTBEAT_STALE_AFTER_SECONDS = 10
MINIMUM_STALE_JOB_AGE_SECONDS = 300
STALE_JOB_GRACE_SECONDS = 300
_JOB_STATES = {"queued", "running", "waiting_config", "paused_budget", "uncertain",
               "failed", "cancelled", "succeeded"}


def _database_status(path: Path) -> dict:
    path = Path(path)
    if not path.is_file():
        return {"state": "degraded", "code": "database_missing"}
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=1)
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA busy_timeout=1000")
            integrity = connection.execute("PRAGMA quick_check(1)").fetchone()
            required = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('meetings','jobs')")}
        finally:
            connection.close()
    except (OSError, sqlite3.Error, ValueError):
        return {"state": "degraded", "code": "database_integrity_failed"}
    if not integrity or integrity[0] != "ok":
        return {"state": "degraded", "code": "database_integrity_failed"}
    if required != {"meetings", "jobs"}:
        return {"state": "degraded", "code": "database_schema_incomplete"}
    return {"state": "healthy", "integrity": "ok"}


def _storage_status(data_dir: Path) -> dict:
    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        return {"state": "degraded", "code": "data_directory_missing",
                "directory_writable": False, "free_bytes": None,
                "minimum_free_bytes": MINIMUM_FREE_SPACE_BYTES}
    try:
        free_bytes = int(shutil.disk_usage(data_dir).free)
    except (OSError, ValueError):
        return {"state": "degraded", "code": "disk_space_unavailable",
                "directory_writable": bool(os.access(data_dir, os.W_OK)),
                "free_bytes": None, "minimum_free_bytes": MINIMUM_FREE_SPACE_BYTES}
    writable = bool(os.access(data_dir, os.W_OK))
    healthy = writable and free_bytes >= MINIMUM_FREE_SPACE_BYTES
    return {"state": "healthy" if healthy else "degraded",
            "directory_writable": writable, "free_bytes": free_bytes,
            "minimum_free_bytes": MINIMUM_FREE_SPACE_BYTES,
            **({} if healthy else {"code": "data_storage_below_operating_reserve"})}


def _task_state(task, *, configured: bool) -> str:
    if not configured:
        return "not_configured"
    try:
        return "healthy" if task is not None and not task.done() else "degraded"
    except Exception:
        return "degraded"


def _job_status(db, now: datetime, stale_after_seconds: int) -> dict:
    try:
        rows = db.rows("SELECT status,COUNT(*) AS count FROM jobs GROUP BY status")
        running = db.rows("SELECT updated_at FROM jobs WHERE status='running'")
    except sqlite3.Error:
        return {"state": "unavailable", "counts": {},
                "stale_running_candidate_count": None,
                "stale_after_seconds": stale_after_seconds}
    counts: dict[str, int] = {}
    for row in rows:
        label = row.get("status")
        status = label if label in _JOB_STATES else "other"
        counts[status] = counts.get(status, 0) + max(0, int(row.get("count", 0)))
    stale = 0
    for row in running:
        value = row.get("updated_at")
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
        except (AttributeError, TypeError, ValueError):
            stale += 1
            continue
        if max(0, (now - stamp.astimezone(timezone.utc)).total_seconds()) > stale_after_seconds:
            stale += 1
    return {"state": "healthy", "counts": counts,
            "stale_running_candidate_count": stale,
            "stale_after_seconds": stale_after_seconds}


def build_readiness_report(settings, db, *, worker_task, run_worker: bool,
                           worker_heartbeat_age_seconds: float | None,
                           outbound_enabled: bool, publication_configured: bool,
                           publication_task, maintenance, now: datetime | None = None) -> dict:
    """Return coarse local-operability signals; never claim cloud/device qualification."""
    current = now or datetime.now(timezone.utc)
    db_status = _database_status(db.path)
    storage = _storage_status(settings.data_dir)
    processing_state = _task_state(worker_task, configured=run_worker)
    heartbeat_age = None
    if worker_heartbeat_age_seconds is not None:
        try:
            heartbeat_age = max(0, int(worker_heartbeat_age_seconds))
        except (TypeError, ValueError, OverflowError):
            heartbeat_age = None
    if run_worker and (heartbeat_age is None or heartbeat_age > HEARTBEAT_STALE_AFTER_SECONDS):
        processing_state = "degraded"
    processing = {"state": processing_state, "heartbeat_age_seconds": heartbeat_age,
                  "heartbeat_stale_after_seconds": HEARTBEAT_STALE_AFTER_SECONDS}
    publication_state = _task_state(publication_task, configured=publication_configured)

    try:
        ffmpeg = "available" if shutil.which(settings.ffmpeg_path) else "missing"
    except (OSError, TypeError, ValueError):
        ffmpeg = "missing"
    try:
        ffprobe = "available" if shutil.which(settings.ffprobe_path) else "missing"
    except (OSError, TypeError, ValueError):
        ffprobe = "missing"
    frontend = "available" if (Path(settings.project_dir) / "frontend" / "dist" / "index.html").is_file() else "missing"

    if not outbound_enabled or not settings.cloud_enabled:
        cloud_state = "disabled"
    elif not settings.key_configured:
        cloud_state = "waiting_config"
    else:
        cloud_state = "configured_unqualified"

    if maintenance is None:
        maintenance_state = "not_configured"
    else:
        try:
            maintenance_state = "healthy" if maintenance.status().get("mode") == "open" else "degraded"
        except Exception:
            maintenance_state = "degraded"

    stale_after = max(MINIMUM_STALE_JOB_AGE_SECONDS,
                      int(getattr(settings, "request_timeout_seconds", 180)) + STALE_JOB_GRACE_SECONDS)
    jobs = _job_status(db, current.astimezone(timezone.utc), stale_after) if db_status["state"] == "healthy" else {
        "state": "unavailable", "counts": {}, "stale_running_candidate_count": None,
        "stale_after_seconds": stale_after,
    }

    components = {
        "database": db_status,
        "storage": storage,
        "ffmpeg": {"state": ffmpeg},
        "ffprobe": {"state": ffprobe},
        "frontend_build": {"state": frontend},
        "processing_worker": processing,
        "publication_worker": {"state": publication_state},
        "cloud": {"state": cloud_state, "live_qualified": False},
        "device_capture": {"state": "not_qualified"},
        "maintenance": {"state": maintenance_state},
        "backup": {"state": "not_checked"},
        "logging": {"state": "not_checked"},
    }
    blockers = []
    if db_status["state"] != "healthy":
        blockers.append("database_unhealthy")
    if storage["state"] != "healthy":
        blockers.append("storage_unhealthy")
    if ffmpeg != "available":
        blockers.append("ffmpeg_missing")
    if ffprobe != "available":
        blockers.append("ffprobe_missing")
    if frontend != "available":
        blockers.append("frontend_build_missing")
    if processing_state != "healthy":
        blockers.append("processing_worker_degraded")
    if publication_state == "degraded":
        blockers.append("publication_worker_degraded")
    if maintenance_state == "degraded":
        blockers.append("maintenance_degraded")
    if jobs.get("stale_running_candidate_count"):
        blockers.append("stale_running_jobs_present")

    return {"status": "degraded" if blockers else "local_ok",
            "production_qualified": False, "blockers": blockers,
            "components": components, "jobs": jobs}
