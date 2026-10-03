"""HTTP-mocked worker retries preserve paid summary maps and stop on ambiguity."""
from __future__ import annotations

import json

import httpx
import pytest

from secretary.application.worker import Worker
from secretary.domain.models import TranscriptSegment
from secretary.infrastructure.database import Database
from secretary.infrastructure.polza import PolzaClient, ProviderError
from secretary.settings import Settings
from test_backend import prepared


def summary_worker(tmp_path, provider_factory):
    settings = Settings(_env_file=None, project_dir=tmp_path, data_dir=tmp_path / "data",
                        polza_api_key="synthetic-summary-retry-only", cloud_enabled=True,
                        summary_batch_chars=5000, summary_input_rub_per_million=5,
                        summary_output_rub_per_million=25)
    db = Database(settings.data_dir / "test.sqlite3")
    meeting, chunk = prepared(db, settings)
    version = db.ensure_version(meeting["id"])
    transcription = db.enqueue(meeting["id"], "transcribe", chunk_id=chunk["id"], version=version)
    segments = [TranscriptSegment(id=f"source-{ordinal}", meeting_id=meeting["id"],
                    chunk_id=chunk["id"], transcript_version=version, ordinal=ordinal,
                    # v2 request also budgets immutable identity/context metadata.
                    # Keep exactly two map batches for the retry-counter scenario.
                    text=letter * 1500, channel="import").model_dump()
                for ordinal, letter in enumerate(("Я", "Б"))]
    db.save_segments(transcription, segments, [])
    worker = Worker(db, settings, provider_factory)
    job = worker.enqueue_summary(meeting["id"])
    return worker, db, meeting, job


def successful_response(request_id, *, invalid_evidence=False):
    output = {"overview": "Обсуждение", "readable_transcript": "Обсуждение.",
              "decisions": [], "action_items": [], "open_questions": []}
    if invalid_evidence:
        output["decisions"] = [{"text": "Выдуманное решение", "source_segment_ids": ["missing-source"],
                                "evidence_quote": "Несуществующая цитата"}]
    return httpx.Response(200, json={"id": request_id, "choices": [{
        "message": {"content": json.dumps(output)}, "finish_reason": "stop"}], "usage": {"cost_rub": .02}})


@pytest.mark.asyncio
async def test_summary_429_automatically_reuses_paid_map_and_ledger(tmp_path, monkeypatch):
    requests, delays = [], []

    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 2:
            return httpx.Response(429, json={"error": {"code": "RATE_LIMITED"}}, headers={"Retry-After": "7"})
        return successful_response(f"mock-summary-{len(requests)}")

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("secretary.application.worker.asyncio.sleep", sleep)
    worker, db, meeting, job = summary_worker(tmp_path, lambda settings: PolzaClient(settings, httpx.MockTransport(handler)))
    assert await worker.run_once()
    assert db.job(job["id"])["status"] == "queued"
    assert db.job(job["id"])["attempts"] == 1
    first_checkpoint = db.rows("SELECT key,payload FROM summary_checkpoints WHERE job_id=?", (job["id"],))
    assert len(first_checkpoint) == 1
    assert db.rows("SELECT status FROM usage WHERE job_id=? ORDER BY created_at", (job["id"],)) == [
        {"status": "confirmed"}, {"status": "released"}]
    assert len(delays) == 1 and 7 <= delays[0] <= 7.25

    assert await worker.run_once()  # Same queued job, no manual resubmit or new transcript.
    assert db.job(job["id"])["status"] == "succeeded"
    assert db.job(job["id"])["attempts"] == 2
    assert db.summary(meeting["id"]) is not None
    assert len(requests) == 4 and requests.count(requests[0]) == 1
    assert requests[1] == requests[2]  # Only the explicitly rejected map is sent again.
    assert first_checkpoint[0] in db.rows("SELECT key,payload FROM summary_checkpoints WHERE job_id=?", (job["id"],))
    receipts = db.rows("SELECT status,confirmed_rub,provider_request_id FROM usage WHERE job_id=?", (job["id"],))
    assert sum(row["status"] == "released" for row in receipts) == 1
    assert sum(row["confirmed_rub"] or 0 for row in receipts) == pytest.approx(.06)
    assert [row["provider_request_id"] for row in receipts if row["status"] == "confirmed"] == [
        "mock-summary-1", "mock-summary-3", "mock-summary-4"]


@pytest.mark.asyncio
async def test_summary_429_backoff_stops_after_three_attempts(tmp_path, monkeypatch):
    calls, delays = [], []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, json={"error": {"code": "RATE_LIMITED"}})

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("secretary.application.worker.asyncio.sleep", sleep)
    worker, db, _, job = summary_worker(tmp_path, lambda settings: PolzaClient(settings, httpx.MockTransport(handler)))
    for expected_status in ("queued", "queued", "failed"):
        assert await worker.run_once()
        assert db.job(job["id"])["status"] == expected_status
    assert not await worker.run_once()
    assert len(calls) == 3 and len(delays) == 2
    assert 2 <= delays[0] <= 2.25 and 4 <= delays[1] <= 4.25
    assert db.job(job["id"])["attempts"] == 3
    assert all(row["status"] == "released" for row in db.rows("SELECT status FROM usage"))


