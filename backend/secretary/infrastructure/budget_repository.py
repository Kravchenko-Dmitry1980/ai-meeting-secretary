"""SQLite authority shared by the Secretary and Team processes."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3

from secretary.domain.cloud_budget import BudgetError


RESTORE_GUARD_DDL = '''CREATE TABLE IF NOT EXISTS maintenance_restore_guard(
    id INTEGER PRIMARY KEY CHECK(id=1),restore_id TEXT NOT NULL,
    reconciliation_required INTEGER NOT NULL CHECK(reconciliation_required=1),manifest_sha256 TEXT NOT NULL)'''


def assert_paid_billing_allowed(path):
    """Read the restored authority without creating or migrating a database."""
    from secretary.infrastructure.maintenance_binding import readonly_authority_uri

    if path is None:
        return
    path = Path(path).resolve()
    if not path.exists():
        return
    connection = None
    try:
        connection = sqlite3.connect(readonly_authority_uri(path), uri=True, timeout=.2)
        present = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='maintenance_restore_guard'").fetchone()
        if present and connection.execute('SELECT 1 FROM maintenance_restore_guard WHERE id=1 AND reconciliation_required=1').fetchone():
            raise BudgetError('maintenance_restore_blocked')
    except sqlite3.Error:
        raise BudgetError('maintenance_billing_unavailable') from None
    finally:
        if connection is not None:
            connection.close()


class BudgetRepository:
    def __init__(self, path: Path, *, maintenance=None, participant_id=None):
        self.path = Path(path).resolve()
        self.maintenance, self.participant_id = maintenance, participant_id
        with self.transaction() as connection:
            # The original restore guard remains authority even with a newly
            # opened maintenance control. Diagnostics never migrate history.
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='maintenance_restore_guard'").fetchone() and connection.execute(
                    'SELECT 1 FROM maintenance_restore_guard WHERE id=1 AND reconciliation_required=1').fetchone():
                return
            connection.execute("CREATE TABLE IF NOT EXISTS billing_schema(version INTEGER NOT NULL)")
            row = connection.execute("SELECT version FROM billing_schema").fetchone()
            if row is None:
                connection.execute("INSERT INTO billing_schema VALUES(4)")
            elif row[0] not in (1, 2, 3, 4):
                raise BudgetError("unsupported_billing_schema")
            connection.execute("""CREATE TABLE IF NOT EXISTS billing_state (
                name TEXT PRIMARY KEY, value TEXT NOT NULL)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS billing_accounts (
                period TEXT NOT NULL, key_tag TEXT NOT NULL,
                opening_micro INTEGER NOT NULL, baseline_confirmed_micro INTEGER NOT NULL,
                usage_micro INTEGER NOT NULL, remaining_micro INTEGER, limit_micro INTEGER,
                reset TEXT, observed_ms INTEGER NOT NULL,
                opening_request_watermark INTEGER NOT NULL DEFAULT 0,
                latest_request_watermark INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(period,key_tag))""")
            columns = {item[1] for item in connection.execute("PRAGMA table_info(billing_accounts)")}
            for name in ("opening_request_watermark", "latest_request_watermark"):
                if name not in columns:
                    connection.execute(f"ALTER TABLE billing_accounts ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
            connection.execute("UPDATE billing_schema SET version=4")
            connection.execute("""CREATE TABLE IF NOT EXISTS billing_charges (
                operation_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, request_hash TEXT NOT NULL,
                category TEXT NOT NULL, period TEXT NOT NULL, key_tag TEXT NOT NULL,
                estimated_micro INTEGER NOT NULL, reserved_micro INTEGER NOT NULL,
                confirmed_micro INTEGER, observed_cost_micro INTEGER, status TEXT NOT NULL,
                provider_request_id TEXT, provider_job_id TEXT UNIQUE, confirmed_period TEXT,
                meeting_id TEXT, command_id TEXT, created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL)""")
            charge_columns = {item[1] for item in connection.execute("PRAGMA table_info(billing_charges)")}
            if "scope_id" not in charge_columns:
                connection.execute("ALTER TABLE billing_charges ADD COLUMN scope_id TEXT")
            connection.execute("""CREATE TABLE IF NOT EXISTS billing_scopes (
                scope_id TEXT PRIMARY KEY, operation_id TEXT UNIQUE NOT NULL, cap_micro INTEGER NOT NULL,
                created_ms INTEGER NOT NULL)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS billing_meeting_scopes (
                meeting_id TEXT PRIMARY KEY, scope_id TEXT NOT NULL REFERENCES billing_scopes(scope_id))""")
            connection.execute("""CREATE UNIQUE INDEX IF NOT EXISTS billing_receipt_identity
                ON billing_charges(key_tag,provider_request_id) WHERE provider_request_id IS NOT NULL""")
            connection.execute("""CREATE TABLE IF NOT EXISTS billing_warnings (
                period TEXT NOT NULL, threshold INTEGER NOT NULL, created_ms INTEGER NOT NULL,
                PRIMARY KEY(period,threshold))""")
            connection.execute("""CREATE TABLE IF NOT EXISTS billing_opening_confirmed (
                period TEXT NOT NULL,key_tag TEXT NOT NULL,operation_id TEXT NOT NULL,
                PRIMARY KEY(period,key_tag,operation_id))""")
            connection.execute("""CREATE TABLE IF NOT EXISTS billing_included_receipts (
                period TEXT NOT NULL,key_tag TEXT NOT NULL,provider_request_id TEXT NOT NULL,
                PRIMARY KEY(period,key_tag,provider_request_id))""")
            connection.execute("""CREATE TABLE IF NOT EXISTS billing_reconciliation_events (
                event_id INTEGER PRIMARY KEY,
                operation_id TEXT NOT NULL REFERENCES billing_charges(operation_id),
                occurred_ms INTEGER NOT NULL,
                outcome TEXT NOT NULL CHECK(outcome IN (
                    'lookup_started','lookup_blocked','provider_pending','receipt_applied','receipt_unresolved')),
                reason_code TEXT,
                provider_status TEXT CHECK(provider_status IS NULL OR provider_status IN ('pending','completed','failed')),
                provider_request_id TEXT,
                provider_job_id TEXT,
                provider_cost_micro INTEGER CHECK(provider_cost_micro IS NULL OR provider_cost_micro>=0),
                provider_period TEXT CHECK(provider_period IS NULL OR provider_period GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]')
            )""")
            for verb in ('UPDATE', 'DELETE'):
                connection.execute(f'''CREATE TRIGGER IF NOT EXISTS billing_reconciliation_no_{verb.lower()}
                    BEFORE {verb} ON billing_reconciliation_events
                    BEGIN SELECT RAISE(ABORT,'billing_reconciliation_append_only'); END''')
            connection.execute('''CREATE TRIGGER IF NOT EXISTS billing_reconciliation_no_replace
                BEFORE INSERT ON billing_reconciliation_events
                WHEN NEW.event_id<=COALESCE((SELECT MAX(event_id) FROM billing_reconciliation_events),0)
                BEGIN SELECT RAISE(ABORT,'billing_reconciliation_append_only'); END''')
            connection.execute(RESTORE_GUARD_DDL)
            for verb in ('UPDATE', 'DELETE'):
                connection.execute(f'''CREATE TRIGGER IF NOT EXISTS billing_restore_no_{verb.lower()}
                    BEFORE {verb} ON maintenance_restore_guard
                    BEGIN SELECT RAISE(ABORT,'maintenance_restore_blocked'); END''')
            connection.execute('''CREATE TRIGGER IF NOT EXISTS billing_restore_no_replace
                BEFORE INSERT ON maintenance_restore_guard
                WHEN EXISTS(SELECT 1 FROM maintenance_restore_guard WHERE id=NEW.id)
                BEGIN SELECT RAISE(ABORT,'maintenance_restore_blocked'); END''')
            connection.execute('''CREATE TRIGGER IF NOT EXISTS billing_restored_charge_no_insert
                BEFORE INSERT ON billing_charges
                WHEN EXISTS(SELECT 1 FROM maintenance_restore_guard WHERE reconciliation_required=1)
                BEGIN SELECT RAISE(ABORT,'maintenance_restore_blocked'); END''')
            connection.execute('''CREATE TRIGGER IF NOT EXISTS billing_restored_charge_no_submit
                BEFORE UPDATE OF status ON billing_charges
                WHEN NEW.status='submitted' AND EXISTS(
                    SELECT 1 FROM maintenance_restore_guard WHERE reconciliation_required=1)
                BEGIN SELECT RAISE(ABORT,'maintenance_restore_blocked'); END''')

    def assert_paid_allowed(self):
        assert_paid_billing_allowed(self.path)

    @contextmanager
    def transaction(self):
        """Guarded authorities allow reads; denied writes raise BudgetError."""
        from secretary.infrastructure.maintenance_binding import (
            admission, protect_restored_connection, readonly_authority_uri, restore_guard_present,
            RestoredDiagnosticConnection,
        )
        with admission(self.maintenance, self.participant_id, 'sql'):
            guarded = restore_guard_present((self.path,))
            if not guarded:
                self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = (sqlite3.connect(readonly_authority_uri(self.path), uri=True, timeout=15, isolation_level=None,
                                          factory=RestoredDiagnosticConnection)
                          if guarded else sqlite3.connect(self.path, timeout=15, isolation_level=None))
            connection.row_factory = sqlite3.Row
            try:
                current_guard = protect_restored_connection(connection)
                if current_guard != guarded:
                    raise BudgetError('maintenance_restore_blocked')
                if guarded:
                    connection.execute("BEGIN")
                else:
                    connection.execute("PRAGMA busy_timeout=15000")
                    connection.execute("PRAGMA journal_mode=WAL")
                    connection.execute("PRAGMA foreign_keys=ON")
                    connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except sqlite3.Error as exc:
                connection.rollback()
                denied = (getattr(exc, 'sqlite_errorcode', 0) & 0xff) in {sqlite3.SQLITE_AUTH, sqlite3.SQLITE_READONLY}
                if str(exc) == 'maintenance_restore_blocked' or (guarded and denied):
                    raise BudgetError('maintenance_restore_blocked') from None
                raise
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
