"""A cancelled caller cannot detach an accepted publication send thread."""
import asyncio
from threading import Event

import pytest

from secretary.orchestration.publication_worker import PublicationWorker


class BlockingDispatcher:
    def __init__(self):
        self.entered, self.release = Event(), Event()
        self.calls, self.finished = [], False

    def run_once(self, worker_id):
        self.calls.append(worker_id)
        self.entered.set()
        assert self.release.wait(5)
        self.finished = True
        return 'receipt'


def test_stop_prevents_new_intake():
    dispatcher = BlockingDispatcher()
    worker = PublicationWorker(dispatcher, worker_id='sender')
    worker.stop()
    assert asyncio.run(worker.run_once()) is None
    assert dispatcher.calls == []


def test_repeated_cancellation_drains_one_send_and_waiting_caller_is_not_dispatched():
    async def run():
        dispatcher = BlockingDispatcher()
        worker = PublicationWorker(dispatcher, worker_id='sender')
        first = asyncio.create_task(worker.run_once())
        assert await asyncio.to_thread(dispatcher.entered.wait, 2)
        waiting = asyncio.create_task(worker.run_once())
        await asyncio.sleep(0)
        first.cancel()
        waiting.cancel()
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.sleep(0)
        assert not first.done() and not dispatcher.finished
        dispatcher.release.set()
        for task in (first, waiting):
            with pytest.raises(asyncio.CancelledError):
                await task
        assert dispatcher.finished and dispatcher.calls == ['sender']
    asyncio.run(run())


def test_stop_during_owned_send_finishes_it_and_loop_stops():
    async def run():
        dispatcher = BlockingDispatcher()
        worker = PublicationWorker(dispatcher, worker_id='sender')
        task = asyncio.create_task(worker.run())
        assert await asyncio.to_thread(dispatcher.entered.wait, 2)
        worker.stop()
        assert not task.done()
        dispatcher.release.set()
        await asyncio.wait_for(task, 2)
        assert dispatcher.finished and dispatcher.calls == ['sender']
    asyncio.run(run())


@pytest.mark.parametrize('worker_id,poll', [('', 1), ('worker', float('nan')), ('worker', True)])
def test_invalid_worker_configuration_is_rejected(worker_id, poll):
    with pytest.raises(ValueError):
        PublicationWorker(BlockingDispatcher(), worker_id=worker_id, poll_seconds=poll)
