"""GET-only restore issuer; synthetic facts never qualify provider activation."""
import importlib
import importlib.util
import inspect
from types import SimpleNamespace
from contextlib import asynccontextmanager
from dataclasses import asdict
import json
from pathlib import Path

import httpx
import pytest

from test_restore_source_epoch_writer import copy, setup, prepared, staged
from test_restore_fresh_evidence import sources
from test_restore_reconciliation import sql
from test_team_restore_preview import files


def implementation():
    name = 'secretary.infrastructure.restore_provider_observations'
    assert importlib.util.find_spec(name), 'trusted provider GET issuer is missing'
    return importlib.import_module(name)


def test_production_entrypoint_cannot_accept_caller_provider_proof_or_transport():
    target = implementation()
    assert set(inspect.signature(target.observe_restore_providers).parameters) == {
        'project_root', 'backup_dir', 'restore_dir', 'maintenance_path',
        'lifecycle_path', 'config_path', 'reader', 'writer'}


def test_no_native_get_or_subscription_mutation_is_an_observation_operation():
    target = implementation()
    assert target.GET_OPERATIONS == frozenset({
        'polza_key_usage', 'polza_generation_history',
        'max_bot_identity', 'max_subscriptions'})


def liability(**changes):
    value = dict(operation_id='operation-A', key_tag='a' * 64,
        provider_request_id='saved-request-A', provider_job_id='audio-job-is-not-history-id',
        status='uncertain', reserved_micro=500000, observed_cost_micro=None)
    value.update(changes)
    return SimpleNamespace(**value)


@pytest.mark.parametrize('changes,reason', [
    ({'provider_request_id': None}, 'missing_request_id'),
    ({'key_tag': 'b' * 64}, 'different_credential'),
    ({'provider_request_id': None, 'provider_job_id': 'known-audio-job'}, 'missing_request_id'),
])
def test_missing_history_id_or_different_old_key_never_schedules_history_get(changes, reason):
    requests, held = implementation()._history_targets((liability(**changes),), 'a' * 64)
    assert requests == ()
    assert held == ({'operation_sha256': implementation()._sha('operation-A'), 'reason': reason},)


def test_history_uses_exact_recorded_request_not_audio_job_id():
    requests, held = implementation()._history_targets((liability(),), 'a' * 64)
    assert held == ()
    assert tuple(row.provider_request_id for row in requests) == ('saved-request-A',)


def test_conflicting_exact_history_identity_is_not_deduplicated_as_success():
    target = implementation()
    with pytest.raises(target.ProviderObservationError):
        target._history_targets((liability(), liability(operation_id='operation-B')), 'a' * 64)


def test_error_code_with_unhashable_value_is_sanitized():
    assert str(implementation().ProviderObservationError(['synthetic-secret'])) == 'provider_observation_unavailable'


def config_for_ports():
    from pydantic import SecretStr
    return SimpleNamespace(polza_api_key=SecretStr('synthetic-polza-token'),
        max_bot_token=SecretStr('synthetic-max-token'), max_bot_id='11')


@pytest.mark.asyncio
async def test_production_factory_builds_owned_direct_transport_without_env_or_retry():
    target = implementation()
    async with target._production_ports(config_for_ports()) as ports:
        client = ports._max_client
        assert type(client._transport) is httpx.AsyncHTTPTransport
        assert client.trust_env is False and client.follow_redirects is False
        assert client._transport._pool._retries == 0
        assert client._transport._pool._ssl_context.verify_mode == 2
        assert client.event_hooks == {'request': [], 'response': []}


