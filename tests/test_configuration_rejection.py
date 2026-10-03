"""Offline regressions for explicit STT configuration rejection and queue containment."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from secretary.application.worker import Worker
from secretary.infrastructure.database import Database
from secretary.infrastructure.polza import PolzaClient, ProviderError
from secretary.settings import Settings
from test_backend import prepared


def config(tmp_path):
    return Settings(_env_file=None, project_dir=tmp_path, data_dir=tmp_path / "data",
                    polza_api_key="synthetic-configuration-test-only", cloud_enabled=True,
                    stt_price_rub_per_minute=.1)


@pytest.mark.parametrize("method,expected_code", [("POST", "unsupported_response_format"), ("GET", "invalid_request")])
def test_observed_unsupported_format_rejection_is_safe_and_submit_specific(tmp_path, method, expected_code):
    trace_id = "12345678-1234-4234-8234-123456789abc"
    raw_message = ('Формат ответа "verbose_json" не поддерживается для этой модели. '
                   'Допустимые значения: json, text. Для verbose_json/srt/vtt/diarized_json '
                   'выберите другую модель транскрибации. SYNTHETIC-SECRET-SENTINEL')
    calls = []

    def handler(request):
        calls.append(request.method)
        return httpx.Response(400, json={"error": {"code": "BAD_REQUEST", "message": raw_message,
                                                   "trace_id": trace_id}})

    provider = PolzaClient(config(tmp_path), httpx.MockTransport(handler))
    with pytest.raises(ProviderError) as caught:
        asyncio.run(provider._request(method, "audio/transcriptions", payload={} if method == "POST" else None))
    assert caught.value.code == expected_code
    assert not caught.value.retryable and not caught.value.uncertain
    assert "SYNTHETIC-SECRET-SENTINEL" not in str(caught.value)
    assert caught.value.provider_trace_id == trace_id
    assert calls == [method]
    if method == "POST":
        assert "формат ответа" in caught.value.message
        assert "Обновите приложение" in caught.value.message


def test_rejected_stt_configuration_pauses_equivalent_queue_until_explicit_resume(tmp_path):
    settings = config(tmp_path)
    db = Database(settings.data_dir / "test.sqlite3")
    meeting, _ = prepared(db, settings)
    for sequence in range(1, 4):
        prepared(db, settings, meeting=meeting, sequence=sequence, offset=sequence * 1000)
    calls = []

    class RejectedProvider:
        def __init__(self, configuration):
            self.configuration = configuration

        async def transcribe(self, path, **kwargs):
            calls.append(self.configuration.stt_model)
            raise ProviderError("unsupported_response_format", "Выбранная модель не поддерживает формат ответа")

    worker = Worker(db, settings, RejectedProvider)
    worker.enqueue_transcription(meeting["id"])
    asyncio.run(worker.run_once())
    assert [job["status"] for job in db.jobs(meeting["id"])] == ["waiting_config"] * 4
    assert db.meeting(meeting["id"])["status"] == "waiting_config"
    assert db.rows("SELECT status FROM usage") == [{"status": "released"}]
    assert asyncio.run(worker.run_once()) is False
    assert len(calls) == 1
    assert all(chunk["status"] == "ready" for chunk in db.chunks(meeting["id"]))

    settings.stt_model = "openai/whisper-1"
    with pytest.raises(ValueError, match='route_change_required'):
        worker.enqueue_transcription(meeting["id"])
    assert db.meeting(meeting['id'])['transcript_version']==1
    assert all(job['status']=='waiting_config' for job in db.jobs(meeting['id']))
    worker.enqueue_transcription(meeting["id"],new_transcript_version=True)
    jobs = db.rows("SELECT status,payload FROM jobs WHERE version=2")
    assert all(job["status"] == "queued" for job in jobs)
    assert all(json.loads(job["payload"])["settings"]["stt_model"] == "openai/whisper-1" for job in jobs)
    assert len(calls)==1  # explicit version creation still only enqueues


def test_configuration_pause_preserves_other_routes_versions_and_accepted_poll_jobs(tmp_path):
    settings = config(tmp_path)
    db = Database(settings.data_dir / "test.sqlite3")
    meeting, _ = prepared(db, settings)
    for sequence in range(1, 4):
        prepared(db, settings, meeting=meeting, sequence=sequence, offset=sequence * 1000)

    class RejectedProvider:
        def __init__(self, configuration):
            pass

        async def transcribe(self, path, **kwargs):
            raise ProviderError("unsupported_response_format", "Unsupported response format")

    worker = Worker(db, settings, RejectedProvider)
    worker.enqueue_transcription(meeting["id"])
    jobs = db.jobs(meeting["id"])
    other_route = {"settings": {**worker.route_snapshot("transcribe"), "stt_model": "openai/whisper-1"}}
    db.execute("UPDATE jobs SET payload=? WHERE id=?", (json.dumps(other_route), jobs[1]["id"]))
    db.execute("UPDATE jobs SET version=2 WHERE id=?", (jobs[2]["id"],))
    db.execute("UPDATE jobs SET provider_job_id='accepted-mock-id' WHERE id=?", (jobs[3]["id"],))
    asyncio.run(worker.run_once())
    assert db.job(jobs[0]["id"])["status"] == "waiting_config"
    assert all(db.job(job["id"])["status"] == "queued" for job in jobs[1:])


@pytest.mark.parametrize("accepted,uncertain", [(True, False), (False, True)])
def test_ambiguous_or_accepted_request_is_not_a_free_configuration_rejection(tmp_path, accepted, uncertain):
    settings = config(tmp_path)
    db = Database(settings.data_dir / "test.sqlite3")
    meeting, _ = prepared(db, settings)
    prepared(db, settings, meeting=meeting, sequence=1, offset=1000)

    class NonRejectedProvider:
        def __init__(self, configuration):
            pass

        async def transcribe(self, path, *, on_provider_job=None, **kwargs):
            provider_id = "accepted-mock-id" if accepted else None
            if provider_id:
                await on_provider_job(provider_id)
            raise ProviderError("unsupported_response_format", "Mock ambiguous condition",
                                provider_job_id=provider_id, uncertain=uncertain)

    worker = Worker(db, settings, NonRejectedProvider)
    worker.enqueue_transcription(meeting["id"])
    asyncio.run(worker.run_once())
    jobs = db.jobs(meeting["id"])
    assert jobs[0]["status"] == ("uncertain" if uncertain else "failed")
    assert jobs[1]["status"] == "queued"
    assert db.rows("SELECT status FROM usage") == [{"status": "unknown"}]
