"""Independent fault probe for cancellation persistence after a paid STT commit."""
from contextlib import contextmanager

import pytest

from secretary.application.worker import Worker
from test_cancel_completion_regression import Provider, add_chunk, fixture, no_http  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk_count", [1, 2])
async def test_failed_cancel_marker_preserves_cancel_intent_and_continuation(fixture, monkeypatch, chunk_count):
    settings, db, meeting = fixture
    for sequence in range(chunk_count):
        add_chunk(fixture, sequence)
    provider = Provider(db, cancel_first=True)
    worker = Worker(db, settings, lambda cfg: provider)
    first = worker.enqueue_transcription(meeting["id"])
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
    saved = db.job(first["id"], internal=True)
    assert saved["status"] == "succeeded" and saved["cancel_requested"] == 1
    assert db.segments(meeting["id"])["total"] == 1
    assert db.one("SELECT status FROM usage WHERE job_id=?", (first["id"],))["status"] == "confirmed"
    monkeypatch.setattr(db, "transaction", transaction)

    if chunk_count == 2:
        # The next previously queued chunk may finish, but no explicit Continue occurred.
        await worker.run_once()
        assert len(provider.calls) == 2
        assert db.one("SELECT id FROM jobs WHERE meeting_id=? AND stage='summarize'", (meeting["id"],)) is None
    else:
        resumed = worker.enqueue_transcription(meeting["id"], retry=True)
        assert db.meeting(meeting["id"])["transcript_version"] == 1
        assert resumed["stage"] == "summarize"
