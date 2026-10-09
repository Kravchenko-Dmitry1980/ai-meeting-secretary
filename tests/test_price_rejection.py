import asyncio
import json

import httpx
import pytest

from secretary.application.worker import Worker
from secretary.infrastructure.database import Database
from secretary.infrastructure.polza import PolzaClient, ProviderError
from secretary.settings import Settings
from test_backend import prepared


def test_price_rejection_pauses_remaining_chunks_and_resume_uses_new_cap(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, polza_api_key='synthetic-only',
                        cloud_enabled=True, stt_price_rub_per_minute=0.048)
    db = Database(tmp_path / 'test.sqlite3')
    meeting, first = prepared(db, settings)
    _, second = prepared(db, settings, meeting=meeting, sequence=1, offset=1000)
    calls = []

    class RejectedProvider:
        def __init__(self, configuration):
            self.configuration = configuration

        async def transcribe(self, path, **kwargs):
            calls.append(self.configuration.stt_price_rub_per_minute)
            raise ProviderError('price_limit', 'Provider price exceeds configured ceiling')

    worker = Worker(db, settings, RejectedProvider)
    worker.enqueue_transcription(meeting['id'])
    asyncio.run(worker.run_once())
    assert [job['status'] for job in db.jobs(meeting['id'])] == ['paused_budget', 'paused_budget']
    assert db.meeting(meeting['id'])['status'] == 'paused_budget'
    assert db.rows('SELECT status FROM usage') == [{'status': 'released'}]
    assert asyncio.run(worker.run_once()) is False
    assert calls == [0.048]
    settings.stt_price_rub_per_minute = 0.06
    worker.enqueue_transcription(meeting['id'], resume=True)
    jobs = db.rows('SELECT status,payload FROM jobs')
    assert all(job['status'] == 'queued' for job in jobs)
    assert all(json.loads(job['payload'])['settings']['stt_price_rub_per_minute'] == 0.06 for job in jobs)


def test_poll_rejection_does_not_release_accepted_request_reservation(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, polza_api_key='synthetic-only',
                        cloud_enabled=True, stt_price_rub_per_minute=0.1, stt_model='aiesa/transcribe')
    db = Database(tmp_path / 'test.sqlite3')
    meeting, _ = prepared(db, settings)

    class AcceptedThenPollDenied:
        def __init__(self, configuration):
            pass

        async def transcribe(self, path, *, on_provider_job=None, provider_job_id=None, **kwargs):
            await on_provider_job('accepted-test-id')
            raise ProviderError('authentication', 'Polling denied', provider_job_id='accepted-test-id')

    worker = Worker(db, settings, AcceptedThenPollDenied)
    worker.enqueue_transcription(meeting['id'])
    asyncio.run(worker.run_once())
    assert db.rows('SELECT status FROM usage') == [{'status': 'unknown'}]


@pytest.mark.parametrize('status,failed_polls', [(401, 1), (404, 1), (429, 3)])
def test_real_async_poll_http_failures_keep_reservation_and_resume_only_get(tmp_path, status, failed_polls):
    settings = Settings(_env_file=None, data_dir=tmp_path, polza_api_key='synthetic-only',
                        cloud_enabled=True, stt_model='aiesa/transcribe',
                        stt_price_rub_per_minute=0.1)
    db = Database(tmp_path / 'test.sqlite3')
    meeting, _ = prepared(db, settings)
    requests = []
    polls = 0

    def handler(request):
        nonlocal polls
        requests.append((request.method, request.url.path))
        if request.method == 'POST':
            return httpx.Response(201, json={'id': 'accepted-synthetic-id', 'status': 'processing'})
        polls += 1
        if polls <= failed_polls:
            headers = {'Retry-After': '0.01'} if status == 429 else None
            return httpx.Response(status, json={'error': {'message': 'synthetic polling failure'}}, headers=headers)
        return httpx.Response(200, json={'id': 'accepted-synthetic-id', 'status': 'completed',
            'duration': 1, 'text': 'Синтетический результат',
            'segments': [{'text': 'Синтетический результат', 'start': 0, 'end': 1}],
            'usage': {'cost_rub': 0.02}})

    worker = Worker(db, settings, lambda configuration: PolzaClient(
        configuration, httpx.MockTransport(handler)))
    worker.enqueue_transcription(meeting['id'])
    for _ in range(failed_polls):
        asyncio.run(worker.run_once())

    job = db.one("SELECT * FROM jobs WHERE meeting_id=? AND stage='transcribe'", (meeting['id'],))
    assert job['provider_job_id'] == 'accepted-synthetic-id'
    assert db.rows('SELECT status FROM usage WHERE job_id=?', (job['id'],)) == [{'status': 'unknown'}]
    assert sum(method == 'POST' for method, _ in requests) == 1
    assert sum(method == 'GET' for method, _ in requests) == failed_polls

    worker.enqueue_transcription(meeting['id'], resume=True)
    asyncio.run(worker.run_once())

    assert db.job(job['id'])['status'] == 'succeeded'
    assert db.rows('SELECT status,confirmed_rub FROM usage WHERE job_id=?', (job['id'],)) == [
        {'status': 'confirmed', 'confirmed_rub': 0.02}]
    assert requests == [('POST', '/api/v1/audio/transcriptions'), *(
        ('GET', '/api/v1/audio/transcriptions/accepted-synthetic-id') for _ in range(failed_polls + 1))]
