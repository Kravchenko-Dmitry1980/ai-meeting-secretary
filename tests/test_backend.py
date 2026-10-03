from __future__ import annotations

import asyncio
import io
import json
import hashlib
import struct
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import httpx
from fastapi.testclient import TestClient

from secretary.api import create_app
from secretary.application.evidence import validate_summary
from secretary.application.preparation import cloud_audio, prepare_audio
from secretary.application.worker import Worker
from secretary.application.exporting import export_meeting
from secretary.domain.models import TranscriptSegment
from secretary.infrastructure.database import Database, stable_id, uid
from secretary.infrastructure.polza import ProviderError
from secretary.settings import Settings


def wav_bytes(seconds=1, rate=16000, channels=1):
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(b"\0" * (seconds * rate * channels * 2))
    return output.getvalue()


class FakeCapture:
    def list_devices(self):
        return {"available": False, "devices": [], "error": "Test adapter: no hardware"}

    def state(self, meeting_id):
        return {"status": "stopped", "recording": False}

    def close(self):
        pass


class FakeProvider:
    def __init__(self, settings):
        self.settings = settings

    async def transcribe(self, path, *, offset_ms=0, channel="import"):
        return {"segments": [{"text": "Иван сделает отчёт завтра.", "start_ms": offset_ms, "end_ms": offset_ms + 1000, "timing_precision": "segment", "confidence": 0.9, "speaker_label": "speaker_0"}], "usage": {"confirmed_rub": 0.01, "provider_request_id": "test-stt"}}

    async def summarize(self, segments, *, before_request=None, on_usage=None):
        if before_request:
            await before_request("summary", 1000, 500)
        receipt = {"confirmed_rub": 0.01, "provider_request_id": "test-summary"}
        if on_usage:
            await on_usage(receipt)
        return {"overview": "Нужно подготовить отчёт.", "readable_transcript": "Иван сделает отчёт завтра.", "decisions": [], "action_items": [{"text": "Подготовить отчёт", "owner": "Иван", "due_date": "завтра", "source_segment_ids": [segments[0]["id"]]}], "open_questions": [], "usage": receipt}


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, data_dir=tmp_path / "data", polza_api_key="test-key-never-real", stt_price_rub_per_minute=0.05, summary_input_rub_per_million=5, summary_output_rub_per_million=25)


@pytest.fixture
def db(settings):
    return Database(settings.data_dir / "test.sqlite3")


def prepared(db, settings, *, sequence=0, channel="import", meeting=None, offset=0, native=False):
    meeting = meeting or db.create_meeting("Synthetic")
    folder = settings.data_dir / "audio" / meeting["id"]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sequence}-{channel}.wav"
    path.write_bytes(wav_bytes(rate=48000, channels=2) if native else wav_bytes())
    chunk = db.add_chunk(meeting["id"], {"path": str(path), "sequence": sequence, "channel": channel, "offset_ms": offset, "duration_ms": 1000, "sha256": "test"})
    return meeting, chunk


def client(settings):
    return TestClient(create_app(settings, provider_factory=FakeProvider, capture=FakeCapture(), run_worker=False), base_url="http://127.0.0.1:8765")


def auth(test_client):
    return {"X-Secretary-Token": test_client.get("/api/v1/session").json()["csrf_token"]}


def test_local_api_security_and_no_secret(settings):
    with client(settings) as api:
        assert api.get("/health").json() == {"status": "ok"}
        assert api.get("/health", headers={"Host": "evil.example"}).status_code == 403
        assert api.get("/api/v1/session", headers={"Origin": "https://evil.example"}).status_code == 403
        assert api.get("/api/v1/session", headers={"Origin": "http://127.0.0.1:9876"}).status_code == 403
        assert api.get("/api/v1/session", headers={"Origin": "http://localhost:5173"}).status_code == 200
        assert api.post("/api/v1/meetings", json={"title": "X"}).status_code == 403
        assert api.post("/api/v1/meetings", headers=auth(api), json={"title": "X"}).status_code == 201
        config = api.get("/api/v1/config")
        assert config.json()["key_configured"] is True
        assert "test-key-never-real" not in config.text
        assert api.patch("/api/v1/config", headers=auth(api), json={"polza_api_key": "new-secret"}).status_code == 422


def test_config_prices_reset_when_model_changes(settings):
    with client(settings) as api:
        changed = api.patch("/api/v1/config", headers=auth(api), json={"stt_model": "other/model"}).json()
        assert changed["stt_price_rub_per_minute"] is None
        assert api.patch("/api/v1/config", headers=auth(api), json={"chunk_seconds": 900}).status_code == 422


def test_openapi_has_canonical_schemas(settings):
    with client(settings) as api:
        schemas = api.get("/openapi.json").json()["components"]["schemas"]
        for name in ("Meeting", "AudioChunk", "TranscriptSegment", "Speaker", "Summary", "ActionItem", "ProcessingJob", "UsageRecord"):
            assert name in schemas
        assert schemas["TranscriptSegment"]["properties"]["start_ms"]["anyOf"][1]["type"] == "null"


