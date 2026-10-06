"""Real isolated bot repositories, public ASGI and MockTransport boundary proofs."""
import asyncio
from datetime import timedelta
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from test_bot_repository import botcase, callback, command, stored
from test_max_webhook import wire
from secretary.application.bot_commands import BotCommandService
from secretary.domain.bot import BotError, DeterministicReply
from secretary.infrastructure.max_bot import MaxClient
from secretary.interface.team_gateway import TeamGatewayClients, TeamGatewaySettings, create_team_app
from secretary.orchestration.bot_worker import BotInboxWorker, BotOutboxWorker


def test_revocation_after_resolve_before_atomic_accept_denies_command(botcase, monkeypatch):
    c = botcase
    payload = c.repo.store_button(stored(c), key='confirm', action='confirm', command=command(c))
    incoming = callback(c, payload)
    resolve = c.repo.resolve_button

    def revoke(event):
        action = resolve(event)
        c.team.upsert_member(c.member.model_copy(update={'revision': 1, 'enabled': False}), expected_revision=0)
        return action

    monkeypatch.setattr(c.repo, 'resolve_button', revoke)
    service = BotCommandService(c.team, c.repo, public_origin='https://team.example', clock=lambda: c.time[0])
    with pytest.raises(BotError, match='bot_actor_changed'):
        service.handle(incoming)
    with c.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM team_commands').fetchone()[0] == 0


def test_real_transport_loss_is_durable_uncertain_and_restart_does_not_resend(botcase):
    c = botcase
    calls = []
    stored(c)
    c.repo.finish_event(c.repo.claim_event('processor'), DeterministicReply('Synthetic reply'))

    def timeout(request):
        calls.append(request.method)
        raise httpx.ReadTimeout('synthetic lost response')

    client = MaxClient('SYNTHETIC_MAX_TOKEN', bot_id='42', client=httpx.Client(
        trust_env=False, follow_redirects=False, timeout=5, transport=httpx.MockTransport(timeout)))
    try:
        worker = BotOutboxWorker(c.repo, client, c.auth, worker_id='sender')
        receipt = asyncio.run(worker.run_once())
        assert receipt.state == 'uncertain' and calls == ['POST']
        c.time[0] += timedelta(hours=1)
        restarted = c.a.BotRepository(c.a.TeamDatabase(c.db.path), c.team, c.auth, clock=lambda: c.time[0])
        assert asyncio.run(BotOutboxWorker(restarted, client, c.auth, worker_id='restart').run_once()) is None
        assert calls == ['POST']
        with c.db.connection() as conn:
            assert conn.execute('SELECT state,attempt FROM bot_reply_state').fetchone()[:] == ('uncertain', 1)
    finally:
        client.close()


def test_webhook_worker_preview_confirm_and_private_delivery_are_single_effect(botcase):
    c = botcase
    secret = 'SYNTHETIC_WEBHOOK_SECRET'
    settings = TeamGatewaySettings(public_origin='https://team.example', bot_id='42',
        bot_token='SYNTHETIC_MAX_TOKEN', webhook_secret=secret)
    app = create_team_app(settings, c.team, TeamGatewayClients(auth=c.auth, bot_intake=c.repo))
    service = BotCommandService(c.team, c.repo, public_origin=settings.public_origin, clock=lambda: c.time[0])
    worker = BotInboxWorker(c.repo, service, worker_id='processor')
    first = wire()
    first['message']['sender']['user_id'] = int(c.member.max_user_id)
    headers = {'X-Max-Bot-Api-Secret': secret}
    with TestClient(app, base_url=settings.public_origin, client=('203.0.113.42', 45110)) as client:
        assert client.post('/hooks/max', json=first, headers=headers).status_code == 200
        assert client.post('/hooks/max', json=first, headers=headers).status_code == 200
        listing = asyncio.run(worker.run_once())
        action = next(b.payload for row in listing.buttons for b in row if b.text == 'В работе')
        assert asyncio.run(worker.run_once()) is None

        def callback_wire(identifier, payload):
            return {'update_type': 'message_callback', 'timestamp': first['timestamp'], 'message': None,
                'callback': {'callback_id': identifier, 'user': {'user_id': int(c.member.max_user_id), 'is_bot': False}, 'payload': payload}}

        preview_wire = callback_wire('synthetic-preview-callback', action)
        assert client.post('/hooks/max', json=preview_wire, headers=headers).status_code == 200
        assert client.post('/hooks/max', json=preview_wire, headers=headers).status_code == 200
        preview = asyncio.run(worker.run_once())
        assert preview.command.expected_revision == 0
        with c.db.connection() as conn:
            assert conn.execute('SELECT COUNT(*) FROM team_commands').fetchone()[0] == 0
        confirm_wire = callback_wire('synthetic-confirm-callback', preview.buttons[0][0].payload)
        assert client.post('/hooks/max', json=confirm_wire, headers=headers).status_code == 200
        confirmation = asyncio.run(worker.run_once())
        assert confirmation.receipt.execution_state.state == 'queued'
        assert client.post('/hooks/max', json=confirm_wire, headers=headers).status_code == 200
        assert asyncio.run(worker.run_once()) is None
        with c.db.connection() as conn:
            assert conn.execute('SELECT COUNT(*) FROM team_commands').fetchone()[0] == 1
            assert conn.execute('SELECT COUNT(*) FROM bot_button_confirmations').fetchone()[0] == 1
            assert conn.execute('SELECT COUNT(*) FROM bot_replies').fetchone()[0] == 3

    sends = []
    def sent(request):
        sends.append(json.loads(request.content))
        assert request.url.params['user_id'] == c.member.max_user_id
        return httpx.Response(200, json={'message': {'sender': {'user_id': 42},
            'timestamp': first['timestamp'],
            'recipient': {'user_id': int(c.member.max_user_id), 'chat_id': 777, 'chat_type': 'dialog'},
            'body': {'mid': 'synthetic-sent-' + str(len(sends)), 'text': sends[-1]['text']}}})
    transport = MaxClient('SYNTHETIC_MAX_TOKEN', bot_id='42', client=httpx.Client(trust_env=False,
        follow_redirects=False, timeout=5, transport=httpx.MockTransport(sent)))
    try:
        sender = BotOutboxWorker(c.repo, transport, c.auth, worker_id='sender')
        for _ in range(3):
            assert asyncio.run(sender.run_once()).state == 'sent'
        assert asyncio.run(sender.run_once()) is None and len(sends) == 3
    finally:
        transport.close()
