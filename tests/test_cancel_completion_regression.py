"""Offline regressions for cancellation boundaries and durable paid STT completion."""
from __future__ import annotations

import asyncio
import json
import wave
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import secretary.application.worker as worker_module
from secretary.api import create_app
from secretary.application.worker import Worker
from secretary.infrastructure.database import Database
from secretary.settings import Settings

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def no_http(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("Real HTTP is forbidden in this regression suite")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny)


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, project_dir=tmp_path, data_dir=tmp_path / "data",
                        polza_api_key="REGRESSION-FAKE-NEVER-REAL", cloud_enabled=True,
                        polza_base_url="http://127.0.0.1:9", local_cost_limits_enabled=True,
                        stt_price_rub_per_minute=.1, summary_input_rub_per_million=5,
                        summary_output_rub_per_million=20, meeting_budget_rub=100)
    db = Database(settings.data_dir / "regression.sqlite3")
    meeting = db.create_meeting("Synthetic cancellation regression")
    async def local_audio(settings, chunk):
        return Path(chunk["path"])
    monkeypatch.setattr(worker_module, "cloud_audio", local_audio)
    return settings, db, meeting


def add_chunk(fixture, sequence=0):
    settings, db, meeting = fixture
    path = settings.data_dir / "audio" / meeting["id"] / f"chunk-{sequence}.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as stream:
        stream.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        stream.writeframes(b"\0" * 32000)
    return db.add_chunk(meeting["id"], {"path": str(path), "sequence": sequence, "channel": "import",
                                       "offset_ms": sequence * 1000, "duration_ms": 1000, "sha256": "synthetic"})


class Provider:
    def __init__(self, db, *, empty=False, cancel_first=False, close_error=False):
        self.db, self.empty, self.cancel_first, self.close_error = db, empty, cancel_first, close_error
        self.calls, self.summary_calls = [], 0

    async def transcribe(self, path, *, offset_ms, channel):
        self.calls.append(path)
        if self.cancel_first and len(self.calls) == 1:
            self.db.cancel(self.db.one("SELECT id FROM jobs WHERE stage='transcribe' AND status='running'")["id"])
        return {"segments": [] if self.empty else [{"text": "Synthetic retained words", "start_ms": offset_ms,
                                                    "end_ms": offset_ms + 1000, "timing_precision": "segment"}],
                "usage": {"confirmed_rub": .06, "provider_request_id": f"fake-{len(self.calls)}"}}

    async def summarize(self, segments, *, before_request=None, on_usage=None):
        self.summary_calls += 1
        if before_request:
            await before_request("summary", 100, 512)
        receipt = {"confirmed_rub": .02, "provider_request_id": "fake-summary"}
        if on_usage:
            await on_usage(receipt)
        return {"overview": "Synthetic summary", "readable_transcript": "Saved words", "decisions": [],
                "action_items": [], "open_questions": [], "usage": receipt}

    async def aclose(self):
        if self.close_error:
            raise RuntimeError("Synthetic cleanup failed")


async def drain(worker):
    for _ in range(10):
        if not await worker.run_once():
            return
    pytest.fail("Synthetic worker did not finish in ten local jobs")