@pytest.mark.asyncio
async def test_upload_real_ffmpeg_prepares_then_waits_for_key(settings):
    settings.polza_api_key = ""  # restore validated type below
    settings = Settings(**{**settings.model_dump(), "polza_api_key": ""}, _env_file=None)
    with client(settings) as api:
        meeting = api.post("/api/v1/meetings", headers=auth(api), json={"title": "Import"}).json()
        result = api.post(f"/api/v1/meetings/{meeting['id']}/upload", headers=auth(api), files={"file": ("synthetic.wav", wav_bytes(), "audio/wav")})
        assert result.status_code == 202
        worker = api.app.state.worker
        assert await worker.run_once()
        assert len(api.get(f"/api/v1/meetings/{meeting['id']}/chunks").json()) == 1
        assert await worker.run_once()
        jobs = api.get(f"/api/v1/meetings/{meeting['id']}/jobs").json()
        assert [j["status"] for j in jobs] == ["succeeded", "waiting_config"]
        assert api.get(f"/api/v1/meetings/{meeting['id']}/segments").json()["items"] == []
        assert api.get(f"/api/v1/meetings/{meeting['id']}/audio").status_code == 200
        assert Path(api.app.state.db.meeting(meeting["id"], internal=True)["media_path"]).exists()


def test_empty_upload_and_reupload_are_distinct(settings):
    with client(settings) as api:
        meeting = api.post("/api/v1/meetings", headers=auth(api), json={"title": "Import"}).json()
        url = f"/api/v1/meetings/{meeting['id']}/upload"
        assert api.post(url, headers=auth(api), files={"file": ("empty.wav", b"")}).status_code == 422
        assert api.post(url, headers=auth(api), files={"file": ("input.wav", wav_bytes())}).status_code == 202
        assert api.post(url, headers=auth(api), files={"file": ("input.wav", wav_bytes())}).status_code == 409


def test_atomic_claim_and_duplicate_enqueue(db):
    meeting = db.create_meeting("Atomic")
    jobs = [db.enqueue(meeting["id"], "prepare") for _ in range(5)]
    assert len({j["id"] for j in jobs}) == 1
    with ThreadPoolExecutor(max_workers=5) as pool:
        claims = list(pool.map(lambda _: db.claim(), range(5)))
    assert sum(item is not None for item in claims) == 1


@pytest.mark.asyncio
async def test_pipeline_idempotency_and_summary_version(db, settings):
    meeting, chunk = prepared(db, settings)
    worker = Worker(db, settings, FakeProvider)
    first = worker.enqueue_transcription(meeting["id"])
    assert worker.enqueue_transcription(meeting["id"])["id"] == first["id"]
    await worker.run_once()
    await worker.run_once()
    assert db.summary(meeting["id"])["action_items"][0]["owner"] == "Иван"
    assert db.segments(meeting["id"])["total"] == 1
    assert worker.enqueue_transcription(meeting["id"])["stage"] == "summarize"
    worker.enqueue_summary(meeting["id"], retry=True)
    await worker.run_once()
    assert db.summary(meeting["id"])["summary_version"] == 2
    assert db.segments(meeting["id"])["total"] == 1


@pytest.mark.asyncio
async def test_complete_transcript_explicit_retry_creates_version(db, settings):
    meeting, chunk = prepared(db, settings)
    worker = Worker(db, settings, FakeProvider)
    worker.enqueue_transcription(meeting["id"])
    await worker.run_once()
    await worker.run_once()
    worker.enqueue_transcription(meeting["id"], retry=True)
    assert db.meeting(meeting["id"])["transcript_version"] == 2
    assert db.summary(meeting["id"]) is None
    await worker.run_once()
    assert db.segments(meeting["id"])["total"] == 1
    assert db.segments(meeting["id"], version=1)["total"] == 1


@pytest.mark.asyncio
async def test_restart_cloud_request_uncertain_and_not_resubmitted(db, settings):
    meeting, chunk = prepared(db, settings)
    worker = Worker(db, settings, FakeProvider)
    job = worker.enqueue_transcription(meeting["id"])
    claimed = db.claim()
    usage = db.reserve(claimed, 1, 100, False)
    restarted = Worker(db, settings, FakeProvider)
    assert db.job(job["id"])["status"] == "uncertain"
    assert db.one("SELECT status FROM usage WHERE id=?", (usage,))["status"] == "unknown"
    assert restarted.enqueue_transcription(meeting["id"], retry=True)["id"] == job["id"]
    assert db.job(job["id"])["status"] == "uncertain"
    assert not await restarted.run_once()


def test_restart_local_job_requeued_and_capture_retained(db):
    meeting = db.create_meeting("Restart")
    db.update_meeting(meeting["id"], recording=1)
    job = db.enqueue(meeting["id"], "prepare")
    db.claim()
    db.recover()
    assert db.job(job["id"])["status"] == "queued"
    assert db.meeting(meeting["id"])["status"] == "interrupted"


