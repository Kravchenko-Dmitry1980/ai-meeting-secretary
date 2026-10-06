"""T10 fresh canonical checks and exactly one MAX POST; synthetic transports only."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from importlib import import_module, util
import json
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import httpx
import pytest

from secretary.domain.bot import SendReceipt
from secretary.domain.cloud_budget import BudgetSnapshot
from secretary.infrastructure.max_bot import MaxClient
from test_vikunja_adapter import case as adapter_case, task as remote_task


def modules():
    assert util.find_spec('secretary.application.notification_sender'), 'T10 sender absent'
    return import_module('secretary.application.notification_sender'), import_module('secretary.domain.notifications')


@pytest.fixture
def sender_case():
    api, domain = modules()
    clock = [datetime(2026, 10, 5, 9, tzinfo=timezone.utc)]
    due = clock[0] + timedelta(hours=1)
    c = adapter_case(record=remote_task(due_date=due.isoformat()))
    baseline = c.baseline.model_copy(update={'due_at': due, 'due_confirmed': True})
    current = c.adapter.get_task(str(c.remote['id']), context=c.api.TaskReadContext(revision=3, baseline=baseline))
    member = [c.member]
    snapshots = [(current,)]
    intent = domain.NotificationIntent(notification_id=str(uuid5(NAMESPACE_URL, 'synthetic-notification')),
        dedup_key='synthetic-notification-key', recipient_id=c.member.id, recipient_revision=c.member.revision,
        rule='due_1h', scheduled_at=clock[0], task_refs=(domain.NotificationTaskRef(
            project_id='7', task_id=current.task_id, due_revision=0),), project_ids=('7',),
        canonical_verified_at=clock[0], text='PREVIEW_ONLY_NEVER_OUTBOUND',
        group_id=str(uuid5(NAMESPACE_URL, 'synthetic-group')), part_index=0, part_count=1)
    claim = domain.NotificationClaim(id=intent.notification_id, intent=intent, worker_id='synthetic-worker',
        fence=1, lease_until=clock[0] + timedelta(seconds=60), attempt=0, state='preparing')
    calls, frozen, trace = [], [], []
    def handler(request):
        calls.append(request)
        trace.append('max-post')
        body = json.loads(request.content)
        return httpx.Response(200, json={'message': {'recipient': {'user_id': int(request.url.params['user_id']),
            'chat_type': 'dialog'}, 'body': {'mid': 'synthetic-mid', 'text': body['text']},
            'timestamp': int(clock[0].timestamp() * 1000), 'sender': {'user_id': 42}}})
    max_http = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False, timeout=5)
    max_client = MaxClient('SYNTHETIC_ONLY', bot_id='42', client=max_http)
    team = SimpleNamespace(get_member=lambda identifier: member[0] if identifier == member[0].id else None)
    sender = api.NotificationSender(team, {'7': c.adapter}, max_client, clock=lambda: clock[0])
    def authorize():
        trace.append('authorize')
        return domain.NotificationAuthorization(claim=claim, recipient=member[0], tasks=snapshots[0])
    def before_send(send, observed_fingerprints):
        trace.append('freeze')
        frozen.append((send, observed_fingerprints))
    c.calls.clear()
    yield SimpleNamespace(api=api, d=domain, c=c, clock=clock, due=due, member=member, snapshots=snapshots,
        intent=intent, claim=claim, calls=calls, frozen=frozen, trace=trace, sender=sender,
        authorize=authorize, before_send=before_send, max_client=max_client, max_http=max_http, team=team)
    max_http.close()
    c.http.close()


def dispatch(c, **kwargs):
    return c.sender.dispatch(kwargs.pop('intent', c.intent), authorize=c.authorize,
                             before_send=c.before_send, **kwargs)


def test_actual_adapters_get_fresh_then_freeze_exact_body_before_one_post(sender_case):
    c = sender_case
    receipt = dispatch(c)
    assert receipt.state == 'sent' and receipt.operation_id == c.intent.notification_id
    assert len(c.calls) == 1 and len(c.frozen) == 1
    body = json.loads(c.calls[0].content)
    assert body['text'] == c.frozen[0][0].text
    assert 'PREVIEW_ONLY' not in body['text'] and c.snapshots[0][0].title in body['text']
    assert c.calls[0].url.params['user_id'] == c.member[0].max_user_id
    assert c.trace[-2:] == ['freeze', 'max-post']
    assert c.frozen[0][1] == ((c.snapshots[0][0].task_id, c.snapshots[0][0].remote_fingerprint),)
    assert c.c.calls and all(request.method == 'GET' for request in c.c.calls)


def test_changed_title_after_verified_sync_renders_current_not_planned_title(sender_case):
    c = sender_case
    c.c.remote['title'] = 'Новое актуальное название'
    old = c.snapshots[0][0]
    c.snapshots[0] = (c.c.adapter.get_task(old.task_id,
        context=c.c.api.TaskReadContext(revision=old.revision + 1, baseline=old)),)
    assert dispatch(c).state == 'sent'
    assert 'Новое актуальное название' in json.loads(c.calls[0].content)['text']
    assert 'Synthetic task' not in json.loads(c.calls[0].content)['text']


@pytest.mark.parametrize('field,value', [('title', 'External unsynced change'),
    ('due_date', '2026-10-07T12:00:00Z'), ('done', True)])
def test_unsynced_canonical_change_defers_without_post(sender_case, field, value):
    c = sender_case
    c.c.remote[field] = value
    with pytest.raises(c.d.NotificationDeferred):
        dispatch(c)
    assert c.calls == [] and c.frozen == []


@pytest.mark.parametrize('change', [{'enabled': False}, {'revision': 1}, {'project_ids': ()},
    {'max_user_id': '999'}, {'vikunja_user_id': '999'}])
def test_current_member_rebinding_or_revocation_prevents_post(sender_case, change):
    c = sender_case
    original = c.member[0]
    c.sender.team = SimpleNamespace(get_member=lambda _: original.model_copy(update=change))
    with pytest.raises(c.d.NotificationError):
        dispatch(c)
    assert c.calls == [] and c.frozen == []


def test_quiet_hours_defer_to_next_nine_moscow_without_even_canonical_get(sender_case):
    c = sender_case
    c.clock[0] = datetime(2026, 10, 5, 19, tzinfo=timezone.utc)  # 22:00 Moscow.
    with pytest.raises(c.d.NotificationDeferred) as error:
        dispatch(c)
    assert error.value.available_at == datetime(2026, 10, 6, 6, tzinfo=timezone.utc)
    assert c.calls == [] and c.c.calls == []


def test_cancellation_during_free_get_never_reaches_freeze_or_post(sender_case, monkeypatch):
    c = sender_case
    cancelled = [False]
    original = c.c.adapter.get_task
    def read(*args, **kwargs):
        value = original(*args, **kwargs)
        cancelled[0] = True
        return value
    monkeypatch.setattr(c.c.adapter, 'get_task', read)
    with pytest.raises(c.d.NotificationDeferred):
        dispatch(c, cancelled=lambda: cancelled[0])
    assert c.calls == [] and c.frozen == []


def test_digest_sends_only_current_authorized_subset_and_bounds_long_unicode(sender_case):
    c = sender_case
    c.c.remote['title'] = 'Я😀' * 1500
    old = c.snapshots[0][0]
    c.snapshots[0] = (c.c.adapter.get_task(old.task_id, context=c.c.api.TaskReadContext(revision=4, baseline=old)),)
    intent = c.intent.model_copy(update={'rule':'daily_digest', 'task_refs':(*c.intent.task_refs,
        c.d.NotificationTaskRef(project_id='7', task_id='9007199254740999', due_revision=0))})
    c.claim = c.claim.model_copy(update={'intent':intent})
    c.authorize = lambda: c.d.NotificationAuthorization(claim=c.claim, recipient=c.member[0], tasks=c.snapshots[0])
    assert dispatch(c, intent=intent).state == 'sent'
    text = json.loads(c.calls[0].content)['text']
    assert len(text) <= 3500 and old.task_id in text and '9007199254740999' not in text
    assert len(c.calls) == 1


def test_known_429_returns_retryable_but_unknown_timeout_is_uncertain(sender_case):
    c = sender_case
    c.max_http._transport = httpx.MockTransport(lambda _: httpx.Response(429, headers={'Retry-After': '17'}))
    first = dispatch(c)
    assert first.state == 'retryable' and first.retry_after == 17 and first.error_code == 'max_rate_limited'
    def failed(request):
        raise httpx.ReadTimeout('SYNTHETIC_PRIVATE_FAILURE')
    c.max_http._transport = httpx.MockTransport(failed)
    second = dispatch(c)
    assert second.state == 'uncertain' and 'SYNTHETIC_PRIVATE_FAILURE' not in repr(second)


def test_budget_requires_current_owner_period_and_threshold_without_task_get(sender_case):
    c = sender_case
    c.member[0] = c.member[0].model_copy(update={'role': 'owner'})
    intent = c.intent.model_copy(update={'rule':'budget_80', 'task_refs':(),
        'budget_period':'2026-10', 'budget_threshold':80})
    c.claim = c.claim.model_copy(update={'intent':intent})
    c.authorize = lambda: c.d.NotificationAuthorization(claim=c.claim, recipient=c.member[0], tasks=())
    snapshot = BudgetSnapshot('2026-10', 3000000000, 3000000000, 2400000000, 0, 600000000, None)
    c.sender.budget_reader = lambda: snapshot
    result = dispatch(c, intent=intent)
    assert result.state == 'sent' and c.c.calls == []
    text = json.loads(c.calls[0].content)['text']
    assert '80%' in text and '2026-10' in text and '2400' not in text
    c.sender.budget_reader = lambda: replace(snapshot, period='2026-09')
    with pytest.raises(c.d.NotificationError):
        dispatch(c, intent=intent)
    assert len(c.calls) == 1


def test_previous_day_digest_and_quiet_delayed_reminder_never_send_historical_packet(sender_case):
    c = sender_case
    c.clock[0] = datetime(2026,10,5,18,0,tzinfo=timezone.utc)  # 21:00 Moscow.
    with pytest.raises(c.d.NotificationDeferred) as error:
        dispatch(c)
    assert error.value.code == 'notification_quiet_hours'
    c.clock[0] = datetime(2026,10,6,6,0,tzinfo=timezone.utc)
    for rule in ('due_1h','daily_digest'):
        intent = c.intent.model_copy(update={'rule':rule})
        c.claim = c.claim.model_copy(update={'intent':intent})
        c.authorize = lambda: c.d.NotificationAuthorization(claim=c.claim,
            recipient=c.member[0], tasks=c.snapshots[0])
        with pytest.raises(c.d.NotificationError) as error:
            dispatch(c, intent=intent)
        assert error.value.code == 'notification_stale'
    assert c.calls == [] and c.frozen == [] and c.c.calls == []


def test_current_budget_reserved_usage_and_lowered_explicit_cap_are_valid(sender_case):
    c = sender_case
    c.member[0] = c.member[0].model_copy(update={'role':'owner'})
    intent = c.intent.model_copy(update={'rule':'budget_80','task_refs':(),
        'budget_period':'2026-10','budget_threshold':80})
    c.claim = c.claim.model_copy(update={'intent':intent})
    c.authorize = lambda: c.d.NotificationAuthorization(claim=c.claim, recipient=c.member[0],tasks=())
    snapshot = BudgetSnapshot('2026-10',2900000000,2000000000,1000000000,600000000,400000000,None)
    c.sender.budget_reader = lambda: snapshot
    assert dispatch(c,intent=intent).state == 'sent' and not c.c.calls
    c.sender.budget_reader = None
    with pytest.raises(c.d.NotificationDeferred):
        dispatch(c,intent=intent)
    assert len(c.calls) == 1


def test_provider_success_does_not_claim_human_read(sender_case):
    c = sender_case
    receipt = dispatch(c)
    assert receipt.accepted_at == c.clock[0].isoformat(timespec='milliseconds')
    assert not hasattr(receipt,'read_at') and not hasattr(receipt,'delivered_at')


def test_last_authorization_change_after_get_never_freezes(sender_case):
    c = sender_case
    first = c.authorize
    count = [0]
    def changed():
        count[0] += 1
        if count[0] == 2:
            c.snapshots[0] = (c.snapshots[0][0].model_copy(update={'revision':4,'title':'Concurrent local write'}),)
        return first()
    c.authorize = changed
    with pytest.raises(c.d.NotificationDeferred):
        dispatch(c)
    assert c.calls == [] and c.frozen == []


def test_freeze_failure_never_post_and_malformed_post_receipt_is_uncertain(sender_case):
    c = sender_case
    def deny(*args):
        raise c.d.NotificationError('notification_claim_lost')
    original = c.before_send
    c.before_send = deny
    with pytest.raises(c.d.NotificationError):
        dispatch(c)
    assert not c.calls
    c.before_send = original
    c.sender.max_client = SimpleNamespace(send_message=lambda _: object())
    result = dispatch(c)
    assert result.state == 'uncertain' and result.operation_id == c.intent.notification_id


def test_budget_released_reserve_defers_same_key_until_confirmed_crossing(sender_case):
    c = sender_case
    c.member[0] = c.member[0].model_copy(update={'role':'owner'})
    intent = c.intent.model_copy(update={'rule':'budget_50','task_refs':(),
        'budget_period':'2026-10','budget_threshold':50})
    c.claim = c.claim.model_copy(update={'intent':intent})
    c.authorize = lambda: c.d.NotificationAuthorization(claim=c.claim,recipient=c.member[0],tasks=())
    snapshot = BudgetSnapshot('2026-10',3000000000,3000000000,0,0,3000000000,None)
    c.sender.budget_reader = lambda: snapshot
    with pytest.raises(c.d.NotificationDeferred) as error:
        dispatch(c,intent=intent)
    assert error.value.available_at == c.clock[0]+timedelta(seconds=60)
    assert c.calls == [] and c.frozen == []
    snapshot = replace(snapshot,confirmed_micro=1500000000,remaining_micro=1500000000)
    assert dispatch(c,intent=intent).state == 'sent'
    assert c.frozen[0][0].operation_id == intent.notification_id and not c.c.calls
    c.clock[0] = datetime(2026,11,1,6,tzinfo=timezone.utc)
    with pytest.raises(c.d.NotificationError) as error:
        dispatch(c,intent=intent)
    assert type(error.value) is c.d.NotificationError and error.value.code == 'notification_stale'
    assert len(c.calls)==1


@pytest.mark.parametrize('change',[{'period':'2026-09'},{'effective_limit_micro':0},
    {'approved_limit_micro':4000000000},{'confirmed_micro':True}])
def test_invalid_current_budget_proof_defers_instead_of_losing_immutable_key(sender_case,change):
    c=sender_case
    c.member[0]=c.member[0].model_copy(update={'role':'owner'})
    intent=c.intent.model_copy(update={'rule':'budget_50','task_refs':(),
        'budget_period':'2026-10','budget_threshold':50})
    c.claim=c.claim.model_copy(update={'intent':intent})
    c.authorize=lambda:c.d.NotificationAuthorization(claim=c.claim,recipient=c.member[0],tasks=())
    value=BudgetSnapshot('2026-10',3000000000,3000000000,1500000000,0,1500000000,None)
    c.sender.budget_reader=lambda:replace(value,**change)
    with pytest.raises(c.d.NotificationDeferred):
        dispatch(c,intent=intent)
    assert not c.calls and not c.frozen
