"""Real MAX validator, using pinned synthetic vectors and adversarial inputs."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from importlib import import_module
import hashlib
import hmac
import json
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import pytest

from secretary.domain.team import TeamMember, content_hash


ROOT = Path(__file__).resolve().parents[1]
BOT_ID = 'synthetic-bot'
TOKEN = 'SYNTHETIC_MAX_TOKEN_NOT_A_CREDENTIAL'
STAMP = 1791045908
NOW = datetime.fromtimestamp(STAMP, timezone.utc)


def domain():
    return import_module('secretary.domain.team_auth')


def auth():
    return import_module('secretary.interface.team_auth')


@pytest.fixture
def vector():
    return json.loads((ROOT / 'docs/contracts/max-contract.json').read_text(encoding='utf-8'))['fixtures']['init_data_vector']


def signed(*, user=None, user_text=None, auth_date=str(STAMP), extra=None):
    user = {'id': 9007199254740993, 'first_name': 'A+B %2F'} if user is None else user
    fields = {'auth_date': auth_date,
        'user': user_text if user_text is not None else json.dumps(user, ensure_ascii=False, separators=(',', ':')),
        'query_id': 'synthetic'}
    fields.update(extra or {})
    data = '\n'.join(key + '=' + fields[key] for key in sorted(fields, key=lambda key: key.encode('utf-16-be')))
    secret = hmac.new(b'WebAppData', TOKEN.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, data.encode(), hashlib.sha256).hexdigest()
    return '&'.join(quote(key, safe='') + '=' + quote(value, safe='') for key, value in fields.items()) + '&hash=' + signature


def validate(raw, *, now=NOW, token=TOKEN, bot_id=BOT_ID):
    return auth().validate_init_data(raw, bot_token=token, bot_id=bot_id, now=now)


def assert_error(raw, code, **kwargs):
    with pytest.raises(domain().AuthError) as caught:
        validate(raw, **kwargs)
    assert caught.value.code == code and str(caught.value) == code
    assert 'PRIVATE' not in str(caught.value) and TOKEN not in str(caught.value)


def test_real_validator_matches_pinned_golden_vector(vector):
    identity = validate(vector['init_data'])
    assert identity.user_id == '9007199254740993' and identity.auth_date == STAMP
    assert identity.signature_hex == vector['signature_hex']
    assert identity.replay_key == content_hash([BOT_ID, identity.user_id, STAMP, vector['signature_hex']])
    with pytest.raises(FrozenInstanceError): identity.user_id = '1'


@pytest.mark.parametrize('mutation', ['reverse', 'lowercase_percent', 'encoded_key', 'uppercase_hash', 'literal_plus'])
def test_equivalent_encodings_keep_verified_replay_identity(vector, mutation):
    raw = vector['init_data']
    if mutation == 'reverse': raw = '&'.join(reversed(raw.split('&')))
    if mutation == 'lowercase_percent': raw = raw.replace('%7B', '%7b')
    if mutation == 'encoded_key': raw = raw.replace('user=', 'u%73er=')
    if mutation == 'uppercase_hash': raw = raw.replace(vector['signature_hex'], vector['signature_hex'].upper())
    if mutation == 'literal_plus': raw = raw.replace('%2B', '+')
    assert validate(raw) == validate(vector['init_data'])


def test_signed_unknown_fields_are_included_without_json_reserialization():
    raw = signed(extra={'extra_data': 'A+B %2F = &', '\U0001f600': 'astral', '\ue000': 'bmp'})
    identity = validate(raw)
    assert identity.user_id == '9007199254740993'
    assert_error(raw.replace('extra_data=', 'other_data='), 'max_signature_invalid')


def test_replay_identity_is_bound_to_bot_and_auth_date():
    raw = signed()
    first = validate(raw)
    assert validate(raw, bot_id='other-bot').replay_key != first.replay_key
    assert validate(signed(auth_date=str(STAMP - 1))).replay_key != first.replay_key


@pytest.mark.parametrize('suffix', ['&hash=00', '&user=x', '&auth_date=1', '&u%73er=x', '&h%61sh=00', '&%61uth_date=1'])
def test_duplicate_decoded_keys_are_refused(vector, suffix):
    assert_error(vector['init_data'] + suffix, 'max_init_data_invalid')


@pytest.mark.parametrize('part', ['bad=%', 'bad=%0', 'bad=%GG', 'bad=%C0%AF', 'bad=%FF', '%GG=x', '%FF=x'])
def test_malformed_percent_or_utf8_is_refused_even_in_unknown_fields(vector, part):
    assert_error(vector['init_data'] + '&' + part, 'max_init_data_invalid')


@pytest.mark.parametrize('extra', [
    {'bad': 'PRIVATE\nuser={"id":1}'}, {'bad': 'PRIVATE\rvalue'}, {'bad': 'PRIVATE\x00'},
    {'bad\nkey': 'value'}, {'bad=key': 'value'},
])
def test_correctly_signed_canonical_ambiguities_are_refused(extra):
    assert_error(signed(extra=extra), 'max_init_data_invalid')


@pytest.mark.parametrize('user_id', [True, False, 0, -1, 2 ** 63, 1.0, 1.5, '1', None])
def test_verified_user_id_requires_positive_signed_int64_actual_integer(user_id):
    assert_error(signed(user={'id': user_id}), 'max_identity_invalid')


@pytest.mark.parametrize('user_text', [
    '{"id":1,"id":2}', '{"id":1,"profile":{"x":1,"x":2}}',
    '{"user_id":1}', '[]', 'null', '{"id":NaN}', '{"id":Infinity}',
])
def test_user_json_requires_unambiguous_object_id(user_text):
    assert_error(signed(user_text=user_text), 'max_identity_invalid')


@pytest.mark.parametrize('user_id', [1, 9007199254740993, 2 ** 63 - 1])
def test_large_valid_user_ids_stay_exact_decimal_strings(user_id):
    assert validate(signed(user={'id': user_id})).user_id == str(user_id)


@pytest.mark.parametrize('date', ['true', '1.0', '-1', '+1791045908', ' 1791045908', '1791045908 ', '01791045908', str(2 ** 63)])
def test_auth_date_requires_canonical_integer_unix_seconds(date):
    assert_error(signed(auth_date=date), 'max_auth_date_invalid')


@pytest.mark.parametrize('offset,accepted', [(-300, True), (-301, False), (30, True), (31, False)])
def test_freshness_and_future_skew_edges_are_exact(offset, accepted):
    raw = signed(auth_date=str(STAMP + offset))
    if accepted: assert validate(raw).auth_date == STAMP + offset
    else: assert_error(raw, 'max_init_data_expired' if offset < 0 else 'max_init_data_future')


def test_fractional_now_does_not_extend_expiry_by_rounding():
    assert_error(signed(auth_date=str(STAMP - 300)), 'max_init_data_expired', now=NOW + timedelta(microseconds=1))


@pytest.mark.parametrize('raw', ['', '&', 'x', '=x', 'x=y&', 'x=y&&hash=z', '\ud800', 123, None])
def test_raw_shape_type_and_utf8_fail_safely(raw):
    assert_error(raw, 'max_init_data_invalid')


def test_raw_byte_bound_is_utf8_based():
    assert_error('x=' + 'я' * (8 * 1024), 'max_init_data_too_large')


@pytest.mark.parametrize('missing', ['hash', 'user', 'auth_date'])
def test_missing_required_authentication_field_is_refused(vector, missing):
    raw = '&'.join(pair for pair in vector['init_data'].split('&') if not pair.startswith(missing + '='))
    assert_error(raw, 'max_init_data_invalid')


@pytest.mark.parametrize('signature', ['0' * 63, 'g' * 64, '0' * 65, '00 ' * 32])
def test_signature_format_is_exact_64_hex(vector, signature):
    raw = vector['init_data'].replace(vector['signature_hex'], quote(signature, safe=''))
    assert_error(raw, 'max_signature_invalid')


def test_signature_is_checked_before_exposing_invalid_user_schema(vector):
    raw = vector['init_data'].replace('%3A9007199254740993', '%3A42')
    assert_error(raw, 'max_signature_invalid')
    assert_error(vector['init_data'], 'max_signature_invalid', token='SYNTHETIC_OTHER_TOKEN')


@pytest.mark.parametrize('kwargs', [{'now': datetime(2026, 10, 3)}, {'token': ''}, {'bot_id': ''}, {'bot_id': 'bad\nID'}])
def test_invalid_server_validator_configuration_is_sanitized(vector, kwargs):
    assert_error(vector['init_data'], 'max_auth_configuration_invalid', **kwargs)


def member():
    return TeamMember(id=str(uuid4()), display_name='Synthetic member', max_user_id='1',
                      vikunja_user_id='2', project_ids=('7',))


def test_frozen_grants_are_strict_and_do_not_repr_secrets():
    m = member()
    grant = domain().SessionGrant(token='PRIVATE_SESSION', csrf='PRIVATE_CSRF', expires_at=NOW, member=m)
    invitation = domain().InvitationGrant(id=str(uuid4()), value='PRIVATE_INVITATION', expires_at=NOW)
    desktop = domain().DesktopCode(value='PRIVATE_DESKTOP', expires_at=NOW)
    view = domain().InvitationView(id=invitation.id, project_ids=('7',), candidate_user_id='9007199254740993',
        expires_at=NOW, confirmed_member_id=m.id, revoked=False)
    for value in (grant, invitation, desktop, view):
        assert 'PRIVATE' not in repr(value)
        with pytest.raises(FrozenInstanceError): value.expires_at = NOW + timedelta(seconds=1)
    assert grant.member == m and view.project_ids == ('7',)


@pytest.mark.parametrize('kind,fields', [
    ('MaxIdentity', {'user_id': 1, 'auth_date': STAMP, 'signature_hex': 'a' * 64, 'replay_key': 'b' * 64}),
    ('MaxIdentity', {'user_id': '01', 'auth_date': STAMP, 'signature_hex': 'a' * 64, 'replay_key': 'b' * 64}),
    ('MaxIdentity', {'user_id': '1', 'auth_date': True, 'signature_hex': 'a' * 64, 'replay_key': 'b' * 64}),
    ('MaxIdentity', {'user_id': '1', 'auth_date': STAMP, 'signature_hex': 'bad', 'replay_key': 'b' * 64}),
    ('MaxIdentity', {'user_id': '1', 'auth_date': STAMP, 'signature_hex': 'a' * 64, 'replay_key': 'bad'}),
    ('InvitationGrant', {'id': '1', 'value': 'synthetic', 'expires_at': NOW}),
    ('DesktopCode', {'value': '', 'expires_at': NOW}),
    ('DesktopCode', {'value': 'synthetic', 'expires_at': NOW.replace(tzinfo=None)}),
    ('InvitationView', {'id': str(uuid4()), 'project_ids': ['7'], 'candidate_user_id': None, 'expires_at': NOW, 'confirmed_member_id': None, 'revoked': False}),
    ('InvitationView', {'id': str(uuid4()), 'project_ids': ('7', '7'), 'candidate_user_id': None, 'expires_at': NOW, 'confirmed_member_id': None, 'revoked': False}),
    ('InvitationView', {'id': str(uuid4()), 'project_ids': ('7',), 'candidate_user_id': 1, 'expires_at': NOW, 'confirmed_member_id': None, 'revoked': False}),
    ('InvitationView', {'id': str(uuid4()), 'project_ids': ('7',), 'candidate_user_id': None, 'expires_at': NOW, 'confirmed_member_id': None, 'revoked': 0}),
])
def test_auth_dataclasses_refuse_type_coercion_and_invalid_fields(kind, fields):
    with pytest.raises(domain().AuthError) as caught:
        getattr(domain(), kind)(**fields)
    assert caught.value.code == 'auth_dto_invalid'


def test_valid_raw_at_exact_16kib_byte_boundary_is_accepted():
    overhead = len(signed(extra={'extra': ''}).encode('utf-8'))
    raw = signed(extra={'extra': 'x' * (16 * 1024 - overhead)})
    assert len(raw.encode('utf-8')) == 16 * 1024
    assert validate(raw).user_id == '9007199254740993'
    assert_error(signed(extra={'extra': 'x' * (16 * 1024 - overhead + 1)}), 'max_init_data_too_large')


def test_json_spelling_is_signed_without_reserialization_or_extra_decoding():
    raw = signed(user_text='{"id":1, "first_name":"A+B %2F", "note":"line\\ntext"}')
    assert validate(raw).user_id == '1'
    # A semantic-equivalent JSON spacing change still changes the MAC input.
    assert_error(raw.replace('%2C%20', '%2C'), 'max_signature_invalid')
    # Literal '+' is not an encoded space, despite standard form parsers.
    assert_error(raw.replace('%20', '+'), 'max_signature_invalid')


def test_max_identity_repr_never_exposes_authentication_material(vector):
    identity = validate(vector['init_data'])
    assert identity.signature_hex not in repr(identity)
    assert identity.replay_key not in repr(identity)


@pytest.mark.parametrize('value', ['PRIVATE\nsecret', 'PRIVATE\rsecret', 'PRIVATE\x00secret'])
def test_opaque_grants_reject_header_control_characters(value):
    with pytest.raises(domain().AuthError) as caught:
        domain().SessionGrant(token=value, csrf='synthetic', expires_at=NOW, member=member())
    assert caught.value.code == 'auth_dto_invalid' and 'PRIVATE' not in str(caught.value)
