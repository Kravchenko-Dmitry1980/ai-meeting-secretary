"""Fresh local operator authority; every path/principal is synthetic."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import base64
import importlib
import importlib.util
import json
import os
from pathlib import Path
import shutil

import pytest


def implementation():
    name = 'secretary.infrastructure.restore_operator'
    assert importlib.util.find_spec(name), 'Fresh independent operator authority is missing'
    return importlib.import_module(name)


class SyntheticAdapter:
    """Explicit test-only custody/DPAPI seam, never a production qualification."""
    def __init__(self):
        self.principal = 'synthetic-SID-A'
        self.private = True
        self.calls = []

    def identity(self):
        return self.principal

    def protect(self, value):
        self.calls.append('protect')
        return self.principal.encode() + b'\0' + value

    def unprotect(self, value):
        self.calls.append('unprotect')
        prefix = self.principal.encode() + b'\0'
        if not value.startswith(prefix):
            raise implementation().OperatorError('operator_custody_invalid')
        return value[len(prefix):]

    def mkdir(self, path):
        path.mkdir()

    def write_new(self, path, data):
        with path.open('xb') as stream:
            stream.write(data)

    @contextmanager
    def scope(self, path):
        if not self.private:
            raise implementation().OperatorError('operator_custody_invalid')
        yield

    @contextmanager
    def root_scope(self, path):
        yield

    @contextmanager
    def file(self, path):
        with path.open('rb') as stream:
            yield stream


@pytest.fixture
def case(tmp_path):
    module = implementation()
    root = tmp_path / 'synthetic-project'
    root.mkdir()
    adapter = SyntheticAdapter()
    clock = [datetime(2026, 10, 4, 12, tzinfo=timezone.utc)]
    authority = module._test_authority(root, adapter=adapter, clock=lambda: clock[0])
    return module, root, adapter, clock, authority


def consent(capture):
    def read(_):
        digest = next(value.split(' ', 1)[1] for value in reversed(capture) if value.startswith('digest '))
        challenge = next(value.split(' ', 1)[1] for value in reversed(capture) if value.startswith('challenge '))
        return challenge + ' ' + digest
    return read


def enroll(authority):
    capture = []
    digest = authority.enroll(reader=consent(capture), writer=capture.append)
    assert len(digest) == 64
    return digest


def commitment():
    return {'restore_uuid': 'synthetic-restore', 'inventory': {'rows': 105, 'sha256': 'a' * 64},
            'watermarks': {role: 'b' * 64 for role in ('secretary', 'team', 'billing', 'vikunja')},
            'scope': 'c' * 64, 'evidence': 'd' * 64}


def issue(authority, value=None):
    capture = []
    receipt = authority.issue(value or commitment(), reader=consent(capture), writer=capture.append)
    assert all(line.startswith(('digest ', 'challenge ')) for line in capture)
    assert 'synthetic-restore' not in ''.join(capture)
    return receipt


def test_constructor_no_files_and_unenrolled_denies(case):
    module, root, _, _, authority = case
    assert list(root.iterdir()) == []
    assert authority.anchor == root / '.runtime/team-operator'
    with pytest.raises(module.OperatorError, match='operator_not_enrolled'):
        authority.issue(commitment(), reader=lambda _: pytest.fail('Unenrolled prompted'))
    with pytest.raises(module.OperatorError, match='operator_not_enrolled'):
        _ = authority.operator_digest


def test_explicit_enrollment_issue_authenticate_and_no_nonce_consumption(case):
    _, _, adapter, _, authority = case
    operator = enroll(authority)
    assert authority.operator_digest == operator
    receipt = issue(authority)
    verified = authority.authenticate(commitment(), receipt)
    assert verified.operator_digest == operator
    assert len(verified.approval_digest) == len(verified.nonce_hash) == 64
    assert authority.authenticate(commitment(), receipt) == verified  # R3 control owns consumption.
    assert adapter.calls.count('protect') == 1
    assert 'key' not in json.dumps(receipt)


@pytest.mark.parametrize('phase', ('enroll', 'issue'))
def test_wrong_interactive_consent_grants_nothing(case, phase):
    module, root, _, _, authority = case
    if phase == 'issue':
        enroll(authority)
    before = sorted(p.relative_to(root).as_posix() for p in root.rglob('*'))
    action = authority.enroll if phase == 'enroll' else lambda **kw: authority.issue(commitment(), **kw)
    with pytest.raises(module.OperatorError, match='operator_consent_required'):
        action(reader=lambda _: 'yes', writer=lambda _: None)
    assert sorted(p.relative_to(root).as_posix() for p in root.rglob('*')) == before


def test_enrollment_never_overwrites_existing_anchor(case):
    module, _, _, _, authority = case
    enroll(authority)
    before = (authority.anchor / 'operator.json').read_bytes()
    with pytest.raises(module.OperatorError, match='operator_already_enrolled'):
        authority.enroll(reader=lambda _: pytest.fail('Existing enrollment prompted'))
    assert (authority.anchor / 'operator.json').read_bytes() == before


def test_interrupted_anchor_write_never_becomes_enrollment_or_gets_overwritten(case, monkeypatch):
    module, _, adapter, _, authority = case
    def interrupted(path, data):
        path.write_bytes(data[:9])
        raise OSError('SYNTHETIC PRIVATE DETAIL')
    monkeypatch.setattr(adapter, 'write_new', interrupted)
    with pytest.raises(module.OperatorError) as error:
        enroll(authority)
    assert str(error.value) == 'operator_custody_invalid'
    before = (authority.anchor / 'operator.json').read_bytes()
    with pytest.raises(module.OperatorError):
        _ = authority.operator_digest
    with pytest.raises(module.OperatorError, match='operator_already_enrolled'):
        authority.enroll(reader=lambda _: pytest.fail('Partial enrollment prompted'))
    assert (authority.anchor / 'operator.json').read_bytes() == before


@pytest.mark.parametrize('field', ('restore_uuid', 'inventory', 'watermarks', 'scope', 'evidence'))
def test_any_commitment_drift_denies(case, field):
    module, _, _, _, authority = case
    enroll(authority)
    receipt = issue(authority)
    changed = commitment()
    changed[field] = 'changed'
    with pytest.raises(module.OperatorError, match='operator_approval_invalid'):
        authority.authenticate(changed, receipt)


@pytest.mark.parametrize('field', ('operator_digest', 'commitment_digest', 'nonce', 'created_at', 'expires_at', 'signature'))
def test_receipt_tamper_denies(case, field):
    module, _, _, _, authority = case
    enroll(authority)
    receipt = issue(authority)
    receipt[field] = '0' * 64
    with pytest.raises(module.OperatorError, match='operator_approval_invalid'):
        authority.authenticate(commitment(), receipt)


def test_future_expired_and_wrong_principal_or_acl_denies(case):
    module, _, adapter, clock, authority = case
    enroll(authority)
    receipt = issue(authority)
    initial = clock[0]
    for shifted in (initial - timedelta(seconds=1), initial + timedelta(minutes=6)):
        clock[0] = shifted
        with pytest.raises(module.OperatorError, match='operator_approval_invalid'):
            authority.authenticate(commitment(), receipt)
    clock[0] = initial
    adapter.principal = 'synthetic-SID-B'
    with pytest.raises(module.OperatorError):
        authority.authenticate(commitment(), receipt)
    adapter.principal = 'synthetic-SID-A'
    adapter.private = False
    with pytest.raises(module.OperatorError, match='operator_custody_invalid'):
        authority.authenticate(commitment(), receipt)


@pytest.mark.parametrize('payload', (True, [], {'nested': float('nan')}, {1: 'mixed'}, {'x': '\ud800'}))
def test_malformed_commitment_rejected_before_prompt(case, payload):
    module, _, _, _, authority = case
    enroll(authority)
    with pytest.raises(module.OperatorError, match='operator_commitment_invalid'):
        authority.issue(payload, reader=lambda _: pytest.fail('Malformed prompted'))


def test_duplicate_or_oversized_anchor_json_has_code_only_error(case):
    module, _, _, _, authority = case
    enroll(authority)
    target = authority.anchor / 'operator.json'
    for data in (b'{"schema":1,"schema":1}', b'x' * (128 * 1024 + 1)):
        target.write_bytes(data)
        with pytest.raises(module.OperatorError) as error:
            _ = authority.operator_digest
        assert str(error.value).startswith('operator_')
        assert 'schema' not in str(error.value)


def test_anchor_hardlink_is_rejected(case):
    module, _, _, _, authority = case
    enroll(authority)
    target = authority.anchor / 'operator.json'
    os.link(target, authority.anchor / 'alias')
    with pytest.raises(module.OperatorError, match='operator_custody_invalid'):
        _ = authority.operator_digest


def test_production_constructor_has_no_adapter_bypass(case):
    module, root, adapter, _, _ = case
    with pytest.raises(TypeError):
        module.OperatorAuthority(root, adapter=adapter)


def test_public_scope_paths_cannot_be_rebound(case):
    _, root, _, _, authority = case
    for name in ('root', 'anchor'):
        with pytest.raises(AttributeError):
            setattr(authority, name, root / 'restored-copy')


def test_same_user_anchor_copy_to_another_project_scope_denies(case):
    module, root, adapter, clock, authority = case
    enroll(authority)
    receipt = issue(authority)
    alternate = root.parent / 'another-project'
    (alternate / '.runtime/team-operator').mkdir(parents=True)
    shutil.copyfile(authority.anchor / 'operator.json', alternate / '.runtime/team-operator/operator.json')
    copied = module._test_authority(alternate, adapter=adapter, clock=lambda: clock[0])
    with pytest.raises(module.OperatorError, match='operator_custody_invalid'):
        copied.authenticate(commitment(), receipt)


@pytest.mark.parametrize('changed', ('commitment', 'principal', 'private_acl'))
def test_change_during_consent_does_not_mint_approval(case, changed):
    module, _, adapter, _, authority = case
    enroll(authority)
    body = commitment()
    capture = []
    valid_consent = consent(capture)
    def reader(prompt):
        answer = valid_consent(prompt)
        if changed == 'commitment':
            body['inventory']['sha256'] = 'f' * 64
        elif changed == 'principal':
            adapter.principal = 'synthetic-SID-B'
        else:
            adapter.private = False
        return answer
    with pytest.raises(module.OperatorError):
        authority.issue(body, reader=reader, writer=capture.append)


def test_replaced_project_directory_before_enrollment_denies(case):
    module, root, _, _, authority = case
    root.rename(root.with_name('old-synthetic-project'))
    root.mkdir()
    with pytest.raises(module.OperatorError, match='operator_custody_invalid'):
        enroll(authority)


def test_replaced_project_during_enrollment_consent_never_receives_writes(case):
    module, root, _, _, authority = case
    capture = []
    valid = consent(capture)
    def reader(prompt):
        answer = valid(prompt)
        root.rename(root.with_name('old-project-after-consent'))
        root.mkdir()
        return answer
    with pytest.raises(module.OperatorError, match='operator_custody_invalid'):
        authority.enroll(reader=reader, writer=capture.append)
    assert list(root.iterdir()) == []


@pytest.mark.skipif(os.name != 'nt', reason='Actual private Windows descendant only')
def test_private_scope_is_revalidated_before_releasing_pins(tmp_path, monkeypatch):
    module = implementation()
    private = tmp_path / 'revalidated-private-parent'
    module.create_private_directory(private)
    with pytest.raises(module.OperatorError, match='operator_custody_invalid'):
        with module.protected_scope(private):
            monkeypatch.setattr(module, '_private_handle', lambda *a, **kw: (False, False))


@pytest.mark.skipif(os.name != 'nt', reason='Actual private Windows descendant only')
def test_actual_windows_inherited_private_directory_and_anchor_file_pin(tmp_path):
    module = implementation()
    private = tmp_path / 'explicit-private-parent'
    module.create_private_directory(private)
    inherited = private / 'inherited-child'
    inherited.mkdir()
    with module.protected_scope(inherited):
        assert inherited.is_dir()
    root = tmp_path / 'native-pinned-project'
    root.mkdir()
    authority = module.OperatorAuthority(root)
    enroll(authority)
    target = authority.anchor / 'operator.json'
    with authority._adapter.scope(authority.anchor), authority._adapter.file(target):
        with pytest.raises(PermissionError):
            target.write_bytes(b'broken')
        with pytest.raises(PermissionError):
            target.unlink()
    assert authority.operator_digest


@pytest.mark.skipif(os.name != 'nt', reason='Actual Windows CurrentUser custody/DPAPI only')
def test_actual_windows_private_dpapi_anchor_roundtrip(tmp_path):
    module = implementation()
    root = tmp_path / 'actual-windows-synthetic-project'
    root.mkdir()
    authority = module.OperatorAuthority(root)
    operator = enroll(authority)
    receipt = issue(authority)
    verified = authority.authenticate(commitment(), receipt)
    assert verified.operator_digest == operator
    assert authority.operator_digest == operator
    target = authority.anchor / 'operator.json'
    stored = json.loads(target.read_text())
    assert stored.keys() == {'schema', 'protected'}
    assert base64.b64decode(stored['protected'], validate=True)
    with module.protected_scope(authority.anchor):
        assert target.stat().st_nlink == 1