def test_budget_counts_in_flight_and_unknown_is_not_free(db):
    meeting = db.create_meeting("Budget")
    job = db.enqueue(meeting["id"], "summarize")
    db.reserve(job, 8, 10, False)
    with pytest.raises(ValueError, match="budget"):
        db.reserve(job, 3, 10, False)
    with pytest.raises(ValueError, match="Unknown model price"):
        db.reserve(job, None, 100, False)
    with pytest.raises(ValueError, match="positive"):
        db.reserve(job, None, 100, True)
    unknown = db.reserve(job, None, 100, True, unknown_reservation=2)
    db.settle(unknown)
    db.reserve(job, 1, 100, False)
    # Legacy unresolved request has no conservative reservation: never treated as0.
    db.execute("UPDATE usage SET reserved_rub=NULL WHERE id=?", (unknown,))
    with pytest.raises(ValueError, match="unknown cost"):
        db.reserve(job, 1, 100, False)


@pytest.mark.asyncio
async def test_unknown_price_pauses_without_call(db, settings):
    meeting, chunk = prepared(db, settings)
    settings.stt_price_rub_per_minute = None
    class Forbidden(FakeProvider):
        async def transcribe(self, *args, **kwargs):
            pytest.fail("Provider called without price authorization")
    worker = Worker(db, settings, Forbidden)
    job = worker.enqueue_transcription(meeting["id"])
    await worker.run_once()
    assert db.job(job["id"])["status"] == "paused_budget"
    assert Path(chunk["path"]).exists()


@pytest.mark.asyncio
async def test_timeout_uncertain_audio_and_estimate_retained(db, settings):
    meeting, chunk = prepared(db, settings)
    class Timeout(FakeProvider):
        async def transcribe(self, *args, **kwargs):
            raise ProviderError("timeout", "Response lost", uncertain=True)
    worker = Worker(db, settings, Timeout)
    job = worker.enqueue_transcription(meeting["id"])
    await worker.run_once()
    assert db.job(job["id"])["status"] == "uncertain"
    record = db.one("SELECT * FROM usage WHERE job_id=?", (job["id"],))
    assert record["status"] == "unknown" and record["estimated_rub"] == .05
    assert Path(chunk["path"]).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("code,status", [("authentication", "waiting_config"), ("insufficient_funds", "paused_budget"), ("payload_too_large", "failed")])
async def test_rejected_provider_request_releases_reservation(db, settings, code, status):
    meeting, chunk = prepared(db, settings)
    class Rejected(FakeProvider):
        async def transcribe(self, *args, **kwargs):
            raise ProviderError(code, "Explicit rejection")
    worker = Worker(db, settings, Rejected)
    job = worker.enqueue_transcription(meeting["id"])
    await worker.run_once()
    assert db.job(job["id"])["status"] == status
    assert db.one("SELECT status FROM usage WHERE job_id=?", (job["id"],))["status"] == "released"


@pytest.mark.asyncio
async def test_partial_failure_does_not_summarize_incomplete_transcript(db, settings):
    meeting, one = prepared(db, settings)
    prepared(db, settings, meeting=meeting, sequence=1, offset=1000)
    class Partial(FakeProvider):
        async def transcribe(self, path, *, offset_ms=0, channel="import"):
            if offset_ms:
                raise ProviderError("invalid_request", "Second chunk rejected")
            return await super().transcribe(path, offset_ms=offset_ms, channel=channel)
    worker = Worker(db, settings, Partial)
    worker.enqueue_transcription(meeting["id"])
    await worker.run_once()
    await worker.run_once()
    assert db.segments(meeting["id"])["total"] == 1
    assert db.summary(meeting["id"]) is None
    assert not any(j["stage"] == "summarize" for j in db.jobs(meeting["id"]))


@pytest.mark.asyncio
async def test_empty_transcript_summary_has_no_cloud_request_or_reservation(db, settings):
    meeting, chunk = prepared(db, settings)
    job = db.enqueue(meeting["id"], "transcribe", chunk_id=chunk["id"], version=db.ensure_version(meeting["id"]))
    db.finish_job(job["id"], "succeeded")
    settings = Settings(**{**settings.model_dump(), "polza_api_key": "", "stt_price_rub_per_minute": None, "summary_input_rub_per_million": None}, _env_file=None)
    def forbidden(_):
        pytest.fail("Empty summary invoked provider")
    worker = Worker(db, settings, forbidden)
    worker.enqueue_summary(meeting["id"])
    await worker.run_once()
    assert db.summary(meeting["id"])["action_items"] == []
    assert db.rows("SELECT * FROM usage") == []
    with client(settings) as api:
        # client fixture DB differs; direct API ready empty summary is covered below
        assert api.get("/api/v1/config").json()["key_configured"] is False


