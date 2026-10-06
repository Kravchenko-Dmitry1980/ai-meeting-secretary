"""Atomic monthly reservations; unknown external outcomes retain their reserve."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import inspect
import json
import re
import sqlite3
from uuid import UUID, uuid4

from secretary.domain.cloud_budget import (
    APPROVED_MONTHLY_MICRO, AccountUsage, BudgetError, BudgetSnapshot, CloudCharge,
    Reservation, budget_period, timestamp_ms, to_micro,
)

_EXPLICIT_REFRESH = object()


class MonthlyBudget:
    def __init__(self, repository, *, approved_budget_micro=APPROVED_MONTHLY_MICRO,
                 clock=None, account_reader=None):
        if type(approved_budget_micro) is not int or not 0 <= approved_budget_micro <= APPROVED_MONTHLY_MICRO:
            raise BudgetError("invalid_approved_monthly_budget")
        self.repository = repository
        self.approved_budget_micro = approved_budget_micro
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.account_reader = account_reader
        with self.repository.transaction() as connection:
            policy = connection.execute("SELECT value FROM billing_state WHERE name='approved_micro'").fetchone()
            if policy and int(policy[0]) != approved_budget_micro:
                raise BudgetError("monthly_budget_policy_changed")
            if policy is None:
                connection.execute("INSERT INTO billing_state VALUES('approved_micro',?)",
                                   (str(approved_budget_micro),))

    def refresh_account(self, usage: AccountUsage, *, expected_key=_EXPLICIT_REFRESH) -> None:
        period = budget_period(usage.observed_at)
        with self.repository.transaction() as connection:
            if expected_key is not _EXPLICIT_REFRESH:
                active = connection.execute("SELECT value FROM billing_state WHERE name='active_key'").fetchone()
                if (active[0] if active else None) != expected_key:
                    raise BudgetError("monthly_budget_key_changed")
            old = connection.execute("SELECT * FROM billing_accounts WHERE period=? AND key_tag=?",
                                     (period, usage.key_tag)).fetchone()
            confirmed = connection.execute("""SELECT COALESCE(SUM(confirmed_micro),0) FROM billing_charges
                WHERE key_tag=? AND confirmed_period=? AND status='confirmed'""", (usage.key_tag, period)).fetchone()[0]
            opening = old["opening_micro"] if old else max(usage.usage_micro, confirmed)
            baseline = old["baseline_confirmed_micro"] if old else confirmed
            watermark = connection.execute("SELECT COALESCE(MAX(rowid),0) FROM billing_charges").fetchone()[0]
            # A slow older reader must not rewind a fresher account observation.
            if old is not None and old["observed_ms"] > timestamp_ms(usage.observed_at):
                return
            connection.execute("""INSERT INTO billing_accounts VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(period,key_tag) DO UPDATE SET usage_micro=excluded.usage_micro,
                remaining_micro=excluded.remaining_micro,limit_micro=excluded.limit_micro,
                reset=excluded.reset,observed_ms=excluded.observed_ms,
                latest_request_watermark=excluded.latest_request_watermark""",
                (period, usage.key_tag, opening, baseline,
                 max(usage.usage_micro, old["usage_micro"] if old else 0),
                 usage.remaining_micro, usage.limit_micro, usage.reset, timestamp_ms(usage.observed_at),
                 old["opening_request_watermark"] if old else watermark, watermark))
            if old is None:
                connection.execute("""INSERT OR IGNORE INTO billing_opening_confirmed
                    SELECT ?,?,operation_id FROM billing_charges WHERE key_tag=?
                    AND confirmed_period=? AND status='confirmed'""", (period, usage.key_tag, usage.key_tag, period))
            for provider_id in usage.included_receipt_ids:
                connection.execute("INSERT OR IGNORE INTO billing_included_receipts VALUES(?,?,?)",
                                   (period, usage.key_tag, provider_id))
            connection.execute("INSERT INTO billing_state VALUES('active_key',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",
                               (usage.key_tag,))

    def _snapshot(self, connection, now, *, excluded_reservation_id=None) -> BudgetSnapshot:
        period = budget_period(now)
        active = connection.execute("SELECT value FROM billing_state WHERE name='active_key'").fetchone()
        accounts = connection.execute("SELECT * FROM billing_accounts WHERE period=?", (period,)).fetchall()
        account = next((a for a in accounts if active and a["key_tag"] == active[0]), None)
        totals = {r["key_tag"]: r["amount"] for r in connection.execute("""SELECT key_tag,
            SUM(confirmed_micro) amount FROM billing_charges WHERE confirmed_period=? AND status='confirmed'
            GROUP BY key_tag""", (period,))}
        spent = 0
        overlap_reserve = 0
        for observed in accounts:
            local = totals.pop(observed["key_tag"], 0)
            def confirmed_after(watermark):
                return connection.execute("""SELECT COALESCE(SUM(confirmed_micro),0) FROM billing_charges
                    WHERE key_tag=? AND confirmed_period=? AND status='confirmed' AND rowid>?""",
                    (observed["key_tag"], period, watermark)).fetchone()[0]
            included = connection.execute("""SELECT COALESCE(SUM(c.confirmed_micro),0)
                FROM billing_charges c JOIN billing_included_receipts i
                ON c.provider_request_id=i.provider_request_id AND c.key_tag=i.key_tag
                AND c.confirmed_period=i.period WHERE c.key_tag=? AND c.confirmed_period=?
                AND c.status='confirmed' AND NOT EXISTS (SELECT 1 FROM billing_opening_confirmed b
                WHERE b.period=i.period AND b.key_tag=i.key_tag AND b.operation_id=c.operation_id)""",
                (observed["key_tag"], period)).fetchone()[0]
            floor = max(observed["usage_micro"] + confirmed_after(observed["latest_request_watermark"]),
                        observed["opening_micro"] + confirmed_after(observed["opening_request_watermark"]), local)
            upper = observed["usage_micro"] + max(0, local - observed["baseline_confirmed_micro"]) - included
            spent += floor
            # Aggregate growth alone never proves which known receipt it includes.
            overlap_reserve += max(0, upper - floor)
        spent += sum(totals.values())
        # Carry all outstanding attempts, including the previous month's unknowns.
        reserved = overlap_reserve + connection.execute("""SELECT COALESCE(SUM(MAX(reserved_micro,
            COALESCE(observed_cost_micro,0))),0) FROM billing_charges
            WHERE status IN ('reserved','submitted','uncertain') AND operation_id IS NOT ?""",
            (excluded_reservation_id,)).fetchone()[0]
        code = None
        cap = self.approved_budget_micro
        if (account is None or account["reset"] != "monthly" or account["limit_micro"] is None
                or account["remaining_micro"] is None or account["limit_micro"] > cap):
            code = "monthly_budget_configuration"
        elif not 0 <= timestamp_ms(now) - account["observed_ms"] <= 60_000:
            code = "monthly_budget_stale"
        if account and account["limit_micro"] is not None:
            cap = min(cap, account["limit_micro"])
        remaining = max(0, cap - spent - reserved)
        if account and account["remaining_micro"] is not None:
            remaining = min(remaining, max(0, account["remaining_micro"] - reserved))
        return BudgetSnapshot(period, self.approved_budget_micro, cap, spent, reserved, remaining, code)

    def snapshot(self, now=None) -> BudgetSnapshot:
        with self.repository.transaction() as connection:
            return self._snapshot(connection, now or self.clock())

    def create_scope(self, operation_id: str, cap_micro: int) -> dict:
        try:
            if str(UUID(operation_id)) != operation_id:
                raise ValueError
        except (ValueError, TypeError, AttributeError) as exc:
            raise BudgetError("invalid_charge_identity") from exc
        if type(cap_micro) is not int or not 0 < cap_micro <= self.approved_budget_micro:
            raise BudgetError("invalid_budget_scope_cap")
        with self.repository.transaction() as connection:
            old = connection.execute("SELECT * FROM billing_scopes WHERE operation_id=?", (operation_id,)).fetchone()
            if old:
                if old["cap_micro"] != cap_micro:
                    raise BudgetError("budget_operation_conflict")
                return {"scope_id": old["scope_id"], "cap_rub": cap_micro / 1_000_000}
            identifier = str(uuid4())
            connection.execute("INSERT INTO billing_scopes VALUES(?,?,?,?)",
                               (identifier, operation_id, cap_micro, timestamp_ms(self.clock())))
            return {"scope_id": identifier, "cap_rub": cap_micro / 1_000_000}

    @staticmethod
    def _scope_totals(connection, scope_id: str, *, excluded_reservation_id=None):
        scope = connection.execute("SELECT * FROM billing_scopes WHERE scope_id=?", (scope_id,)).fetchone()
        if scope is None:
            raise BudgetError("unknown_budget_scope")
        totals = connection.execute("""SELECT
            COALESCE(SUM(CASE WHEN status='confirmed' THEN confirmed_micro ELSE 0 END),0) confirmed,
            COALESCE(SUM(CASE WHEN status IN ('reserved','submitted','uncertain') AND operation_id IS NOT ?
                THEN MAX(reserved_micro,COALESCE(observed_cost_micro,0)) ELSE 0 END),0) reserved,
            COALESCE(SUM(CASE WHEN status='uncertain' THEN 1 ELSE 0 END),0) uncertain
            FROM billing_charges WHERE scope_id=?""", (excluded_reservation_id, scope_id)).fetchone()
        return scope, totals

    @staticmethod
    def _scope_snapshot(connection, scope_id: str) -> dict:
        scope, totals = MonthlyBudget._scope_totals(connection, scope_id)
        remaining = max(0, scope["cap_micro"] - totals["confirmed"] - totals["reserved"])
        return {"scope_id": scope_id, "cap_rub": scope["cap_micro"] / 1_000_000,
                "confirmed_rub": totals["confirmed"] / 1_000_000,
                "reserved_rub": totals["reserved"] / 1_000_000, "remaining_rub": remaining / 1_000_000,
                "uncertain_count": totals["uncertain"],
                "paused_code": "monthly_budget_scope_exhausted" if remaining == 0 else None}

    def scope_snapshot(self, scope_id: str) -> dict:
        with self.repository.transaction() as connection:
            return self._scope_snapshot(connection, scope_id)

    def bind_meeting_scope(self, meeting_id: str, scope_id: str):
        with self.repository.transaction() as connection:
            self._scope_snapshot(connection, scope_id)
            old = connection.execute("SELECT scope_id FROM billing_meeting_scopes WHERE meeting_id=?", (meeting_id,)).fetchone()
            if old and old[0] != scope_id:
                raise BudgetError("budget_scope_binding_conflict")
            connection.execute("INSERT OR IGNORE INTO billing_meeting_scopes VALUES(?,?)", (meeting_id, scope_id))

    def scope_for_meeting(self, meeting_id: str) -> str | None:
        with self.repository.transaction() as connection:
            row = connection.execute("SELECT scope_id FROM billing_meeting_scopes WHERE meeting_id=?", (meeting_id,)).fetchone()
            return row[0] if row else None

    async def ensure_account(self, *, key_tag=None):
        with self.repository.transaction() as connection:
            snapshot = self._snapshot(connection, self.clock())
            active = connection.execute("SELECT value FROM billing_state WHERE name='active_key'").fetchone()
        if key_tag and active and key_tag != active[0]:
            raise BudgetError("monthly_budget_key_changed")
        if snapshot.paused_code or active is None:
            if self.account_reader is None:
                raise BudgetError(snapshot.paused_code or "monthly_budget_configuration")
            usage = self.account_reader.read_key_usage()
            if inspect.isawaitable(usage):
                usage = await usage
            if key_tag and usage.key_tag != key_tag:
                raise BudgetError("monthly_budget_key_changed")
            self.refresh_account(usage, expected_key=active[0] if active else None)
        snapshot = self.snapshot()
        if snapshot.paused_code:
            raise BudgetError(snapshot.paused_code)
        return snapshot

    @staticmethod
    def _reservation(row) -> Reservation:
        return Reservation(row["operation_id"], row["period"], row["status"], row["reserved_micro"],
                           row["confirmed_micro"], row["provider_job_id"], row["provider_request_id"], row["key_tag"])

    def reservation(self, operation_id: str) -> Reservation:
        with self.repository.transaction() as connection:
            row = connection.execute("SELECT * FROM billing_charges WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None:
                raise BudgetError("unknown_budget_operation")
            return self._reservation(row)

    def reserve(self, charge: CloudCharge, *, key_tag=None) -> Reservation:
        payload_hash = hashlib.sha256(json.dumps(asdict(charge), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        with self.repository.transaction() as connection:
            old = connection.execute("SELECT * FROM billing_charges WHERE operation_id=?", (charge.operation_id,)).fetchone()
            if old:
                if old["payload_hash"] != payload_hash:
                    raise BudgetError("budget_operation_conflict")
                if key_tag and old["key_tag"] != key_tag:
                    raise BudgetError("monthly_budget_key_changed")
                return self._reservation(old)
            now = self.clock()
            snapshot = self._snapshot(connection, now)
            if snapshot.paused_code:
                raise BudgetError(snapshot.paused_code)
            if charge.reserved_micro > snapshot.remaining_micro or snapshot.remaining_micro == 0:
                raise BudgetError("monthly_budget_exhausted")
            scope_id = charge.scope_id
            bound = connection.execute("SELECT scope_id FROM billing_meeting_scopes WHERE meeting_id=?", (charge.meeting_id,)).fetchone()
            if bound:
                if scope_id and scope_id != bound[0]:
                    raise BudgetError("budget_scope_binding_conflict")
                scope_id = bound[0]
            if scope_id:
                scope = self._scope_snapshot(connection, scope_id)
                if charge.reserved_micro > to_micro(scope["remaining_rub"]) or scope["remaining_rub"] == 0:
                    raise BudgetError("monthly_budget_scope_exhausted")
            key = connection.execute("SELECT value FROM billing_state WHERE name='active_key'").fetchone()[0]
            if key_tag and key_tag != key:
                raise BudgetError("monthly_budget_key_changed")
            connection.execute("""INSERT INTO billing_charges(operation_id,payload_hash,request_hash,category,
                period,key_tag,estimated_micro,reserved_micro,status,meeting_id,command_id,created_ms,updated_ms,scope_id)
                VALUES(?,?,?,?,?,?,?,?,'reserved',?,?,?,?,?)""",
                (charge.operation_id, payload_hash, charge.request_hash, charge.category, snapshot.period, key,
                 charge.estimated_micro, charge.reserved_micro, charge.meeting_id, charge.command_id,
                 timestamp_ms(now), timestamp_ms(now), scope_id))
            return self._reservation(connection.execute("SELECT * FROM billing_charges WHERE operation_id=?",
                                                       (charge.operation_id,)).fetchone())

    def mark_uncertain(self, operation_id: str):
        with self.repository.transaction() as connection:
            cursor = connection.execute("""UPDATE billing_charges SET status='uncertain',updated_ms=?
                WHERE operation_id=? AND status IN ('reserved','submitted','uncertain')""",
                (timestamp_ms(self.clock()), operation_id))
            if not cursor.rowcount and connection.execute("SELECT 1 FROM billing_charges WHERE operation_id=?", (operation_id,)).fetchone() is None:
                raise BudgetError("unknown_budget_operation")

    def mark_submitted(self, operation_id: str):
        with self.repository.transaction() as connection:
            row = connection.execute("SELECT * FROM billing_charges WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None:
                raise BudgetError("unknown_budget_operation")
            if row["status"] != "reserved":
                raise BudgetError("budget_operation_already_dispatched")
            active = connection.execute("SELECT value FROM billing_state WHERE name='active_key'").fetchone()
            if not active or row["key_tag"] != active[0]:
                raise BudgetError("monthly_budget_key_changed")
            now = self.clock()
            if row["period"] != budget_period(now):
                raise BudgetError("monthly_budget_period_changed")
            # Revalidate inside the final write transaction: the durable provider
            # checkpoint may have yielded while account proof or costs changed.
            # Exclude only this hold before clamping; adding it to an already
            # clamped balance could admit a request when the budget is exceeded.
            snapshot = self._snapshot(connection, now, excluded_reservation_id=operation_id)
            if snapshot.paused_code:
                raise BudgetError(snapshot.paused_code)
            required = max(row["reserved_micro"], row["observed_cost_micro"] or 0)
            if required > snapshot.remaining_micro or snapshot.remaining_micro == 0:
                raise BudgetError("monthly_budget_exhausted")
            if row["scope_id"]:
                scope, totals = self._scope_totals(connection, row["scope_id"],
                                                  excluded_reservation_id=operation_id)
                remaining = max(0, scope["cap_micro"] - totals["confirmed"] - totals["reserved"])
                if required > remaining or remaining == 0:
                    raise BudgetError("monthly_budget_scope_exhausted")
            connection.execute("UPDATE billing_charges SET status='submitted',updated_ms=? WHERE operation_id=?",
                               (timestamp_ms(now), operation_id))

    def observe_cost(self, operation_id: str, raw_cost):
        cost = to_micro(raw_cost)
        with self.repository.transaction() as connection:
            row = connection.execute("SELECT * FROM billing_charges WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None:
                raise BudgetError("unknown_budget_operation")
            if row["status"] == "confirmed" and cost == row["confirmed_micro"]:
                return
            if row["status"] in {"released", "confirmed"}:
                raise BudgetError("budget_receipt_conflict")
            connection.execute("UPDATE billing_charges SET observed_cost_micro=MAX(COALESCE(observed_cost_micro,0),?),updated_ms=? WHERE operation_id=?",
                               (cost, timestamp_ms(self.clock()), operation_id))

    def release_unsubmitted(self, operation_id: str):
        with self.repository.transaction() as connection:
            changed = connection.execute("""UPDATE billing_charges SET status='released',updated_ms=?
                WHERE operation_id=? AND status='reserved'""", (timestamp_ms(self.clock()), operation_id)).rowcount
            if not changed:
                raise BudgetError("budget_operation_already_dispatched")

    def release_rejected(self, operation_id: str):
        """Internal HTTP boundary only: a bound explicit rejection with no cost."""
        with self.repository.transaction() as connection:
            changed = connection.execute("""UPDATE billing_charges SET status='released',updated_ms=?
                WHERE operation_id=? AND status IN ('submitted','uncertain') AND confirmed_micro IS NULL
                AND observed_cost_micro IS NULL""", (timestamp_ms(self.clock()), operation_id)).rowcount
            if not changed:
                raise BudgetError("budget_receipt_conflict")

    def bind_provider_job(self, operation_id: str, provider_job_id: str):
        if not isinstance(provider_job_id, str) or not 1 <= len(provider_job_id) <= 256:
            raise BudgetError("budget_provider_job_conflict")
        with self.repository.transaction() as connection:
            row = connection.execute("SELECT * FROM billing_charges WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None or (row["provider_job_id"] and row["provider_job_id"] != provider_job_id):
                raise BudgetError("budget_provider_job_conflict")
            try:
                connection.execute("UPDATE billing_charges SET provider_job_id=?,updated_ms=? WHERE operation_id=?",
                                   (provider_job_id, timestamp_ms(self.clock()), operation_id))
            except sqlite3.IntegrityError as exc:
                raise BudgetError("budget_provider_job_conflict") from exc

    def bind_provider_receipt(self, operation_id: str, provider_request_id: str):
        if not isinstance(provider_request_id, str) or not 1 <= len(provider_request_id) <= 256:
            raise BudgetError("budget_receipt_identity_conflict")
        with self.repository.transaction() as connection:
            row = connection.execute("SELECT * FROM billing_charges WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None:
                raise BudgetError("unknown_budget_operation")
            if row["provider_request_id"] and row["provider_request_id"] != provider_request_id:
                raise BudgetError("budget_receipt_identity_conflict")
            try:
                connection.execute("UPDATE billing_charges SET provider_request_id=COALESCE(provider_request_id,?) WHERE operation_id=?",
                                   (provider_request_id, operation_id))
            except sqlite3.IntegrityError as exc:
                raise BudgetError("budget_receipt_identity_conflict") from exc

    def operation_for_job(self, provider_job_id: str) -> str | None:
        with self.repository.transaction() as connection:
            row = connection.execute("SELECT operation_id FROM billing_charges WHERE provider_job_id=?",
                                     (provider_job_id,)).fetchone()
            return row[0] if row else None

    def settle(self, operation_id: str, receipt: dict) -> BudgetSnapshot:
        raw_cost = receipt.get("confirmed_rub")
        cost = None
        if raw_cost is not None:
            try:
                cost = to_micro(raw_cost)
            except BudgetError:
                pass  # Invalid upstream usage remains unknown, never zero.
        provider_id = receipt.get("provider_request_id")
        if provider_id is not None and (not isinstance(provider_id, str) or not 1 <= len(provider_id) <= 256):
            provider_id = None
        with self.repository.transaction() as connection:
            row = connection.execute("SELECT * FROM billing_charges WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None:
                raise BudgetError("unknown_budget_operation")
            if provider_id and row["provider_request_id"] and provider_id != row["provider_request_id"]:
                raise BudgetError("budget_receipt_identity_conflict")
            now = self.clock()
            period = receipt.get("provider_period")
            if period is not None and (not isinstance(period, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", period)):
                raise BudgetError("invalid_receipt_period")
            # No server billing timestamp: never guess a late receipt's month.
            if period is None and budget_period(now) == row["period"]:
                period = row["period"]
            if row["status"] == "confirmed":
                if period is not None and period != row["confirmed_period"]:
                    raise BudgetError("budget_receipt_conflict")
                if cost is not None and (cost != row["confirmed_micro"] or (provider_id and provider_id != row["provider_request_id"])):
                    raise BudgetError("budget_receipt_conflict")
                return self._snapshot(connection, now)
            if row["status"] == "released":
                raise BudgetError("budget_receipt_conflict")
            observed = row["observed_cost_micro"]
            if cost is not None and observed is not None and cost < observed:
                raise BudgetError("budget_receipt_conflict")
            held_cost = max(cost or 0, observed or 0) if cost is not None or observed is not None else None
            try:
                connection.execute("""UPDATE billing_charges SET confirmed_micro=?,observed_cost_micro=?,
                    provider_request_id=COALESCE(?,provider_request_id),confirmed_period=?,status=?,updated_ms=?
                    WHERE operation_id=?""", (cost if period else None, held_cost, provider_id, period,
                    "confirmed" if cost is not None and period else "uncertain", timestamp_ms(now), operation_id))
            except sqlite3.IntegrityError as exc:
                raise BudgetError("budget_receipt_identity_conflict") from exc
            return self._snapshot(connection, now)

    def take_warnings(self) -> list[int]:
        with self.repository.transaction() as connection:
            snapshot = self._snapshot(connection, self.clock())
            due = []
            for threshold in (50, 80, 90):
                if snapshot.effective_limit_micro and snapshot.confirmed_micro * 100 >= snapshot.effective_limit_micro * threshold:
                    inserted = connection.execute("INSERT OR IGNORE INTO billing_warnings VALUES(?,?,?)",
                                                  (snapshot.period, threshold, timestamp_ms(self.clock()))).rowcount
                    if inserted:
                        due.append(threshold)
            return due
