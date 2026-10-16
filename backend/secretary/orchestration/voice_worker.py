"""Owned async voice jobs; cloud clients and budget locks stay on the owner loop."""
from __future__ import annotations

import asyncio

from secretary.domain.bot import BotError
from secretary.domain.team import TeamConflict, TeamForbidden
from secretary.orchestration.bot_worker import _BotWorker


class VoiceWorker(_BotWorker):
    def __init__(self, repository, service, **options):
        super().__init__(repository, **options)
        self.service = service

    async def _voice_heartbeat(self, claim, finished, lost):
        while not finished.is_set():
            try:
                await asyncio.wait_for(finished.wait(), timeout=self.lease_seconds / 3)
            except TimeoutError:
                try:
                    await asyncio.to_thread(self.repository.renew_job, claim, lease_seconds=self.lease_seconds)
                except (BotError, TeamConflict, TeamForbidden):
                    lost.set()
                    return
                except Exception:
                    # A local renewal failure cannot grant permission for a
                    # subsequent paid request; recovery owns the expired claim.
                    lost.set()
                    return

    async def _run_once_owned(self, cancellation):
        async with self._busy:
            if self._stop.is_set() or cancellation.is_set():
                return None
            await asyncio.to_thread(self.repository.recover_expired)
            if self._stop.is_set() or cancellation.is_set():
                return None
            claim = await asyncio.to_thread(self.repository.claim_job, self.worker_id,
                                            lease_seconds=self.lease_seconds)
            if claim is None:
                return None
            finished, lost = asyncio.Event(), asyncio.Event()
            heartbeat = asyncio.create_task(self._voice_heartbeat(claim, finished, lost))
            try:
                # Calling asyncio.run in a worker thread would move shared
                # AsyncClient transports and monthly-budget locks across loops.
                # execute owns fresh authorization, each paid checkpoint, and a
                # trusted fenced failure path even after actor revocation.
                try:
                    return await self.service.execute(claim, cancelled=lambda: cancellation.is_set() or lost.is_set())
                except (BotError, TeamConflict, TeamForbidden):
                    # An expired fence cannot write. Recovery decides its state;
                    # one revoked actor must not stop intake for everyone else.
                    return None
            finally:
                finished.set()
                await heartbeat
        # Unexpected failures intentionally leave the durable claim for expiry
        # recovery. A worker cannot infer that an ambiguous paid POST was free.