def test_empty_ready_tasks_api_and_exports(settings):
    with client(settings) as api:
        db = api.app.state.db
        meeting = db.create_meeting("Empty ready")
        version = db.ensure_version(meeting["id"])
        job = db.enqueue(meeting["id"], "summarize", version=version)
        db.save_summary(job, {"meeting_id": meeting["id"], "transcript_version": version, "summary_version": 0, "overview": "", "readable_transcript": "", "decisions": [], "action_items": [], "open_questions": [], "status": "succeeded"})
        response = api.get(f"/api/v1/meetings/{meeting['id']}/tasks")
        assert response.status_code == 200 and response.json() == []
        for format in ("txt", "md", "json", "docx"):
            assert api.get(f"/api/v1/meetings/{meeting['id']}/export?format={format}").status_code == 200


def test_evidence_validation_preserves_nulls_and_rejects_unknown_refs():
    segments = [{"id": "source", "text": "Сделаем отчёт. Кто возьмёт задачу?"}]
    raw = {"overview": "Отчёт", "readable_transcript": "", "decisions": [], "action_items": [{"text": "Отчёт", "owner": "Иван", "due_date": "2026-10-02", "source_segment_ids": ["source"]}], "open_questions": []}
    result = validate_summary(raw, "meeting", 1, segments)
    assert result["action_items"][0]["owner"] is None
    assert result["action_items"][0]["due_date"] is None
    raw["action_items"][0]["source_segment_ids"] = ["invented"]
    with pytest.raises(ValueError, match="Invalid source"):
        validate_summary(raw, "meeting", 1, segments)


@pytest.mark.asyncio
async def test_native_audio_cloud_derivative_preserves_original(db, settings):
    meeting, chunk = prepared(db, settings, native=True)
    original_size = Path(chunk["path"]).stat().st_size
    derivative = await cloud_audio(settings, chunk)
    assert derivative != Path(chunk["path"])
    with wave.open(str(derivative), "rb") as stream:
        assert stream.getframerate() == 16000 and stream.getnchannels() == 1
    assert Path(chunk["path"]).stat().st_size == original_size


def test_timestamps_unknown_and_outside_chunk_rejected(db, settings):
    meeting, chunk = prepared(db, settings, offset=1000)
    worker = Worker(db, settings, FakeProvider)
    job = worker.enqueue_transcription(meeting["id"])
    with pytest.raises(ValueError, match="outside"):
        worker.normalize_segments(job, chunk, {"segments": [{"text": "X", "start_ms": 0, "end_ms": 100, "timing_precision": "segment"}]})
    segments, _ = worker.normalize_segments(job, chunk, {"segments": [{"text": "X", "start_ms": None, "end_ms": None, "timing_precision": "unknown"}]})
    assert segments[0]["start_ms"] is None


def test_speaker_labels_scoped_to_chunk_and_channel_order(db, settings):
    meeting, system = prepared(db, settings, channel="system", offset=1000)
    _, microphone = prepared(db, settings, meeting=meeting, channel="microphone", offset=0)
    worker = Worker(db, settings, FakeProvider)
    worker.enqueue_transcription(meeting["id"])
    jobs = db.rows("SELECT * FROM jobs")
    for job in jobs:
        chunk = system if job["chunk_id"] == system["id"] else microphone
        segments, speakers = worker.normalize_segments(job, chunk, {"segments": [{"text": chunk["channel"], "timing_precision": "chunk", "start_ms": chunk["offset_ms"], "end_ms": chunk["offset_ms"] + 1000, "speaker_label": "0"}]})
        db.save_segments(job, segments, speakers)
    items = db.segments(meeting["id"])["items"]
    assert [i["channel"] for i in items] == ["microphone", "system"]
    assert items[0]["speaker_id"] != items[1]["speaker_id"]


def test_env_example_empty_prices_valid(tmp_path, monkeypatch):
    # The example describes a fresh no-key install, independent of credentials
    # inherited by the test process from an already configured host.
    monkeypatch.delenv("POLZA_API_KEY", raising=False)
    source = Path(__file__).resolve().parents[1] / ".env.example"
    local = tmp_path / "example.env"
    local.write_text(source.read_text(), encoding="utf-8")
    settings = Settings(_env_file=local)
    assert not settings.key_configured
    assert settings.stt_price_rub_per_minute is None
    assert settings.summary_model


def test_unknown_reservation_is_positive_and_counts_budget(db):
    meeting = db.create_meeting("Unknown prices")
    job = db.enqueue(meeting["id"], "summarize")
    with pytest.raises(ValueError, match="positive"):
        db.reserve(job, None, 3, True)
    for _ in range(3):
        usage = db.reserve(job, None, 3, True, unknown_reservation=1)
        db.settle(usage)
    with pytest.raises(ValueError, match="budget"):
        db.reserve(job, None, 3, True, unknown_reservation=1)


