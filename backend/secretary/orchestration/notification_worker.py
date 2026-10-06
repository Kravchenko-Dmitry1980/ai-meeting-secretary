"""One owned notification attempt; cancellation never detaches a native thread.

Planning/full scans are separate. This worker only drains committed intents.
Runtime factories remain opt-in; importing this module starts nothing.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from threading import Event

from secretary.domain.bot import SendReceipt
from secretary.domain.notifications import NotificationDeferred, NotificationError
from secretary.orchestration.team_worker import TeamWorker


class NotificationWorker:
    def __init__(self, repository, sender, *, worker_id, lease_seconds=60, poll_seconds=1.0):
        if not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id) > 128:
            raise ValueError('notification_worker_id_invalid')
        if type(lease_seconds) is not int or not 3 <= lease_seconds <= 3600:
            raise ValueError('notification_lease_invalid')
        if isinstance(poll_seconds, bool) or not isinstance(poll_seconds, (int, float)) or not .01 <= poll_seconds <= 60:
            raise ValueError('notification_poll_invalid')
        self.repository, self.sender, self.worker_id = repository, sender, worker_id
        self.lease_seconds, self.poll_seconds = lease_seconds, poll_seconds
        self._stop = asyncio.Event()
        self._busy = asyncio.Lock()

    def stop(self):
        """Stop preparation; drain an already committed POST and its receipt."""
        self._stop.set()

    async def _heartbeat(self, claim, finished, lost):
        while not finished.is_set():
            try:
                await asyncio.wait_for(finished.wait(), timeout=self.lease_seconds / 3)
            except TimeoutError:
                try:
                    # Same immutable fence stays valid when mark_sending changes
                    # state/attempt. Do not race by replacing the executing claim.
                    await asyncio.to_thread(self.repository.renew, claim, lease_seconds=self.lease_seconds)
                except Exception:
                    lost.set()
                    return

    def _release(self, claim, *, error=None, cancelled=False):
        try:
            if isinstance(error, NotificationError) and not isinstance(error, NotificationDeferred):
                if error.code == 'notification_claim_lost':
                    self.repository.recover_expired()
                else:
                    self.repository.cancel(claim, error.code)
            else:
                available = getattr(error, 'available_at', None)
                # A deferred callback always supplies a current time when useful.
                # Fall back to the injected sender clock, never an old task date.
                clock = getattr(self.sender, 'clock', lambda: datetime.now(timezone.utc))
                available = available or clock() + timedelta(seconds=30)
                self.repository.defer(claim, available_at=available,
                    error_code=(getattr(error, 'code', None) or
                                ('notification_not_ready' if cancelled else 'notification_unavailable')))
        except NotificationError:
            # Fencing/recovery is authoritative after any lease loss.
            self.repository.recover_expired()

    def _execute(self, claim, cancelled):
        started = None
        def authorize():
            if cancelled():
                raise NotificationDeferred('notification_not_ready')
            return self.repository.authorize(claim)
        def before_send(send, proof):
            nonlocal started
            if cancelled():
                raise NotificationDeferred('notification_not_ready')
            # Exact body, recipient, due generations, current ACL and observed
            # fingerprints are checked/persisted in this one transaction.
            started = self.repository.mark_sending(claim, send, observed_fingerprints=proof)
        try:
            if cancelled():
                self._release(claim, cancelled=True)
                return None
            receipt = self.sender.dispatch(claim.intent, authorize=authorize,
                before_send=before_send, cancelled=cancelled)
            if started is None:
                raise NotificationError('notification_corrupt')
            if not isinstance(receipt, SendReceipt) or receipt.operation_id != claim.id:
                receipt = SendReceipt(claim.id, 'uncertain', error_code='notification_receipt_invalid')
        except Exception as error:
            if started is None:
                self._release(claim, error=error)
                return None
            receipt = SendReceipt(claim.id, 'uncertain', error_code='notification_send_uncertain')
        try:
            self.repository.finish(started, receipt)
        except NotificationError:
            self.repository.recover_expired()
            return None  # Never expose an unpersisted sent result after fence loss.
        return receipt

    async def _run_once_owned(self, cancellation):
        async with self._busy:
            cancelled = lambda: self._stop.is_set() or cancellation.is_set()
            if cancelled():
                return None
            await asyncio.to_thread(self.repository.recover_expired)
            if cancelled():
                return None
            claim = await asyncio.to_thread(self.repository.claim, self.worker_id,
                                            lease_seconds=self.lease_seconds)
            if claim is None:
                return None
            lost, finished = Event(), asyncio.Event()
            heartbeat = asyncio.create_task(self._heartbeat(claim, finished, lost))
            try:
                return await asyncio.to_thread(self._execute, claim,
                    lambda: cancelled() or lost.is_set())
            finally:
                finished.set()
                await heartbeat

    async def run_once(self):
        cancellation = Event()
        task = asyncio.create_task(self._run_once_owned(cancellation))
        return await TeamWorker._drain(task, cancellation)

    async def run(self):
        while not self._stop.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass
