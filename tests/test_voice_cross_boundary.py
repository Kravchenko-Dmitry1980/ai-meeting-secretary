"""T8 end-to-end local queues with real stores and synthetic provider responses."""
import json
from types import SimpleNamespace
import wave

import httpx
import pytest

from secretary.application.bot_commands import BotCommandService
from secretary.application.voice_commands import VoiceCommandService
from secretary.domain.bot import SendReceipt
from secretary.orchestration.bot_worker import BotInboxWorker, BotOutboxWorker
from secretary.orchestration.voice_worker import VoiceWorker
from test_voice_budget import CAP, _billing_rows, _case, _count, _event


LITERAL = 'Создай задачу проверить смету и назначь мне'
TITLE = 'проверить смету'


class _SyntheticMedia:
    """Match the synchronous native adapter port without downloads or FFmpeg."""
    def __init__(self, path):
        self.path = path
        self.calls = []

    def download_voice(self, event, *, cancelled=None):
        assert cancelled is None or not cancelled()
        self.calls.append(event.event_id)
        return SimpleNamespace(path=self.path, duration_ms=1000, command_id=event.event_id)


@pytest.mark.asyncio
async def test_voice_queues_budget_preview_fifo_and_double_confirmation(tmp_path, monkeypatch):
    paid_calls = []
    c = None

    def provider(request):
        assert request.method == 'POST'
        payload = json.loads(request.content)
        stage = 'stt' if request.url.path == '/api/v1/audio/transcriptions' else 'intent'
        assert request.url.path in {'/api/v1/audio/transcriptions', '/api/v1/chat/completions'}
        # Before a synthetic POST, both the paid reservation and the durable
        # voice checkpoint must already exist for the same operation.
        with c.db.connection() as conn:
            row = conn.execute('SELECT * FROM voice_requests WHERE stage=?', (stage,)).fetchone()
            assert row is not None and row['attempt'] == 0
            checkpoint = json.loads(row['payload'])
        charge = c.budget.reservation(row['operation_id'])
        assert charge.status == 'submitted' and charge.reserved_micro > 0
        assert checkpoint['operation_id'] == charge.operation_id
        paid_calls.append((stage, charge.operation_id))
        if stage == 'stt':
            assert payload['model'] == 'openai/whisper-large-v3-turbo'
            assert payload['language'] == 'ru' and payload['file'].startswith('data:audio/wav;base64,')
            return httpx.Response(200, json={'id': 'gen_synthetic_cross_stt',
                'text': LITERAL, 'duration': 1, 'usage': {'cost_rub': .02}})
        assert payload['model'] == 'openai/gpt-4.1-mini'
        literal = json.loads(payload['messages'][1]['content'])
        assert literal['literal_text'] == LITERAL
        assert literal['allowed_context']['actor_id'] == c.actor.id
        proposal = {'action': 'create', 'project_id': '7', 'task_id': None,
            'member_id': c.actor.id, 'person_mention': 'мне', 'title': TITLE,
            'text': None, 'bucket': None, 'important': None, 'urgent': None,
            'due_phrase': None, 'proposed_due_at': None, 'evidence_quote': LITERAL,
            'utterance_kind': 'command', 'unresolved_fields': []}
        return httpx.Response(200, json={'id': 'gen_synthetic_cross_intent',
            'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant',
                'content': json.dumps({'proposals': [proposal]}, ensure_ascii=False)}}],
            'usage': {'cost_rub': .02}})

    c = _case(tmp_path, monkeypatch, provider)
    path = tmp_path / 'synthetic-command.wav'
    with wave.open(str(path), 'wb') as recording:
        recording.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        recording.writeframes(b'\x00\x00' * 16000)
    media = _SyntheticMedia(path)
    voice = VoiceCommandService(c.team, c.bot, c.voices, media, c.client, clock=lambda: c.clock[0])
    commands = BotCommandService(c.team, c.bot, public_origin='https://synthetic.invalid',
                                voice=voice, clock=lambda: c.clock[0])
    inbox = BotInboxWorker(c.bot, commands, worker_id='synthetic-cross-inbox')
    processing = VoiceWorker(c.voices, voice, worker_id='synthetic-cross-voice')
    sends, blocked_while_sending = [], []

    def send(message):
        # All progress, previews and confirmation receipts target the original
        # private decimal MAX user; the value deliberately exceeds JS precision.
        assert message.user_id == c.actor.max_user_id == '9007199254740993'
        assert not message.sensitive
        # A competing voice outbox cannot overtake either the initial reply
        # while it is sending or a currently sending earlier voice notice.
        blocked_while_sending.append(c.voices.claim_reply('synthetic-contender') is None)
        sends.append(message)
        return SendReceipt(message.operation_id, 'sent', message_id=f'synthetic-message-{len(sends)}')

    sender = SimpleNamespace(send_message=send)
    initial_outbox = BotOutboxWorker(c.bot, sender, c.auth, worker_id='synthetic-cross-bot-outbox')
    voice_outbox = BotOutboxWorker(c.voices, sender, c.auth, worker_id='synthetic-cross-voice-outbox')
    event = _event(c)

    initial = await inbox.run_once()
    assert initial is not None and 'Обрабатываю аудиосообщение' in initial.text
    assert paid_calls == [] and media.calls == [] and _count(c, 'team_commands') == 0
    with c.db.connection() as conn:
        job_id = conn.execute('SELECT id FROM voice_jobs WHERE event_id=?', (event.event_id,)).fetchone()[0]
    assert c.voices.get_job(job_id)['state'] == 'queued'

    preview = await processing.run_once()
    saved = c.voices.get_job(job_id)
    assert saved['state'] == 'complete', saved.get('error_code')
    assert preview is not None and 'Изменения ещё не применены' in preview.text
    assert TITLE in preview.text and c.actor.display_name in preview.text
    assert len(saved['proposals']) == 1
    proposal = saved['proposals'][0]
    assert not proposal.unresolved_fields and proposal.command.values.assignee_id == c.actor.id
    assert proposal.command.values.title == TITLE
    assert [stage for stage, _ in paid_calls] == ['stt', 'intent']
    assert len({operation for _, operation in paid_calls}) == 2
    assert media.calls == [event.event_id]
    assert _count(c, 'voice_requests') == _count(c, 'voice_responses') == 2
    assert _count(c, 'team_commands') == 0  # AI output alone is never a task mutation.
    charges = _billing_rows(c)
    assert len(charges) == 2
    assert {row['operation_id'] for row in charges} == {operation for _, operation in paid_calls}
    assert {row['category'] for row in charges} == {'voice_stt', 'voice_intent'}
    assert all(row['status'] == 'confirmed' and row['confirmed_micro'] == 20_000
               and row['command_id'] == event.event_id for row in charges)
    snapshot = c.budget.snapshot()
    assert snapshot.approved_limit_micro == snapshot.effective_limit_micro == CAP
    assert snapshot.confirmed_micro == 40_000 and snapshot.reserved_micro == 0
    assert snapshot.remaining_micro == CAP - 40_000 and snapshot.paused_code is None

    # Even a fully generated preview waits behind the durable initial reply.
    assert await voice_outbox.run_once() is None and sends == []
    assert (await initial_outbox.run_once()).state == 'sent'
    assert sends[0].text == initial.text
    delivered = [await voice_outbox.run_once() for _ in range(3)]
    assert all(receipt is not None and receipt.state == 'sent' for receipt in delivered)
    assert [message.text for message in sends[1:]] == ['Расшифровываю аудио.', 'Разбираю поручение.', preview.text]
    assert [receipt.operation_id for receipt in delivered] == [message.operation_id for message in sends[1:]]
    assert await voice_outbox.run_once() is None and await processing.run_once() is None
    with c.db.connection() as conn:
        rows = conn.execute('SELECT state,receipt FROM voice_notice_state').fetchall()
    assert len(rows) == 3 and all(row['state'] == 'sent' for row in rows)
    assert all(json.loads(row['receipt'])['state'] == 'sent' for row in rows)

    confirm = next(button for row in sends[-1].buttons for button in row if button.text == 'Подтвердить 1')
    receipts = []
    for _ in range(2):
        _event(c, callback_payload=confirm.payload)
        reply = await inbox.run_once()
        assert reply is not None and reply.receipt is not None
        assert 'Команда принята в очередь' in reply.text and 'Выполнение ещё не подтверждено' in reply.text
        assert reply.receipt.execution_state.state == 'queued' and reply.receipt.current is None
        receipts.append(reply.receipt)
        assert (await initial_outbox.run_once()).state == 'sent'
    assert receipts[0].acceptance_receipt == receipts[1].acceptance_receipt
    assert receipts[0].acceptance_receipt.operation_id == proposal.operation_id
    assert _count(c, 'team_commands') == 1
    with c.db.connection() as conn:
        assert conn.execute('SELECT COUNT(*) FROM voice_confirmations').fetchone()[0] == 1
    current = c.team.get_receipt(c.actor.id, proposal.operation_id, expected_actor_revision=0)
    assert current.execution_state.state == 'queued' and current.current is None
    assert c.team.get_projection(c.actor.id, c.task.task_id) == c.task
    assert len(paid_calls) == 2 and media.calls == [event.event_id]
    assert len(_billing_rows(c)) == 2 and c.budget.snapshot() == snapshot
    assert len(sends) == 6 and len({message.operation_id for message in sends}) == 6
    assert all(blocked_while_sending) and len(blocked_while_sending) == 6
    assert await inbox.run_once() is None and await initial_outbox.run_once() is None