@pytest.mark.asyncio
async def test_month_rollover_account_observation_does_not_make_second_get(monkeypatch):
    target = implementation()
    from datetime import datetime, timezone
    dates = iter([datetime(2026, 10, 31, 21, 59, 59, tzinfo=timezone.utc),
                  datetime(2026, 10, 31, 22, 0, 1, tzinfo=timezone.utc),
                  datetime(2026, 10, 31, 22, 0, 2, tzinfo=timezone.utc),
                  datetime(2026, 10, 31, 22, 0, 3, tzinfo=timezone.utc)])
    monkeypatch.setattr(target, '_now', lambda: next(dates))
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=dict(limit='1000', limit_remaining='996',
                                           limit_reset='monthly', usage_monthly='4'))
    original = target.PolzaAccountClient.read_key_usage
    async def synthetic_old_reader(self):
        self.transport = httpx.MockTransport(handle)
        self.clock = target._now
        return await original(self)
    monkeypatch.setattr(target.PolzaAccountClient, 'read_key_usage', synthetic_old_reader)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        ports = target._ProductionPorts(config_for_ports(), client)
        with pytest.raises(target.BudgetError, match='monthly_budget_account_unavailable'):
            await ports.get('polza_key_usage')
    assert len(requests) == 1 and requests[0].url.path == '/api/v1/key'


@pytest.mark.asyncio
@pytest.mark.parametrize('status,cost,expected', [
    ('pending', '9', None), ('completed', '4.22', 4220000), ('failed', '0', 0),
])
async def test_polza_exact_single_get_preserves_pending_and_terminal_facts(status, cost, expected):
    target = implementation()
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={'id': 'saved-ID', 'status': status, 'clientCost': cost})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        actual = await target._ProductionPorts(config_for_ports(), client).get('polza_generation_history', 'saved-ID')
    assert len(requests) == 1 and requests[0].method == 'GET'
    assert str(requests[0].url) == 'https://polza.ai/api/v1/history/generations/saved-ID'
    assert actual == dict(provider_request_id='saved-ID', status=status,
                         confirmed_cost_micro=expected, provider_period=None)


@pytest.mark.asyncio
@pytest.mark.parametrize('request_id', [None, '.', '..', ' spaced ', 'newline\n', 'A' * 257])
async def test_invalid_recorded_history_id_does_not_issue_get(request_id):
    target = implementation()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail('invalid ID sent'))) as client:
        with pytest.raises(target.BudgetError):
            await target._ProductionPorts(config_for_ports(), client).get('polza_generation_history', request_id)


def test_actual_windows_config_error_retains_provider_error_domain(tmp_path):
    target = implementation()
    from uuid import uuid4
    from secretary.infrastructure.restore_operator import _WindowsAdapter, create_private_directory
    folder = tmp_path / 'private-config'
    create_private_directory(folder)
    deployment = str(uuid4())
    paths = tuple((role, str(tmp_path / (role + '.sqlite3'))) for role in ('secretary', 'team', 'billing', 'vikunja'))
    config = dict(deployment_id=deployment, project_dir=str(tmp_path),
        control_database_path=str(tmp_path / 'control.sqlite3'), restore_blocked=True, outbound_enabled=True)
    config.update({role + '_database_path': path for role, path in paths})
    path = folder / 'config.json'
    _WindowsAdapter().write_new(path, json.dumps(config).encode('utf-8'))
    evidence = SimpleNamespace(runtime_deployment_id=deployment, source_paths=paths)
    with pytest.raises(target.ProviderObservationError, match='provider_configuration_changed'):
        with target._pinned_config(tmp_path, path, evidence): pytest.fail('config admitted')


@pytest.mark.asyncio
@pytest.mark.parametrize('operation,body,expected', [
    ('max_bot_identity', {'user_id': 11, 'is_bot': True}, {'bot_id': '11'}),
    ('max_subscriptions', {'subscriptions': [{'url': 'https://example.org/hooks/max?secret=synthetic-secret',
        'time': 1, 'update_types': ['message_created', 'bot_started']}]},
     {'bot_id': '11', 'subscriptions': [{'url_sha256': None, 'time': 1,
                                       'update_types': ['bot_started', 'message_created']}]}),
])
async def test_max_observation_is_get_only_and_persists_no_url_query_or_username(operation, body, expected):
    target = implementation()
    requests = []
    def handle(request):
        requests.append(request)
        if operation == 'max_subscriptions' and request.url.path == '/me':
            return httpx.Response(200, json={'user_id': 11, 'is_bot': True})
        return httpx.Response(200, json=body)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        ports = target._ProductionPorts(config_for_ports(), client)
        if operation == 'max_subscriptions':
            await ports.get('max_bot_identity')
        actual = await ports.get(operation)
    if operation == 'max_subscriptions':
        expected['subscriptions'][0]['url_sha256'] = target._sha('https://example.org/hooks/max')
    assert actual == expected
    assert len(requests) == (2 if operation == 'max_subscriptions' else 1)
    assert all(request.method == 'GET' for request in requests)
    assert requests[0].url.host == 'platform-api2.max.ru'
    assert 'synthetic-secret' not in json.dumps(actual)


