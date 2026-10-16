"""Own the source dispatcher thread through shutdown and cancellation."""
from __future__ import annotations

import asyncio

from secretary.orchestration.team_worker import TeamWorker


class PublicationWorker:
    def __init__(self, dispatcher, *, worker_id, poll_seconds=1.0, maintenance=None, participant_id=None):
        if not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id) > 4000:
            raise ValueError('worker_id_required')
        if isinstance(poll_seconds, bool) or not isinstance(poll_seconds, (int, float)) or not 0.01 <= poll_seconds <= 60:
            raise ValueError('invalid_poll_seconds')
        self.dispatcher, self.worker_id, self.poll_seconds = dispatcher, worker_id, poll_seconds
        self._stop, self._busy = asyncio.Event(), asyncio.Lock()
        self.maintenance, self.participant_id = maintenance, participant_id

    def stop(self):
        self._stop.set()

    async def _owned(self, cancellation):
        async with self._busy:
            if self._stop.is_set() or cancellation.is_set():
                return None
            from secretary.infrastructure.maintenance_binding import async_admission
            from secretary.infrastructure.team_maintenance import MaintenanceBlocked
            try:
                async with async_admission(self.maintenance, self.participant_id, 'outbound_publication'):
                    return await asyncio.to_thread(self.dispatcher.run_once, self.worker_id)
            except MaintenanceBlocked:
                return None

    async def run_once(self):
        cancellation = asyncio.Event()
        # Same tested drain protocol as the task worker: a cancelled await must
        # not detach a thread that may already have committed outbound intent.
        return await TeamWorker._drain(asyncio.create_task(self._owned(cancellation)), cancellation)

    async def run(self):
        while not self._stop.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass
