"""Read-only Polza account boundary; no paid requests or secret-bearing errors."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from urllib.parse import urlsplit

import httpx

from secretary.domain.cloud_budget import AccountUsage, BudgetError, budget_period, to_micro

_KEY_URL = "https://polza.ai/api/v1/key"
_MAX_BODY_BYTES = 64 * 1024
_RESET_VALUES = {"daily", "weekly", "monthly", "never"}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("nonfinite_json_number")


class PolzaAccountClient:
    def __init__(self, settings, *, transport=None, clock=None):
        self.settings = settings
        self.transport = transport
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _key(self) -> str:
        value = getattr(self.settings, "polza_api_key", None)
        if hasattr(value, "get_secret_value"):
            value = value.get_secret_value()
        if not isinstance(value, str) or not value.strip():
            raise BudgetError("monthly_budget_configuration")
        key = value.strip()
        if any(ord(character) < 32 or ord(character) == 127 for character in key):
            raise BudgetError("monthly_budget_configuration")
        return key

    @property
    def key_tag(self) -> str:
        return hashlib.sha256(self._key().encode("utf-8")).hexdigest()

    def _validate_base(self) -> None:
        base = getattr(self.settings, "polza_base_url", "")
        try:
            parsed = urlsplit(base)
            valid = (parsed.scheme == "https" and parsed.hostname == "polza.ai"
                     and parsed.port in (None, 443) and not parsed.username and not parsed.password
                     and parsed.path.rstrip("/") == "/api/v1" and not parsed.query and not parsed.fragment)
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise BudgetError("monthly_budget_configuration")

    def _time(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise BudgetError("monthly_budget_configuration")
        return value

    async def _read_body(self, client, key: str) -> bytes:
        async with client.stream("GET", _KEY_URL, headers={
                "Authorization": f"Bearer {key}", "Accept": "application/json",
                "Accept-Encoding": "identity"}) as response:
            if response.status_code != 200:
                raise BudgetError("monthly_budget_account_unavailable")
            # Avoid decompression before checking the expanded-body boundary.
            if response.headers.get("Content-Encoding", "identity").strip().lower() != "identity":
                raise BudgetError("monthly_budget_account_unavailable")
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdecimal() or int(length) > _MAX_BODY_BYTES):
                raise BudgetError("monthly_budget_account_unavailable")
            if response.is_stream_consumed:
                if len(response.content) > _MAX_BODY_BYTES:
                    raise BudgetError("monthly_budget_account_unavailable")
                return response.content
            body = bytearray()
            async for chunk in response.aiter_raw():
                if len(chunk) > _MAX_BODY_BYTES - len(body):
                    raise BudgetError("monthly_budget_account_unavailable")
                body.extend(chunk)
            return bytes(body)

    @staticmethod
    def _usage(body: bytes, key_tag: str, observed_at: datetime) -> AccountUsage:
        try:
            value = json.loads(body, parse_float=Decimal, object_pairs_hook=_unique_object,
                               parse_constant=_reject_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise BudgetError("monthly_budget_account_unavailable") from None
        required = {"limit", "limit_remaining", "limit_reset", "usage_monthly"}
        if not isinstance(value, dict) or "data" in value or not required.issubset(value):
            raise BudgetError("monthly_budget_configuration")
        reset = value["limit_reset"]
        if reset is not None and (not isinstance(reset, str) or reset not in _RESET_VALUES):
            raise BudgetError("monthly_budget_configuration")
        try:
            cap = None if value["limit"] is None else to_micro(value["limit"])
            remaining = None if value["limit_remaining"] is None else to_micro(value["limit_remaining"])
            usage = to_micro(value["usage_monthly"])
            for optional_counter in ("usage", "usage_daily", "usage_weekly"):
                if optional_counter in value:
                    to_micro(value[optional_counter])
            return AccountUsage(key_tag=key_tag, limit_micro=cap, remaining_micro=remaining,
                                usage_micro=usage, reset=reset, observed_at=observed_at)
        except BudgetError:
            raise BudgetError("monthly_budget_configuration") from None

    async def read_key_usage(self) -> AccountUsage:
        self._validate_base()
        key = self._key()
        key_tag = hashlib.sha256(key.encode("utf-8")).hexdigest()
        try:
            async with asyncio.timeout(30):
                async with httpx.AsyncClient(
                        transport=self.transport, verify=True, trust_env=False, follow_redirects=False,
                        timeout=httpx.Timeout(10, connect=5, pool=5)) as client:
                    for attempt in range(2):
                        started = self._time()
                        body = await self._read_body(client, key)
                        finished = self._time()
                        if finished < started:
                            raise BudgetError("monthly_budget_account_unavailable")
                        if budget_period(started) == budget_period(finished):
                            return self._usage(body, key_tag, finished)
                        if attempt == 1:
                            raise BudgetError("monthly_budget_account_unavailable")
        except BudgetError:
            raise
        except Exception:
            raise BudgetError("monthly_budget_account_unavailable") from None
        raise BudgetError("monthly_budget_account_unavailable")
