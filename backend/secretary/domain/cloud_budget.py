"""Money and immutable identities at the shared Polza billing boundary."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import re
from uuid import UUID

MICRO_RUB = 1_000_000
APPROVED_MONTHLY_MICRO = 3000 * MICRO_RUB
# Polza's documented current Moscow reset: first day at 01:00 UTC+03:00.
MOSCOW = timezone(timedelta(hours=3), name="Europe/Moscow")


class BudgetError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def to_micro(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise BudgetError("invalid_money")
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or number > Decimal("1000000000"):
            raise BudgetError("invalid_money")
        return int((number * MICRO_RUB).to_integral_value(rounding=ROUND_CEILING))
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise BudgetError("invalid_money") from exc


def timestamp_ms(value: datetime) -> int:
    if value.tzinfo is None or value.utcoffset() is None:
        raise BudgetError("naive_billing_time")
    return int(value.timestamp() * 1000)


def budget_period(value: datetime) -> str:
    timestamp_ms(value)
    return (value.astimezone(MOSCOW) - timedelta(hours=1)).strftime("%Y-%m")


def _micro(value, *, optional=False):
    if optional and value is None:
        return
    if type(value) is not int or value < 0 or value > 10**15:
        raise BudgetError("invalid_money")


@dataclass(frozen=True)
class AccountUsage:
    key_tag: str
    limit_micro: int | None
    remaining_micro: int | None
    usage_micro: int
    reset: str | None
    observed_at: datetime
    # Optional reconciliation proof; GET/key alone does not provide these IDs.
    included_receipt_ids: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.key_tag, str) or not 1 <= len(self.key_tag) <= 128:
            raise BudgetError("invalid_account_identity")
        for value in (self.limit_micro, self.remaining_micro):
            _micro(value, optional=True)
        _micro(self.usage_micro)
        timestamp_ms(self.observed_at)
        if (not isinstance(self.included_receipt_ids, tuple)
                or any(not isinstance(value, str) or not 1 <= len(value) <= 256
                       for value in self.included_receipt_ids)):
            raise BudgetError("invalid_account_receipt_proof")


@dataclass(frozen=True)
class CloudCharge:
    operation_id: str
    category: str
    request_hash: str
    estimated_micro: int
    reserved_micro: int
    meeting_id: str | None = None
    command_id: str | None = None
    scope_id: str | None = None

    def __post_init__(self):
        try:
            if str(UUID(self.operation_id)) != self.operation_id:
                raise ValueError
        except (ValueError, AttributeError, TypeError) as exc:
            raise BudgetError("invalid_charge_identity") from exc
        if self.category not in {"meeting_stt", "meeting_summary", "voice_stt", "voice_intent", "benchmark"}:
            raise BudgetError("invalid_charge_category")
        if not re.fullmatch(r"[0-9a-f]{64}", self.request_hash):
            raise BudgetError("invalid_request_hash")
        _micro(self.estimated_micro)
        _micro(self.reserved_micro)
        if self.reserved_micro < self.estimated_micro:
            raise BudgetError("invalid_charge_reserve")
        if self.scope_id is not None:
            try:
                if str(UUID(self.scope_id)) != self.scope_id:
                    raise ValueError
            except (TypeError, ValueError, AttributeError) as exc:
                raise BudgetError("invalid_budget_scope") from exc


@dataclass(frozen=True)
class Reservation:
    operation_id: str
    period: str
    status: str
    reserved_micro: int
    confirmed_micro: int | None
    provider_job_id: str | None = None
    provider_request_id: str | None = None
    key_tag: str | None = None


@dataclass(frozen=True)
class BudgetSnapshot:
    period: str
    approved_limit_micro: int
    effective_limit_micro: int
    confirmed_micro: int
    reserved_micro: int
    remaining_micro: int
    paused_code: str | None

    def public(self) -> dict:
        return {"period": self.period, "approved_rub": self.approved_limit_micro / MICRO_RUB,
                "effective_limit_rub": self.effective_limit_micro / MICRO_RUB,
                "confirmed_rub": self.confirmed_micro / MICRO_RUB,
                "reserved_rub": self.reserved_micro / MICRO_RUB,
                "remaining_rub": self.remaining_micro / MICRO_RUB, "paused_code": self.paused_code}
