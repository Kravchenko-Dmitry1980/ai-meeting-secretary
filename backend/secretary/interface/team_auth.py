"""MAX inner window.WebApp.initData verification from pinned official contract.

This function authenticates a signed identity, not membership, replay consumption
or a session. Those checks are atomic repository operations performed afterward.
Only the inner query is accepted: callers must not guess another decoding layer.
No HTTP, local environment, settings, capture or provider access is used here.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
from urllib.parse import unquote_to_bytes

from secretary.domain.team import content_hash
from secretary.domain.team_auth import AuthError, MaxIdentity


MAX_INIT_DATA_BYTES = 16 * 1024
MAX_AGE_SECONDS = 300
MAX_FUTURE_SKEW_SECONDS = 30


def _decode(value):
    if re.search(r'%(?![0-9a-fA-F]{2})', value):
        raise AuthError('max_init_data_invalid')
    try:
        # unquote_to_bytes follows decodeURIComponent's percent decoding while
        # keeping literal '+' intact. JSON spelling is never reconstructed.
        decoded = unquote_to_bytes(value).decode('utf-8', errors='strict')
    except (UnicodeError, ValueError):
        raise AuthError('max_init_data_invalid') from None
    if any(character in decoded for character in ('\r', '\n', '\x00')):
        raise AuthError('max_init_data_invalid')
    return decoded


def _fields(raw):
    if not isinstance(raw, str) or not raw:
        raise AuthError('max_init_data_invalid')
    try:
        size = len(raw.encode('utf-8'))
    except UnicodeError:
        raise AuthError('max_init_data_invalid') from None
    if size > MAX_INIT_DATA_BYTES:
        raise AuthError('max_init_data_too_large')
    fields = {}
    for pair in raw.split('&'):
        raw_key, separator, raw_value = pair.partition('=')
        if not separator or not raw_key:
            raise AuthError('max_init_data_invalid')
        key, value = _decode(raw_key), _decode(raw_value)
        if not key or '=' in key or key in fields:
            raise AuthError('max_init_data_invalid')
        fields[key] = value
    if not {'hash', 'auth_date', 'user'} <= fields.keys():
        raise AuthError('max_init_data_invalid')
    return fields


def _unique_json(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate_json_key')
        value[key] = item
    return value


def _user_id(raw):
    try:
        user = json.loads(raw, object_pairs_hook=_unique_json,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError('nonfinite_json')))
        if not isinstance(user, dict) or type(user.get('id')) is not int or not 1 <= user['id'] <= 2 ** 63 - 1:
            raise ValueError
        return str(user['id'])
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise AuthError('max_identity_invalid') from None


def validate_init_data(raw: str, *, bot_token: str, bot_id: str, now: datetime) -> MaxIdentity:
    try:
        if (not isinstance(bot_token, str) or not bot_token
                or not isinstance(bot_id, str) or not bot_id.strip() or len(bot_id) > 128
                or any(value in bot_id for value in ('\r', '\n', '\x00'))
                or not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None):
            raise ValueError
        token_bytes = bot_token.encode('utf-8')
        bot_id.encode('utf-8')
        now_utc = now.astimezone(timezone.utc)
    except (ValueError, TypeError, UnicodeError, OverflowError):
        raise AuthError('max_auth_configuration_invalid') from None
    fields = _fields(raw)
    supplied_hex = fields.pop('hash')
    if not re.fullmatch(r'[0-9a-fA-F]{64}', supplied_hex):
        raise AuthError('max_signature_invalid')
    # JavaScript lexicographic ordering is UTF-16 code-unit order. UTF-16BE
    # byte ordering keeps compatibility for unknown Unicode signed field keys.
    data = '\n'.join(key + '=' + fields[key] for key in sorted(fields, key=lambda key: key.encode('utf-16-be')))
    secret = hmac.new(b'WebAppData', token_bytes, hashlib.sha256).digest()
    expected_signature = hmac.new(secret, data.encode('utf-8'), hashlib.sha256).digest()
    supplied_signature = bytes.fromhex(supplied_hex)
    if not hmac.compare_digest(expected_signature, supplied_signature):
        raise AuthError('max_signature_invalid')
    user_id = _user_id(fields['user'])
    raw_date = fields['auth_date']
    if not re.fullmatch(r'(?:0|[1-9][0-9]{0,18})', raw_date) or int(raw_date) > 2 ** 63 - 1:
        raise AuthError('max_auth_date_invalid')
    auth_date = int(raw_date)
    delta = now_utc - datetime(1970, 1, 1, tzinfo=timezone.utc)
    now_microseconds = (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
    age_microseconds = now_microseconds - auth_date * 1_000_000
    if age_microseconds > MAX_AGE_SECONDS * 1_000_000:
        raise AuthError('max_init_data_expired')
    if age_microseconds < -MAX_FUTURE_SKEW_SECONDS * 1_000_000:
        raise AuthError('max_init_data_future')
    signature_hex = supplied_signature.hex()
    replay_key = content_hash([bot_id, user_id, auth_date, signature_hex])
    return MaxIdentity(user_id, auth_date, signature_hex, replay_key)
