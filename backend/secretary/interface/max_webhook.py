"""Normalize trusted MAX wire events; this module performs no I/O or authorization."""
from __future__ import annotations

import hashlib
import json
import re
from uuid import NAMESPACE_URL, uuid5

from secretary.domain.bot import BotError, BotEvent


def _wire_id(value, *, signed=False):
    if type(value) is not int or value == 0 or not -(2**63) <= value <= 2**63-1 or (not signed and value < 0):
        raise BotError('bot_wire_identity_invalid')
    return str(value)


def _text(value, maximum, *, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, str) or len(value) > maximum or required and not value or '\x00' in value:
        raise BotError('bot_wire_payload_invalid')
    value.encode('utf-8')
    return value


def _user(value):
    if not isinstance(value, dict) or value.get('is_bot') is not False:
        raise BotError('bot_wire_identity_invalid')
    return _wire_id(value.get('user_id'))


def _chat(message):
    recipient = message.get('recipient') if isinstance(message, dict) else None
    if not isinstance(recipient, dict):
        raise BotError('bot_wire_payload_invalid')
    return _wire_id(recipient.get('chat_id'), signed=True)


def normalize_max_event(value, *, bot_id: str) -> BotEvent:
    """Stable provider IDs identify accepted events. Hashes identify quarantine only.

    No sender profile names or supplied actor/scope fields cross this boundary.
    AuthRepository and BotRepository bind access while committing the inbox.
    """
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        encoded = b'invalid-normalized-event'
    diagnostic = hashlib.sha256(encoded).hexdigest()
    timestamp = value.get('timestamp') if isinstance(value, dict) else None
    if type(timestamp) is not int or not 0 <= timestamp <= 2**63-1:
        timestamp = 0
        invalid_timestamp = True
    else:
        invalid_timestamp = False
    kind = value.get('update_type') if isinstance(value, dict) else None
    kind = kind if isinstance(kind, str) and re.fullmatch(r'[a-z_]{1,64}', kind) else 'invalid_update'
    try:
        if invalid_timestamp or not isinstance(value, dict):
            raise BotError('bot_wire_payload_invalid')
        fields = {}
        if kind == 'message_created':
            message = value.get('message')
            if not isinstance(message, dict) or not isinstance(message.get('body'), dict):
                raise BotError('bot_wire_payload_invalid')
            fields['user_id'] = _user(message.get('sender'))
            fields['chat_id'] = _chat(message)
            body = message['body']
            fields['message_id'] = _text(body.get('mid'), 1024, required=True)
            fields['text'] = _text(body.get('text'), 4000)
            attachments = body.get('attachments') or []
            if not isinstance(attachments, list) or len(attachments) > 10:
                raise BotError('bot_wire_payload_invalid')
            media = []
            for attachment in attachments:
                if not isinstance(attachment, dict):
                    raise BotError('bot_wire_payload_invalid')
                if attachment.get('type') not in {'audio', 'file'}:
                    continue
                payload = attachment.get('payload')
                if not isinstance(payload, dict):
                    raise BotError('bot_wire_payload_invalid')
                url = _text(payload.get('url'), 8192, required=True)
                item = {'type': attachment['type'], 'url': url}
                for key in ('token', 'file_id'):
                    if payload.get(key) is not None:
                        item[key] = _text(payload[key], 4096)
                media.append(item)
            fields['media'] = tuple(media)
            key = json.dumps([bot_id, fields['chat_id'], fields['message_id'], kind], separators=(',', ':'))
        elif kind == 'message_callback':
            callback = value.get('callback')
            if not isinstance(callback, dict):
                raise BotError('bot_wire_payload_invalid')
            fields['user_id'] = _user(callback.get('user'))
            fields['callback_id'] = _text(callback.get('callback_id'), 1024, required=True)
            fields['callback_payload'] = _text(callback.get('payload'), 1024, required=True)
            if value.get('message') is not None:
                fields['chat_id'] = _chat(value['message'])
            key = json.dumps([bot_id, fields['callback_id'], kind], separators=(',', ':'))
        elif kind == 'bot_started':
            fields['user_id'] = _user(value.get('user'))
            fields['chat_id'] = _wire_id(value.get('chat_id'), signed=True)
            fields['invitation_value'] = _text(value.get('payload'), 256, required=True)
            # One invitation claim per candidate. Timestamp is not a MAX event ID.
            invite_hash = hashlib.sha256(fields['invitation_value'].encode('utf-8')).hexdigest()
            key = json.dumps([bot_id, fields['user_id'], kind, invite_hash], separators=(',', ':'))
        else:
            raise BotError('bot_wire_update_unsupported')
        dedup = 'max:' + hashlib.sha256(key.encode('utf-8')).hexdigest()
        return BotEvent(event_id=str(uuid5(NAMESPACE_URL, dedup)), dedup_key=dedup,
            bot_id=bot_id, kind=kind, timestamp_ms=timestamp, **fields)
    except (BotError, UnicodeError, ValueError, TypeError):
        dedup = 'quarantine:' + diagnostic
        return BotEvent(event_id=str(uuid5(NAMESPACE_URL, bot_id + ':' + dedup)), dedup_key=dedup,
            bot_id=bot_id, kind=kind, timestamp_ms=timestamp, quarantine_reason='bot_wire_payload_invalid')
