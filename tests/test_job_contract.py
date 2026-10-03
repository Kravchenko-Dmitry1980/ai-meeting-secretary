"""Public job snapshots retain the scope needed for accurate processing progress."""
from __future__ import annotations

import json
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from secretary.api import create_app
from secretary.settings import Settings


class NoCapture:
    def close(self):
        pass


@pytest.fixture
def scoped_jobs(tmp_path, monkeypatch):
    # create_app changes this process-local default; restore it after each test.
    monkeypatch.setattr(tempfile, "tempdir", tempfile.tempdir)
    settings = Settings(
        _env_file=None,
        project_dir=tmp_path,
        data_dir=tmp_path / "data",
        polza_api_key="synthetic-job-contract-key",
        cloud_enabled=False,
    )
    app = create_app(settings, capture=NoCapture(), run_worker=False)
    db = app.state.db
    meeting = db.create_meeting("Synthetic versioned progress")
    db.update_meeting(meeting["id"], transcript_version=2)
    chunks = [db.add_chunk(meeting["id"], {
        "sequence": sequence, "channel": "import",
        "path": str(tmp_path / f"synthetic-{sequence}.wav"),
        "offset_ms": sequence * 120000, "duration_ms": 120000,
        "sha256": f"synthetic-{sequence}",
    }) for sequence in range(2)]
    prepare = db.enqueue(meeting["id"], "prepare")
    db.finish_job(prepare["id"], "succeeded")
    previous = db.enqueue(meeting["id"], "transcribe", chunk_id=chunks[0]["id"], version=1)
    db.finish_job(previous["id"], "failed", "Synthetic previous-version failure")
    current = [db.enqueue(meeting["id"], "transcribe", chunk_id=chunk["id"], version=2)
               for chunk in chunks]
    db.finish_job(current[0]["id"], "succeeded")
    expected = {
        prepare["id"]: (0, None),
        previous["id"]: (1, chunks[0]["id"]),
        current[0]["id"]: (2, chunks[0]["id"]),
        current[1]["id"]: (2, chunks[1]["id"]),
    }
    return app, meeting["id"], expected, current[1]["id"]


def assert_job_scope(jobs, expected):
    assert {job["id"]: (job["version"], job["chunk_id"]) for job in jobs} == expected
    assert all("payload" not in job and "provider_job_id" not in job for job in jobs)


def test_jobs_api_and_cancel_preserve_version_and_chunk(scoped_jobs):
    app, meeting_id, expected, pending_id = scoped_jobs
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.get(f"/api/v1/meetings/{meeting_id}/jobs")
        assert response.status_code == 200
        assert_job_scope(response.json(), expected)
        token = client.get("/api/v1/session").json()["csrf_token"]
        cancelled = client.post(f"/api/v1/jobs/{pending_id}/cancel",
                                headers={"X-Secretary-Token": token})
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert_job_scope([cancelled.json()], {pending_id: expected[pending_id]})


@pytest.mark.asyncio
async def test_sse_snapshot_retains_same_job_scope(scoped_jobs):
    app, meeting_id, expected, _ = scoped_jobs
    route = next(route for route in app.routes
                 if getattr(route, "path", None) == "/api/v1/meetings/{meeting_id}/events")
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    response = await route.endpoint(meeting_id, request)
    try:
        event = await anext(response.body_iterator)
        snapshot = json.loads(event.split("data: ", 1)[1])
        assert snapshot["meeting"]["transcript_version"] == 2
        assert_job_scope(snapshot["jobs"], expected)
    finally:
        await response.body_iterator.aclose()


def test_openapi_exposes_optional_job_scope(scoped_jobs):
    app, _, _, _ = scoped_jobs
    schema = app.openapi()["components"]["schemas"]["ProcessingJob"]
    assert schema["properties"]["version"]["type"] == "integer"
    assert schema["properties"]["version"]["default"] == 0
    assert {item["type"] for item in schema["properties"]["chunk_id"]["anyOf"]} == {"string", "null"}
    assert "version" not in schema["required"]
    assert "chunk_id" not in schema["required"]
