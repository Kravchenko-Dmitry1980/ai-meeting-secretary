"""Isolated API audit probes. Deliberate failures record defects, not product fixes."""
from __future__ import annotations

import io
import json
import tempfile
import wave
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from secretary.api import create_app
from secretary.domain.models import TranscriptSegment
from secretary.settings import Settings


class FakeCapture:
    def __init__(self):
        self.active = set()
    def list_devices(self):
        return {"available": True, "microphones": [{"id": "synthetic-mic"}], "loopback": []}
    def state(self, mid):
        return {"status": "recording" if mid in self.active else "stopped", "recording": mid in self.active}
    def start(self, mid, microphone_id, system_id, on_chunk, on_error):
        if microphone_id != "synthetic-mic":
            raise ValueError("Synthetic microphone must be selected")
        self.active.add(mid)
        return self.state(mid)
    def stop(self, mid):
        self.active.discard(mid)
        return self.state(mid)
    def close(self):
        self.active.clear()


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Audit forbids real HTTP transport")
    async def denied_async(*args, **kwargs):
        denied()
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", denied)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", denied_async)
    monkeypatch.setattr(tempfile, "tempdir", tempfile.tempdir)
    settings = Settings(_env_file=None, project_dir=tmp_path, data_dir=tmp_path / "data",
                        polza_api_key="synthetic-audit-key", cloud_enabled=False,
                        max_upload_bytes=4096)
    app = create_app(settings, capture=FakeCapture(), provider_factory=denied, run_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False) as api:
        token = api.get("/api/v1/session").json()["csrf_token"]
        meeting = app.state.db.create_meeting("Синтетическая встреча")
        yield SimpleNamespace(app=app, api=api, db=app.state.db, settings=settings,
                              mid=meeting["id"], auth={"X-Secretary-Token": token})


def wav_bytes():
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 1600)
    return output.getvalue()


def add_audio(ctx, *, mid=None, sequence=0, version=0):
    mid = mid or ctx.mid
    folder = ctx.settings.data_dir / "audio" / mid
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sequence}.wav"
    path.write_bytes(wav_bytes())
    chunk = ctx.db.add_chunk(mid, {"sequence": sequence, "channel": "import", "path": str(path),
                                  "offset_ms": sequence * 100, "duration_ms": 100, "sha256": "synthetic"})
    if version:
        ctx.db.update_meeting(mid, transcript_version=version)
        job = ctx.db.enqueue(mid, "transcribe", version=version, chunk_id=chunk["id"])
        segment = TranscriptSegment(id=f"{mid}-{version}-{sequence}", meeting_id=mid,
            chunk_id=chunk["id"], transcript_version=version, ordinal=0,
            start_ms=sequence * 100, end_ms=(sequence + 1) * 100, timing_precision="chunk",
            text=f"Контрольный фрагмент {sequence}: 17 рублей, срок не согласован.", channel="import")
        ctx.db.save_segments(job, [segment.model_dump()], [])
    return chunk


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1.evil.example", "user@localhost:8765", "localhost:8765/path"])
def test_untrusted_host(ctx, host):
    assert ctx.api.get("/api/v1/session", headers={"Host": host}).status_code == 403


@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://localhost:9876", "https://localhost:8765", "http://localhost:8765/path"])
def test_untrusted_origin(ctx, origin):
    assert ctx.api.get("/api/v1/config", headers={"Origin": origin}).status_code == 403


def test_cors_preflight_and_csrf(ctx):
    accepted = ctx.api.options("/api/v1/meetings", headers={"Origin": "http://localhost:5173"})
    assert accepted.status_code == 204
    assert accepted.headers["access-control-allow-origin"] == "http://localhost:5173"
    for headers in ({}, {"X-Secretary-Token": "wrong"}):
        assert ctx.api.post("/api/v1/meetings", headers=headers, json={"title": "X"}).status_code == 403
    blocked = ctx.api.post("/api/v1/meetings", headers={**ctx.auth, "Origin": "http://evil.example"}, json={"title": "X"})
    assert blocked.status_code == 403
    valid = ctx.api.post("/api/v1/meetings", headers=ctx.auth, json={"title": "X"})
    assert valid.status_code == 201
    assert valid.headers["cache-control"] == "no-store"
    assert valid.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("method,path,body", [
    ("post", "/api/v1/meetings", {"title": ""}),
    ("post", "/api/v1/meetings", {"title": "x" * 251}),
    ("post", "/api/v1/meetings", {"title": "x", "extra": True}),
    ("post", "/api/v1/meetings/{mid}/process", {"stage": "delete"}),
    ("post", "/api/v1/meetings/{mid}/process", {"retry": "not-a-bool"}),
    ("post", "/api/v1/meetings/{mid}/recording/start", {"auto_process": False, "extra": True}),
    ("patch", "/api/v1/config", {"polza_api_key": "rejected-secret"}),
    ("patch", "/api/v1/config", {"chunk_seconds": 14}),
    ("patch", "/api/v1/config", {"chunk_seconds": 241}),
    ("patch", "/api/v1/config", {"meeting_budget_rub": -1}),
    ("patch", "/api/v1/config", {"summary_max_output_tokens": 1}),
])
def test_invalid_mutation_rejected_without_state_change(ctx, method, path, body):
    before = ctx.db.configuration()
    response = getattr(ctx.api, method)(path.format(mid=ctx.mid), headers=ctx.auth, json=body)
    assert response.status_code == 422
    assert "synthetic-audit-key" not in response.text
    assert ctx.db.configuration() == before


@pytest.mark.parametrize("suffix", ["", "/chunks", "/jobs", "/segments", "/speakers", "/summary", "/tasks", "/recording", "/audio", "/export", "/events"])
def test_missing_meeting_is_404(ctx, suffix):
    response = ctx.api.get("/api/v1/meetings/not-present" + suffix)
    assert response.status_code == 404
    assert response.json() == {"detail": "Not found"}


@pytest.mark.parametrize("stage", ["prepare", "transcribe", "summarize"])
def test_process_empty_meeting_conflict(ctx, stage):
    assert ctx.api.post(f"/api/v1/meetings/{ctx.mid}/process", headers=ctx.auth, json={"stage": stage}).status_code == 409
    assert ctx.db.jobs(ctx.mid) == []


def test_duplicate_process_cancel_and_explicit_retry(ctx):
    add_audio(ctx)
    url = f"/api/v1/meetings/{ctx.mid}/process"
    first = ctx.api.post(url, headers=ctx.auth, json={}).json()["job_id"]
    second = ctx.api.post(url, headers=ctx.auth, json={}).json()["job_id"]
    assert first == second and len(ctx.db.jobs(ctx.mid)) == 1
    cancel = ctx.api.post(f"/api/v1/jobs/{first}/cancel", headers=ctx.auth)
    assert cancel.status_code == 200 and cancel.json()["status"] == "cancelled"
    resumed = ctx.api.post(url, headers=ctx.auth, json={})
    assert resumed.status_code == 202 and resumed.json()["job_id"] == first
    assert ctx.db.job(first)["status"] == "queued" and len(ctx.db.jobs(ctx.mid)) == 1
    retry = ctx.api.post(url, headers=ctx.auth, json={"retry": True})
    assert retry.status_code == 202 and retry.json()["job_id"] == first
    assert len(ctx.db.jobs(ctx.mid)) == 1
    assert ctx.api.get("/api/v1/usage", params={"meeting_id": ctx.mid}).json()["records"] == []
    assert ctx.api.post("/api/v1/jobs/missing/cancel", headers=ctx.auth).status_code == 404


def test_upload_empty_duplicate_and_size_cap(ctx):
    url = f"/api/v1/meetings/{ctx.mid}/upload"
    assert ctx.api.post(url, headers=ctx.auth, files={"file": ("empty.wav", b"")}).status_code == 422
    assert ctx.api.post(url, headers=ctx.auth, files={"file": ("large.wav", b"x" * 4097)}).status_code == 413
    accepted = ctx.api.post(url, headers=ctx.auth, files={"file": ("normal.wav", wav_bytes())})
    assert accepted.status_code == 202
    assert ctx.api.post(url, headers=ctx.auth, files={"file": ("again.wav", wav_bytes())}).status_code == 409
    assert len(ctx.db.jobs(ctx.mid)) == 1


def test_recording_start_stop_duplicate_and_import_conflicts(ctx):
    url = f"/api/v1/meetings/{ctx.mid}/recording"
    body = {"microphone_id": "synthetic-mic", "auto_process": False}
    assert ctx.api.post(url + "/start", headers=ctx.auth, json=body).status_code == 200
    assert ctx.api.get(url).json()["recording"] is True
    assert ctx.api.post(url + "/start", headers=ctx.auth, json=body).status_code == 409
    assert ctx.api.post(f"/api/v1/meetings/{ctx.mid}/upload", headers=ctx.auth, files={"file": ("audio.wav", wav_bytes())}).status_code == 409
    assert ctx.api.post(url + "/stop", headers=ctx.auth).status_code == 200
    assert ctx.api.get(url).json()["recording"] is False
    assert ctx.db.jobs(ctx.mid) == []


