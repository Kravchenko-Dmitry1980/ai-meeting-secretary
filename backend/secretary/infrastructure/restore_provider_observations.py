"""Explicit fresh GET observations of an inactive prepared restore.

The only production entrypoint collects actual sources and pins an explicit
private configuration itself. No supplied proof, transport, clock, URL or client
can qualify a production observation. The private synthetic lane is labelled
in each durable record. Nothing here grants activation, settles Billing, starts
native runtime, registers subscriptions or sends messages.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time

import httpx

from secretary.domain.cloud_budget import APPROVED_MONTHLY_MICRO, BudgetError, to_micro, budget_period
from secretary.domain.restore_quarantine import digest
from secretary.team_settings import load_team_settings
from .max_bot import BASE_URL, MaxError, _https_url, _pairs, _remote_id
from .polza_account import PolzaAccountClient
from .polza_history import PolzaHistoryClient
from .restore_fresh_evidence import prepared_restore_evidence
from .restore_operator import OperatorAuthority, create_private_directory, protected_scope, _local, _win32_path
from .restore_provider_observation_store import ObservationStore
from .team_backup import _safe, _hash
from .team_restore_reconciliation import _pin_file


GET_OPERATIONS = frozenset({'polza_key_usage', 'polza_generation_history',
    'max_bot_identity', 'max_subscriptions'})
_MAX_BODY = 64 * 1024
_ERROR_CODES = frozenset({'monthly_budget_configuration', 'monthly_budget_account_unavailable',
    'monthly_budget_receipt_unavailable', 'max_configuration_invalid', 'max_transport_unsafe',
    'max_response_invalid', 'max_http_rejected', 'max_transport_uncertain',
    'max_subscription_uncertain', 'provider_configuration_required',
    'provider_configuration_changed', 'provider_observation_unavailable'})


class ProviderObservationError(ValueError):
    def __init__(self, code='provider_observation_unavailable'):
        self.code = code if type(code) is str and code in _ERROR_CODES else 'provider_observation_unavailable'
        super().__init__(self.code)


class _ProviderBodyError(Exception):
    def __init__(self, error):
        self.error = error


@contextmanager
def _provider_scope(path):
    # Preserve our sanitized domain errors without hiding genuine ACL/custody
    # failures in the shared Windows helper's ValueError mapping.
    try:
        with protected_scope(path):
            try:
                yield
            except ProviderObservationError as error:
                raise _ProviderBodyError(error) from None
    except _ProviderBodyError as error:
        raise error.error from None


def _sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _secret(value):
    text = value.get_secret_value()
    if text and (len(text) > 4096 or any(not 33 <= ord(c) <= 126 for c in text)):
        raise ProviderObservationError('provider_configuration_required')
    return text


def _iso(value):
    return value.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')


def _now():
    return datetime.now(timezone.utc)


def _monotonic():
    return time.monotonic()


def _history_targets(liabilities, key_tag):
    requests, held, seen = [], [], set()
    for row in liabilities:
        if row.key_tag != key_tag:
            reason = 'different_credential'
        elif row.provider_request_id is None:
            reason = 'missing_request_id'
        else:
            if row.provider_request_id in seen:
                raise ProviderObservationError()
            seen.add(row.provider_request_id)
            requests.append(row)
            continue
        held.append(dict(operation_sha256=_sha(row.operation_id), reason=reason))
    return tuple(requests), tuple(held)


def _binding(evidence):
    return dict(protocol=1, purpose='restore_provider_observation',
        restore_id=evidence.restore_id, epoch_id=evidence.epoch_id,
        preparation_id=evidence.preparation_id,
        source_snapshot_sha256=evidence.snapshot_sha256,
        scope_sha256=evidence.scope_sha256, binding_sha256=evidence.binding_sha256)


@contextmanager
def _pinned_config(project_root, config_path, evidence):
    root, path = _safe(_local(project_root)), _safe(_local(config_path), file=True)
    if not path.is_relative_to(root) or path.suffix != '.json':
        raise ProviderObservationError('provider_configuration_required')
    with _provider_scope(path.parent), _pin_file(path, deny_write=True):
        info = path.stat()
        if info.st_nlink != 1 or info.st_size > 128 * 1024:
            raise ProviderObservationError('provider_configuration_required')
        file_id, file_sha = (info.st_dev, info.st_ino), _hash(path)
        try:
            config = load_team_settings(path)
        except (ValueError, OSError, RecursionError):
            raise ProviderObservationError('provider_configuration_required') from None
        expected = dict(evidence.source_paths)
        if (config.project_dir != root or config.deployment_id != evidence.runtime_deployment_id
                or not config.restore_blocked or config.outbound_enabled
                or config.subscription_reconcile_enabled
                or any(str(getattr(config, role + '_database_path')) != expected[role]
                       for role in ('secretary', 'team', 'billing', 'vikunja'))):
            raise ProviderObservationError('provider_configuration_changed')
        polza_key, max_token = _secret(config.polza_api_key), _secret(config.max_bot_token)
        resources = dict(configuration_sha256=file_sha,
            polza_key_sha256=_sha(polza_key) if polza_key else None,
            max_token_sha256=_sha(max_token) if max_token else None,
            max_bot_id=config.max_bot_id, owner_id=config.local_owner_id,
            projects=[row.model_dump(mode='json') for row in config.project_bindings],
            vikunja_token_sha256=_sha(config.vikunja_token.get_secret_value()),
            vikunja_base_url=config.vikunja_base_url,
            public_origin_sha256=None if config.public_origin is None else _sha(config.public_origin),
            monthly_limit_micro=APPROVED_MONTHLY_MICRO)
        def verify():
            after = path.stat()
            if ((after.st_dev, after.st_ino) != file_id or after.st_nlink != 1
                    or _hash(path) != file_sha):
                raise ProviderObservationError('provider_configuration_changed')
        yield config, resources, verify
        verify()


@contextmanager
def _journal(project_root, evidence):
    directory = (_local(project_root) / '.runtime/team-operator/activations' /
        evidence.restore_id / 'source-epochs' / evidence.epoch_id / 'provider-observations')
    created = False
    if not os.path.lexists(_win32_path(directory)):
        create_private_directory(directory)
        created = True
    with _provider_scope(directory):
        path = directory / 'observations.sqlite3'
        binding = _binding(evidence)
        if os.path.lexists(_win32_path(path)):
            store = ObservationStore.open_existing(path, binding)
        elif created:
            store = ObservationStore.create(path, binding)
        else:
            raise ProviderObservationError()
        yield store


@dataclass(frozen=True)
class _PolzaSettings:
    polza_api_key: str
    polza_base_url: str = 'https://polza.ai/api/v1'


class _ProductionPorts:
    def __init__(self, config, client):
        self._polza = _PolzaSettings(_secret(config.polza_api_key))
        self._max_token, self._bot_id = _secret(config.max_bot_token), config.max_bot_id
        self._max_client = client
        self._identity_verified = False

    async def _max_json(self, path):
        if path not in ('/me', '/subscriptions'):
            raise ProviderObservationError()
        async with self._max_client.stream('GET', BASE_URL + path,
                headers={'Authorization': self._max_token, 'Accept': 'application/json',
                         'Accept-Encoding': 'identity'}) as response:
            if response.status_code != 200:
                raise MaxError('max_http_rejected' if 400 <= response.status_code < 500
                               else 'max_transport_uncertain')
            if (response.headers.get('content-type', '').split(';')[0].strip().lower() != 'application/json'
                    or response.headers.get('content-encoding', 'identity').strip().lower() != 'identity'):
                raise MaxError('max_response_invalid')
            length = response.headers.get('content-length')
            if length is not None and (not length.isdecimal() or int(length) > _MAX_BODY):
                raise MaxError('max_response_invalid')
            raw = bytearray()
            if response.is_stream_consumed:
                if len(response.content) > _MAX_BODY:
                    raise MaxError('max_response_invalid')
                raw.extend(response.content)
            else:
                async for chunk in response.aiter_raw():
                    if len(chunk) > _MAX_BODY - len(raw):
                        raise MaxError('max_response_invalid')
                    raw.extend(chunk)
            try:
                value = json.loads(raw, object_pairs_hook=_pairs,
                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
                if type(value) is not dict:
                    raise ValueError()
                return value
            except (ValueError, UnicodeError, RecursionError):
                raise MaxError('max_response_invalid') from None

    async def get(self, operation, request_id=None):
        if operation == 'polza_key_usage':
            # The ordinary account adapter retries a GET across a reset. This
            # journal instead records exactly one actual request/interval.
            started = _now()
            body = await PolzaAccountClient(self._polza)._read_body(self._max_client, self._polza.polza_api_key)
            finished = _now()
            if finished < started or budget_period(started) != budget_period(finished):
                raise BudgetError('monthly_budget_account_unavailable')
            value = PolzaAccountClient._usage(body, _sha(self._polza.polza_api_key), finished)
            return dict(key_tag=value.key_tag, limit_micro=value.limit_micro,
                remaining_micro=value.remaining_micro, usage_micro=value.usage_micro, reset=value.reset)
        if operation == 'polza_generation_history':
            if (type(request_id) is not str or not 1 <= len(request_id) <= 256
                    or request_id in ('.', '..') or request_id.strip() != request_id
                    or not request_id.isprintable()):
                raise BudgetError('monthly_budget_receipt_unavailable')
            body = await PolzaHistoryClient._read_body(self._max_client, self._polza.polza_api_key, request_id)
            value = PolzaHistoryClient._receipt(body, request_id)
            if value['provider_request_id'] != request_id:
                raise BudgetError('monthly_budget_receipt_unavailable')
            cost = value['confirmed_rub']
            return dict(provider_request_id=request_id, status=value['status'],
                confirmed_cost_micro=None if cost is None else to_micro(cost), provider_period=None)
        if operation == 'max_bot_identity':
            value = await self._max_json('/me')
            if value.get('is_bot') is not True or _remote_id(value.get('user_id')) != self._bot_id:
                raise MaxError('max_response_invalid')
            self._identity_verified = True
            return dict(bot_id=self._bot_id)
        if operation == 'max_subscriptions':
            if not self._identity_verified:
                raise MaxError('max_configuration_invalid')
            value = await self._max_json('/subscriptions')
            rows = value.get('subscriptions')
            if type(rows) is not list or len(rows) > 1000:
                raise MaxError('max_response_invalid')
            records, urls = [], set()
            for row in rows:
                if type(row) is not dict:
                    raise MaxError('max_response_invalid')
                url = _https_url(row.get('url'), redact=True)
                stamp, types = row.get('time'), row.get('update_types')
                if (url in urls or type(stamp) is not int or not 0 <= stamp < 2**63
                        or type(types) is not list or len(types) > 100
                        or any(type(item) is not str or not re.fullmatch('[a-z_]{1,128}', item) for item in types)
                        or len(set(types)) != len(types)):
                    raise MaxError('max_response_invalid')
                urls.add(url)
                records.append(dict(url_sha256=_sha(url), time=stamp, update_types=sorted(types)))
            return dict(bot_id=self._bot_id, subscriptions=sorted(records, key=lambda row: row['url_sha256']))
        raise ProviderObservationError()


@asynccontextmanager
async def _production_ports(config):
    # No proxy, cache, custom hooks/mounts, redirect, retry or injected transport.
    transport = httpx.AsyncHTTPTransport(verify=True, trust_env=False, retries=0)
    async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(10, connect=5, pool=5)) as client:
        yield _ProductionPorts(config, client)


def _requests(resources, liabilities):
    result, holds = [], ()
    polza_tag = resources['polza_key_sha256']
    if polza_tag is not None:
        result.append(('polza', 'polza_key_usage', polza_tag, None, None))
        targets, holds = _history_targets(liabilities, polza_tag)
        for row in targets:
            result.append(('polza', 'polza_generation_history', polza_tag, row.provider_request_id,
                           digest(asdict(row))))
    else:
        holds = tuple(dict(operation_sha256=_sha(row.operation_id), reason='configuration_required')
                      for row in liabilities)
    if resources['max_token_sha256'] is not None and resources['max_bot_id'] is not None:
        for operation in ('max_bot_identity', 'max_subscriptions'):
            result.append(('max', operation, resources['max_token_sha256'], resources['max_bot_id'], None))
    return tuple(result), holds


async def _observe(project_root, backup_dir, restore_dir, *, maintenance_path, lifecycle_path,
        config_path, reader, writer, ports_factory, evidence_kind):
    with prepared_restore_evidence(project_root, backup_dir, restore_dir,
            maintenance_path=maintenance_path, lifecycle_path=lifecycle_path) as evidence:
        with _pinned_config(project_root, config_path, evidence) as (config, resources, verify_config):
            liabilities = evidence.read_liabilities()
            requests, held = _requests(resources, liabilities)
            operator = OperatorAuthority(project_root)
            operator_digest = operator.operator_digest
            binding = _binding(evidence)
            commitment = dict(binding, evidence_kind=evidence_kind, resources=resources,
                requests=requests, liabilities_sha256=digest([asdict(row) for row in liabilities]))
            writer('GET-only restore observations; restore ' + evidence.restore_id +
                   '; saved liabilities ' + str(len(liabilities)) + '; requests ' + str(len(requests)))
            approval = operator.issue(commitment, reader=reader, writer=writer)
            first_verified = None
            def authenticate():
                verify_config()
                if operator.operator_digest != operator_digest:
                    raise ProviderObservationError()
                verified = operator.authenticate(commitment, approval)
                if first_verified is not None and verified != first_verified:
                    raise ProviderObservationError()
                return verified
            first_verified = authenticate()
            evidence.verify_current()
            with _journal(project_root, evidence) as store:
                store.consume_approval(first_verified)
                first = len(store.snapshot()['observations'])
                deadline = _monotonic() + 30
                async with ports_factory(config) as ports:
                    for provider, operation, credential, request_id, liability_sha in requests:
                        remaining = deadline - _monotonic()
                        if remaining <= 0:
                            break
                        authenticate()
                        started = _now()
                        monotonic_start = _monotonic()
                        facts, error, state = None, None, 'observed'
                        try:
                            async with asyncio.timeout(remaining):
                                facts = await ports.get(operation,
                                    request_id if operation == 'polza_generation_history' else None)
                        except Exception as exc:
                            state = 'unavailable'
                            code = getattr(exc, 'code', None)
                            error = code if type(code) is str and code in _ERROR_CODES else 'provider_observation_unavailable'
                        finished = _now()
                        if finished < started or _monotonic() - monotonic_start > 30.1:
                            raise ProviderObservationError()
                        expires = min(finished + timedelta(seconds=300),
                            datetime.fromtimestamp(first_verified.expires_at, timezone.utc))
                        payload = dict(binding, provider=provider, operation=operation,
                            credential_sha256=credential,
                            resource_sha256=digest([resources, request_id, liability_sha]),
                            request_sha256=digest([provider, operation, request_id]), method='GET',
                            started_at=_iso(started), finished_at=_iso(finished), expires_at=_iso(expires),
                            state=state, facts=facts, error_code=error, evidence_kind=evidence_kind,
                            operator_approval_sha256=authenticate().approval_digest)
                        evidence.verify_current()
                        store.append_observation(payload)
                evidence.verify_current()
                authenticate()
                final = store.snapshot()
                added = len(final['observations']) - first
                return dict(protocol=1, state='partial_observations_blocked' if added < len(requests)
                    else 'observations_recorded_blocked',
                    source_snapshot_sha256=evidence.snapshot_sha256,
                    resource_scope_sha256=digest(resources),
                    observations_sha256=digest(final), observation_store_id=final['store_id'],
                    added_observations=added,
                    requested_observations=len(requests), held_liabilities=held,
                    legacy_liability_count=len(liabilities),
                    gross_hold_micro=sum(max(row.reserved_micro, row.observed_cost_micro or 0) for row in liabilities),
                    evidence_kind=evidence_kind, native_observation='unavailable_native_hold',
                    activation_supported=False, outbound_enabled=False)


async def observe_restore_providers(project_root, backup_dir, restore_dir, *, maintenance_path,
        lifecycle_path, config_path, reader=input, writer=print):
    """Explicit GET-only production diagnostics; requires fresh operator consent."""
    try:
        return await _observe(project_root, backup_dir, restore_dir,
            maintenance_path=maintenance_path, lifecycle_path=lifecycle_path, config_path=config_path,
            reader=reader, writer=writer, ports_factory=_production_ports, evidence_kind='production_get')
    except ProviderObservationError:
        raise
    except (ValueError, OSError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        raise ProviderObservationError() from None


async def _test_observe_restore_providers(*args, ports_factory, **kwargs):
    """Private synthetic seam; every durable record is explicitly unqualified."""
    return await _observe(*args, **kwargs, ports_factory=ports_factory, evidence_kind='synthetic_get')