async def test_cancel_during_conversion_prevents_first_dispatch_and_releases_reservation(fixture, monkeypatch):
    settings, db, meeting = fixture
    chunk = add_chunk(fixture)
    entered, converted = asyncio.Event(), asyncio.Event()
    async def conversion(settings, chunk):
        entered.set()
        await converted.wait()
        return Path(chunk["path"])
    monkeypatch.setattr(worker_module, "cloud_audio", conversion)
    provider = Provider(db)
    worker = Worker(db, settings, lambda cfg: provider)
    job = worker.enqueue_transcription(meeting["id"])
    running = asyncio.create_task(worker.run_once())
    await entered.wait()
    db.cancel(job["id"])
    converted.set()
    await running
    assert provider.calls == []
    assert db.job(job["id"])["status"] == "cancelled"
    assert db.meeting(meeting["id"])["status"] == "cancelled"
    assert db.rows("SELECT status,confirmed_rub FROM usage") == [{"status": "released", "confirmed_rub": None}]
    assert Path(chunk["path"]).exists() and db.segments(meeting["id"])["total"] == 0


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("chunk_count", [1, 3])
@pytest.mark.parametrize("cancel_remainder", [False, True])
async def test_cancelled_committed_result_continues_same_version_after_restart(fixture, empty, chunk_count, cancel_remainder):
    settings, db, meeting = fixture
    for sequence in range(chunk_count):
        add_chunk(fixture, sequence)
    provider = Provider(db, empty=empty, cancel_first=True)
    worker = Worker(db, settings, lambda cfg: provider)
    original = worker.enqueue_transcription(meeting["id"])
    if cancel_remainder:
        for pending in db.rows("SELECT id FROM jobs WHERE id!=? AND stage='transcribe'", (original["id"],)):
            db.cancel(pending["id"])
    await worker.run_once()
    retained = db.job(original["id"], internal=True)
    assert retained["status"] == "cancelled"
    assert worker.transcription_completed(retained)
    assert db.segments(meeting["id"])["total"] == (0 if empty else 1)
    assert db.summary(meeting["id"]) is None
    restarted = Worker(Database(db.path), settings, lambda cfg: provider)
    resumed = restarted.enqueue_transcription(meeting["id"], retry=True)
    assert resumed["stage"] == ("summarize" if chunk_count == 1 else "transcribe")
    assert db.meeting(meeting["id"])["status"] == "queued"
    assert db.job(original["id"])["status"] == "succeeded"
    await drain(restarted)
    assert db.meeting(meeting["id"])["transcript_version"] == 1
    assert db.meeting(meeting["id"])["status"] == "ready"
    assert len(provider.calls) == chunk_count and len(set(provider.calls)) == chunk_count
    assert provider.summary_calls == (0 if empty else 1)
    assert db.summary(meeting["id"]) is not None
    succeeded = db.rows("SELECT DISTINCT chunk_id FROM jobs WHERE stage='transcribe' AND version=1 AND status='succeeded'")
    assert len(succeeded) == chunk_count  # The public job status gives UI accurate completion.
    receipts = db.rows("SELECT u.confirmed_rub FROM usage u JOIN jobs j ON j.id=u.job_id WHERE j.stage='transcribe'")
    assert receipts == [{"confirmed_rub": .06}] * chunk_count


async def test_cancelled_result_without_explicit_resume_stays_terminal(fixture):
    settings, db, meeting = fixture
    add_chunk(fixture)
    provider = Provider(db, empty=True, cancel_first=True)
    worker = Worker(db, settings, lambda cfg: provider)
    original = worker.enqueue_transcription(meeting["id"])
    await worker.run_once()
    returned = worker.enqueue_transcription(meeting["id"])
    assert returned["id"] == original["id"] and returned["status"] == "cancelled"
    assert db.meeting(meeting["id"])["status"] == "cancelled"
    assert db.meeting(meeting["id"])["transcript_version"] == 1
    assert await worker.run_once() is False and len(provider.calls) == 1


async def test_missing_result_is_not_committed_empty_success(fixture):
    settings, db, meeting = fixture
    add_chunk(fixture)
    provider = Provider(db)
    async def missing_result(path, *, offset_ms, channel):
        provider.calls.append(path)
        db.cancel(db.one("SELECT id FROM jobs WHERE stage='transcribe' AND status='running'")["id"])
        return {"usage": {"confirmed_rub": .06, "provider_request_id": "fake-missing"}}
    provider.transcribe = missing_result
    worker = Worker(db, settings, lambda cfg: provider)
    original = worker.enqueue_transcription(meeting["id"])
    await worker.run_once()
    stored = db.job(original["id"], internal=True)
    assert stored["status"] == "uncertain" and not worker.transcription_completed(stored)
    assert db.segments(meeting["id"])["total"] == 0
    assert db.summary(meeting["id"]) is None
    worker.enqueue_transcription(meeting["id"], retry=True)
    assert await worker.run_once() is False and len(provider.calls) == 1
    with pytest.raises(ValueError, match="часть аудио"):
        worker.enqueue_summary(meeting["id"])
    assert db.rows("SELECT status,confirmed_rub FROM usage") == [{"status": "confirmed", "confirmed_rub": .06}]


