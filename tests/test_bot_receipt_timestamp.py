"""MAX acceptance timestamps stay JSON safe in synthetic Bot and Voice ledgers."""
from dataclasses import asdict
from datetime import datetime, timezone
import json
from uuid import uuid4

import httpx
import pytest

from secretary.domain.bot import BotButton, BotError, BotSend, DeterministicReply, SendReceipt
from secretary.infrastructure.max_bot import MaxClient
from test_bot_repository import botcase, stored
from test_voice_repository import c, job, acknowledge_initial_reply


ACCEPTED_AT = '2026-10-03T21:00:00.123+00:00'
MAX_TIMESTAMP = 1791061200123


def actual_receipt(operation_id, user_id):
    def handler(request):
        assert request.method == 'POST'
        payload = json.loads(request.content)
        return httpx.Response(200, json={'message': {
            'sender': {'user_id': 42}, 'recipient': {'user_id': int(user_id), 'chat_type': 'dialog'},
            'timestamp': MAX_TIMESTAMP, 'body': {'mid': 'synthetic-accepted-mid', 'text': payload['text']}}})
    transport = MaxClient('SYNTHETIC_NOT_A_CREDENTIAL', bot_id='42',
        client=httpx.Client(trust_env=False, follow_redirects=False, timeout=5,
                            transport=httpx.MockTransport(handler)))
    try:
        return transport.send_message(BotSend(operation_id, user_id, 'Synthetic reply'))
    finally:
        transport.close()


def test_legacy_send_receipt_json_without_timestamp_keeps_none():
    legacy = {'operation_id': str(uuid4()), 'state': 'sent', 'message_id': 'synthetic-legacy-mid',
              'error_code': None, 'retry_after': None}
    receipt = SendReceipt(**json.loads(json.dumps(legacy)))
    assert receipt.accepted_at is None
    assert SendReceipt(**json.loads(json.dumps(asdict(receipt)))) == receipt


@pytest.mark.parametrize('accepted_at', [ACCEPTED_AT, '2026-10-03T21:00:00Z',
    '2026-10-03T21:00:00.123Z', '2026-10-03T21:00:00.123456+00:00'])
def test_sent_receipt_utc_timestamp_roundtrips_as_json_string(accepted_at):
    receipt = SendReceipt(str(uuid4()), 'sent', message_id='synthetic-mid', accepted_at=accepted_at)
    assert SendReceipt(**json.loads(json.dumps(asdict(receipt)))) == receipt
    assert receipt.accepted_at == accepted_at


@pytest.mark.parametrize('accepted_at', [True, 1791061200123,
    datetime(2026, 10, 4, tzinfo=timezone.utc), '', 'PRIVATE_INVALID_TIMESTAMP',
    '2026-10-03', '2026-10-03T21:00:00', '2026-10-03 21:00:00+00:00',
    '2026-10-03T21:00:00+03:00', '2026-10-03T21:00:00-00:00',
    '2026-02-30T21:00:00Z', '2026-10-03T21:00:60Z',
    '2026-10-03T21:00:00.1234567Z', '0000-01-01T00:00:00Z',
    '10000-01-01T00:00:00Z', '2026-10-03T21:00:00Z\x00'])
def test_send_receipt_rejects_invalid_naive_non_utc_or_non_string_time(accepted_at):
    with pytest.raises(BotError, match='bot_receipt_invalid') as caught:
        SendReceipt(str(uuid4()), 'sent', message_id='synthetic-mid', accepted_at=accepted_at)
    assert 'PRIVATE' not in str(caught.value)


@pytest.mark.parametrize('state', ['retryable', 'uncertain', 'rejected'])
def test_non_sent_receipt_cannot_claim_max_acceptance_time(state):
    with pytest.raises(BotError, match='bot_receipt_invalid'):
        SendReceipt(str(uuid4()), state, accepted_at=ACCEPTED_AT)


@pytest.mark.parametrize('legacy', [False, True])
def test_bot_receipt_persists_real_timestamp_or_legacy_none_without_event_or_lease_time(botcase, legacy):
    case = botcase
    source = stored(case)
    case.repo.finish_event(case.repo.claim_event('synthetic-worker'), DeterministicReply('Synthetic reply'))
    delivery = case.repo.claim_reply('synthetic-sender')
    receipt = (SendReceipt(delivery.id, 'sent', message_id='synthetic-legacy-mid') if legacy
               else actual_receipt(delivery.id, source.user_id))
    case.repo.finish_reply(delivery, receipt)
    with case.db.connection() as conn:
        row = conn.execute('SELECT state,receipt FROM bot_reply_state WHERE id=?', (delivery.id,)).fetchone()
    saved = json.loads(row['receipt'])
    assert row['state'] == 'sent' and saved == asdict(receipt)
    assert SendReceipt(**saved) == receipt
    assert saved['accepted_at'] == (None if legacy else ACCEPTED_AT)
    assert ACCEPTED_AT != case.time[0].isoformat() and ACCEPTED_AT != delivery.lease_until.isoformat()


@pytest.mark.parametrize('legacy', [False, True])
def test_voice_receipt_persistence_and_confirmation_dependency_accept_timestamp_or_legacy_json(c, legacy):
    source, _, _ = job(c, text='Synthetic instruction')
    acknowledge_initial_reply(c)
    detail = c.repo.enqueue_notice(source, 'synthetic-detail', DeterministicReply('Synthetic full detail'))
    confirmation = c.repo.enqueue_notice(source, 'synthetic-confirmation',
        DeterministicReply('Synthetic confirmation', ((BotButton('Confirm', payload='synthetic-button'),),)))
    delivery = c.repo.claim_reply('synthetic-sender')
    assert delivery.id == detail and c.repo.claim_reply('competing-sender') is None
    receipt = (SendReceipt(delivery.id, 'sent', message_id='synthetic-legacy-mid') if legacy
               else actual_receipt(delivery.id, source.user_id))
    c.repo.finish_reply(delivery, receipt)
    with c.db.connection() as conn:
        row = conn.execute('SELECT state,receipt FROM voice_notice_state WHERE id=?', (detail,)).fetchone()
    saved = json.loads(row['receipt'])
    assert row['state'] == 'sent' and saved == asdict(receipt)
    assert SendReceipt(**saved) == receipt
    assert saved['accepted_at'] == (None if legacy else ACCEPTED_AT)
    if legacy:
        # Historical JSON has no accepted_at. Dependency readback must retain its old contract.
        saved.pop('accepted_at')
        with c.db.transaction() as conn:
            conn.execute('UPDATE voice_notice_state SET receipt=? WHERE id=?', (json.dumps(saved), detail))
    restarted = c.a.VoiceRepository(c.db, c.team, c.bot, clock=lambda: c.clock[0])
    ready = restarted.claim_reply('synthetic-confirmation-sender')
    assert ready.id == confirmation and restarted.authorize_reply(ready) == ready
    assert ACCEPTED_AT != c.clock[0].isoformat() and ACCEPTED_AT != delivery.lease_until.isoformat()