@pytest.mark.asyncio
@pytest.mark.parametrize('operation,body', [
    ('max_bot_identity', {'user_id': 12, 'is_bot': True}),
    ('max_bot_identity', {'user_id': 11, 'is_bot': False}),
    ('max_bot_identity', {'user_id': '11', 'is_bot': True}),
    ('max_subscriptions', {'subscriptions': [{'url': 'http://example.org/hook', 'time': 1, 'update_types': []}]}),
    ('max_subscriptions', {'subscriptions': [{'url': 'https://example.org/hook', 'time': True, 'update_types': []}]}),
    ('max_subscriptions', {'subscriptions': [{'url': 'https://example.org/hook', 'time': 1, 'update_types': ['bad', 'bad']}]}),
    ('max_subscriptions', {'subscriptions': 'synthetic-secret'}),
])
async def test_max_invalid_identity_or_subscription_is_unavailable(operation, body):
    target = implementation()
    def handle(request):
        if operation == 'max_subscriptions' and request.url.path == '/me':
            return httpx.Response(200, json={'user_id': 11, 'is_bot': True})
        return httpx.Response(200, json=body)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        ports = target._ProductionPorts(config_for_ports(), client)
        if operation == 'max_subscriptions':
            await ports.get('max_bot_identity')
        with pytest.raises((target.MaxError, target.ProviderObservationError)) as error:
            await ports.get(operation)
    assert 'synthetic-secret' not in str(error.value)


@pytest.mark.asyncio
async def test_subscription_cannot_be_bound_to_unverified_configured_bot():
    target = implementation()
    requests = []
    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={'user_id': 12, 'is_bot': True})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        ports = target._ProductionPorts(config_for_ports(), client)
        with pytest.raises(target.MaxError): await ports.get('max_bot_identity')
        with pytest.raises(target.MaxError): await ports.get('max_subscriptions')
    assert len(requests) == 1 and requests[0].url.path == '/me'


@pytest.mark.asyncio
@pytest.mark.parametrize('response', [
    httpx.Response(302, headers={'location': 'https://example.org/steal'}),
    httpx.Response(200, content=b'{}', headers={'content-type': 'text/plain'}),
    httpx.Response(200, content=b'{}', headers={'content-type': 'application/json', 'content-encoding': 'br'}),
    httpx.Response(200, content=b'{}', headers={'content-type': 'application/json', 'content-length': '65537'}),
    httpx.Response(200, content=b'{"user_id":11,"user_id":11,"is_bot":true}', headers={'content-type': 'application/json'}),
    httpx.Response(200, content=b'NaN', headers={'content-type': 'application/json'}),
])
async def test_max_get_rejects_redirect_encoding_oversize_and_ambiguous_json(response):
    target = implementation()
    requests = []
    def handle(request):
        requests.append(request)
        return response
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=False) as client:
        with pytest.raises(target.MaxError):
            await target._ProductionPorts(config_for_ports(), client).get('max_bot_identity')
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_fragmented_oversize_max_body_without_content_length_is_bounded():
    target = implementation()
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b' ' * 32768
            yield b' ' * 32768
            yield b'{}'
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200,
            headers={'content-type': 'application/json'}, stream=Stream()))) as client:
        with pytest.raises(target.MaxError, match='max_response_invalid'):
            await target._ProductionPorts(config_for_ports(), client).get('max_bot_identity')