async def test_direct_summary_accepts_committed_cancelled_result_and_explicit_retranscribe_still_versions(fixture):
    settings, db, meeting = fixture
    add_chunk(fixture)
    provider = Provider(db, cancel_first=True)
    worker = Worker(db, settings, lambda cfg: provider)
    original = worker.enqueue_transcription(meeting["id"])
    await worker.run_once()
    worker.maybe_summary(meeting["id"], 1)
    assert db.summary(meeting["id"]) is None and db.claim() is None
    summary = worker.enqueue_summary(meeting["id"])
    assert summary["stage"] == "summarize" and db.job(original["id"])["status"] == "succeeded"
    await worker.run_once()
    assert len(provider.calls) == 1
    retranscription = worker.enqueue_transcription(meeting["id"], retry=True)
    assert retranscription["stage"] == "transcribe" and retranscription["version"] == 2
    assert db.meeting(meeting["id"])["transcript_version"] == 2
    await drain(worker)
    assert len(provider.calls) == 2 and db.summary(meeting["id"]) is not None


@pytest.mark.parametrize("changed_field", ["result_chunk_id", "result_version"])
async def test_completion_marker_cannot_complete_another_chunk_or_version(fixture, changed_field):
    settings, db, meeting = fixture
    add_chunk(fixture)
    provider = Provider(db, empty=True, cancel_first=True)
    worker = Worker(db, settings, lambda cfg: provider)
    original = worker.enqueue_transcription(meeting["id"])
    await worker.run_once()
    job = db.job(original["id"], internal=True)
    payload = json.loads(job["payload"])
    payload[changed_field] = "different" if changed_field == "result_chunk_id" else 2
    db.execute("UPDATE jobs SET payload=? WHERE id=?", (json.dumps(payload), job["id"]))
    assert not worker.transcription_completed(db.job(job["id"], internal=True))
    with pytest.raises(ValueError, match="часть аудио"):
        worker.enqueue_summary(meeting["id"])
    assert len(provider.calls) == 1


@pytest.mark.parametrize("confirmed", [False, True])
async def test_cancel_before_poll_keeps_accepted_id_and_its_existing_receipt(fixture, monkeypatch, confirmed):
    settings, db, meeting = fixture
    add_chunk(fixture)
    provider = Provider(db)
    worker = Worker(db, settings, lambda cfg: provider)
    job = worker.enqueue_transcription(meeting["id"])
    db.execute("UPDATE jobs SET provider_job_id='accepted-existing' WHERE id=?", (job["id"],))
    receipt = db.reserve(job, .1, 100, False)
    db.settle(receipt, {"confirmed_rub": .06, "provider_request_id": "accepted-existing"} if confirmed else None)
    async def cancel_conversion(settings, chunk):
        db.cancel(job["id"])
        return Path(chunk["path"])
    monkeypatch.setattr(worker_module, "cloud_audio", cancel_conversion)
    await worker.run_once()
    stored = db.job(job["id"], internal=True)
    assert stored["status"] == "uncertain" and stored["provider_job_id"] == "accepted-existing"
    assert db.meeting(meeting["id"])["status"] == "partial_error"
    assert provider.calls == []
    assert db.rows("SELECT status,confirmed_rub FROM usage") == [
        {"status": "confirmed" if confirmed else "unknown", "confirmed_rub": .06 if confirmed else None}]


@pytest.mark.parametrize("failure", ["provider_untyped", "raw_write", "save_result"])
async def test_unexpected_failure_after_dispatch_never_enables_another_paid_submit(fixture, monkeypatch, failure):
    settings, db, meeting = fixture
    chunk = add_chunk(fixture)
    provider = Provider(db)
    if failure == "provider_untyped":
        async def broken_transcribe(path, *, offset_ms, channel):
            provider.calls.append(path)
            raise RuntimeError("Unexpected failure after dispatch")
        provider.transcribe = broken_transcribe
    elif failure == "raw_write":
        original_write = Path.write_text
        def broken_write(path, *args, **kwargs):
            if path.suffix == ".partial":
                raise OSError("Synthetic disk full after response")
            return original_write(path, *args, **kwargs)
        monkeypatch.setattr(Path, "write_text", broken_write)
    else:
        def broken_save(*args, **kwargs):
            raise OSError("Synthetic database unavailable after response")
        monkeypatch.setattr(db, "save_segments", broken_save)
    worker = Worker(db, settings, lambda cfg: provider)
    job = worker.enqueue_transcription(meeting["id"])
    await worker.run_once()
    assert db.job(job["id"])["status"] == "uncertain"
    repeat = worker.enqueue_transcription(meeting["id"], retry=True)
    assert repeat["id"] == job["id"] and await worker.run_once() is False
    assert len(provider.calls) == 1 and Path(chunk["path"]).exists()
    assert db.rows("SELECT status,confirmed_rub FROM usage") == [
        {"status": "unknown" if failure == "provider_untyped" else "confirmed",
         "confirmed_rub": None if failure == "provider_untyped" else .06}]


async def test_local_failure_before_dispatch_is_failed_and_released_not_uncertain(fixture, monkeypatch):
    settings, db, meeting = fixture
    add_chunk(fixture)
    provider = Provider(db)
    async def conversion(settings, chunk):
        raise OSError("Synthetic conversion failed before HTTP")
    monkeypatch.setattr(worker_module, "cloud_audio", conversion)
    worker = Worker(db, settings, lambda cfg: provider)
    job = worker.enqueue_transcription(meeting["id"])
    await worker.run_once()
    assert db.job(job["id"])["status"] == "failed" and provider.calls == []
    assert db.rows("SELECT status FROM usage") == [{"status": "released"}]


@pytest.mark.parametrize("cancel", [False, True])
async def test_cleanup_error_after_committed_result_cannot_revoke_completion(fixture, cancel):
    settings, db, meeting = fixture
    add_chunk(fixture)
    provider = Provider(db, cancel_first=cancel, close_error=True)
    worker = Worker(db, settings, lambda cfg: provider)
    job = worker.enqueue_transcription(meeting["id"])
    await worker.run_once()
    assert db.job(job["id"])["status"] == ("cancelled" if cancel else "succeeded")
    assert worker.transcription_completed(db.job(job["id"], internal=True))
    assert db.meeting(meeting["id"])["status"] == "partial_error"
    assert db.meeting(meeting["id"])["error"] == "Synthetic cleanup failed"
    worker.enqueue_transcription(meeting["id"], retry=cancel)
    await drain(worker)
    assert len(provider.calls) == 1 and db.meeting(meeting["id"])["transcript_version"] == 1
    assert db.summary(meeting["id"]) is not None
    assert db.meeting(meeting["id"])["status"] == "partial_error"