def test_pagination_current_version_and_meeting_scope(ctx):
    add_audio(ctx, sequence=0, version=1)
    for sequence in range(3):
        add_audio(ctx, sequence=sequence, version=2)
    other = ctx.db.create_meeting("Other synthetic")
    add_audio(ctx, mid=other["id"], version=1)
    url = f"/api/v1/meetings/{ctx.mid}/segments"
    page = ctx.api.get(url, params={"offset": 1, "limit": 1}).json()
    assert page["total"] == 3 and page["transcript_version"] == 2
    assert [row["ordinal"] for row in page["items"]] == [0]
    assert page["items"][0]["id"] == f"{ctx.mid}-2-1"
    assert ctx.api.get(url, params={"offset": 100}).json()["items"] == []
    for segment_id in (f"{ctx.mid}-1-0", f"{other['id']}-1-0", "missing"):
        assert ctx.api.get(url, params={"segment_id": segment_id}).json()["items"] == []


@pytest.mark.parametrize("suffix", ["segments?offset=-1", "segments?limit=0", "segments?limit=1001", "segments?limit=x", "audio?channel=unknown", "export?format=exe"])
def test_query_validation(ctx, suffix):
    assert ctx.api.get(f"/api/v1/meetings/{ctx.mid}/{suffix}").status_code == 422


@pytest.mark.parametrize("format,media", [("txt", "text/plain"), ("md", "text/markdown"), ("json", "application/json"), ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")])
def test_export_content_and_download_headers(ctx, format, media):
    add_audio(ctx, version=1)
    response = ctx.api.get(f"/api/v1/meetings/{ctx.mid}/export", params={"format": format})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(media)
    assert response.headers["content-disposition"] == f'attachment; filename="meeting-{ctx.mid}.{format}"'
    if format == "docx":
        from docx import Document
        text = "\n".join(paragraph.text for paragraph in Document(io.BytesIO(response.content)).paragraphs)
    else:
        text = response.text
    assert "17 рублей" in text and f"{ctx.mid}-1-0" in text


@pytest.mark.parametrize("range_header,expected", [("bytes=0-43", 206), ("bytes=-4", 206), ("bytes=44-", 206), ("bytes=999999-", 416), ("garbage", 400)])
def test_audio_range_contract(ctx, range_header, expected):
    chunk = add_audio(ctx)
    response = ctx.api.get(f"/api/v1/meetings/{ctx.mid}/audio", headers={"Range": range_header})
    assert response.status_code == expected
    if expected == 206:
        assert response.headers["accept-ranges"] == "bytes"
        assert response.headers["content-type"].startswith("audio/wav")
        assert response.headers["content-range"].endswith(f"/{Path(chunk['path']).stat().st_size}")
        if range_header == "bytes=0-43":
            assert response.content == Path(chunk["path"]).read_bytes()[:44]


def test_openapi_saved_schema_parity(ctx):
    saved = json.loads((Path(__file__).resolve().parents[2] / "docs" / "openapi.json").read_text(encoding="utf-8"))
    actual = ctx.app.openapi()
    assert saved["components"] == actual["components"]
    assert {k: v for k, v in saved["paths"].items() if k != "/"} == {k: v for k, v in actual["paths"].items() if k != "/"}


def test_read_endpoints_and_empty_catalog(ctx):
    assert ctx.api.get("/health").json() == {"status": "ok"}
    assert ctx.api.get("/api/v1/meetings").json()[0]["id"] == ctx.mid
    assert ctx.api.get(f"/api/v1/meetings/{ctx.mid}").json()["title"] == "Синтетическая встреча"
    assert ctx.api.get(f"/api/v1/meetings/{ctx.mid}/chunks").json() == []
    assert ctx.api.get(f"/api/v1/meetings/{ctx.mid}/speakers").json() == []
    assert ctx.api.get("/api/v1/audio/devices").json()["available"] is True
    assert ctx.api.get("/api/v1/models").json() == {"models": [], "source": "not_available", "updated_at": None}
    config = ctx.api.get("/api/v1/config")
    assert config.json()["cloud_enabled"] is False and "synthetic-audit-key" not in config.text


def test_usage_api_aggregates_reserved_confirmed_unknown_released_and_filters(ctx):
    for version, estimate in enumerate((2, 3, 1, 9), 1):
        job = ctx.db.enqueue(ctx.mid, "summarize", version=version)
        usage = ctx.db.reserve(job, estimate, 100, False)
        if version == 2:
            ctx.db.settle(usage)
        elif version == 3:
            ctx.db.settle(usage, {"confirmed_rub": 4, "provider_request_id": "synthetic-receipt"})
        elif version == 4:
            ctx.db.settle(usage, rejected=True)
    result = ctx.api.get("/api/v1/usage", params={"meeting_id": ctx.mid}).json()
    assert result["estimated_rub"] == 6 and result["confirmed_rub"] == 4
    assert result["unknown_count"] == 1 and len(result["records"]) == 4
    assert ctx.api.get("/api/v1/usage", params={"meeting_id": "other"}).json() == {
        "records": [], "estimated_rub": 0, "confirmed_rub": 0, "unknown_count": 0}


def test_successful_summary_tasks_and_source_references(ctx):
    add_audio(ctx, version=1)
    source_id = f"{ctx.mid}-1-0"
    summary = {"meeting_id": ctx.mid, "transcript_version": 1, "summary_version": 1,
               "overview": "Синтетический итог", "readable_transcript": "17 рублей",
               "decisions": [], "action_items": [{"id": "synthetic-task", "text": "Проверить сумму",
                  "owner": None, "due_date": None, "source_segment_ids": [source_id]}],
               "open_questions": [], "excluded_items_count": 0, "status": "succeeded"}
    job = ctx.db.enqueue(ctx.mid, "summarize", version=1)
    ctx.db.save_summary(job, summary)
    result = ctx.api.get(f"/api/v1/meetings/{ctx.mid}/summary")
    assert result.status_code == 200 and result.json()["overview"] == "Синтетический итог"
    tasks = ctx.api.get(f"/api/v1/meetings/{ctx.mid}/tasks")
    assert tasks.status_code == 200 and len(tasks.json()) == 1
    task = tasks.json()[0]
    # The current task API adds reviewed-assignment metadata to source fields.
    assert {key: task[key] for key in summary["action_items"][0]} == summary["action_items"][0]
    assert task["assignment_status"] == "needs_review"
    assert task["participant_id"] is None and "missing_evidence" in task["assignment_reason_codes"]
    located = ctx.api.get(f"/api/v1/meetings/{ctx.mid}/segments", params={"segment_id": source_id})
    assert located.json()["items"][0]["id"] == source_id


@pytest.mark.parametrize("suffix", ["summary", "tasks"])
def test_pending_response_is_documented_in_openapi(ctx, suffix):
    assert ctx.api.get(f"/api/v1/meetings/{ctx.mid}/{suffix}").status_code == 202
    responses = ctx.app.openapi()["paths"][f"/api/v1/meetings/{{meeting_id}}/{suffix}"]["get"]["responses"]
    assert "202" in responses, f"Observed 202 response is absent from OpenAPI: {suffix}"


@pytest.mark.parametrize("suffix,media", [("events", "text/event-stream"), ("audio", "audio/wav"), ("export", "text/markdown")])
def test_binary_and_stream_media_are_documented(ctx, suffix, media):
    response = ctx.app.openapi()["paths"][f"/api/v1/meetings/{{meeting_id}}/{suffix}"]["get"]["responses"]["200"]
    assert media in response.get("content", {}), f"Actual media {media} absent from OpenAPI"


@pytest.mark.parametrize("field", ["stt_model", "summary_model"])
def test_empty_provider_model_is_rejected(ctx, field):
    response = ctx.api.patch("/api/v1/config", headers=ctx.auth, json={field: ""})
    assert response.status_code == 422, f"Blank provider model persisted: {ctx.db.configuration()}"


@pytest.mark.parametrize("field", ["stt_price_rub_per_minute", "summary_input_rub_per_million", "summary_output_rub_per_million"])
def test_nonfinite_price_rejected_without_poisoning_config(ctx, field):
    response = ctx.api.patch("/api/v1/config", headers=ctx.auth, json={field: "Infinity"})
    subsequent = ctx.api.get("/api/v1/config")
    assert (response.status_code, subsequent.status_code) == (422, 200), (
        f"PATCH={response.status_code}, subsequent GET={subsequent.status_code}; "
        f"stored={ctx.db.configuration()}")


def test_missing_audio_file_returns_actionable_not_found(ctx):
    chunk = add_audio(ctx)
    Path(chunk["path"]).unlink()
    response = ctx.api.get(f"/api/v1/meetings/{ctx.mid}/audio")
    assert response.status_code in {404, 409}, f"Missing retained audio produces HTTP {response.status_code}: {response.text}"
