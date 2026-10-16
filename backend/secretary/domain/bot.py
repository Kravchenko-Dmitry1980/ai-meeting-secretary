"""MAX bot contracts. Credentials and transient login codes are never replies in storage."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import re
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from secretary.domain.team import CommandReceipt, TaskCommand


class BotError(ValueError):
    def __init__(self, code: str):
        self.code = code if isinstance(code, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,100}', code) else 'bot_contract_invalid'
        super().__init__(self.code)


def decimal(value, *, signed=False):
    pattern = r'-?[1-9][0-9]{0,18}' if signed else r'[1-9][0-9]{0,18}'
    if not isinstance(value, str) or not re.fullmatch(pattern, value) or not -(2**63) <= int(value) <= 2**63-1:
        raise BotError('bot_identity_invalid')


def uuid_value(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise BotError('bot_contract_invalid') from None


def text_value(value, maximum, *, empty=False):
    if not isinstance(value, str) or (not empty and not value) or len(value) > maximum or '\x00' in value:
        raise BotError('bot_contract_invalid')
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise BotError('bot_contract_invalid') from None


@dataclass(frozen=True)
class BotButton:
    text: str
    kind: Literal['callback', 'link', 'open_app'] = 'callback'
    payload: str | None = field(default=None, repr=False)
    url: str | None = None
    web_app: str | None = None
    contact_id: str | None = None

    def __post_init__(self):
        text_value(self.text, 128)
        if self.kind == 'callback':
            text_value(self.payload, 1024)
            if any(x is not None for x in (self.url, self.web_app, self.contact_id)):
                raise BotError('bot_button_invalid')
        elif self.kind == 'link':
            text_value(self.url, 2048)
            parsed = urlsplit(self.url)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or any(ord(c) <= 32 for c in self.url):
                raise BotError('bot_button_invalid')
            if any(x is not None for x in (self.payload, self.web_app, self.contact_id)):
                raise BotError('bot_button_invalid')
        elif self.kind == 'open_app':
            text_value(self.web_app, 128)
            if self.url is not None or (self.payload is not None and not re.fullmatch(r'[A-Za-z0-9_-]{0,512}', self.payload)):
                raise BotError('bot_button_invalid')
            if self.contact_id is not None:
                decimal(self.contact_id)
        else:
            raise BotError('bot_button_invalid')


@dataclass(frozen=True)
class BotSend:
    operation_id: str
    user_id: str
    text: str = field(repr=False)
    buttons: tuple[tuple[BotButton, ...], ...] = ()
    sensitive: bool = False

    def __post_init__(self):
        uuid_value(self.operation_id)
        decimal(self.user_id)
        text_value(self.text, 4000)
        if type(self.sensitive) is not bool or not isinstance(self.buttons, tuple) or len(self.buttons) > 30:
            raise BotError('bot_contract_invalid')
        for row in self.buttons:
            if not isinstance(row, tuple) or not 1 <= len(row) <= 7 or not all(isinstance(b, BotButton) for b in row):
                raise BotError('bot_button_invalid')


@dataclass(frozen=True)
class SendReceipt:
    """API acceptance evidence; accepted_at is MAX UTC time, never local send time."""
    operation_id: str
    state: Literal['sent', 'retryable', 'uncertain', 'rejected']
    message_id: str | None = None
    error_code: str | None = None
    retry_after: float | None = None
    accepted_at: str | None = None

    def __post_init__(self):
        uuid_value(self.operation_id)
        if self.state not in {'sent', 'retryable', 'uncertain', 'rejected'}:
            raise BotError('bot_contract_invalid')
        if self.message_id is not None:
            text_value(self.message_id, 1024)
        if self.state == 'sent' and not self.message_id:
            raise BotError('bot_receipt_invalid')
        if self.error_code is not None:
            text_value(self.error_code, 128)
        if self.retry_after is not None and (isinstance(self.retry_after, bool) or not isinstance(self.retry_after, (int, float)) or not 0 <= self.retry_after <= 3600):
            raise BotError('bot_receipt_invalid')
        if self.accepted_at is not None:
            if (self.state != 'sent' or not isinstance(self.accepted_at, str)
                    or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|\+00:00)', self.accepted_at)):
                raise BotError('bot_receipt_invalid')
            try:
                datetime.fromisoformat(self.accepted_at.replace('Z', '+00:00'))
            except ValueError:
                raise BotError('bot_receipt_invalid') from None


@dataclass(frozen=True)
class MaxSubscription:
    url: str
    update_types: tuple[str, ...]
    secret: str = field(repr=False)

    def __post_init__(self):
        parsed = urlsplit(self.url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.port not in (None, 443) or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path != '/hooks/max':
            raise BotError('bot_subscription_invalid')
        if (not isinstance(self.update_types, tuple) or len(set(self.update_types)) != len(self.update_types)
                or not self.update_types or not set(self.update_types) <= {'message_created', 'message_callback', 'bot_started'}):
            raise BotError('bot_subscription_invalid')
        text_value(self.secret, 256)
        if not re.fullmatch(r'[A-Za-z0-9_-]{5,256}', self.secret):
            raise BotError('bot_subscription_invalid')


@dataclass(frozen=True)
class SubscriptionReceipt:
    state: Literal['verified', 'unchanged', 'uncertain', 'rejected']
    verified_at: datetime | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class BotEvent:
    """Normalized event. invitation_value exists only between trusted parsing and intake."""
    event_id: str
    dedup_key: str
    bot_id: str
    kind: str
    timestamp_ms: int
    user_id: str | None = None
    chat_id: str | None = None
    actor_id: str | None = None
    actor_revision: int | None = None
    project_ids: tuple[str, ...] = ()
    message_id: str | None = None
    callback_id: str | None = None
    callback_payload: str | None = field(default=None, repr=False)
    text: str | None = field(default=None, repr=False)
    invitation_id: str | None = None
    invitation_value: str | None = field(default=None, repr=False)
    media: tuple[dict, ...] = field(default=(), repr=False)
    quarantine_reason: str | None = None

    def __post_init__(self):
        uuid_value(self.event_id)
        decimal(self.bot_id)
        text_value(self.dedup_key, 1024)
        text_value(self.kind, 64)
        if type(self.timestamp_ms) is not int or not 0 <= self.timestamp_ms <= 2**63-1:
            raise BotError('bot_event_invalid')
        if self.user_id is not None:
            decimal(self.user_id)
        if self.chat_id is not None:
            decimal(self.chat_id, signed=True)
        if self.actor_id is not None:
            uuid_value(self.actor_id)
        if self.actor_revision is not None and (type(self.actor_revision) is not int or self.actor_revision < 0):
            raise BotError('bot_event_invalid')
        for name, maximum in (('text', 4000), ('callback_payload', 1024), ('message_id', 1024), ('callback_id', 1024), ('invitation_value', 256)):
            if (value := getattr(self, name)) is not None:
                text_value(value, maximum, empty=name == 'text')
        if self.invitation_id is not None:
            uuid_value(self.invitation_id)


@dataclass(frozen=True)
class IntakeReceipt:
    event_id: str
    state: Literal['queued', 'duplicate', 'quarantined']
    error_code: str | None = None


@dataclass(frozen=True)
class EventClaim:
    event: BotEvent
    worker_id: str
    fence: int
    lease_until: datetime


@dataclass(frozen=True)
class BotAction:
    action: str
    task_id: str | None = None
    command: TaskCommand | None = None
    operation_id: str | None = None
    expected_revision: int | None = None
    expected_fingerprint: str | None = None


@dataclass(frozen=True)
class DeterministicReply:
    text: str = field(repr=False)
    buttons: tuple[tuple[BotButton, ...], ...] = ()
    sensitive_action: Literal['desktop_code'] | None = None
    receipt: CommandReceipt | None = None

    def __post_init__(self):
        text_value(self.text, 4000)
        if self.sensitive_action not in (None, 'desktop_code'):
            raise BotError('bot_reply_invalid')


@dataclass(frozen=True)
class CommandPreview(DeterministicReply):
    command: TaskCommand | None = None
