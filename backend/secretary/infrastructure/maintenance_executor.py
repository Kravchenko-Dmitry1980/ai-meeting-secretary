"""Own an executor admission across its complete queued or running lifetime."""
from uuid import uuid4


class MaintenanceExecutor:
    def __init__(self, executor, *, maintenance=None, participant_id=None):
        self.executor = executor
        self.maintenance = maintenance
        self.participant_id = participant_id

    def submit(self, function, /, *args, **kwargs):
        if self.maintenance is None:
            return self.executor.submit(function, *args, **kwargs)
        gate = self.maintenance
        # Enter independently before submit. A parent request may return while
        # this thread is still copying files or committing its final receipt.
        ticket = gate.enter(self.participant_id, 'background_job', str(uuid4()))

        def owned():
            try:
                with gate.ticket_context(ticket):
                    return function(*args, **kwargs)
            finally:
                gate.leave(ticket)

        try:
            future = self.executor.submit(owned)
        except BaseException:
            gate.leave(ticket)
            raise

        def cancelled(result):
            # A cancelled queued future never entered owned(); cancellation of
            # a running native/thread operation cannot release its admission.
            if result.cancelled():
                gate.leave(ticket)

        future.add_done_callback(cancelled)
        return future

    def shutdown(self, wait=True, *, cancel_futures=False):
        return self.executor.shutdown(wait=wait, cancel_futures=cancel_futures)
