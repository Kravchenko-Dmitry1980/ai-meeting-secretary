"""Policy negatives run under the existing application offline guard."""
from pathlib import Path
import os
import sys

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from local_asr_test_guard import (FINALIZER_COMMAND, GuardPolicy,
                                  JUNCTION_COMMAND, ROOT_ENV, SCRATCH_ENV,
                                  PYTHON_ENV, BASE_PYTHON_ENV)


@pytest.fixture
def policy(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    return GuardPolicy(package, tmp_path, Path(sys.executable), Path(sys.executable),
                       app_root=tmp_path.parent / "live", pwsh=tmp_path / "pwsh.exe",
                       tasklist=tmp_path / "tasklist.exe")


def environment(policy):
    return {ROOT_ENV: str(policy.package), SCRATCH_ENV: str(policy.scratch),
            PYTHON_ENV: str(policy.python), BASE_PYTHON_ENV: str(policy.base_python),
            "PYTHONDONTWRITEBYTECODE": "1"}


def test_fixture_files_allowed_but_live_data_and_credentials_denied(policy):
    policy.path(policy.scratch / "audio.wav", writing=True)
    policy.path(policy.scratch / "model.safetensors", writing=True)
    for path in (policy.app_root / "data/secretary.sqlite3", policy.app_root / "audio/meeting.wav",
                 policy.app_root / "models/weights.bin", policy.app_root / ".env",
                 policy.app_root / "secrets/key", policy.app_root / "credentials/token"):
        with pytest.raises(PermissionError):
            policy.path(path)
    with pytest.raises(PermissionError, match="write outside scratch"):
        policy.path(policy.app_root / "config.json", writing=True)


def test_normalized_traversal_cannot_escape_write_boundary(policy):
    with pytest.raises(PermissionError):
        policy.path(policy.scratch / "../outside.txt", writing=True)


def test_network_path_is_refused_before_filesystem_resolution(policy):
    with pytest.raises(PermissionError):
        policy.path(r"\\server\share\file.txt")


@pytest.mark.skipif(os.name != 'nt', reason='Windows NUL device semantics')
def test_only_null_device_is_exempt_before_resolution(policy, monkeypatch):
    def must_not_resolve(*args, **kwargs):
        raise AssertionError('device path must not be resolved')
    monkeypatch.setattr(Path, 'resolve', must_not_resolve)
    for path in ('nul', 'NUL', r'\\.\NUL', policy.package / 'nul', policy.app_root / 'NUL'):
        policy.path(path, writing=True)
    for path in (r'\\server\share\nul', r'\\?\D:\nul', r'\\.\COM1', r'\\.\NUL\other'):
        with pytest.raises(PermissionError):
            policy.path(path, writing=True)


def test_only_new_loopback_listener_and_owned_connections_allowed(policy):
    policy.network(("127.0.0.1", 0), binding=True)
    for address, binding in [(('0.0.0.0', 0), True), (('127.0.0.1', 28340), True),
                             (('127.0.0.1', 28340), False), (('192.0.2.1', 443), False)]:
        with pytest.raises(PermissionError):
            policy.network(address, binding=binding)
    policy.owned_ports.add(('127.0.0.1', 54321))
    policy.network(('127.0.0.1', 54321))
    policy.owned_ports.clear()
    with pytest.raises(PermissionError):
        policy.network(('127.0.0.1', 54321))


def test_python_child_must_keep_isolation_environment(policy):
    command = [str(policy.python), '-B', '-c', 'print(1)']
    policy.process(command, environment=environment(policy))
    with pytest.raises(PermissionError, match='drops isolation'):
        policy.process(command, environment={})
    for option in ('-S', '-E', '-I', '-BS', '-BI', '-BE', '-cpass', '-u'):
        with pytest.raises(PermissionError, match='disables test guard'):
            policy.process([str(policy.python), option, '-c', 'pass'], environment=environment(policy))
    with pytest.raises(PermissionError, match='shell'):
        policy.process(command, environment=environment(policy), shell=True)


def test_python_children_cannot_change_interpreter_or_pythonhome(policy):
    command = [str(policy.python), '-B', '-c', 'pass']
    for key in (ROOT_ENV, SCRATCH_ENV, PYTHON_ENV, BASE_PYTHON_ENV):
        env = environment(policy)
        env[key] = str(policy.scratch / 'other')
        with pytest.raises(PermissionError, match='drops isolation'):
            policy.process(command, environment=env)
    for key in ('PYTHONHOME', 'pythonhome'):
        env = dict(environment(policy), **{key: ''})
        with pytest.raises(PermissionError, match='Python home'):
            policy.process(command, environment=env)


def test_python_options_end_at_command_module_or_reviewed_script(policy):
    for args in (['-B', '-c', "print('-S')", '-S'], ['-m', 'pytest', '-q'],
                 ['-B', str(policy.package / 'scripts/report.py'), '-S']):
        policy.process([str(policy.python), *args], environment=environment(policy))
    for args in ([], ['-B'], ['-c'], ['-m', 'pip'], [str(policy.app_root / 'other.py')]):
        with pytest.raises(PermissionError):
            policy.process([str(policy.python), *args], environment=environment(policy))


def test_tasklist_only_reads_exact_pid(policy):
    policy.process([str(policy.tasklist), '/FI', 'PID eq 1234', '/FO', 'CSV', '/NH'])
    for arguments in ([], ['/S', 'remote'], ['/FI', 'PID eq 0', '/FO', 'CSV', '/NH']):
        with pytest.raises(PermissionError):
            policy.process([str(policy.tasklist), *arguments])


def test_taskkill_and_other_executables_are_not_whitelisted(policy):
    with pytest.raises(PermissionError, match='unreviewed executable'):
        policy.process([str(policy.scratch / 'taskkill.exe'), '/T', '/F', '/PID', '1234'])


def test_direct_base_python_cannot_omit_venv_guard(policy):
    policy.base_python = policy.scratch / 'base-python.exe'
    with pytest.raises(PermissionError, match='unreviewed executable'):
        policy.process([str(policy.base_python), '-c', 'pass'], environment=environment(policy))


def test_only_exact_junction_script_inside_scratch_is_accepted(policy):
    env = {'PILOT_TEST_LINK': str(policy.scratch / 'junction'),
           'PILOT_TEST_TARGET': str(policy.scratch / 'target')}
    command = [str(policy.pwsh), '-NoProfile', '-NonInteractive', '-Command', JUNCTION_COMMAND]
    policy.process(command, environment=env)
    with pytest.raises(PermissionError):
        policy.process(command[:-1] + [JUNCTION_COMMAND + '; Write-Output unsafe'], environment=env)
    env['PILOT_TEST_TARGET'] = str(policy.app_root)
    with pytest.raises(PermissionError, match='outside scratch'):
        policy.process(command, environment=env)


def test_finalizer_requires_exact_source_and_scratch_destinations(policy):
    env = {'PILOT_TEST_DIRECTORY': str(policy.scratch / 'pilot'),
           'PILOT_TEST_METRICS': str(policy.scratch),
           'PILOT_TEST_WRAPPER': str(policy.package / 'scripts/start-dev-gpu-pilot.ps1')}
    command = [str(policy.pwsh), '-NoProfile', '-NonInteractive', '-Command', FINALIZER_COMMAND]
    policy.process(command, environment=env)
    env['PILOT_TEST_WRAPPER'] = str(policy.app_root / 'different.ps1')
    with pytest.raises(PermissionError, match='unreviewed finalizer source'):
        policy.process(command, environment=env)


def test_package_boundary_must_be_strict_child_of_scratch(tmp_path):
    with pytest.raises(PermissionError):
        GuardPolicy(tmp_path, tmp_path, Path(sys.executable), Path(sys.executable))
