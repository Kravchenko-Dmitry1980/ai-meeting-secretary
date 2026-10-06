"""One fenced task at a time, with a lease heartbeat and no mutation retries."""
from __future__ import annotations

import asyncio

from secretary.domain.team import TeamConflict


class TeamWorker:
    def __init__(self, repository, service, *, worker_id, lease_seconds=120, poll_seconds=1.0):
        if not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id) > 4000:
            raise ValueError('worker_id_required')
        if type(lease_seconds) is not int or not 3 <= lease_seconds <= 3600:
            raise ValueError('invalid_lease_seconds')
        if isinstance(poll_seconds, bool) or not isinstance(poll_seconds, (int, float)) or not 0.01 <= poll_seconds <= 60:
            raise ValueError('invalid_poll_seconds')
        self.repository, self.service, self.worker_id = repository, service, worker_id
        self.lease_seconds, self.poll_seconds = lease_seconds, poll_seconds
        self._stop = asyncio.Event()
        self._busy = asyncio.Lock()

    def stop(self):
        """Stop intake; finish the already-owned command rather than detach it."""
        self._stop.set()

    async def _heartbeat(self, claim, finished):
        while not finished.is_set():
            try:
                await asyncio.wait_for(finished.wait(), timeout=self.lease_seconds / 3)
            except TimeoutError:
                try:
                    await asyncio.to_thread(self.repository.renew_claim, claim, lease_seconds=self.lease_seconds)
                except TeamConflict:
                    return  # Any future dispatch must still pass repository fencing.

    async def _execute_owned(self, claim):
        finished = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat(claim, finished))
        try:
            try:
                return await asyncio.to_thread(self.service.execute, claim)
            except Exception:
                return await self._failed(claim)
        finally:
            finished.set()
            await heartbeat

    async def _failed(self, claim):
        try:
            return await asyncio.to_thread(self.repository.record_remote_result, claim,
                                          state='uncertain', error_code='team_worker_failure')
        except TeamConflict:
            # Expired/finished claims cannot write. Only the repository recovery
            # transition may mark an expired operation; no outbound retry here.
            await asyncio.to_thread(self.repository.recover_expired)
            return None

    @staticmethod
    async def _drain(task, cancellation):
        """Repeated caller cancellation cannot detach a native/SQLite thread."""
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(task)
                if cancelled:
                    raise asyncio.CancelledError
                return result
            except asyncio.CancelledError:
                if task.done():
                    # Task completion after a cancellation still has to be
                    # consumed; do not loop forever on an actually cancelled task.
                    if not task.cancelled():
                        task.result()
                    raise
                cancelled = True
                cancellation.set()

    async def _run_once_owned(self, cancellation):
        async with self._busy:
            if self._stop.is_set() or cancellation.is_set():
                return None
            await asyncio.to_thread(self.repository.recover_expired)
            if self._stop.is_set() or cancellation.is_set():
                return None
            claim = await asyncio.to_thread(self.repository.claim_command, self.worker_id,
                                            lease_seconds=self.lease_seconds)
            return await self._execute_owned(claim) if claim else None

    async def run_once(self):
        cancellation = asyncio.Event()
        return await self._drain(asyncio.create_task(self._run_once_owned(cancellation)), cancellation)

    async def _reconcile_once_owned(self, operation_id, cancellation):
        """Explicit internal read-only reconciliation, never a POST retry."""
        async with self._busy:
            if self._stop.is_set() or cancellation.is_set():
                return None
            claim = await asyncio.to_thread(self.repository.claim_reconciliation, operation_id,
                                            self.worker_id, lease_seconds=self.lease_seconds)
            return await self._execute_owned(claim)

    async def reconcile_once(self, operation_id):
        cancellation = asyncio.Event()
        return await self._drain(asyncio.create_task(self._reconcile_once_owned(operation_id, cancellation)), cancellation)

    async def run(self):
        while not self._stop.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass
