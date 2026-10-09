from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
    (project / "frontend" / "dist").mkdir(parents=True)
    (project / "frontend" / "dist" / "index.html").write_text("synthetic", encoding="utf-8")
    settings = Settings(_env_file=None, project_dir=project, data_dir=tmp_path / "data",
                        polza_api_key="secret-never-return", request_timeout_seconds=10)
    db = Database(settings.data_dir / "secretary.sqlite3")
    return settings, db


def make_report(settings, db, **overrides):
    arguments = {
        "worker_task": LiveTask(),
        "run_worker": True,
        "worker_heartbeat_age_seconds": 1,
        "outbound_enabled": False,
        "publication_configured": False,
        "publication_task": None,
        "maintenance": None,
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
