"""Fresh business-owner consent, using synthetic roots and explicit offline seams."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import base64
import importlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

import pytest


def implementation():
    name = 'secretary.infrastructure.restore_business_owner'
    assert importlib.util.find_spec(name), 'Fresh business-owner purpose-consent issuer is missing'
    return importlib.import_module(name)


def test_explicit_local_business_owner_issuer_exists():
    module = implementation()
    assert callable(module.BusinessOwnerAuthority) and callable(module.OwnerPolicy)


class SyntheticAdapter:
    """No production custody/DPAPI claim is made by this explicit test seam."""
    def __init__(self):
        self.principal = 'synthetic-business-SID-A'
        self.private = True

    def identity(self):
        return self.principal

    def mkdir(self, path):
        path.mkdir()

    def protect(self, value):
        return self.principal.encode() + b'\0' + value

    def unprotect(self, value):
        prefix = self.principal.encode() + b'\0'
        if not value.startswith(prefix):
            from secretary.infrastructure.restore_operator import OperatorError
            raise OperatorError('operator_custody_invalid')
        return value[len(prefix):]

    def write_new(self, path, data):
        with path.open('xb') as stream:
            stream.write(data)

    @contextmanager
    def root_scope(self, path):
        yield

    @contextmanager
    def scope(self, path):
        if not self.private:
            from secretary.infrastructure.restore_operator import OperatorError
            raise OperatorError('operator_custody_invalid')
        yield

    @contextmanager
    def file(self, path):
        with path.open('rb') as stream:
            yield stream


def policy_fields():
    return dict(protocol=1, member_id='11111111-1111-4111-8111-111111111111',
        max_user_id='123456789', vikunja_user_id='19', project_ids=('27', '28'))


@pytest.fixture
def case(tmp_path):
    module = implementation()
    root = tmp_path / 'synthetic-business-project'
    root.mkdir()
    adapter = SyntheticAdapter()
    clock = [datetime(2026, 10, 4, 12, tzinfo=timezone.utc)]
    authority = module._test_authority(root, adapter=adapter, clock=lambda: clock[0])
    policy = module.OwnerPolicy(**policy_fields())
    return module, root, adapter, clock, authority, policy


def respond(capture, statement):
    def read(_):
        digest = next(line.split(' ', 1)[1] for line in reversed(capture) if line.startswith('digest '))
        challenge = next(line.split(' ', 1)[1] for line in reversed(capture) if line.startswith('challenge '))
        return statement + ' ' + challenge + ' ' + digest
    return read


def enroll(authority, policy):
    capture = []
    result = authority.enroll(policy, reader=respond(capture, 'ENROLL BUSINESS OWNER'), writer=capture.append)
    assert len(result) == 64
    assert 'business-owner policy ' in '\n'.join(capture)
    assert policy.member_id in '\n'.join(capture)
    return result


def commitment(authority):
    return dict(protocol=1, purpose='restore_activation_consent',
        restore_id='22222222-2222-4222-8222-222222222222',
        epoch_id='33333333-3333-4333-8333-333333333333',
        preparation_id='44444444-4444-4444-8444-444444444444',
        source_snapshot_sha256='a' * 64, binding_sha256='b' * 64, scope_sha256='c' * 64,
        provider_observations_sha256='d' * 64, resource_scope_sha256='e' * 64,
        capabilities=['fresh_auth', 'fresh_work'], owner_policy_sha256=authority.owner_policy_sha256)


def issue(authority, value=None):
    value = commitment(authority) if value is None else value
    capture = []
    result = authority.issue(value, reader=respond(capture, 'CONSENT restore_activation_consent'), writer=capture.append)
    assert json.dumps(value, sort_keys=True, separators=(',', ':')) in '\n'.join(capture)
    return result


def files(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob('*') if p.is_file()}


def test_constructor_and_unenrolled_issue_create_no_files(case):
    module, root, _, _, authority, _ = case
    assert list(root.iterdir()) == []
    assert authority.anchor == root / '.runtime/team-business-owner'
    with pytest.raises(module.BusinessOwnerError, match='business_owner_not_enrolled'):
        authority.issue({}, reader=lambda _: pytest.fail('Unenrolled owner prompted'))
    assert list(root.iterdir()) == []


def test_real_enrollment_issue_and_authenticate_have_no_consumption(case):
    module, root, _, _, authority, policy = case
    digest = enroll(authority, policy)
    before = files(root)
    value = commitment(authority)
    receipt = issue(authority, value)
    result = authority.authenticate(value, receipt)
    assert type(result) is module.VerifiedBusinessOwnerConsent
    assert result.owner_digest == digest == authority.owner_digest
    assert result.owner_policy_sha256 == authority.owner_policy_sha256
    assert all(len(getattr(result, field)) == 64 for field in ('consent_digest', 'nonce_hash'))
    assert authority.authenticate(value, receipt) == result
    assert files(root) == before  # Future independent decision store owns nonce consumption.
    assert not (root / '.runtime/team-operator').exists()
    assert 'key' not in receipt and not hasattr(authority, 'activate')


@pytest.mark.parametrize('phase', ('enroll', 'issue'))
@pytest.mark.parametrize('answer', ('yes', 'ENROLL BUSINESS OWNER', 'CONSENT restore_activation_consent'))
def test_owner_statement_without_exact_fresh_challenge_is_denied(case, phase, answer):
    module, root, _, _, authority, policy = case
    if phase == 'issue':
        enroll(authority, policy)
    before = files(root)
    action = lambda: authority.enroll(policy, reader=lambda _: answer, writer=lambda _: None)
    if phase == 'issue':
        action = lambda: authority.issue(commitment(authority), reader=lambda _: answer, writer=lambda _: None)
    with pytest.raises(module.BusinessOwnerError, match='business_owner_consent_required'):
        action()
    assert files(root) == before


@pytest.mark.parametrize('field,bad', (
    ('protocol', True), ('protocol', 2), ('member_id', uuid4()), ('member_id', 'not-uuid'),
    ('max_user_id', 1), ('max_user_id', '0'), ('max_user_id', '01'), ('max_user_id', str(2**63)),
    ('vikunja_user_id', False), ('vikunja_user_id', '-1'), ('vikunja_user_id', '2\n'),
    ('project_ids', ['27']), ('project_ids', ()), ('project_ids', ('27', '27')),
    ('project_ids', ('28', '27')), ('project_ids', ('27', 28)),
))
def test_owner_policy_ids_and_schema_are_strict_before_any_write(case, field, bad):
    module, root, _, _, _, _ = case
    fields = policy_fields(); fields[field] = bad
    with pytest.raises(module.BusinessOwnerError, match='business_owner_policy_invalid'):
        module.OwnerPolicy(**fields)
    assert list(root.iterdir()) == []


@pytest.mark.parametrize('bad', (None, True, {}, policy_fields()))
def test_caller_policy_dictionary_or_boolean_is_not_enrollment(case, bad):
    module, root, _, _, authority, _ = case
    with pytest.raises(module.BusinessOwnerError, match='business_owner_policy_invalid'):
        authority.enroll(bad, reader=lambda _: pytest.fail('Invalid policy prompted'))
    assert list(root.iterdir()) == []


@pytest.mark.parametrize('field,bad', (
    ('protocol', True), ('purpose', 'source_epoch_preparation'), ('restore_id', 'not-uuid'),
    ('epoch_id', 123), ('preparation_id', None), ('source_snapshot_sha256', 'short'),
    ('binding_sha256', 'B' * 64), ('scope_sha256', False),
    ('provider_observations_sha256', {'trusted': True}), ('resource_scope_sha256', 'bad'),
    ('owner_policy_sha256', 'f' * 64), ('capabilities', []), ('capabilities', ['unknown']),
    ('capabilities', ['fresh_auth', 'fresh_auth']), ('capabilities', ['fresh_work', 'fresh_auth']),
    ('capabilities', ('fresh_auth',)),
))
def test_closed_commitment_rejects_invalid_binding_before_prompt(case, field, bad):
    module, root, _, _, authority, policy = case
    enroll(authority, policy)
    value = commitment(authority); value[field] = bad
    before = files(root)
    with pytest.raises(module.BusinessOwnerError, match='business_owner_commitment_invalid'):
        authority.issue(value, reader=lambda _: pytest.fail('Invalid commitment prompted'))
    assert files(root) == before


@pytest.mark.parametrize('mutation', ('missing', 'extra'))
def test_commitment_requires_exact_fields(case, mutation):
    module, _, _, _, authority, policy = case
    enroll(authority, policy)
    value = commitment(authority)
    if mutation == 'missing': value.pop('binding_sha256')
    else: value['caller_approved'] = True
    with pytest.raises(module.BusinessOwnerError, match='business_owner_commitment_invalid'):
        authority.issue(value, reader=lambda _: pytest.fail('Wrong fields prompted'))


@pytest.mark.parametrize('field', ('restore_id', 'epoch_id', 'preparation_id', 'source_snapshot_sha256',
    'binding_sha256', 'scope_sha256', 'provider_observations_sha256', 'resource_scope_sha256', 'capabilities'))
def test_any_valid_commitment_drift_refuses_actual_signed_receipt(case, field):
    module, _, _, _, authority, policy = case
    enroll(authority, policy)
    value = commitment(authority); receipt = issue(authority, value)
    changed = dict(value)
    changed[field] = [ 'fresh_auth' ] if field == 'capabilities' else str(uuid4()) if field.endswith('_id') else 'f' * 64
    with pytest.raises(module.BusinessOwnerError, match='business_owner_consent_invalid'):
        authority.authenticate(changed, receipt)


@pytest.mark.parametrize('field', ('owner_digest', 'owner_policy_sha256', 'scope_digest',
    'commitment_digest', 'nonce', 'signature', 'created_at', 'expires_at', 'schema'))
def test_receipt_tamper_and_boolean_times_do_not_authenticate(case, field):
    module, _, _, _, authority, policy = case
    enroll(authority, policy)
    value = commitment(authority); receipt = issue(authority, value)
    receipt[field] = True if field in ('created_at', 'expires_at', 'schema') else 'f' * 64
    with pytest.raises(module.BusinessOwnerError, match='business_owner_consent_invalid'):
        authority.authenticate(value, receipt)


@pytest.mark.parametrize('offset', (-1, 300))
def test_future_or_exact_expiry_rejects_receipt(case, offset):
    module, _, _, clock, authority, policy = case
    enroll(authority, policy)
    value = commitment(authority); receipt = issue(authority, value)
    clock[0] += timedelta(seconds=offset)
    with pytest.raises(module.BusinessOwnerError, match='business_owner_consent_invalid'):
        authority.authenticate(value, receipt)


@pytest.mark.parametrize('kind', ('missing', 'empty', 'corrupt'))
def test_existing_anchor_never_resets_or_adopts_policy(case, kind):
    module, root, _, _, authority, policy = case
    anchor = authority.anchor; anchor.parent.mkdir(); anchor.mkdir()
    if kind != 'missing': (anchor / 'owner.json').write_bytes(b'' if kind == 'empty' else b'corrupt-owner')
    before = files(root)
    with pytest.raises(module.BusinessOwnerError, match='business_owner_already_enrolled'):
        authority.enroll(policy, reader=lambda _: pytest.fail('Existing anchor prompted'))
    with pytest.raises(module.BusinessOwnerError):
        _ = authority.owner_digest
    assert files(root) == before


def test_interrupted_write_leaves_unadopted_anchor(case, monkeypatch):
    module, root, adapter, _, authority, policy = case
    def broken(path, raw):
        path.write_bytes(raw[:9]); raise OSError('synthetic-sensitive-error')
    monkeypatch.setattr(adapter, 'write_new', broken)
    with pytest.raises(module.BusinessOwnerError, match='business_owner_custody_invalid'):
        enroll(authority, policy)
    before = files(root)
    with pytest.raises(module.BusinessOwnerError, match='business_owner_already_enrolled'):
        enroll(authority, policy)
    assert files(root) == before


@pytest.mark.parametrize('change', ('sid', 'acl', 'policy', 'key', 'root'))
def test_drift_during_actual_owner_prompt_cannot_issue_consent(case, change):
    module, root, adapter, _, authority, policy = case
    enroll(authority, policy)
    value = commitment(authority); capture = []
    def reader(prompt):
        answer = respond(capture, 'CONSENT restore_activation_consent')(prompt)
        if change == 'sid': adapter.principal = 'synthetic-business-SID-B'
        elif change == 'acl': adapter.private = False
        elif change == 'root': root.rename(root.with_name('moved')); root.mkdir()
        else:
            path = authority.anchor / 'owner.json'
            outer = json.loads(path.read_bytes())
            inner = json.loads(adapter.unprotect(base64.b64decode(outer['protected'])))
            if change == 'key': inner['key'] = 'f' * 64
            else: inner['policy']['max_user_id'] = '99'
            outer['protected'] = base64.b64encode(adapter.protect(json.dumps(inner).encode())).decode()
            path.write_text(json.dumps(outer), encoding='utf8')
        return answer
    with pytest.raises(module.BusinessOwnerError):
        authority.issue(value, reader=reader, writer=capture.append)


def test_policy_key_rotation_invalidates_prior_signed_receipt(case):
    module, _, adapter, _, authority, policy = case
    enroll(authority, policy)
    value = commitment(authority); receipt = issue(authority, value)
    path = authority.anchor / 'owner.json'; outer = json.loads(path.read_bytes())
    inner = json.loads(adapter.unprotect(base64.b64decode(outer['protected'])))
    inner['key'] = 'f' * 64
    outer['protected'] = base64.b64encode(adapter.protect(json.dumps(inner).encode())).decode()
    path.write_text(json.dumps(outer), encoding='utf8')
    with pytest.raises(module.BusinessOwnerError):
        authority.authenticate(value, receipt)


def test_hardlinked_owner_file_is_refused_without_mutation(case):
    module, root, _, _, authority, policy = case
    enroll(authority, policy)
    os.link(authority.anchor / 'owner.json', root / 'foreign-owner-alias')
    before = files(root)
    with pytest.raises(module.BusinessOwnerError, match='business_owner_custody_invalid'):
        _ = authority.owner_digest
    assert files(root) == before


def test_same_account_anchor_copy_to_new_scope_never_authenticates(case):
    module, root, adapter, clock, authority, policy = case
    enroll(authority, policy)
    value = commitment(authority); receipt = issue(authority, value)
    other = root.parent / 'other-synthetic-project'; other.mkdir()
    shutil.copytree(authority.anchor.parent, other / '.runtime')
    copied = module._test_authority(other, adapter=adapter, clock=lambda: clock[0])
    with pytest.raises(module.BusinessOwnerError, match='business_owner_custody_invalid'):
        copied.authenticate(value, receipt)


def test_production_constructor_exposes_no_adapter_clock_or_anchor_override(case):
    module, root, _, _, _, _ = case
    assert list(inspect.signature(module.BusinessOwnerAuthority).parameters) == ['project_root']
    with pytest.raises(TypeError): module.BusinessOwnerAuthority(root, adapter=SyntheticAdapter())


def test_forged_verified_dataclass_is_not_an_authentication_receipt(case):
    module, _, _, _, authority, policy = case
    enroll(authority, policy)
    fake = module.VerifiedBusinessOwnerConsent(authority.owner_digest, authority.owner_policy_sha256,
        'a' * 64, 'b' * 64, 2**62)
    with pytest.raises(module.BusinessOwnerError, match='business_owner_consent_invalid'):
        authority.authenticate(commitment(authority), fake)


def test_enrollment_root_replacement_during_consent_never_writes_new_root(case):
    module, root, _, _, authority, policy = case
    capture = []
    def reader(prompt):
        answer = respond(capture, 'ENROLL BUSINESS OWNER')(prompt)
        root.rename(root.with_name('original-synthetic-root')); root.mkdir()
        return answer
    with pytest.raises(module.BusinessOwnerError, match='business_owner_custody_invalid'):
        authority.enroll(policy, reader=reader, writer=capture.append)
    assert list(root.iterdir()) == []


@pytest.mark.parametrize('change', ('duplicate', 'extra', 'policy_hash', 'scope', 'enrollment'))
def test_stored_policy_exact_schema_and_scope_cannot_be_rebound(case, change):
    module, _, adapter, _, authority, policy = case
    enroll(authority, policy)
    path = authority.anchor / 'owner.json'; outer = json.loads(path.read_bytes())
    inner = json.loads(adapter.unprotect(base64.b64decode(outer['protected'])))
    if change == 'duplicate': raw = '{"schema":1,' + json.dumps(inner)[1:]
    else:
        if change == 'extra': inner['caller_owner'] = True
        elif change == 'policy_hash': inner['policy_sha256'] = 'f' * 64
        elif change == 'scope': inner['scope_digest'] = 'f' * 64
        else: inner['enrollment_id'] = 'not-a-uuid'
        raw = json.dumps(inner)
    outer['protected'] = base64.b64encode(adapter.protect(raw.encode())).decode()
    path.write_text(json.dumps(outer), encoding='utf8')
    with pytest.raises(module.BusinessOwnerError, match='business_owner_custody_invalid'):
        _ = authority.owner_digest


@pytest.mark.parametrize('code', (None, {}, 'sensitive owner initData=secret'))
def test_error_codes_never_echo_unknown_values(case, code):
    module, _, _, _, _, _ = case
    error = module.BusinessOwnerError(code)
    assert str(error) == error.code == 'business_owner_custody_invalid'


@pytest.mark.skipif(os.name != 'nt', reason='Actual CurrentUser DPAPI and Windows custody only')
def test_actual_windows_private_owner_roundtrip_and_file_write_denial(tmp_path):
    module = implementation()
    from secretary.infrastructure.restore_operator import _WindowsAdapter
    root = tmp_path / 'actual-private-owner-project'; root.mkdir()
    authority = module.BusinessOwnerAuthority(root); policy = module.OwnerPolicy(**policy_fields())
    enroll(authority, policy)
    value = commitment(authority); receipt = issue(authority, value)
    assert authority.authenticate(value, receipt).owner_digest == authority.owner_digest
    path = authority.anchor / 'owner.json'
    with _WindowsAdapter().file(path):
        with pytest.raises(OSError): path.write_bytes(b'replacement-owner')
        with pytest.raises(OSError): path.rename(path.with_name('moved-owner.json'))
    assert authority.authenticate(value, receipt).owner_policy_sha256 == authority.owner_policy_sha256


@pytest.mark.skipif(os.name != 'nt', reason='Actual Windows scope exception mapping')
def test_actual_windows_wrong_enrollment_statement_retains_domain_error(tmp_path):
    module = implementation()
    root = tmp_path / 'actual-owner-wrong-consent'; root.mkdir()
    authority = module.BusinessOwnerAuthority(root)
    with pytest.raises(module.BusinessOwnerError, match='business_owner_consent_required'):
        authority.enroll(module.OwnerPolicy(**policy_fields()), reader=lambda _: 'yes', writer=lambda _: None)
    assert list(root.iterdir()) == []


@pytest.mark.skipif(os.name != 'nt', reason='Actual private no-delete Windows directory leases')
def test_actual_windows_enrollment_prompt_pins_project_against_rename(tmp_path):
    module = implementation()
    root = tmp_path / 'actual-owner-pinned-consent'; root.mkdir()
    authority = module.BusinessOwnerAuthority(root); capture = []
    def reader(prompt):
        with pytest.raises(OSError): root.rename(root.with_name('foreign-owner-project'))
        return respond(capture, 'ENROLL BUSINESS OWNER')(prompt)
    authority.enroll(module.OwnerPolicy(**policy_fields()), reader=reader, writer=capture.append)
    assert authority.owner_digest


@pytest.mark.skipif(os.name != 'nt', reason='Actual Windows junction reparse points')
def test_owner_anchor_junction_never_adopts_another_private_policy(tmp_path):
    module = implementation()
    import ctypes
    from ctypes import wintypes as w
    from secretary.infrastructure.restore_operator import _win32_path
    root = tmp_path / 'owner-junction-root'; root.mkdir()
    authority = module.BusinessOwnerAuthority(root)
    (root / '.runtime').mkdir()
    # A symbolic directory is a reparse point too; no OS policy changes needed.
    target = tmp_path / 'foreign-owner-root'; target.mkdir()
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateSymbolicLinkW.argtypes = [w.LPCWSTR, w.LPCWSTR, w.DWORD]
    kernel.CreateSymbolicLinkW.restype = w.BOOLEAN
    created = kernel.CreateSymbolicLinkW(_win32_path(authority.anchor), _win32_path(target), 3)
    if not created: pytest.skip('Synthetic directory symlink unavailable on this Windows account')
    with pytest.raises(module.BusinessOwnerError): _ = authority.owner_digest
    with pytest.raises(module.BusinessOwnerError): enroll(authority, module.OwnerPolicy(**policy_fields()))
    assert list(target.iterdir()) == []
