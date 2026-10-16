"""Confirmation delivery requires receipts for the complete immutable preview."""
from dataclasses import asdict
from datetime import timedelta
import json
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest

from test_voice_repository import c, job, acknowledge_initial_reply


def identity(source, kind):
    return str(uuid5(NAMESPACE_URL, f'secretary:voice:{source.event_id}:{kind}'))


def preview(c, *, progress=False):
    source, _, _ = job(c, text='Синтетическое поручение')
    acknowledge_initial_reply(c)
    if progress:
        c.repo.enqueue_notice(source, identity(source, 'progress:intent'),
                              c.a.DeterministicReply('Разбираю поручение.'))
    details = tuple(c.repo.enqueue_notice(source, identity(source, f'detail:{index}'),
        c.a.DeterministicReply(str(index) + ' полное значение' * 200)) for index in range(2))
    reply = c.a.DeterministicReply('Подтвердите полный текст',
                                  ((c.a.BotButton('Подтвердить', payload='synthetic-button'),),))
    final = c.repo.enqueue_notice(source, identity(source, 'result'), reply)
    return source, details, final, reply


def finish(c, delivery, state='sent'):
    c.repo.finish_reply(delivery, c.a.SendReceipt(delivery.id, state,
        message_id='synthetic-' + delivery.id if state == 'sent' else None,
        error_code='max_rate_limited' if state == 'retryable' else None,
        retry_after=1 if state == 'retryable' else None))


@pytest.mark.parametrize('failed_state', ['rejected', 'uncertain', 'retryable'])
def test_failed_mandatory_detail_never_releases_confirmation(c, failed_state):
    _, details, final, _ = preview(c)
    attempts = 3 if failed_state == 'retryable' else 1
    for _ in range(attempts):
        delivery = c.repo.claim_reply('sender')
        assert delivery.id == details[0]
        finish(c, delivery, failed_state)
        c.clock[0] += timedelta(seconds=1)
    remaining = c.repo.claim_reply('sender')
    assert remaining.id == details[1]
    finish(c, remaining)
    assert c.repo.claim_reply('sender') is None
    with c.db.connection() as conn:
        assert conn.execute('SELECT state FROM voice_notice_state WHERE id=?', (final,)).fetchone()[0] == 'pending'
        assert conn.execute('SELECT state FROM voice_notice_state WHERE id=?', (details[0],)).fetchone()[0] == failed_state


@pytest.mark.parametrize('progress_state', ['sent', 'rejected', 'uncertain', 'retryable'])
def test_all_detail_receipts_release_confirmation_despite_terminal_progress(c, progress_state):
    _, details, final, reply = preview(c, progress=True)
    for _ in range(3 if progress_state == 'retryable' else 1):
        progress = c.repo.claim_reply('sender')
        assert progress.id not in details and progress.id != final
        finish(c, progress, progress_state)
        c.clock[0] += timedelta(seconds=1)
    for identifier in details:
        delivery = c.repo.claim_reply('sender')
        assert delivery.id == identifier
        assert c.repo.claim_reply('competing-sender') is None
        finish(c, delivery)
    delivery = c.repo.claim_reply('sender')
    assert delivery.id == final and delivery.reply == reply
    assert c.repo.authorize_reply(delivery) == delivery


def test_expired_detail_delivery_blocks_confirmation_after_repository_restart(c):
    _, details, _, _ = preview(c)
    delivery = c.repo.claim_reply('crashed-sender', lease_seconds=1)
    assert delivery.id == details[0]
    c.clock[0] += timedelta(seconds=2)
    reopened = c.a.VoiceRepository(c.db, c.team, c.bot, clock=lambda: c.clock[0])
    reopened.recover_expired()
    remaining = reopened.claim_reply('replacement')
    assert remaining.id == details[1]
    reopened.finish_reply(remaining, c.a.SendReceipt(remaining.id, 'sent', message_id='synthetic-second'))
    assert reopened.claim_reply('replacement') is None


def test_confirmation_freezes_exact_detail_dependencies_and_replays_same_payload(c):
    source, details, final, reply = preview(c, progress=True)
    with c.db.connection() as conn:
        original = dict(conn.execute('SELECT * FROM voice_notices WHERE id=?', (final,)).fetchone())
    assert json.loads(original['payload'])['required_notice_ids'] == list(details)
    c.repo.enqueue_notice(source, 'later-notice', c.a.DeterministicReply('Позднее уведомление'))
    assert c.repo.enqueue_notice(source, identity(source, 'result'), reply) == final
    with c.db.connection() as conn:
        assert dict(conn.execute('SELECT * FROM voice_notices WHERE id=?', (final,)).fetchone()) == original


@pytest.mark.parametrize('receipt', [None, {'operation_id': 'wrong', 'state': 'sent', 'message_id': 'fake'}])
def test_sent_flag_without_valid_matching_receipt_cannot_release_confirmation(c, receipt):
    _, details, _, _ = preview(c)
    for identifier in details:
        delivery = c.repo.claim_reply('sender')
        assert delivery.id == identifier
        finish(c, delivery)
    with c.db.transaction() as conn:
        conn.execute('UPDATE voice_notice_state SET receipt=? WHERE id=?',
                     (json.dumps(receipt) if receipt else None, details[0]))
    assert c.repo.claim_reply('sender') is None


def test_required_receipts_are_rechecked_before_sending_claimed_confirmation(c):
    _, details, _, _ = preview(c)
    for identifier in details:
        delivery = c.repo.claim_reply('sender')
        assert delivery.id == identifier
        finish(c, delivery)
    final = c.repo.claim_reply('sender')
    with c.db.transaction() as conn:
        conn.execute('UPDATE voice_notice_state SET receipt=NULL WHERE id=?', (details[0],))
    with pytest.raises(c.a.VoiceError):
        c.repo.authorize_reply(final)


@pytest.mark.parametrize('detail_state', ['sent', 'rejected', 'uncertain'])
def test_legacy_confirmation_without_dependency_metadata_keeps_same_gate(c, detail_state):
    source, _, _ = job(c, text='Предложение до исправления')
    acknowledge_initial_reply(c)
    c.repo.enqueue_notice(source, identity(source, 'detail:0'), c.a.DeterministicReply('Полное значение'))
    reply = c.a.DeterministicReply('Подтвердите', ((c.a.BotButton('Да', payload='synthetic-button'),),))
    identifier = str(uuid4())
    payload = c.a.canonical(asdict(reply))
    with c.db.transaction() as conn:
        conn.execute('INSERT INTO voice_notices VALUES(?,?,?,?,?)',
                     (identifier, source.event_id, identity(source, 'result'), c.a.content_hash(json.loads(payload)), payload))
        conn.execute("INSERT INTO voice_notice_state(id,state) VALUES(?,'pending')", (identifier,))
    assert c.repo.enqueue_notice(source, identity(source, 'result'), reply) == identifier
    finish(c, c.repo.claim_reply('sender'), detail_state)
    delivery = c.repo.claim_reply('sender')
    if detail_state == 'sent':
        assert delivery.id == identifier and delivery.reply == reply
    else:
        assert delivery is None
    with c.db.connection() as conn:
        assert conn.execute('SELECT payload FROM voice_notices WHERE id=?', (identifier,)).fetchone()[0] == payload
