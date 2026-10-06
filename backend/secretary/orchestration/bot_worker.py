"""Owned local bot processing and one-attempt outbound delivery, never on webhook ACK."""
from __future__ import annotations

import asyncio

from secretary.domain.bot import BotError, BotSend, SendReceipt
from secretary.domain.team import TeamConflict, TeamForbidden
from secretary.domain.team_auth import AuthError
from secretary.orchestration.team_worker import TeamWorker


class _BotWorker:
    def __init__(self, repository, *, worker_id, lease_seconds=60, poll_seconds=1.0):
        if not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id) > 128:
            raise ValueError('bot_worker_id_invalid')
        if type(lease_seconds) is not int or not 3 <= lease_seconds <= 3600:
            raise ValueError('bot_lease_invalid')
        if isinstance(poll_seconds, bool) or not isinstance(poll_seconds, (int, float)) or not 0.01 <= poll_seconds <= 60:
            raise ValueError('bot_poll_invalid')
        self.repository, self.worker_id = repository, worker_id
        self.lease_seconds, self.poll_seconds = lease_seconds, poll_seconds
        self._stop = asyncio.Event()
        self._busy = asyncio.Lock()

    def stop(self):
        self._stop.set()

    async def _heartbeat(self, claim, finished):
        while not finished.is_set():
            try:
                await asyncio.wait_for(finished.wait(), timeout=self.lease_seconds / 3)
            except TimeoutError:
                try:
                    await asyncio.to_thread(self.renew, claim, lease_seconds=self.lease_seconds)
                except (BotError, TeamConflict, TeamForbidden):
                    return

    async def _run_once_owned(self, cancellation):
        async with self._busy:
            if self._stop.is_set() or cancellation.is_set():
                return None
            await asyncio.to_thread(self.repository.recover_expired)
            if self._stop.is_set() or cancellation.is_set():
                return None
            claim = await asyncio.to_thread(self.claim, self.worker_id, lease_seconds=self.lease_seconds)
            if claim is None:
                return None
            finished = asyncio.Event()
            heartbeat = asyncio.create_task(self._heartbeat(claim, finished))
            try:
                return await asyncio.to_thread(self._execute, claim)
            finally:
                finished.set()
                await heartbeat

    async def run_once(self):
        cancellation = asyncio.Event()
        return await TeamWorker._drain(asyncio.create_task(self._run_once_owned(cancellation)), cancellation)

    async def run(self):
        while not self._stop.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
            except TimeoutError:
                pass


class BotInboxWorker(_BotWorker):
    def __init__(self, repository, service, **options):
        super().__init__(repository, **options)
        self.service = service
        self.claim = repository.claim_event
        self.renew = repository.renew_event

    def _execute(self, claim):
        try:
            event = self.repository.authorize_event(claim)
            reply = self.service.handle(event)
            self.repository.finish_event(claim, reply)
            return reply
        except (BotError, TeamConflict, TeamForbidden, AuthError):
            try:
                self.repository.fail_event(claim, 'bot_event_forbidden')
            except (BotError, TeamConflict, TeamForbidden):
                pass
            return None
        # Unexpected local failure leaves a recoverable processing claim. Its
        # deterministic operation IDs/checkpoints are reused after lease expiry.


class BotOutboxWorker(_BotWorker):
    def __init__(self, repository, client, auth, **options):
        super().__init__(repository, **options)
        self.client, self.auth = client, auth
        self.claim = repository.claim_reply
        self.renew = repository.renew_reply

    def _execute(self, claim):
        started = False
        try:
            current = self.repository.authorize_reply(claim)
            text, sensitive = current.reply.text, current.reply.sensitive_action == 'desktop_code'
            if sensitive:
                # claim_reply already committed sending before code generation.
                # Only the hash survives in AuthRepository; this text is memory-only.
                code = self.auth.issue_code(current.event.user_id)
                text = 'Код входа на компьютере (действует 5 минут):\n' + code.value
            send = BotSend(operation_id=current.id, user_id=current.event.user_id,
                text=text, buttons=current.reply.buttons, sensitive=sensitive)
            self.repository.authorize_reply(current)  # Latest fence/ACL immediately before POST.
            started = True
            receipt = self.client.send_message(send)
            if not isinstance(receipt, SendReceipt) or receipt.operation_id != current.id:
                receipt = SendReceipt(current.id, 'uncertain', error_code='bot_send_receipt_invalid')
        except (BotError, TeamConflict, TeamForbidden, AuthError):
            receipt = SendReceipt(claim.id, 'uncertain' if started else 'rejected',
                error_code='bot_send_uncertain' if started else 'bot_send_forbidden')
        except Exception:
            receipt = SendReceipt(claim.id, 'uncertain' if started else 'rejected',
                error_code='bot_send_uncertain' if started else 'bot_send_unavailable')
        try:
            self.repository.finish_reply(claim, receipt)
        except (BotError, TeamConflict, TeamForbidden):
            # Expiry recovery is the only authority after losing the lease. It
            # marks sending uncertain; this worker never resends an unproved POST.
            self.repository.recover_expired()
        return receipt