async def test_followup_failure_is_visible_and_continuation_uses_committed_stt(fixture, monkeypatch):
    settings, db, meeting = fixture
    add_chunk(fixture)
    provider = Provider(db)
    worker = Worker(db, settings, lambda cfg: provider)
    followup = worker.maybe_summary
    def unavailable_followup(*args):
        raise RuntimeError("Synthetic follow-up failure " + settings.polza_api_key.get_secret_value())
    monkeypatch.setattr(worker, "maybe_summary", unavailable_followup)
    job = worker.enqueue_transcription(meeting["id"])
    await worker.run_once()
    assert db.job(job["id"])["status"] == "succeeded"
    assert db.meeting(meeting["id"])["status"] == "partial_error"
    assert db.meeting(meeting["id"])["error"] == "Synthetic follow-up failure [redacted]"
    assert db.summary(meeting["id"]) is None
    monkeypatch.setattr(worker, "maybe_summary", followup)
    resumed = worker.enqueue_transcription(meeting["id"])
    assert resumed["stage"] == "summarize" and resumed["status"] == "queued"
    await drain(worker)
    assert len(provider.calls) == 1 and db.meeting(meeting["id"])["transcript_version"] == 1
    assert db.meeting(meeting["id"])["status"] == "ready"


@pytest.mark.parametrize("empty", [False, True])
async def test_api_continue_without_retry_acknowledges_successful_cancel_crash_window(fixture, monkeypatch, empty):
    settings, _, _ = fixture
    class NoCapture:
        def close(self):
            pass
    provider = None
    app = create_app(settings, provider_factory=lambda cfg: provider, capture=NoCapture(), run_worker=False)
    db, worker = app.state.db, app.state.worker
    meeting = db.create_meeting("Synthetic cancellation-marker crash window")
    add_chunk((settings, db, meeting))
    provider = Provider(db, empty=empty, cancel_first=True)
    original = worker.enqueue_transcription(meeting["id"])
    transaction = db.transaction
    class MarkerFailure:
        def __init__(self, connection):
            self.connection = connection
        def execute(self, sql, *args):
            if "UPDATE jobs SET status='cancelled',payload=" in sql:
                raise OSError("Synthetic cancellation-marker write failure")
            return self.connection.execute(sql, *args)
    @contextmanager
    def failing_marker_transaction():
        with transaction() as connection:
            yield MarkerFailure(connection)
    monkeypatch.setattr(db, "transaction", failing_marker_transaction)
    await worker.run_once()
    monkeypatch.setattr(db, "transaction", transaction)
    survived = db.job(original["id"], internal=True)
    assert survived["status"] == "succeeded" and survived["cancel_requested"] == 1
    assert not json.loads(survived["payload"]).get("result_committed")
    assert db.meeting(meeting["id"])["status"] == "partial_error"
    worker.maybe_summary(meeting["id"], 1)
    automatic = worker.enqueue_transcription(meeting["id"])
    assert automatic["id"] == original["id"]
    assert db.one("SELECT id FROM jobs WHERE stage='summarize'") is None
    assert provider.summary_calls == 0 and len(provider.calls) == 1

    with TestClient(app, base_url="http://127.0.0.1:8765") as api:
        token = api.get("/api/v1/session").json()["csrf_token"]
        continued = api.post(f"/api/v1/meetings/{meeting['id']}/process", json={"stage": "transcribe", "retry": False},
                             headers={"X-Secretary-Token": token})
        assert continued.status_code == 202
        accepted = db.job(continued.json()["job_id"], internal=True)
        assert accepted["stage"] == "summarize" and accepted["status"] == "queued"
        assert db.job(original["id"], internal=True)["cancel_requested"] == 0
        assert db.meeting(meeting["id"])["transcript_version"] == 1
        assert db.meeting(meeting["id"])["status"] == "queued"
        await drain(worker)
        assert db.meeting(meeting["id"])["status"] == "ready"
        assert len(provider.calls) == 1 and provider.summary_calls == (0 if empty else 1)
        assert db.meeting(meeting["id"])["transcript_version"] == 1
        assert db.rows("SELECT u.confirmed_rub FROM usage u JOIN jobs j ON j.id=u.job_id WHERE j.stage='transcribe'") == [{"confirmed_rub": .06}]
