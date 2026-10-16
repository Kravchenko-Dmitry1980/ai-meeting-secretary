"""Full-cycle synthetic faults; separate actual source/HANDLE transfer below.

No native binary, HTTP, provider calls or production approval are executed.
"""
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, asdict, replace
import inspect
from types import SimpleNamespace
from uuid import uuid4

import pytest

from secretary.infrastructure import restore_managed_native as lane
from secretary.infrastructure.restore_native_observations import NativeGetBinding, NativeObservationError
from secretary.infrastructure.restore_operator import VerifiedOperatorApproval
from secretary.team_settings import TeamRuntimeSettings, RuntimeProjectBinding, TokenEvidence
from pydantic import SecretStr
import hashlib

# Reuse the actual four-source preparation fixture; its operator and stopped
# runtime observations remain explicitly synthetic, as in the original suite.
from test_restore_fresh_evidence import sources, copy, setup, prepared, staged


@dataclass
class Receipt:
    result: str = 'NATIVE_PROCESS_OBSERVED_CLOSED'
    cleanup_complete: bool = True
    known_child_reaped: bool = True
    readers_stopped: bool = True
    actual_job_root_only: bool = True


@dataclass
class Get:
    evidence_kind: str = 'synthetic_get'


class Ports:
    kind = 'synthetic_managed_native'
    def __init__(self, fault=None):
        self.fault = fault
        self.log = []
        self.pending = self.terminal = None
        self.alive = False
        self.abandoned = False
        self.lease_closed = False
        self.collectors = 0
        self.authentications = 0
        self.deployment = str(uuid4())
        self.values = {key: 'a' * 64 for key in lane._FIELDS}
        self.values.update(restore_id=str(uuid4()), epoch_id=str(uuid4()), preparation_id=str(uuid4()),
                           runtime_deployment_id=self.deployment)
        self.native = SimpleNamespace(enter_nodelete_phase=self.enter, restore_nowrite_and_verify=self.restore)
        self.source_instance = SimpleNamespace(native=self.native, native_path='native', prepared_binding='prepared',
            verify=lambda: self.log.append('held_verify'), closed_digest=self.closed)
        self.inputs_value = dict(resources={'source': 'a' * 64}, verify=self.verify_inputs,
            projection=((2, 1), (1, 0)), binding=NativeGetBinding(3456, 7, 2, 1, 'b' * 64))
        self.verified = VerifiedOperatorApproval('a' * 64, 'b' * 64, 'c' * 64, 9999999999)

    def now(self):
        return '2026-10-05T12:00:00.000000Z'

    @contextmanager
    def source(self, **kwargs):
        self.collectors += 1
        number = self.collectors
        self.log.append('collector_' + str(number) + '_enter')
        if number == 2 and self.fault == 'final_open':
            raise ValueError('RAW_SENSITIVE_SQL')
        values = dict(self.values)
        if number == 2 and self.fault == 'drift':
            values['snapshot_sha256'] = 'd' * 64
        evidence = SimpleNamespace(**values, verify_current=lambda: self.log.append('source_verify'))
        try:
            yield evidence
        finally:
            self.log.append('collector_' + str(number) + '_exit')

    def sources(self, evidence, stack):
        self.log.append('duplicate_lease')
        @contextmanager
        def held():
            try:
                yield
            finally:
                assert not self.alive or self.abandoned
                self.lease_closed = True
                self.log.append('owned_lease_close')
        stack.enter_context(held())
        return self.source_instance

    @contextmanager
    def inputs(self, *args):
        self.log.append('inputs_enter')
        yield self.inputs_value
        self.log.append('inputs_close')

    def verify_inputs(self):
        self.log.append('inputs_verify')
        if self.fault == 'config' and self.alive:
            raise lane.ManagedNativeError('managed_native_configuration')

    def authority(self, root):
        return SimpleNamespace(operator_digest='a' * 64, issue=self.issue, authenticate=self.authenticate)

    def issue(self, commitment, reader, writer):
        self.log.append('issue')
        return 'signed'

    def authenticate(self, commitment, approval):
        self.authentications += 1
        self.log.append('authenticate')
        if self.fault == 'approval' and self.alive:
            raise ValueError('RAW_APPROVAL_SECRET')
        return self.verified

    def store(self, inputs, evidence):
        return self

    def start_attempt(self, verified, commitment):
        self.log.append('durable_started')
        self.pending = dict(attempt_id=str(uuid4()), commitment_sha256='c' * 64, started_at=self.now())
        return self.pending

    def finish_attempt(self, attempt, commitment, terminal):
        self.log.append('durable_terminal')
        assert not self.alive and self.lease_closed
        if self.fault == 'journal':
            raise ValueError('RAW_JOURNAL_CONTENT')
        self.terminal = terminal

    def enter(self):
        self.log.append('nodelete')

    def restore(self):
        assert not self.alive
        self.log.append('return_nowrite')
        if self.fault in ('wal', 'get_wal'):
            raise ValueError('RAW_JOURNAL_CONTENT')

    def closed(self):
        assert self.lease_closed
        self.log.append('closed_receipts')
        return 'f' * 64

    def projection(self, *args):
        self.log.append('projection')
        return ((2, 3), (3, 0)) if self.fault == 'projection' else self.inputs_value['projection']

    def process(self, inputs):
        self.log.append('process_construct')
        if self.fault == 'constructor':
            raise ValueError('RAW_OS_ERROR')
        ports = self
        class Process:
            receipt = Receipt()
            def __enter__(self):
                ports.log.append('launch')
                ports.alive = True
                return self
            def wait_for_listener(self):
                ports.log.append('listener')
            def checkpoint(self):
                ports.log.append('process_check')
            def remaining_seconds(self):
                return 40
            def __exit__(self, kind, value, trace):
                ports.log.append('exact_stop')
                ports.alive = ports.fault in ('cleanup', 'missing_receipt')
                if ports.fault == 'missing_receipt':
                    del self.__class__.receipt
                    raise RuntimeError('RAW_JOIN_UNSTARTED')
                if ports.fault == 'cleanup':
                    self.receipt = Receipt(cleanup_complete=False, actual_job_root_only=False)
                if ports.fault == 'stop_receipt':
                    self.receipt = Receipt(result='NATIVE_PROCESS_FAILED')
        return Process()

    async def get(self, inputs):
        self.log.append('GET')
        if self.fault in ('get', 'get_wal'):
            raise NativeObservationError('native_http_unavailable')
        if self.fault == 'cancel':
            import asyncio
            raise asyncio.CancelledError()
        if self.fault == 'timeout':
            raise TimeoutError()
        return Get('diagnostic_get' if self.fault == 'wrong_kind' else 'synthetic_get')

    def abort_worker(self):
        self.log.append('worker_death')
        self.abandoned = True
        self.alive = False  # synthetic owned Job kill-on-close, never actual death proof


async def perform(ports):
    return await lane._test_observe_restored_native({'project_root': 'synthetic-root'}, 'config', reader=lambda _: '', writer=lambda _: None, ports=ports)


@pytest.mark.asyncio
async def test_full_cycle_transfers_custody_before_initial_closes_and_seals_after_final_closes():
    ports = Ports()
    result = await perform(ports)
    sequence = ['duplicate_lease', 'collector_1_exit', 'durable_started', 'process_construct', 'nodelete',
                'launch', 'listener', 'GET', 'exact_stop', 'return_nowrite', 'collector_2_enter',
                'projection', 'collector_2_exit', 'inputs_close', 'owned_lease_close',
                'closed_receipts', 'durable_terminal']
    assert [ports.log.index(step) for step in sequence] == sorted(ports.log.index(step) for step in sequence)
    assert result.state == 'observed' and result.evidence_kind == 'synthetic_managed_native'
    assert result.closed_observation_only and not result.activation_supported and not result.outbound_enabled
    assert all(ports.terminal[key] for key in ('get_sha256', 'process_sha256', 'final_snapshot_sha256', 'custody_sha256'))


@pytest.mark.asyncio
@pytest.mark.parametrize('fault,code', [('get', 'managed_native_get_failed'), ('cancel', 'managed_native_interrupted'),
    ('timeout', 'managed_native_timeout'), ('config', 'managed_native_configuration'),
    ('approval', 'managed_native_approval'), ('stop_receipt', 'managed_native_process_failed'),
    ('wrong_kind', 'managed_native_get_failed')])
async def test_failed_work_still_stops_returns_nowrite_and_performs_final_readback(fault, code):
    ports = Ports(fault)
    result = await perform(ports)
    assert result.state == 'failed' and result.code == code
    assert ports.log.index('exact_stop') < ports.log.index('return_nowrite') < ports.log.index('collector_2_enter')
    assert 'RAW_' not in str(asdict(result)) + str(ports.terminal)


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['wal', 'get_wal', 'drift', 'projection'])
async def test_readback_drift_or_journal_always_dominates_get_success_or_failure(fault):
    ports = Ports(fault)
    result = await perform(ports)
    assert result.state == 'unreconciled' and result.code == 'native_readonly_get_unreconciled'


