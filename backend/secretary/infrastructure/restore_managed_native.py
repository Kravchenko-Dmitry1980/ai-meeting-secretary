"""One-shot stopped-source → native GET → exact stop → closed readback diagnostic.

No runtime admission, activation, token issuance or external provider calls.
The production entry owns every observation port. Its private injected lane is
permanently synthetic; neither result can authorize a restored runtime.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack, closing, contextmanager
import ctypes
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import sys
import time
from urllib.parse import quote, urlsplit
from uuid import uuid4

from secretary.team_settings import load_team_settings
from .restore_fresh_evidence import SourceEvidence, _Observation, prepared_restore_evidence
from .restore_native_custody import _native_file_custody, _Win32FileAPI
from .restore_native_history import PINNED_NATIVE_BINARY_SHA256
from .restore_native_observations import NativeGetBinding, observe_native_diagnostic
from .restore_native_preparation import read_prepared_native
from .restore_operator import (OperatorAuthority, _local, _win32_path,
                               create_private_directory, protected_scope)
from .restore_source_epoch_writer import _native_binding


PURPOSE = 'restore_managed_native_observation'
WORKER_MARKER = '--managed-native-observation-worker'
_FIELDS = ('snapshot_sha256', 'restore_id', 'epoch_id', 'preparation_id',
           'scope_sha256', 'context_sha256', 'native_head_sha256', 'binding_sha256',
           'runtime_deployment_id')
_CODES = frozenset(('managed_native_observed', 'managed_native_invalid',
    'managed_native_configuration', 'managed_native_source_changed',
    'managed_native_approval', 'managed_native_process_failed', 'managed_native_get_failed',
    'managed_native_timeout', 'managed_native_cleanup_failed', 'managed_native_readback_failed',
    'managed_native_custody_failed', 'managed_native_interrupted', 'native_readonly_get_unreconciled'))


class ManagedNativeError(RuntimeError):
    def __init__(self, code='managed_native_invalid'):
        self.code = code if type(code) is str and code in _CODES else 'managed_native_invalid'
        super().__init__(self.code)


class _WorkerAbandoned(BaseException):
    """Private fault harness representation of process death, never a receipt."""


def _fail(code='managed_native_invalid'):
    raise ManagedNativeError(code) from None


def _sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('ascii')).hexdigest()


def _utc():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def _summary(evidence):
    # Never serialize the live _session or publish it as a reusable SourceEvidence.
    return {key: getattr(evidence, key) for key in _FIELDS}


def _handle_identity(api, handle):
    """Full Win32 FileIdInfo matches Python's volume64/FileID128 source facts.

    The custody component's legacy volume32/index64 still protects each phase;
    this coordinator additionally checks the full identity on that same HANDLE.
    No truncation of an existing SourceEvidence identity is accepted.
    """
    from ctypes import wintypes as w
    class FileIdInfo(ctypes.Structure):
        _fields_ = [('volume', ctypes.c_ulonglong), ('identifier', ctypes.c_ubyte * 16)]
    kernel = api._kernel
    kernel.GetFileInformationByHandleEx.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    kernel.GetFileInformationByHandleEx.restype = w.BOOL
    facts = FileIdInfo()
    if not kernel.GetFileInformationByHandleEx(handle, 18, ctypes.byref(facts), ctypes.sizeof(facts)):
        _fail('managed_native_custody_failed')
    return facts.volume, int.from_bytes(bytes(facts.identifier), 'little')


def _binding(evidence):
    return dict(protocol=1, purpose=PURPOSE, restore_id=evidence.restore_id,
        epoch_id=evidence.epoch_id, preparation_id=evidence.preparation_id,
        source_snapshot_sha256=evidence.snapshot_sha256, scope_sha256=evidence.scope_sha256,
        binding_sha256=evidence.binding_sha256)


@dataclass(frozen=True)
class ManagedNativeDiagnostic:
    attempt_id: str
    state: str
    code: str
    evidence_kind: str
    terminal_sha256: str
    closed_observation_only: bool = field(default=True, init=False)
    diagnostic_only: bool = field(default=True, init=False)
    activation_supported: bool = field(default=False, init=False)
    outbound_enabled: bool = field(default=False, init=False)


@contextmanager
def _directory_lease(path, expected=None):
    """Checked, non-inherited owned NoDelete directory HANDLE; no ACL repair."""
    from ctypes import wintypes as w
    api = _Win32FileAPI()
    kernel = api._kernel
    handle = kernel.CreateFileW(_win32_path(path), 0x80000000, 1, None, 3, 0x02200000, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        _fail('managed_native_custody_failed')
    try:
        def verify():
            facts = api.metadata(handle)
            identity = _handle_identity(api, handle)
            if (not facts.attributes & 0x10 or facts.attributes & 0x400
                    or api.inheritance(handle) is not False
                    or expected is not None and list(identity) != list(expected)):
                _fail('managed_native_custody_failed')
            return identity
        identity = verify()
        yield identity, verify
        if verify() != identity:
            _fail('managed_native_custody_failed')
    finally:
        api.close(handle)


class _Sources:
    """Duplicate EXACT live-collector holdings before that collector closes."""
    def __init__(self, evidence, stack):
        if type(evidence) is not SourceEvidence or type(evidence._session) is not _Observation:
            _fail('managed_native_source_changed')
        evidence.verify_current()
        session = evidence._session
        self.native_path = session.paths['vikunja']
        self.snapshot = _summary(evidence)
        self._files, self._directories = {}, []
        if not 4 <= len(session.files) <= 512 or len(session.directories) > 512:
            _fail('managed_native_custody_failed')
        for raw, expected in sorted(session.directories.items()):
            identity, verify = stack.enter_context(_directory_lease(Path(raw), expected))
            self._directories.append(verify)
        for raw, expected in sorted(session.files.items()):
            if Path(raw) == self.native_path:
                continue
            lease = stack.enter_context(_native_file_custody(Path(raw)))
            if (list(_handle_identity(lease._api, lease._current)) != list(expected['file_id'])
                    or lease.baseline.sha256 != expected['sha256']):
                _fail('managed_native_source_changed')
            self._files[raw] = lease
        self._expected = dict(session.files)
        self.native = stack.enter_context(_native_file_custody(self.native_path))
        expected = session.files[str(self.native_path)]
        if (list(_handle_identity(self.native._api, self.native._current)) != list(expected['file_id'])
                or self.native.baseline.sha256 != expected['sha256']):
            _fail('managed_native_source_changed')
        self.prepared_binding = _native_binding(session.proposal)
        self.target = session.target
        self.verify()
        evidence.verify_current()

    def verify(self):
        for verify in self._directories:
            verify()
        for raw, lease in self._files.items():
            lease._finish()
            if list(_handle_identity(lease._api, lease._current)) != list(self._expected[raw]['file_id']):
                _fail('managed_native_source_changed')
        if list(_handle_identity(self.native._api, self.native._current)) != list(self._expected[str(self.native_path)]['file_id']):
            _fail('managed_native_source_changed')

    def closed_digest(self):
        # Receipt access fails unless all corresponding contexts exited cleanly.
        return _sha(dict(files=[asdict(lease.receipt) for lease in self._files.values()],
                         native=asdict(self.native.receipt)))


def _principal_projection(path, prepared_binding, principal):
    """No usernames, email, API-token hashes or token-table secret columns."""
    from .team_backup import _inactive_source
    _inactive_source(Path(_win32_path(path)))
    uri = ('file:' + quote(_win32_path(path), safe='') +
           '?mode=ro&immutable=1&vfs=win32-longpath')
    with closing(sqlite3.connect(uri, uri=True, timeout=.2, isolation_level=None)) as conn:
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        deadline = time.monotonic() + 10
        conn.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        read_prepared_native(conn, binding=prepared_binding)
        rows = conn.execute('SELECT id,bot_owner_id FROM main.users WHERE id=? LIMIT 2',
                            (principal,)).fetchall()
        if (len(rows) != 1 or rows[0][0] != principal
                or any(type(value) is not int or not 0 < value < 2**63 for value in rows[0])
                or rows[0][0] == rows[0][1]):
            _fail('managed_native_configuration')
        owner = conn.execute('SELECT id,bot_owner_id FROM main.users WHERE id=? LIMIT 2',
                             (rows[0][1],)).fetchall()
        if (len(owner) != 1 or owner[0][0] != rows[0][1]
                or type(owner[0][1]) is not int or owner[0][1] != 0):
            _fail('managed_native_configuration')
        return tuple(rows[0]), tuple(owner[0])


def _settings_binding(config, evidence):
    expected = dict(evidence.source_paths)
    if (config.deployment_id != evidence.runtime_deployment_id
            or not config.restore_blocked or config.outbound_enabled
            or config.subscription_reconcile_enabled
            or any(str(getattr(config, role + '_database_path')) != expected[role]
                   for role in ('secretary', 'team', 'billing', 'vikunja'))):
        _fail('managed_native_configuration')
    credential = config.vikunja_credential_binding
    projects = [row for row in config.project_bindings
                if row.project_id == config.publication_project_id]
    raw = config.vikunja_token.get_secret_value()
    if (credential is None or len(projects) != 1 or not raw.startswith('tk_')
            or credential.owner_id != projects[0].bot_user_id
            or not 3 < len(raw) <= 4096 or any(not 33 <= ord(c) <= 126 for c in raw)
            or hashlib.sha256(raw.encode('ascii')).hexdigest() != credential.token_sha256):
        _fail('managed_native_configuration')
    url = urlsplit(config.vikunja_base_url)
    if (url.scheme != 'http' or url.hostname != '127.0.0.1'
            or url.path != '/api/v2/' or url.query or url.fragment or url.username or url.password
            or url.port in (None, config.local_secretary_port, config.gateway_port, 8765)
            or not 1024 <= url.port <= 65535):
        _fail('managed_native_configuration')
    project, principal = int(projects[0].project_id), int(credential.owner_id)
    if not 0 < project < 2**63 or not 0 < principal < 2**63:
        _fail('managed_native_configuration')
    return url.port, project, principal, credential.token_sha256


def _windows_environment(folder):
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetWindowsDirectoryW.argtypes = [w.LPWSTR, w.UINT]
    buffer = ctypes.create_unicode_buffer(4096)
    count = kernel.GetWindowsDirectoryW(buffer, len(buffer))
    if not 0 < count < len(buffer):
        _fail('managed_native_configuration')
    windows = _local(buffer.value)
    environment = dict(SystemRoot=str(windows), WINDIR=str(windows),
        PATH=str(windows / 'System32'), VIKUNJA_SERVICE_SECRET=secrets.token_hex(32))
    for directory, names in (('temp', ('TEMP', 'TMP')), ('profile', ('USERPROFILE', 'HOME')),
                            ('appdata', ('APPDATA',)), ('localappdata', ('LOCALAPPDATA',))):
        path = folder / directory
        create_private_directory(path)
        environment.update({name: str(path) for name in names})
    return environment


def _launch_directory(root):
    # CreateProcess CWD has a separate compatibility limit from extended-path
    # file I/O. Keep fresh launch inputs outside the deep durable journal tree.
    folder = root / '.runtime/team-operator' / ('launch-' + uuid4().hex)
    try:
        units = len(str(folder).encode('utf-16-le')) // 2
    except UnicodeError:
        _fail('managed_native_configuration')
    if units > 259:
        _fail('managed_native_configuration')
    return folder


@contextmanager
def _inputs(root, config_path, evidence, sources):
    root, path = _local(root), _local(config_path)
    if not path.is_relative_to(root) or path.suffix != '.json':
        _fail('managed_native_configuration')
    folder = _launch_directory(root)
    with ExitStack() as stack:
        config_lease = stack.enter_context(_native_file_custody(path))
        if not 0 < config_lease.baseline.length <= 128 * 1024:
            _fail('managed_native_configuration')
        config = load_team_settings(path)
        if (config.project_dir != root
                or config.control_database_path != Path(evidence._session.runtime_args['maintenance_path'])):
            _fail('managed_native_configuration')
        port, project, principal, token_sha = _settings_binding(config, evidence)
        projection = _principal_projection(sources.native_path, sources.prepared_binding, principal)
        binding = NativeGetBinding(port, project, principal, projection[0][1], token_sha)
        binary = root / '.runtime/team/vikunja-2.7.0/vikunja-v2.7.0-windows-4.0-amd64.exe'
        binary_lease = stack.enter_context(_native_file_custody(binary))
        if binary_lease.baseline.sha256 != PINNED_NATIVE_BINARY_SHA256:
            _fail('managed_native_configuration')
        directory = (root / '.runtime/team-operator/activations' / evidence.restore_id /
                     'source-epochs' / evidence.epoch_id / 'native-observations')
        created = not os.path.lexists(_win32_path(directory))
        if created:
            create_private_directory(directory)
        stack.enter_context(protected_scope(directory))
        # Initialize once at directory creation. Missing durable history in an
        # existing directory is ambiguous and must never be silently recreated.
        from .restore_native_observation_store import NativeObservationStore
        journal = directory / 'observations.sqlite3'
        if created:
            NativeObservationStore.create(journal, _binding(evidence), private_folder=directory)
        else:
            NativeObservationStore.open_existing(journal, _binding(evidence), private_folder=directory)
        create_private_directory(folder)
        stack.enter_context(protected_scope(folder))
        # Reserve a loopback port without listening: the configured proxy refuses
        # native outgoing connections. This is not universal network isolation.
        proxy = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
        proxy.bind(('127.0.0.1', 0))
        proxy_url = f'http://127.0.0.1:{proxy.getsockname()[1]}'
        environment = _windows_environment(folder)
        native_config = folder / 'config.yml'
        settings = dict(service=dict(interface=f'127.0.0.1:{port}', publicurl=f'http://127.0.0.1:{port}/',
                rootpath=str(folder), enableregistration=False, enableemailreminders=False,
                enabletaskcomments=False), database=dict(type='sqlite', path=str(sources.native_path)),
            files=dict(basepath=str(sources.target / 'assets/vikunja_files')),
            cors=dict(enable=False), mailer=dict(enabled=False), sentry=dict(enabled=False),
            log=dict(level='error'), defaultsettings=dict(avatar_provider='initials'),
            autotls=dict(enabled=False), plugins=dict(enabled=False),
            outgoingrequests=dict(proxyurl=proxy_url))
        # JSON is a YAML subset; bytes are generated here, never caller YAML/env.
        with Path(_win32_path(native_config)).open('x', encoding='utf-8') as stream:
            json.dump(settings, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        launch_config_lease = stack.enter_context(_native_file_custody(native_config))
        resources = dict(configuration_sha256=config_lease.baseline.sha256,
            binary_sha256=binary_lease.baseline.sha256,
            native_config_sha256=launch_config_lease.baseline.sha256,
            launch_sha256=_sha(dict(argv=[str(binary), 'web', '--config', str(native_config)],
                cwd=str(folder), environment_sha256=_sha(environment), port=port)),
            credential_sha256=token_sha, native_binding_sha256=_sha(asdict(binding)),
            principal_projection_sha256=_sha(projection))
        def verify():
            for lease in (config_lease, binary_lease, launch_config_lease):
                lease._finish()
        yield dict(config=config, binding=binding, resources=resources, directory=directory,
            binary=binary, native_config=native_config, cwd=folder, environment=environment,
            verify=verify, projection=projection)
        verify()


class _ProductionPorts:
    kind = 'managed_native_diagnostic'
    source = staticmethod(prepared_restore_evidence)
    inputs = staticmethod(_inputs)
    sources = staticmethod(_Sources)
    authority = staticmethod(OperatorAuthority)
    projection = staticmethod(_principal_projection)
    now = staticmethod(_utc)

    @staticmethod
    def store(inputs, evidence):
        from .restore_native_observation_store import NativeObservationStore
        path = inputs['directory'] / 'observations.sqlite3'
        return NativeObservationStore.open_existing(path, _binding(evidence), private_folder=path.parent)

    @staticmethod
    def process(inputs):
        from .restore_native_process import ManagedNativeProcess
        return ManagedNativeProcess(binary=inputs['binary'], config=inputs['native_config'],
            cwd=inputs['cwd'], environment=inputs['environment'], port=inputs['binding'].port)

    @staticmethod
    async def get(inputs):
        return await observe_native_diagnostic(inputs['binding'], token=inputs['config'].vikunja_token)

    @staticmethod
    def abort_worker():
        # Do not unwind retained source leases while a child may still be alive.
        # Process death closes the owned kill-on-close Job and kernel HANDLEs;
        # the durable started record remains unfinished and cannot be replayed.
        os._exit(70)


async def _run(arguments, config_path, reader, writer, ports):
    attempt = store = None
    state, code = 'failed', 'managed_native_invalid'
    get_sha = process_sha = final_sha = custody_sha = None
    started_at = ports.now()
    stage = 'managed_native_source_changed'
    try:
        with ExitStack() as leases:
            with ports.source(**arguments) as initial:
                sources = ports.sources(initial, leases)
                initial_summary = _summary(initial)
                inputs = leases.enter_context(ports.inputs(arguments['project_root'], config_path, initial, sources))
                operator = ports.authority(arguments['project_root'])
                original_operator = operator.operator_digest
                commitment = dict(binding=_binding(initial), evidence_kind=ports.kind,
                    resources=inputs['resources'], attempt_context_sha256=_sha(initial_summary))
                stage = 'managed_native_approval'
                writer('Native GET diagnostic; restore ' + initial.restore_id + '; activation remains blocked')
                approval = operator.issue(commitment, reader=reader, writer=writer)
                verified = operator.authenticate(commitment, approval)
                def authenticate():
                    inputs['verify']()
                    sources.verify()
                    try:
                        valid = (operator.operator_digest == original_operator
                                 and operator.authenticate(commitment, approval) == verified)
                    except Exception:
                        _fail('managed_native_approval')
                    if not valid:
                        _fail('managed_native_approval')
                authenticate()
                initial.verify_current()
                store = ports.store(inputs, initial)
            # The duplicated NoWrite holdings already exist; no custody gap.
            authenticate()
            attempt = store.start_attempt(verified, commitment)
            started_at = attempt['started_at']
            stage = 'managed_native_process_failed'
            # Constructor owns no child. Construct before the writable-sharing
            # transition so constructor failure leaves native in initial NoWrite.
            process = ports.process(inputs)
            stage = 'managed_native_custody_failed'
            sources.native.enter_nodelete_phase()
            stage = 'managed_native_process_failed'
            # Context exit owns unconditional original-child stop and reader cleanup.
            process_error = None
            try:
                with process:
                    process.wait_for_listener()
                    authenticate()
                    process.checkpoint()
                    stage = 'managed_native_get_failed'
                    async with asyncio.timeout(min(30, process.remaining_seconds())):
                        result = await ports.get(inputs)
                    if result.evidence_kind != ('diagnostic_get' if ports.kind == 'managed_native_diagnostic' else 'synthetic_get'):
                        _fail('managed_native_get_failed')
                    get_sha = _sha(asdict(result))
                    process.checkpoint()
                    authenticate()
            except BaseException as error:
                process_error = ManagedNativeError(
                    error.code if isinstance(error, ManagedNativeError) else
                    'managed_native_interrupted' if isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)) else
                    'managed_native_timeout' if isinstance(error, TimeoutError) else
                    'managed_native_process_failed' if type(error).__name__ == 'NativeProcessError' else stage)
            try:
                receipt = process.receipt
                cleaned = (receipt.cleanup_complete is True and receipt.known_child_reaped is True
                    and receipt.readers_stopped is True and receipt.actual_job_root_only is True)
            except BaseException:
                cleaned = False
            if not cleaned:
                ports.abort_worker()
                raise _WorkerAbandoned()  # unreachable in actual worker; leave started unfinished
            process_sha = _sha(asdict(receipt))
            if process_error is None and receipt.result != 'NATIVE_PROCESS_OBSERVED_CLOSED':
                process_error = ManagedNativeError('managed_native_process_failed')
            stage = 'native_readonly_get_unreconciled'
            sources.native.restore_nowrite_and_verify()
            stage = 'managed_native_readback_failed'
            with ports.source(**arguments) as final:
                final.verify_current()
                if _summary(final) != initial_summary:
                    _fail('native_readonly_get_unreconciled')
                projection = ports.projection(sources.native_path, sources.prepared_binding, inputs['binding'].principal_id)
                if projection != inputs['projection']:
                    _fail('native_readonly_get_unreconciled')
                final_sha = final.snapshot_sha256
            # A failed/expired GET still receives after-stop reconciliation; it
            # never becomes observed just because final source bytes match.
            if process_error is not None:
                raise process_error
            authenticate()
            stage = 'managed_native_custody_failed'
        custody_sha = sources.closed_digest()
        # Closed holders and closed fresh collector precede durable success.
        # This is a historical closed cutoff, never a current resource permit.
        stage = 'managed_native_approval'
        if operator.operator_digest != original_operator or operator.authenticate(commitment, approval) != verified:
            _fail('managed_native_approval')
        state, code = 'observed', 'managed_native_observed'
    except _WorkerAbandoned:
        raise
    except BaseException as error:
        code = (error.code if isinstance(error, ManagedNativeError) else
                'managed_native_interrupted' if isinstance(error, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)) else
                'managed_native_timeout' if isinstance(error, TimeoutError) else stage)
        state = 'unreconciled' if code == 'native_readonly_get_unreconciled' else 'failed'
        if attempt is None:
            _fail(code)
        # No retry, repair, checkpoint, sidecar deletion or successful fallback.
    finished_at = ports.now()
    if finished_at < started_at:
        state, code = 'failed', 'managed_native_invalid'
    terminal = dict(state=state, evidence_kind=ports.kind, code=code, started_at=started_at,
        finished_at=finished_at, get_sha256=get_sha, process_sha256=process_sha,
        final_snapshot_sha256=final_sha, custody_sha256=custody_sha,
        diagnostic_only=True, activation_supported=False, outbound_enabled=False)
    store.finish_attempt(attempt['attempt_id'], attempt['commitment_sha256'], terminal)
    return ManagedNativeDiagnostic(attempt['attempt_id'], state, code, ports.kind, _sha(terminal))


async def observe_restored_native(project_root, backup_dir, restore_dir, *, maintenance_path,
        lifecycle_path, config_path, reader=input, writer=print):
    """Use only from the explicit one-shot CLI worker, never an ordinary server."""
    if os.name != 'nt' or WORKER_MARKER not in sys.argv:
        _fail('managed_native_configuration')
    arguments = dict(project_root=_local(project_root), backup_dir=_local(backup_dir),
        restore_dir=_local(restore_dir), maintenance_path=_local(maintenance_path),
        lifecycle_path=_local(lifecycle_path))
    return await _run(arguments, config_path, reader, writer, _ProductionPorts())


async def _test_observe_restored_native(arguments, config_path, *, reader, writer, ports):
    """Injected faults never receive a production evidence label."""
    if ports.kind != 'synthetic_managed_native':
        _fail('managed_native_invalid')
    return await _run(arguments, config_path, reader, writer, ports)