@pytest.mark.asyncio
async def test_canonical_duplicate_subscription_url_is_not_two_independent_resources():
    target = implementation()
    def handle(request):
        body = {'user_id': 11, 'is_bot': True} if request.url.path == '/me' else {'subscriptions': [
            {'url': 'https://EXAMPLE.org:443/hooks/max?one=secret', 'time': 1, 'update_types': []},
            {'url': 'https://example.org/hooks/max?two=secret', 'time': 2, 'update_types': []}]}
        return httpx.Response(200, json=body)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        ports = target._ProductionPorts(config_for_ports(), client)
        await ports.get('max_bot_identity')
        with pytest.raises(target.MaxError): await ports.get('max_subscriptions')


@pytest.fixture
def issuer_case(sources, monkeypatch):
    sample, preparation, collector = sources
    target = implementation()
    from secretary.infrastructure import restore_source_epoch_writer as source_writer
    # This old fixture's authenticate returns a newly moving expiry. Freeze
    # its synthetic approval to the one issue, as actual MAC receipts do.
    class FixtureOperator(source_writer.OperatorAuthority):
        def issue(self, commitment, **kwargs):
            from datetime import datetime, timezone
            self._fixture_expiry = int(datetime.now(timezone.utc).timestamp()) + 300
            return super().issue(commitment, **kwargs)
        def authenticate(self, commitment, receipt):
            from secretary.infrastructure.restore_operator import VerifiedOperatorApproval
            value = super().authenticate(commitment, receipt)
            return VerifiedOperatorApproval(value.operator_digest, value.approval_digest,
                                            value.nonce_hash, self._fixture_expiry)
    monkeypatch.setattr(target, 'OperatorAuthority', FixtureOperator)
    monkeypatch.setattr(target, 'protected_scope', collector.protected_scope)
    config = dict(schema_version=1, deployment_id=preparation['binding']['runtime_deployment_id'],
        project_dir=str(sample.tmp), control_database_path=str(sample.tmp / 'new-control.sqlite3'),
        polza_api_key='synthetic-polza-token', max_bot_token='synthetic-max-token', max_bot_id='11',
        restore_blocked=True, outbound_enabled=False)
    config.update({role + '_database_path': str(path) for role, path in sample.restored.databases.items()})
    path = sample.tmp / 'synthetic-observation-config.json'
    path.write_text(json.dumps(config), encoding='utf-8')
    args = dict(project_root=sample.tmp, backup_dir=sample.backup, restore_dir=sample.restored.destination,
        maintenance_path=sample.tmp / 'original-control.sqlite3',
        lifecycle_path=sample.tmp / 'original-lifecycle.json', config_path=path,
        reader=lambda _: 'synthetic fixture approval', writer=lambda _: None)
    return sample, target, args, config


@pytest.mark.asyncio
async def test_synthetic_get_records_current_scope_without_modifying_sources_or_old_liability(issuer_case):
    sample, target, args, _ = issuer_case
    before = files(sample.restored.destination)
    old_charges = sql(sample.restored.databases['billing'], 'SELECT * FROM billing_charges')
    requests = []
    class Ports:
        async def get(self, operation, request_id=None):
            requests.append((operation, request_id))
            return {'polza_key_usage': dict(key_tag=target._sha('synthetic-polza-token'),
                        limit_micro=1000000000, remaining_micro=996000000, usage_micro=4000000, reset='monthly'),
                    'max_bot_identity': dict(bot_id='11'),
                    'max_subscriptions': dict(bot_id='11', subscriptions=[])}[operation]
    @asynccontextmanager
    async def ports(_): yield Ports()
    result = await target._test_observe_restore_providers(**args, ports_factory=ports)
    assert requests == [('polza_key_usage', None), ('max_bot_identity', None), ('max_subscriptions', None)]
    assert result['added_observations'] == 3
    assert result['legacy_liability_count'] == 1 and result['gross_hold_micro'] == 500000
    assert result['held_liabilities'][0]['reason'] == 'different_credential'
    assert result['evidence_kind'] == 'synthetic_get'
    assert result['activation_supported'] is False and result['outbound_enabled'] is False
    assert result['native_observation'] == 'unavailable_native_hold'
    assert files(sample.restored.destination) == before
    assert sql(sample.restored.databases['billing'], 'SELECT * FROM billing_charges') == old_charges
    assert not (sample.tmp / 'new-control.sqlite3').exists()
    serialized = json.dumps(result)
    assert 'synthetic-polza-token' not in serialized and 'synthetic-max-token' not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['deployment_id', 'team_database_path', 'outbound_enabled', 'restore_blocked', 'subscription_reconcile_enabled'])
