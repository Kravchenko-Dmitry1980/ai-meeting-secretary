import asyncio
import json

from secretary.application.worker import Worker
from secretary.infrastructure.database import Database
from secretary.infrastructure.polza import ProviderError
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
