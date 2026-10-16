"""Worker ordering, cancellation and sensitive-message boundaries (no sockets)."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest

from secretary.domain.bot import BotError, BotEvent, DeterministicReply, SendReceipt
from secretary.orchestration.bot_worker import BotInboxWorker, BotOutboxWorker


def delivery(*, sensitive=False):
    event = BotEvent(event_id=str(uuid4()), dedup_key='synthetic-event', bot_id='99',
        kind='message_created', timestamp_ms=1791061200000, user_id='12', actor_id=str(uuid4()),
        actor_revision=0, project_ids=('7',), message_id='synthetic-mid', text='synthetic')
    return SimpleNamespace(id=str(uuid4()), event=event, reply=DeterministicReply('generic reply',
        sensitive_action='desktop_code' if sensitive else None), fence=1, worker_id='worker',
        lease_until=datetime(2026, 10, 4, tzinfo=timezone.utc))


class Ports:
    def __init__(self, item):
        self.item, self.trace, self.finished, self.claimed = item, [], [], False

    def recover_expired(self):
        self.trace.append('recover')

    def claim_reply(self, worker, **options):
        if self.claimed:
            return None
        self.trace.append('durable-sending')
        self.claimed = True
        return self.item

    def authorize_reply(self, claim):
        self.trace.append('authorize')
        return claim

    def renew_reply(self, claim, **options):
        self.trace.append('renew')

    def finish_reply(self, claim, receipt):
        self.trace.append('finish')
        self.finished.append(receipt)

    claim_event = claim_reply
    renew_event = renew_reply

    def authorize_event(self, claim):
        self.trace.append('authorize-event')
        return claim.event

    def finish_event(self, claim, reply):
        self.trace.append('durable-reply')
        self.finished.append(reply)

    def fail_event(self, claim, code):
        self.trace.append('fail-event')


def test_desktop_code_is_generated_after_sending_record_and_never_passed_to_finish():
    item = delivery(sensitive=True)
    repo = Ports(item)
    def issue(user):
        assert repo.trace == ['recover', 'durable-sending', 'authorize']
        assert user == '12'
        repo.trace.append('code-generated')
        return SimpleNamespace(value='SYNTHETIC_MEMORY_ONLY_CODE')
    sent = []
    def send(value):
        assert repo.trace[-1] == 'authorize'
        sent.append(value)
        return SendReceipt(value.operation_id, 'sent', message_id='synthetic-mid')
    worker = BotOutboxWorker(repo, SimpleNamespace(send_message=send), SimpleNamespace(issue_code=issue), worker_id='worker')
    receipt = asyncio.run(worker.run_once())
    assert receipt.state == 'sent' and len(sent) == 1 and sent[0].sensitive
    assert 'SYNTHETIC_MEMORY_ONLY_CODE' in sent[0].text
    assert 'SYNTHETIC_MEMORY_ONLY_CODE' not in repr(repo.finished)
    assert item.reply.text == 'generic reply'
    assert asyncio.run(worker.run_once()) is None


def test_revoked_actor_after_code_generation_prevents_post():
    item = delivery(sensitive=True)
    repo = Ports(item)
    def issue(user):
        def denied(claim):
            raise BotError('bot_actor_revoked')
        repo.authorize_reply = denied
        return SimpleNamespace(value='SYNTHETIC_NEVER_SENT_CODE')
    def send(value):
        pytest.fail('revoked actor caused a send')
    worker = BotOutboxWorker(repo, SimpleNamespace(send_message=send), SimpleNamespace(issue_code=issue), worker_id='worker')
    assert asyncio.run(worker.run_once()).state == 'rejected'


def test_lost_send_ack_is_uncertain_not_a_second_send():
    item, sent = delivery(), []
    repo = Ports(item)
    def send(value):
        sent.append(value)
        raise TimeoutError('SYNTHETIC_PRIVATE_PROVIDER_DETAIL')
    worker = BotOutboxWorker(repo, SimpleNamespace(send_message=send), object(), worker_id='worker')
    result = asyncio.run(worker.run_once())
    assert result.state == 'uncertain' and result.error_code == 'bot_send_uncertain'
    assert asyncio.run(worker.run_once()) is None and len(sent) == 1


def test_wrong_operation_receipt_cannot_claim_success():
    item = delivery()
    repo = Ports(item)
    sender = SimpleNamespace(send_message=lambda value: SendReceipt(str(uuid4()), 'sent', message_id='other-message'))
    worker = BotOutboxWorker(repo, sender, object(), worker_id='worker')
    assert asyncio.run(worker.run_once()).state == 'uncertain'


def test_inbox_processes_locally_and_never_dispatches_max():
    item = delivery()
    repo = Ports(item)
    def handle(event):
        assert repo.trace[-1] == 'authorize-event'
        return DeterministicReply('ready locally')
    worker = BotInboxWorker(repo, SimpleNamespace(handle=handle), worker_id='worker')
    assert asyncio.run(worker.run_once()).text == 'ready locally'
    assert repo.trace[-1] == 'durable-reply'


def test_repeated_cancellation_waits_for_owned_send_thread():
    async def scenario():
        item = delivery()
        repo = Ports(item)
        started, release, ended = Event(), Event(), Event()
        def send(value):
            started.set()
            assert release.wait(3), 'bounded fixture release missing'
            ended.set()
            return SendReceipt(value.operation_id, 'sent', message_id='synthetic-mid')
        worker = BotOutboxWorker(repo, SimpleNamespace(send_message=send), object(), worker_id='worker')
        task = asyncio.create_task(worker.run_once())
        assert await asyncio.to_thread(started.wait, 2)
        task.cancel()
        await asyncio.sleep(0.02)
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done() and not ended.is_set()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert ended.is_set() and repo.finished[0].state == 'sent'
    asyncio.run(scenario())


def test_stop_before_intake_starts_no_work():
    repo = Ports(delivery())
    worker = BotOutboxWorker(repo, object(), object(), worker_id='worker')
    worker.stop()
    assert asyncio.run(worker.run_once()) is None and repo.trace == []


def test_stopping_during_recovery_prevents_claim():
    async def scenario():
        repo = Ports(delivery())
        started, release = Event(), Event()
        def recover():
            started.set()
            assert release.wait(3)
        repo.recover_expired = recover
        worker = BotOutboxWorker(repo, object(), object(), worker_id='worker')
        task = asyncio.create_task(worker.run_once())
        assert await asyncio.to_thread(started.wait, 2)
        worker.stop()
        release.set()
        assert await task is None and not repo.claimed
    asyncio.run(scenario())