async def test_wrong_current_configuration_stops_before_any_provider_get(issuer_case, change):
    sample, target, args, config = issuer_case
    from uuid import uuid4
    config[change] = (str(uuid4()) if change == 'deployment_id' else
        str(sample.tmp / 'another.sqlite3') if change == 'team_database_path' else change != 'restore_blocked')
    args['config_path'].write_text(json.dumps(config), encoding='utf-8')
    @asynccontextmanager
    async def forbidden(_):
        pytest.fail('configuration drift reached provider factory')
        yield
    with pytest.raises(target.ProviderObservationError):
        await target._test_observe_restore_providers(**args, ports_factory=forbidden)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['rotation', 'expired'])
async def test_mid_get_operator_change_prevents_append_and_next_get(issuer_case, monkeypatch, mode):
    sample, target, args, _ = issuer_case
    from secretary.infrastructure.restore_operator import OperatorError
    original = target.OperatorAuthority.authenticate
    changed, requests = [False], []
    def authenticate(self, *args):
        if changed[0]: raise OperatorError('operator_approval_invalid')
        return original(self, *args)
    monkeypatch.setattr(target.OperatorAuthority, 'authenticate', authenticate)
    class Ports:
        async def get(self, operation, request_id=None):
            requests.append(operation)
            changed[0] = True
            if mode == 'rotation':
                monkeypatch.setattr(target.OperatorAuthority, 'operator_digest', 'b' * 64)
            return dict(key_tag=target._sha('synthetic-polza-token'), limit_micro=None,
                        remaining_micro=None, usage_micro=4000000, reset='monthly')
    @asynccontextmanager
    async def ports(_): yield Ports()
    before = files(sample.restored.destination)
    with pytest.raises(ValueError):
        await target._test_observe_restore_providers(**args, ports_factory=ports)
    assert requests == ['polza_key_usage'] and files(sample.restored.destination) == before
    from test_restore_fresh_evidence import observe
    # Current source evidence needs no changed issuer object or provider proof.
    with target.prepared_restore_evidence(args['project_root'], args['backup_dir'], args['restore_dir'],
            maintenance_path=args['maintenance_path'], lifecycle_path=args['lifecycle_path']) as evidence:
        with target._journal(sample.tmp, evidence) as store:
            assert store.snapshot()['observations'] == ()
            assert len(store.snapshot()['approvals']) == 1


@pytest.mark.asyncio
async def test_total_get_deadline_keeps_partial_journal_explicit(issuer_case, monkeypatch):
    _, target, args, _ = issuer_case
    ticks = iter([0, 0, 0, 1, 31])
    monkeypatch.setattr(target, '_monotonic', lambda: next(ticks))
    requests = []
    class Ports:
        async def get(self, operation, request_id=None):
            requests.append(operation)
            return dict(key_tag=target._sha('synthetic-polza-token'), limit_micro=None,
                        remaining_micro=None, usage_micro=4000000, reset='monthly')
    @asynccontextmanager
    async def ports(_): yield Ports()
    result = await target._test_observe_restore_providers(**args, ports_factory=ports)
    assert requests == ['polza_key_usage']
    assert result['state'] == 'partial_observations_blocked'
    assert result['added_observations'] == 1 and result['requested_observations'] == 3
    assert result['gross_hold_micro'] == 500000 and result['activation_supported'] is False
