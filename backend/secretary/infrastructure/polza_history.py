"""Exact-ID Polza history receipts; no guessed billing period or aggregate proof."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import re
from urllib.parse import quote, urlsplit

import httpx

from secretary.domain.cloud_budget import BudgetError

_HISTORY_URL = "https://polza.ai/api/v1/history/generations/"
_MAX_BODY_BYTES = 64 * 1024
_ERROR = "monthly_budget_receipt_unavailable"
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("nonfinite_json_number")


class PolzaHistoryClient:
    def __init__(self, settings, transport=None, clock=None):
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
        base = getattr(self.settings, "polza_base_url", "https://polza.ai/api/v1")
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
            raise BudgetError(_ERROR)
        return value

    @staticmethod
    async def _read_body(client, key: str, generation_id: str) -> bytes:
        url = _HISTORY_URL + quote(generation_id, safe="")
        async with client.stream("GET", url, headers={
                "Authorization": f"Bearer {key}", "Accept": "application/json",
                "Accept-Encoding": "identity"}) as response:
            if response.status_code != 200:
                raise BudgetError(_ERROR)
            if response.headers.get("Content-Encoding", "identity").strip().lower() != "identity":
                raise BudgetError(_ERROR)
            length = response.headers.get("Content-Length")
            if length is not None and (not length.isdecimal() or int(length) > _MAX_BODY_BYTES):
                raise BudgetError(_ERROR)
            if response.is_stream_consumed:
                if len(response.content) > _MAX_BODY_BYTES:
                    raise BudgetError(_ERROR)
                return response.content
            body = bytearray()
            async for chunk in response.aiter_raw():
                if len(chunk) > _MAX_BODY_BYTES - len(body):
                    raise BudgetError(_ERROR)
                body.extend(chunk)
            return bytes(body)

    @staticmethod
    def _receipt(body: bytes, generation_id: str) -> dict:
        value = json.loads(body, parse_float=Decimal, object_pairs_hook=_unique_object,
                           parse_constant=_reject_constant)
        if (not isinstance(value, dict) or "data" in value or value.get("id") != generation_id
                or not isinstance(value.get("status"), str)
                or value["status"] not in {"completed", "failed", "pending"}):
            raise BudgetError(_ERROR)
        # Official clientCost is RUB even though detail has no mandatory currency field.
        if "currency" in value and value["currency"] != "RUB":
            raise BudgetError(_ERROR)
        cost = None
        if "clientCost" in value:
            raw = value["clientCost"]
            if not isinstance(raw, str) or _NUMBER.fullmatch(raw) is None:
                raise BudgetError(_ERROR)
            cost = Decimal(raw)
            if not cost.is_finite() or cost < 0:
                raise BudgetError(_ERROR)
        if value["status"] == "pending":
            cost = None  # A pending reported price is not a confirmed charge.
        elif cost is None:
            raise BudgetError(_ERROR)
        return {"provider_request_id": generation_id, "confirmed_rub": cost,
                "status": value["status"], "provider_period": None}

    async def read_receipt(self, generation_id: str) -> dict:
        try:
            if (not isinstance(generation_id, str) or not 1 <= len(generation_id) <= 256
                    or generation_id in {".", ".."} or generation_id.strip() != generation_id
                    or not generation_id.isprintable()):
                raise BudgetError(_ERROR)
            self._validate_base()
            key = self._key()
            started = self._time()
            async with asyncio.timeout(30):
                async with httpx.AsyncClient(
                        transport=self.transport, verify=True, trust_env=False, follow_redirects=False,
                        timeout=httpx.Timeout(10, connect=5, pool=5)) as client:
                    body = await self._read_body(client, key, generation_id)
            if self._time() < started:
                raise BudgetError(_ERROR)
            return self._receipt(body, generation_id)
        except BudgetError:
            raise
        except Exception:
            raise BudgetError(_ERROR) from None
