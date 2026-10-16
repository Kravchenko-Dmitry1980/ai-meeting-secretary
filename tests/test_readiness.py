from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from secretary.application import diagnostics
from secretary.infrastructure.database import Database
from secretary.settings import Settings


class LiveTask:
    def done(self):
        return False


class StoppedTask:
    def done(self):
        return True


def context(tmp_path):
    project = tmp_path / "project"
    dist = project / "frontend" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("synthetic", encoding="utf-8")
    (project / "uv.lock").write_text("synthetic python lock", encoding="utf-8")
    (project / "frontend" / "package-lock.json").write_text("synthetic npm lock", encoding="utf-8")
    write_manifest(project)
    settings = Settings(_env_file=None, project_dir=project, data_dir=tmp_path / "data",
                        polza_api_key="secret-never-return", request_timeout_seconds=10)
    db = Database(settings.data_dir / "secretary.sqlite3")
    return settings, db


def write_manifest(project, *, backend_version="0.1.0", frontend_version="0.1.0",
                   build_check="vite-production", python_lock_hash=None, frontend_lock_hash=None):
    python_lock = project / "uv.lock"
    frontend_lock = project / "frontend" / "package-lock.json"
    manifest = {
        "schema_version": 2,
        "backend_version": backend_version,
        "frontend_version": frontend_version,
        "build_check": build_check,
        "lockfiles": {
            "uv.lock": python_lock_hash or hashlib.sha256(python_lock.read_bytes()).hexdigest(),
            "frontend/package-lock.json": frontend_lock_hash or hashlib.sha256(frontend_lock.read_bytes()).hexdigest(),
        },
    }
    (project / "frontend" / "dist" / "secretary-release.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    return manifest


def make_report(settings, db, **overrides):
    arguments = {
        "worker_task": LiveTask(),
        "run_worker": True,
        "worker_heartbeat_age_seconds": 1,
        "outbound_enabled": False,
        "publication_configured": False,
        "publication_task": None,
        "maintenance": None,
        "backend_version": "0.1.0",
    }
    arguments.update(overrides)
    return diagnostics.build_readiness_report(settings, db, **arguments)


def test_readiness_reports_local_health_without_claiming_production_qualification(tmp_path, monkeypatch):
    settings, db = context(tmp_path)
    monkeypatch.setattr(diagnostics.shutil, "which", lambda command: f"C:/tools/{command}.exe")

    report = make_report(settings, db)

    assert report["status"] == "local_ok"
    assert report["production_qualified"] is False
    assert report["components"]["database"]["state"] == "healthy"
    assert report["components"]["storage"]["state"] == "healthy"
    assert report["components"]["processing_worker"]["state"] == "healthy"
    assert report["components"]["cloud"]["state"] == "disabled"
    assert report["components"]["device_capture"]["state"] == "not_qualified"
    assert report["jobs"]["counts"] == {}
    assert "secret-never-return" not in str(report)
    assert str(settings.data_dir) not in str(report)
    assert "uv.lock" not in str(report)
    assert "package-lock.json" not in str(report)
    assert hashlib.sha256((Path(settings.project_dir) / "uv.lock").read_bytes()).hexdigest() not in str(report)


def test_readiness_counts_jobs_and_marks_stale_running_work_as_candidate(tmp_path, monkeypatch):
    settings, db = context(tmp_path)
    monkeypatch.setattr(diagnostics.shutil, "which", lambda command: f"C:/tools/{command}.exe")
    meeting = db.create_meeting("Sensitive synthetic title")
    job = db.enqueue(meeting["id"], "transcribe")
    stale_stamp = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat()
    db.execute("UPDATE jobs SET status='running',updated_at=?,error=? WHERE id=?",
               (stale_stamp, "private error text", job["id"]))

    report = make_report(settings, db)

    assert report["jobs"]["counts"] == {"running": 1}
    assert report["jobs"]["stale_running_candidate_count"] == 1
    assert report["status"] == "degraded"
    assert "Sensitive synthetic title" not in str(report)
    assert "private error text" not in str(report)
    assert job["id"] not in str(report)


def test_readiness_reports_storage_and_tool_failures_without_exposing_exception_details(tmp_path, monkeypatch):
    settings, db = context(tmp_path)
    monkeypatch.setattr(diagnostics.os, "access", lambda path, mode: False)
    monkeypatch.setattr(diagnostics.shutil, "disk_usage",
                        lambda path: SimpleNamespace(total=10_000, used=9_999, free=1))
    monkeypatch.setattr(diagnostics.shutil, "which", lambda command: None)

    report = make_report(settings, db, worker_task=StoppedTask(), worker_heartbeat_age_seconds=30)

    assert report["status"] == "degraded"
    assert report["components"]["storage"]["state"] == "degraded"
    assert report["components"]["storage"]["directory_writable"] is False
    assert report["components"]["ffmpeg"]["state"] == "missing"
    assert report["components"]["ffprobe"]["state"] == "missing"
    assert report["components"]["processing_worker"]["state"] == "degraded"


def test_readiness_detects_corrupt_database_and_does_not_return_raw_path(tmp_path, monkeypatch):
    settings, db = context(tmp_path)
    db.path.write_bytes(b"not a sqlite database")
    monkeypatch.setattr(diagnostics.shutil, "which", lambda command: f"C:/tools/{command}.exe")

    report = make_report(settings, db, run_worker=False, worker_task=None,
                         worker_heartbeat_age_seconds=None)

    assert report["status"] == "degraded"
    assert report["components"]["database"]["state"] == "degraded"
    assert report["components"]["database"]["code"] == "database_integrity_failed"
    assert str(db.path) not in str(report)


def test_readiness_requires_backend_and_frontend_release_versions_to_match(tmp_path, monkeypatch):
    settings, db = context(tmp_path)
    monkeypatch.setattr(diagnostics.shutil, "which", lambda command: f"C:/tools/{command}.exe")
    write_manifest(Path(settings.project_dir))

    report = make_report(settings, db)

    assert report["components"]["release_parity"] == {
        "state": "healthy", "backend_version": "0.1.0",
        "manifest_backend_version": "0.1.0", "frontend_version": "0.1.0",
        "build_check": "vite-production", "lockfiles_verified": True}
    assert report["status"] == "local_ok"

    write_manifest(Path(settings.project_dir), frontend_version="0.2.0")
    mismatched = make_report(settings, db)

    assert mismatched["components"]["release_parity"]["state"] == "mismatch"
    assert "release_version_mismatch" in mismatched["blockers"]
    assert mismatched["status"] == "degraded"

    write_manifest(Path(settings.project_dir), backend_version="0.2.0")
    backend_mismatched = make_report(settings, db)

    assert backend_mismatched["components"]["release_parity"]["state"] == "mismatch"
    assert backend_mismatched["components"]["release_parity"]["code"] == "release_version_mismatch"
    assert "release_version_mismatch" in backend_mismatched["blockers"]

    write_manifest(Path(settings.project_dir), frontend_lock_hash="0" * 64)
    lock_mismatched = make_report(settings, db)

    assert lock_mismatched["components"]["release_parity"]["state"] == "mismatch"
    assert lock_mismatched["components"]["release_parity"]["code"] == "release_lockfile_mismatch"
    assert lock_mismatched["components"]["release_parity"]["lockfiles_verified"] is False
    assert "release_lockfile_mismatch" in lock_mismatched["blockers"]


def test_readiness_marks_missing_or_invalid_release_manifest_without_leaking_data(tmp_path, monkeypatch):
    settings, db = context(tmp_path)
    monkeypatch.setattr(diagnostics.shutil, "which", lambda command: f"C:/tools/{command}.exe")
    manifest = Path(settings.project_dir) / "frontend" / "dist" / "secretary-release.json"
    manifest.unlink()

    missing = make_report(settings, db)

    assert missing["components"]["release_parity"]["state"] == "not_configured"
    assert "release_manifest_missing" in missing["blockers"]
    assert missing["status"] == "degraded"

    write_manifest(Path(settings.project_dir), build_check="vite-nonproduction")
    invalid = make_report(settings, db)

    assert invalid["components"]["release_parity"]["state"] == "unavailable"
    assert invalid["components"]["release_parity"]["code"] == "release_manifest_invalid"
    assert str(settings.project_dir) not in str(invalid)

    write_manifest(Path(settings.project_dir))
    (Path(settings.project_dir) / "frontend" / "package-lock.json").unlink()
    missing_lock = make_report(settings, db)

    assert missing_lock["components"]["release_parity"]["state"] == "not_configured"
    assert missing_lock["components"]["release_parity"]["code"] == "release_lockfile_missing"
    assert "release_lockfile_missing" in missing_lock["blockers"]