@pytest.mark.asyncio
async def test_constructor_failure_never_relaxes_native_sharing():
    ports = Ports('constructor')
    result = await perform(ports)
    assert result.code == 'managed_native_process_failed'
    assert 'nodelete' not in ports.log and 'launch' not in ports.log


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['cleanup', 'missing_receipt'])
async def test_cleanup_uncertainty_has_worker_death_semantics_and_no_terminal_or_resume(fault):
    ports = Ports(fault)
    with pytest.raises(lane._WorkerAbandoned):
        await perform(ports)
    assert ports.pending and ports.terminal is None
    assert 'return_nowrite' not in ports.log and 'durable_terminal' not in ports.log
    assert ports.log.index('worker_death') < ports.log.index('owned_lease_close')


@pytest.mark.asyncio
async def test_terminal_write_failure_does_not_return_success():
    ports = Ports('journal')
    with pytest.raises(ValueError):
        await perform(ports)
    assert ports.pending and ports.terminal is None and not ports.alive


@pytest.mark.asyncio
async def test_fake_ports_cannot_request_production_kind():
    ports = Ports()
    ports.kind = 'managed_native_diagnostic'
    with pytest.raises(lane.ManagedNativeError):
        await perform(ports)
    assert not ports.log


def test_public_factory_has_no_supplied_proof_transport_clock_or_process():
    assert tuple(inspect.signature(lane.observe_restored_native).parameters) == (
        'project_root', 'backup_dir', 'restore_dir', 'maintenance_path', 'lifecycle_path', 'config_path', 'reader', 'writer')
    with pytest.raises(TypeError):
        lane.ManagedNativeDiagnostic('id', 'observed', 'code', 'synthetic', 'a' * 64, activation_supported=True)


@pytest.mark.asyncio
async def test_public_call_in_ordinary_test_process_never_opens_source(monkeypatch, tmp_path):
    monkeypatch.setattr(lane.sys, 'argv', ['server.py'])
    with pytest.raises(lane.ManagedNativeError, match='managed_native_configuration'):
        await lane.observe_restored_native(tmp_path, tmp_path, tmp_path,
            maintenance_path=tmp_path, lifecycle_path=tmp_path, config_path=tmp_path)


def config(tmp_path):
    token = 'tk_synthetic_command_token'
    row = RuntimeProjectBinding(project_id='7', manual_view_id='1', bot_user_id='2',
        bucket_ids={state: str(index) for index, state in enumerate(
            ('inbox', 'accepted', 'doing', 'blocked', 'review', 'done', 'cancelled'), 1)},
        important_label_id='1', urgent_label_id='2', cancelled_label_id='3')
    values = dict(project_dir=tmp_path, deployment_id=str(uuid4()), restore_blocked=True,
        control_database_path=tmp_path / 'control.sqlite3', publication_project_id='7',
        project_bindings=(row,), vikunja_token=SecretStr(token),
        vikunja_credential_binding=TokenEvidence(owner_id='2', token_id='4',
            token_sha256=hashlib.sha256(token.encode()).hexdigest()))
    values.update({role + '_database_path': tmp_path / (role + '.sqlite3')
                   for role in ('secretary', 'team', 'billing', 'vikunja')})
    cfg = TeamRuntimeSettings(**values)
    evidence = SimpleNamespace(runtime_deployment_id=cfg.deployment_id, source_paths=tuple(
        (role, str(getattr(cfg, role + '_database_path'))) for role in ('secretary', 'team', 'billing', 'vikunja')))
    return cfg, evidence


def test_current_token_owner_is_bot_not_human(tmp_path):
    cfg, evidence = config(tmp_path)
    assert lane._settings_binding(cfg, evidence)[:3] == (3456, 7, 2)


@pytest.mark.parametrize('change', [dict(outbound_enabled=True), dict(restore_blocked=False),
    dict(vikunja_base_url='http://[::1]:3456/api/v2/'), dict(vikunja_base_url='http://127.0.0.1:8766/api/v2/'),
    dict(vikunja_base_url='http://127.0.0.1:8765/api/v2/'), dict(vikunja_token=SecretStr('tk_rotated')),
    dict(vikunja_credential_binding=None), dict(publication_project_id=None)])
def test_unsafe_current_settings_refused_before_process(tmp_path, change):
    cfg, evidence = config(tmp_path)
    cfg = cfg.model_copy(update=change)
    with pytest.raises(lane.ManagedNativeError):
        lane._settings_binding(cfg, evidence)


@pytest.mark.parametrize('bot,owner', [(0, 1), (2, 0), (2, 2), ('2', 1), (2, 3), (True, 1)])
def test_actual_readonly_projection_rejects_missing_or_invalid_principals(tmp_path, monkeypatch, bot, owner):
    import sqlite3
    path = tmp_path / 'projection.sqlite3'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE users(id,bot_owner_id)')
        conn.executemany('INSERT INTO users VALUES(?,?)', [(bot, owner), (1, 0)])
    # Synthetic schema only: this seam cannot qualify prepared native validation.
    monkeypatch.setattr(lane, 'read_prepared_native', lambda *args, **kwargs: None)
    with pytest.raises(lane.ManagedNativeError):
        lane._principal_projection(path, None, 2)


