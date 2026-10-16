"""Exact owner operation IDs follow durable claims to outbound dispatch seams."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

from secretary.domain.bot import SendReceipt
from secretary.infrastructure.team_maintenance import MaintenanceRepository
from secretary.orchestration.bot_worker import BotOutboxWorker
from secretary.orchestration.notification_worker import NotificationWorker


def owner_gate(tmp_path):
    gate = MaintenanceRepository(tmp_path / 'maintenance.sqlite3', str(uuid4()))
    gate.bind_sources({role: tmp_path / (role + '.sqlite3')
        for role in ('secretary', 'team', 'billing', 'vikunja')})
    gate.register_participant('synthetic', run_id=str(uuid4()), identity={
        'pid': 123, 'creation_time': '2026-10-12T00:00:00Z',
        'executable_sha256': 'a' * 64, 'argv_sha256': 'b' * 64})
    return gate


def test_bot_outbox_binds_claim_id_before_max_dispatch(tmp_path):
    gate, operation_id = owner_gate(tmp_path), str(uuid4())
    claim = SimpleNamespace(id=operation_id)

    class Repository:
        receipt = None
        def recover_expired(self): pass
        def claim_reply(self, *args, **kwargs): return claim
        def renew_reply(self, *args, **kwargs): pass
        def authorize_reply(self, current):
            assert current.id == operation_id
            return SimpleNamespace(id=operation_id,
                reply=SimpleNamespace(text='synthetic', sensitive_action=None, buttons=()),
                event=SimpleNamespace(user_id='7001'))
        def finish_reply(self, current, receipt): self.receipt = receipt

    class MaxClient:
        calls = []
        def send_message(self, command):
            gate.assert_dispatch_admission('synthetic', allowed_kinds=('outbound_bot',),
                operation_id=command.operation_id)
            self.calls.append(command)
            return SendReceipt(command.operation_id, 'sent', message_id='synthetic-message')

    repository, client = Repository(), MaxClient()
    worker = BotOutboxWorker(repository, client, None, worker_id='synthetic-bot',
        maintenance=gate, participant_id='synthetic')

    async def run():
        async with gate.async_admission('synthetic', 'outbound_bot'):
            return await worker.run_once()

    receipt = asyncio.run(run())
    assert receipt.state == 'sent' and receipt.operation_id == operation_id
    assert repository.receipt == receipt and len(client.calls) == 1
    assert gate.active_operations('synthetic') == ()


def test_notification_worker_binds_claim_id_before_max_dispatch(tmp_path):
    gate, operation_id = owner_gate(tmp_path), str(uuid4())
    intent = SimpleNamespace(id=operation_id)
    claim = SimpleNamespace(id=operation_id, intent=intent)

    class Repository:
        receipt = None
        def recover_expired(self): pass
        def claim(self, *args, **kwargs): return claim
        def authorize(self, current):
            assert current is claim
            return 'synthetic-proof'
        def mark_sending(self, current, send, *, observed_fingerprints):
            assert current is claim and observed_fingerprints == 'synthetic-proof'
            return current
        def finish(self, current, receipt):
            assert current is claim
            self.receipt = receipt

    class Sender:
        calls = []
        def dispatch(self, current_intent, *, authorize, before_send, cancelled):
            assert current_intent is intent and not cancelled()
            gate.assert_dispatch_admission('synthetic', allowed_kinds=('outbound_notification',),
                operation_id=current_intent.id)
            proof = authorize()
            before_send(SimpleNamespace(operation_id=current_intent.id), proof)
            self.calls.append(current_intent.id)
            return SendReceipt(current_intent.id, 'sent', message_id='synthetic-notification')

    repository, sender = Repository(), Sender()
    worker = NotificationWorker(repository, sender, worker_id='synthetic-notification',
        maintenance=gate, participant_id='synthetic')

    async def run():
        async with gate.async_admission('synthetic', 'outbound_notification'):
            return await worker.run_once()

    receipt = asyncio.run(run())
    assert receipt.state == 'sent' and receipt.operation_id == operation_id
    assert repository.receipt == receipt and sender.calls == [operation_id]
    assert gate.active_operations('synthetic') == ()