def test_stale_summary_cannot_publish_ready(db):
    meeting = db.create_meeting("Version race")
    db.update_meeting(meeting["id"], transcript_version=1)
    job = db.enqueue(meeting["id"], "summarize", version=1)
    db.update_meeting(meeting["id"], transcript_version=2, status="queued")
    raw = {"meeting_id": meeting["id"], "transcript_version": 1, "summary_version": 0, "overview": "Old", "readable_transcript": "", "decisions": [], "action_items": [], "open_questions": [], "status": "succeeded"}
    assert db.save_summary(job, raw) is None
    assert db.meeting(meeting["id"])["status"] == "queued"
    assert db.job(job["id"])["status"] == "cancelled"
    assert db.summary(meeting["id"]) is None


def test_cancelled_prepare_recovery_not_zombie(db):
    meeting = db.create_meeting("Cancel restart")
    job = db.enqueue(meeting["id"], "prepare")
    db.claim()
    db.cancel(job["id"])
    db.recover()
    assert db.job(job["id"])["status"] == "cancelled"
    assert db.claim() is None


@pytest.mark.asyncio
async def test_cancel_inflight_retains_completed_text_and_terminal_status(db, settings):
    meeting, chunk = prepared(db, settings)
    class CancelDuring(FakeProvider):
        async def transcribe(self, path, *, offset_ms=0, channel="import"):
            running = db.one("SELECT id FROM jobs WHERE status='running'")
            db.cancel(running["id"])
            return await super().transcribe(path, offset_ms=offset_ms, channel=channel)
    worker = Worker(db, settings, CancelDuring)
    job = worker.enqueue_transcription(meeting["id"])
    await worker.run_once()
    assert db.job(job["id"])["status"] == "cancelled"
    assert db.meeting(meeting["id"])["status"] == "cancelled"
    assert db.segments(meeting["id"])["total"] == 1
    assert db.summary(meeting["id"]) is None


@pytest.mark.asyncio
async def test_async_poll_failure_manual_retry_reuses_same_provider_job(db, settings):
    meeting, chunk = prepared(db, settings)
    seen = []
    class Polling(FakeProvider):
        async def transcribe(self, path, *, offset_ms=0, channel="import", provider_job_id=None, on_provider_job=None):
            seen.append(provider_job_id)
            if provider_job_id is None:
                await on_provider_job("already-accepted")
                raise ProviderError("poll_unavailable", "Poll connection unavailable", retryable=True, provider_job_id="already-accepted")
            return await super().transcribe(path, offset_ms=offset_ms, channel=channel)
    worker = Worker(db, settings, Polling)
    job = worker.enqueue_transcription(meeting["id"])
    db.execute("UPDATE jobs SET attempts=2 WHERE id=?", (job["id"],))
    await worker.run_once()
    assert db.job(job["id"])["status"] == "uncertain"
    retry = worker.enqueue_transcription(meeting["id"], retry=True)
    assert retry["id"] == job["id"]
    await worker.run_once()
    assert seen == [None, "already-accepted"]
    assert len(db.rows("SELECT * FROM usage WHERE job_id=?", (job["id"],))) == 1


@pytest.mark.asyncio
async def test_preparation_never_includes_stale_attempt_audio(settings, tmp_path):
    source = tmp_path / "source.wav"
    source.write_bytes(wav_bytes())
    directory = tmp_path / "output"
    stale = directory / "building"
    stale.mkdir(parents=True)
    (stale / "part-000001.wav").write_bytes(wav_bytes())
    chunks = await prepare_audio(settings, source, directory)
    assert len(chunks) == 1 and sum(c["duration_ms"] for c in chunks) == 1000


def test_export_preserves_hash_terms_and_source_ids():
    text = "C# и F# используются; issue # 42; markdown ## заголовок."
    segments = [{"id": "source-id", "text": text, "channel": "import", "timing_precision": "unknown", "start_ms": None, "end_ms": None}]
    content, _ = export_meeting({"title": "C# review"}, segments, None, "txt")
    assert text in content.decode()
    assert "[source-id]" in content.decode()


@pytest.mark.asyncio
async def test_parallel_uploads_have_one_lease_and_no_file_mixture(settings):
    app = create_app(settings, provider_factory=FakeProvider, capture=FakeCapture(), run_worker=False)
    meeting = app.state.db.create_meeting("Race")
    endpoint = next(route.endpoint for route in app.routes if getattr(route, "path", "").endswith("/upload"))
    entered = asyncio.Event()
    release = asyncio.Event()
    class File:
        def __init__(self, content, block=False):
            self.content, self.block, self.closed = content, block, False
        async def read(self, limit):
            if self.block:
                entered.set()
                await release.wait()
            content, self.content = self.content, b""
            return content
        async def close(self):
            self.closed = True
    first, second = File(b"A" * 64, True), File(b"BBB")
    pending = asyncio.create_task(endpoint(meeting["id"], first))
    await entered.wait()
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await endpoint(meeting["id"], second)
    assert exc.value.status_code == 409 and second.closed
    release.set()
    await pending
    original = Path(app.state.db.meeting(meeting["id"], internal=True)["media_path"])
    assert original.read_bytes() == b"A" * 64


@pytest.mark.asyncio
async def test_body_limit_prevents_oversized_spooling_with_and_without_length(settings, monkeypatch):
    settings.max_upload_bytes = 1024
    app = create_app(settings, provider_factory=FakeProvider, capture=FakeCapture(), run_worker=False)
    meeting = app.state.db.create_meeting("Bounded multipart")
    written = 0
    from starlette.datastructures import UploadFile
    original_write = UploadFile.write
    async def counted_write(self, data):
        nonlocal written
        written += len(data)
        await original_write(self, data)
    monkeypatch.setattr(UploadFile, "write", counted_write)
    payload = b"--x\r\nContent-Disposition: form-data; name=\"file\"; filename=\"x.wav\"\r\nContent-Type: audio/wav\r\n\r\n" + b"a" * 150000 + b"\r\n--x--\r\n"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as api:
        token = (await api.get("/api/v1/session")).json()["csrf_token"]
        headers = {"Content-Type": "multipart/form-data; boundary=x", "X-Secretary-Token": token}
        url = f"/api/v1/meetings/{meeting['id']}/upload"
        response = await api.post(url, content=payload, headers=headers)
        assert response.status_code == 413 and written == 0
        async def pieces():
            for offset in range(0, len(payload), 32768):
                yield payload[offset:offset + 32768]
        response = await api.post(url, content=pieces(), headers=headers)
        assert response.status_code == 413
        assert written <= settings.max_upload_bytes + 65536


@pytest.mark.asyncio
async def test_summary_checkpoint_retry_does_not_duplicate_payment_ledger(db, settings):
    meeting, chunk = prepared(db, settings)
    calls, rejected = [], False
    class Maps(FakeProvider):
        async def summarize(self, segments, *, before_request, on_usage, checkpoint_get, checkpoint_put):
            nonlocal rejected
            receipts = []
            for key in ("map-one", "map-two"):
                cached = await checkpoint_get(key)
                if cached:
                    receipts.append(cached["receipt"])
                    continue
                await before_request("summary", 100, 500)
                calls.append(key)
                if key == "map-two" and not rejected:
                    rejected = True
                    raise ProviderError("rate_limited", "Second map explicitly rejected", retryable=True, usage_records=receipts)
                receipt = {"confirmed_rub": .1, "provider_request_id": key}
                await on_usage(receipt)
                await checkpoint_put(key, {"receipt": receipt})
                receipts.append(receipt)
            return {"overview": "", "readable_transcript": "", "decisions": [], "action_items": [], "open_questions": [], "usage": {"confirmed_rub": .2}}
    worker = Worker(db, settings, Maps)
    worker.enqueue_transcription(meeting["id"])
    await worker.run_once()
    await worker.run_once()
    summary_job = db.one("SELECT * FROM jobs WHERE stage='summarize'")
    assert summary_job["status"] == "queued"
    ledger = db.rows("SELECT * FROM usage WHERE job_id=? ORDER BY created_at", (summary_job["id"],))
    assert [r["status"] for r in ledger] == ["confirmed", "released"]
    assert sum(r["confirmed_rub"] or 0 for r in ledger) == pytest.approx(.1)
    restarted = Worker(db, settings, Maps)
    await restarted.run_once()
    assert calls == ["map-one", "map-two", "map-two"]
    assert db.summary(meeting["id"]) is not None
    ledger = db.rows("SELECT * FROM usage WHERE job_id=?", (summary_job["id"],))
    assert sum(r["confirmed_rub"] or 0 for r in ledger) == pytest.approx(.2)


@pytest.mark.asyncio
async def test_prepare_failed_stage_can_retry_original_source_and_snapshot(db, settings):
    meeting = db.create_meeting("Prepare retry")
    source = settings.data_dir / "original.wav"
    source.write_bytes(wav_bytes(seconds=31))
    db.update_meeting(meeting["id"], media_path=str(source))
    prior = db.enqueue(meeting["id"], "prepare", payload={"chunk_seconds": 120})
    db.finish_job(prior["id"], "failed", "Synthetic interrupted local attempt")
    settings.chunk_seconds = 15
    worker = Worker(db, settings, FakeProvider)
    retry = worker.enqueue_prepare(meeting["id"], retry=True)
    assert retry["id"] != prior["id"]
    await worker.run_once()
    assert len(db.chunks(meeting["id"])) == 1
    assert source.exists()


def test_api_factory_schema_export_does_not_recover_running_job(settings):
    app = create_app(settings, provider_factory=FakeProvider, capture=FakeCapture(), run_worker=False)
    meeting = app.state.db.create_meeting("Active job")
    job = app.state.db.enqueue(meeting["id"], "transcribe")
    app.state.db.claim()
    exported = create_app(settings, provider_factory=FakeProvider, capture=FakeCapture(), run_worker=False)
    exported.openapi()
    assert app.state.db.job(job["id"])["status"] == "running"


def test_capture_finish_updates_immediately_and_failure_not_erased_by_stop(settings):
    class Recorder(FakeCapture):
        def __init__(self):
            self.snapshot = {"status": "idle", "recording": False, "error": None}
        def start(self, meeting_id, microphone_id, system_id, on_chunk, on_error, on_finish=None):
            self.finish = on_finish
            self.snapshot = {"status": "recording", "recording": True, "error": None}
            return self.snapshot
        def stop(self, meeting_id):
            return self.snapshot
        def state(self, meeting_id):
            return self.snapshot
    capture = Recorder()
    app = create_app(settings, provider_factory=FakeProvider, capture=capture, run_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8765") as api:
        meeting = api.post("/api/v1/meetings", headers=auth(api), json={"title": "Synthetic finish"}).json()
        assert api.post(f"/api/v1/meetings/{meeting['id']}/recording/start", headers=auth(api), json={"auto_process": False}).status_code == 200
        assert app.state.db.meeting(meeting["id"], internal=True)["recording"] == 1
        capture.snapshot = {"status": "failed", "recording": False, "error": "Synthetic device failure", "chunks": []}
        capture.finish(capture.snapshot)
        current = app.state.db.meeting(meeting["id"], internal=True)
        assert current["recording"] == 0 and current["capture_error"] == "Synthetic device failure"
        assert api.post(f"/api/v1/meetings/{meeting['id']}/recording/stop", headers=auth(api)).status_code == 200
        assert api.get(f"/api/v1/meetings/{meeting['id']}").json()["status"] == "partial_error"


def test_capture_incompleteness_remains_visible_after_summary(db):
    meeting = db.create_meeting("Partial recording")
    db.update_meeting(meeting["id"], transcript_version=1, capture_error="Device stopped early")
    job = db.enqueue(meeting["id"], "summarize", version=1)
    summary = validate_summary({"overview": "", "readable_transcript": "", "decisions": [], "action_items": [], "open_questions": []}, meeting["id"], 1, [])
    db.save_summary(job, summary)
    assert db.meeting(meeting["id"])["status"] == "partial_ready"
    assert db.meeting(meeting["id"])["error"] == "Device stopped early"


def native_part(db, settings, meeting_id, sequence, offset_ms, *, channel="system", value=100, frames=4800):
    folder = settings.data_dir / "audio" / meeting_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{channel}-{sequence}.wav"
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(48000)
        stream.writeframes(struct.pack("<hh", value, value) * frames)
    return db.add_chunk(meeting_id, {"path": str(path), "sequence": sequence, "channel": channel, "offset_ms": offset_ms, "duration_ms": frames * 1000 // 48000, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})


@pytest.mark.asyncio
async def test_playback_concurrent_ranges_keep_open_cached_version_immutable(settings):
    app = create_app(settings, provider_factory=FakeProvider, capture=FakeCapture(), run_worker=False)
    db = app.state.db
    meeting = db.create_meeting("Concurrent synthetic playback")
    native_part(db, settings, meeting["id"], 0, 0, value=100)
    native_part(db, settings, meeting["id"], 1, 100, value=200)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://127.0.0.1:8765") as api:
        url = f"/api/v1/meetings/{meeting['id']}/audio?channel=system"
        responses = await asyncio.gather(*(api.get(url) for _ in range(8)))
        assert all(response.status_code == 200 for response in responses)
        first = responses[0].content
        assert all(response.content == first for response in responses)
        with wave.open(io.BytesIO(first), "rb") as stream:
            assert stream.getnchannels() == 2 and stream.getframerate() == 48000
            assert stream.getnframes() == 9600
            assert stream.readframes(9600) == struct.pack("<hh", 100, 100) * 4800 + struct.pack("<hh", 200, 200) * 4800
        directory = settings.data_dir / "audio" / meeting["id"]
        cached = next(directory.glob("playback-system*.wav"))
        with cached.open("rb") as active_player:
            original_cache = active_player.read()
            native_part(db, settings, meeting["id"], 2, 200, value=300)
            updated = await api.get(url)
            assert updated.status_code == 200
            active_player.seek(0)
            assert active_player.read() == original_cache
            assert cached.read_bytes() == original_cache
            versions = list(directory.glob("playback-system*.wav"))
            assert len(versions) == 2
            with wave.open(io.BytesIO(updated.content), "rb") as stream:
                assert stream.getnframes() == 14400
        ranged = await api.get(url, headers={"Range": "bytes=44-47"})
        assert ranged.status_code == 206 and len(ranged.content) == 4


@pytest.mark.asyncio
async def test_playback_uses_channel_offsets_and_bounded_pcm_copy(settings, monkeypatch):
    app = create_app(settings, provider_factory=FakeProvider, capture=FakeCapture(), run_worker=False)
    db = app.state.db
    meeting = db.create_meeting("Synthetic channel timeline")
    later = native_part(db, settings, meeting["id"], 0, 600, value=200)
    earlier = native_part(db, settings, meeting["id"], 1, 250, value=100)
    native_part(db, settings, meeting["id"], 0, 0, channel="microphone", value=999)
    original_hashes = [hashlib.sha256(Path(part["path"]).read_bytes()).hexdigest() for part in (earlier, later)]
    reads = []
    original_read = wave.Wave_read.readframes
    def bounded_read(self, frames):
        reads.append(frames)
        return original_read(self, frames)
    monkeypatch.setattr(wave.Wave_read, "readframes", bounded_read)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as api:
        response = await api.get(f"/api/v1/meetings/{meeting['id']}/audio")
        assert response.status_code == 200
        assert response.headers["X-Secretary-Audio-Channel"] == "system"
        assert reads and max(reads) <= 32768
        monkeypatch.setattr(wave.Wave_read, "readframes", original_read)
        with wave.open(io.BytesIO(response.content), "rb") as stream:
            assert stream.getnframes() == 33600  # end offset600ms + duration100ms
            pcm = stream.readframes(stream.getnframes())
        expected = b"\0" * (12000 * 4) + struct.pack("<hh", 100, 100) * 4800 + b"\0" * (12000 * 4) + struct.pack("<hh", 200, 200) * 4800
        assert pcm == expected
    assert [hashlib.sha256(Path(part["path"]).read_bytes()).hexdigest() for part in (earlier, later)] == original_hashes


@pytest.mark.asyncio
async def test_playback_single_delayed_channel_keeps_meeting_time_origin(settings):
    app = create_app(settings, provider_factory=FakeProvider, capture=FakeCapture(), run_worker=False)
    meeting = app.state.db.create_meeting("Delayed native channel")
    native_part(app.state.db, settings, meeting["id"], 0, 150, channel="microphone", value=123)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as api:
        response = await api.get(f"/api/v1/meetings/{meeting['id']}/audio?channel=microphone")
        assert response.status_code == 200
        with wave.open(io.BytesIO(response.content), "rb") as stream:
            assert stream.getnframes() == 12000
            assert stream.readframes(7200) == b"\0" * (7200 * 4)
            assert stream.readframes(4800) == struct.pack("<hh", 123, 123) * 4800


@pytest.mark.asyncio
@pytest.mark.parametrize("status,retry", [("cancelled", False), ("failed", False), ("uncertain", False), ("uncertain", True)])
async def test_transcribe_non_runnable_repeat_preserves_meeting_status_error_and_no_submit(db, settings, status, retry):
    meeting, chunk = prepared(db, settings)
    def forbidden(_):
        pytest.fail("Existing terminal or ambiguous job must not submit an external request")
    worker = Worker(db, settings, forbidden)
    job = worker.enqueue_transcription(meeting["id"])
    db.finish_job(job["id"], status, "Preserved stage failure")
    original_status = "cancelled" if status == "cancelled" else "partial_error"
    db.update_meeting(meeting["id"], status=original_status, error="Preserved meeting failure")
    returned = worker.enqueue_transcription(meeting["id"], retry=retry)
    assert returned["id"] == job["id"] and returned["status"] == status
    current = db.meeting(meeting["id"])
    assert current["status"] == original_status and current["error"] == "Preserved meeting failure"
    assert len(db.jobs(meeting["id"])) == 1
    assert not await worker.run_once()


def test_transcribe_async_poll_requeue_returns_refreshed_row(db, settings):
    meeting, chunk = prepared(db, settings)
    worker = Worker(db, settings, FakeProvider)
    job = worker.enqueue_transcription(meeting["id"])
    db.finish_job(job["id"], "uncertain", "Poll response lost")
    db.execute("UPDATE jobs SET provider_job_id='already-accepted',cancel_requested=1 WHERE id=?", (job["id"],))
    db.update_meeting(meeting["id"], status="partial_error", error="Poll response lost")
    returned = worker.enqueue_transcription(meeting["id"], retry=True)
    assert returned["id"] == job["id"]
    assert returned["status"] == "queued" and returned["cancel_requested"] == 0
    assert returned["provider_job_id"] == "already-accepted"
    assert db.meeting(meeting["id"])["status"] == "queued"


def test_transcribe_mixed_jobs_returns_runnable_and_running_is_not_queued(db, settings):
    meeting, first = prepared(db, settings)
    _, second = prepared(db, settings, meeting=meeting, sequence=1, offset=1000)
    worker = Worker(db, settings, FakeProvider)
    first_job = worker.enqueue_transcription(meeting["id"])
    db.finish_job(first_job["id"], "cancelled")
    returned = worker.enqueue_transcription(meeting["id"])
    assert returned["chunk_id"] == second["id"] and returned["status"] == "queued"
    db.claim()
    db.update_meeting(meeting["id"], status="transcribe")
    running = worker.enqueue_transcription(meeting["id"])
    assert running["status"] == "running"
    assert db.meeting(meeting["id"])["status"] == "transcribe"