def test_actual_readonly_projection_has_separate_human_owner_without_sensitive_columns(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / 'projection.sqlite3'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE users(id INTEGER,bot_owner_id INTEGER)')
        conn.executemany('INSERT INTO users VALUES(?,?)', [(2, 1), (1, 0)])
    before = path.read_bytes()
    monkeypatch.setattr(lane, 'read_prepared_native', lambda *args, **kwargs: None)
    assert lane._principal_projection(path, None, 2) == ((2, 1), (1, 0))
    assert path.read_bytes() == before


def test_actual_four_source_transfer_keeps_win32_write_exclusion_between_collectors(sources, monkeypatch):
    from secretary.infrastructure import restore_native_custody as custody
    from test_restore_fresh_evidence import observe
    from test_restore_native_custody import writer_handle
    monkeypatch.setattr(custody, 'protected_scope', sources[2].protected_scope)
    sample = sources[0]
    with ExitStack() as leases:
        with observe(sources) as initial:
            held = lane._Sources(initial, leases)
            before = lane._summary(initial)
        # Initial source collector is closed; duplicate leases still exclude writes.
        for path in sample.restored.databases.values():
            with pytest.raises(OSError):
                with writer_handle(path): pass
        held.native.enter_nodelete_phase()
        with writer_handle(sample.restored.databases['vikunja']): pass
        for role in ('secretary', 'team', 'billing'):
            with pytest.raises(OSError):
                with writer_handle(sample.restored.databases[role]): pass
        held.native.restore_nowrite_and_verify()
        with observe(sources) as final:
            final.verify_current()
            assert lane._summary(final) == before
        held.verify()
    assert len(held.closed_digest()) == 64
    for path in sample.restored.databases.values():
        with writer_handle(path): pass


def test_inputs_are_built_from_pinned_config_and_fixed_environment_under_actual_leases(sources, monkeypatch):
    import json
    from secretary.infrastructure import restore_native_custody as custody
    from secretary.infrastructure.restore_operator import create_private_directory
    from test_restore_fresh_evidence import observe
    from test_restore_native_custody import writer_handle
    sample, _, target = sources
    monkeypatch.setattr(custody, 'protected_scope', target.protected_scope)
    monkeypatch.setattr(lane, '_principal_projection', lambda *args: ((2, 1), (1, 0)))
    cfg, _ = config(sample.tmp)
    values = cfg.model_dump(mode='json')
    values.update(deployment_id=sample.deployment_id if hasattr(sample, 'deployment_id') else cfg.deployment_id)
    values.update({role + '_database_path': str(path) for role, path in sample.restored.databases.items()})
    values['control_database_path'] = str(sample.tmp / 'original-control.sqlite3')
    values['vikunja_token'] = cfg.vikunja_token.get_secret_value()  # synthetic fixture credential only
    path = sample.tmp / 'synthetic-config.json'
    directory = sample.tmp / '.runtime/team/vikunja-2.7.0'
    if not directory.parent.exists(): create_private_directory(directory.parent)
    create_private_directory(directory)
    binary = directory / 'vikunja-v2.7.0-windows-4.0-amd64.exe'
    binary.write_bytes(b'SYNTHETIC_NOT_EXECUTABLE')
    monkeypatch.setattr(lane, 'PINNED_NATIVE_BINARY_SHA256', hashlib.sha256(binary.read_bytes()).hexdigest())
    with ExitStack() as leases:
        with observe(sources) as initial:
            values['deployment_id'] = initial.runtime_deployment_id
            path.write_text(json.dumps(values), encoding='utf-8')
            held = lane._Sources(initial, leases)
            with lane._inputs(sample.tmp, path, initial, held) as inputs:
                launch = json.loads(lane.Path(lane._win32_path(inputs['native_config'])).read_text())
                assert launch['database']['path'] == str(sample.restored.databases['vikunja'])
                assert launch['service']['interface'] == '127.0.0.1:3456'
                assert not launch['mailer']['enabled'] and not launch['plugins']['enabled']
                assert not any(key.startswith(('POLZA_', 'MAX_', 'PYTHON')) for key in inputs['environment'])
                assert inputs['binding'].principal_id == 2 and inputs['binding'].human_owner_id == 1
                assert cfg.vikunja_token.get_secret_value() not in repr(inputs['resources'])
                for file in (path, binary, inputs['native_config']):
                    with pytest.raises(OSError):
                        with writer_handle(lane.Path(lane._win32_path(file))): pass
                inputs['verify']()
    with writer_handle(path): pass