@pytest.mark.asyncio
@pytest.mark.parametrize("stopped_status", ["failed", "cancelled", "paused_budget", "waiting_config"])
async def test_explicit_summary_resume_has_bounded_new_attempts_and_keeps_old_paid_map(tmp_path, monkeypatch, stopped_status):
    requests, delays, allow_success = [], [], False

    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) > 1 and not allow_success:
            return httpx.Response(429, json={"error": {"code": "RATE_LIMITED"}})
        return successful_response(f"mock-summary-{len(requests)}")

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr("secretary.application.worker.asyncio.sleep", sleep)
    factory = lambda settings: PolzaClient(settings, httpx.MockTransport(handler))
    worker, db, meeting, job = summary_worker(tmp_path, factory)
    await worker.run_once()  # Pay and checkpoint map one before an explicit rejection.
    first_checkpoint = db.rows("SELECT key,payload FROM summary_checkpoints WHERE job_id=?", (job["id"],))
    assert len(first_checkpoint) == 1
    db.execute("UPDATE jobs SET status=?,attempts=6 WHERE id=?", (stopped_status, job["id"]))
    assert worker.enqueue_summary(meeting["id"], retry=True)["id"] == job["id"]
    assert db.job(job["id"])["attempts"] == 6
    assert json.loads(db.job(job["id"], internal=True)["payload"])["retry_attempt_baseline"] == 6
    delays.clear()

    for count, expected_status in enumerate(("queued", "queued", "failed"), start=7):
        assert await worker.run_once()
        assert db.job(job["id"])["status"] == expected_status
        assert db.job(job["id"])["attempts"] == count
        if expected_status == "queued":
            # Repeated UI clicks and worker recreation must not reset the automatic retry bound.
            assert worker.enqueue_summary(meeting["id"], retry=True)["id"] == job["id"]
            worker = Worker(db, worker.settings, factory)
        assert json.loads(db.job(job["id"], internal=True)["payload"])["retry_attempt_baseline"] == 6
    assert not await worker.run_once()
    assert len(delays) == 2 and 2 <= delays[0] <= 2.25 and 4 <= delays[1] <= 4.25
    assert len(requests) == 5 and requests.count(requests[0]) == 1
    assert db.rows("SELECT key,payload FROM summary_checkpoints WHERE job_id=?", (job["id"],)) == first_checkpoint
    assert sum(row["confirmed_rub"] or 0 for row in db.rows("SELECT confirmed_rub FROM usage")) == pytest.approx(.02)

    allow_success = True
    worker.enqueue_summary(meeting["id"], retry=True)
    assert json.loads(db.job(job["id"], internal=True)["payload"])["retry_attempt_baseline"] == 9
    await worker.run_once()
    assert db.job(job["id"])["status"] == "succeeded" and db.job(job["id"])["attempts"] == 10
    assert requests.count(requests[0]) == 1 and db.summary(meeting["id"]) is not None
    assert sum(row["confirmed_rub"] or 0 for row in db.rows("SELECT confirmed_rub FROM usage")) == pytest.approx(.06)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,expected_status,receipt_status", [
    ("server", "uncertain", "unknown"), ("timeout", "uncertain", "unknown"),
    ("evidence", "succeeded", "confirmed"), ("uncertain_rate", "uncertain", "unknown"),
])
async def test_summary_stops_after_uncertainty_or_exhausted_evidence_correction(tmp_path, monkeypatch, failure, expected_status, receipt_status):
    calls = []

    def handler(request):
        calls.append(request)
        if failure == "server":
            return httpx.Response(500, json={"error": {"code": "UPSTREAM_FAILURE"}})
        if failure == "timeout":
            raise httpx.ReadTimeout("Synthetic timeout", request=request)
        if failure == "uncertain_rate":
            raise ProviderError("rate_limited", "Synthetic ambiguous rate response", retryable=True,
                                uncertain=True, provider_job_id="mock-ambiguous-request-id")
        return successful_response(f"mock-paid-invalid-output-{len(calls)}", invalid_evidence=True)

    async def forbidden_sleep(delay):
        pytest.fail("Worker must not requeue uncertainty or an exhausted evidence correction")

    monkeypatch.setattr("secretary.application.worker.asyncio.sleep", forbidden_sleep)
    worker, db, _, job = summary_worker(tmp_path, lambda settings: PolzaClient(settings, httpx.MockTransport(handler)))
    assert await worker.run_once()
    assert db.job(job["id"])["status"] == expected_status
    expected_calls = 6 if failure == "evidence" else 1  # Two map blocks and one merge, one correction each.
    assert not await worker.run_once() and len(calls) == expected_calls
    assert db.rows("SELECT status FROM usage") == [{"status": receipt_status}] * expected_calls
    if failure == "evidence":
        assert "untrusted_invalid_candidate" in calls[1].content.decode()
        assert sum(row["confirmed_rub"] for row in db.rows("SELECT confirmed_rub FROM usage")) == pytest.approx(.12)
        assert db.summary(job["meeting_id"])["excluded_items_count"] == 3
        assert len(db.rows("SELECT * FROM summary_checkpoints")) == 3
    else:
        assert db.rows("SELECT * FROM summary_checkpoints") == []
