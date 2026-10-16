"""Independent Team SQLite database. No app settings, owner DB or network imports."""
from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path


SCHEMA_VERSION = 9
SCHEMA = (
    'CREATE TABLE team_schema(version INTEGER PRIMARY KEY)',
    '''CREATE TABLE team_members(id TEXT PRIMARY KEY,max_user_id TEXT UNIQUE NOT NULL,
       vikunja_user_id TEXT UNIQUE NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL CHECK(json_valid(payload)))''',
    '''CREATE TABLE team_projections(task_id TEXT PRIMARY KEY,project_id TEXT NOT NULL,
       revision INTEGER NOT NULL,payload TEXT NOT NULL CHECK(json_valid(payload)))''',
    '''CREATE TABLE team_commands(operation_id TEXT PRIMARY KEY,actor_id TEXT NOT NULL,
       project_id TEXT NOT NULL,task_id TEXT,payload_hash TEXT NOT NULL,payload TEXT NOT NULL CHECK(json_valid(payload)),
       acceptance TEXT NOT NULL CHECK(json_valid(acceptance)),created_at TEXT NOT NULL)''',
    '''CREATE TABLE team_execution(operation_id TEXT PRIMARY KEY REFERENCES team_commands(operation_id),
       state TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL CHECK(json_valid(payload)),
       worker_id TEXT,fence INTEGER NOT NULL DEFAULT 0,lease_until REAL,reconciliation INTEGER NOT NULL DEFAULT 0)''',
    '''CREATE TABLE team_resources(resource_key TEXT PRIMARY KEY,operation_id TEXT,
       fence INTEGER NOT NULL DEFAULT 0)''',
    '''CREATE TABLE team_journal(id INTEGER PRIMARY KEY AUTOINCREMENT,operation_id TEXT NOT NULL,
       event TEXT NOT NULL,payload TEXT NOT NULL CHECK(json_valid(payload)),created_at TEXT NOT NULL)''',
    '''CREATE TABLE team_messages(id TEXT PRIMARY KEY,kind TEXT NOT NULL,dedup_key TEXT NOT NULL,
       payload_hash TEXT NOT NULL,payload TEXT NOT NULL CHECK(json_valid(payload)),created_at TEXT NOT NULL,
       UNIQUE(kind,dedup_key))''',
    '''CREATE TABLE team_message_state(id TEXT PRIMARY KEY REFERENCES team_messages(id),state TEXT NOT NULL,
       fence INTEGER NOT NULL DEFAULT 0,worker_id TEXT,lease_until REAL,error_code TEXT,
       reconciliation INTEGER NOT NULL DEFAULT 0)''',
    'CREATE INDEX team_command_queue ON team_execution(state)',
    'CREATE INDEX team_message_queue ON team_message_state(state)',
    *tuple(f'''CREATE TRIGGER {table}_immutable_{verb.lower()} BEFORE {verb} ON {table}
       BEGIN SELECT RAISE(ABORT,'Immutable team evidence'); END'''
       for table in ('team_commands', 'team_journal', 'team_messages') for verb in ('UPDATE', 'DELETE')),
)

INSERT_GUARDS = tuple(
    f'''CREATE TRIGGER {table}_immutable_insert BEFORE INSERT ON {table}
        WHEN EXISTS(SELECT 1 FROM {table} WHERE {condition})
        BEGIN SELECT RAISE(ABORT,'Immutable team evidence'); END'''
    for table, condition in (
        ('team_commands', 'operation_id=NEW.operation_id'),
        ('team_journal', 'id=NEW.id'),
        ('team_messages', 'id=NEW.id OR (kind=NEW.kind AND dedup_key=NEW.dedup_key)'),
    )
)


class TeamDatabase:
    def __init__(self, path: Path, *, maintenance=None, participant_id=None):
        from secretary.infrastructure.maintenance_binding import readonly_authority_uri, protect_restored_connection
        self.path = Path(path).resolve()
        self.maintenance, self.participant_id = maintenance, participant_id
        if self.path.exists():
            # Read-only identification precedes WAL/BEGIN: a wrong path must not
            # even change the header or journal mode of an unrelated SQLite file.
            with closing(sqlite3.connect(readonly_authority_uri(self.path), uri=True, timeout=.2)) as probe:
                tables = {row[0] for row in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if tables and 'team_schema' not in tables:
                    raise ValueError('not_a_team_database')
                if 'team_schema' in tables and probe.execute('SELECT MAX(version) FROM team_schema').fetchone()[0] not in (1, 2, 3, 4, 5, 6, 7, 8, SCHEMA_VERSION):
                    raise ValueError('unsupported_team_schema_version')
                if protect_restored_connection(probe):
                    return  # Restored historical schemas are diagnostics, never migration inputs.
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as conn:
            if protect_restored_connection(conn):
                return  # Guard may have been installed after the identification probe.
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='team_schema'").fetchone()
            if not exists:
                # Never graft Team tables onto an accidentally supplied unrelated database.
                if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                    raise ValueError('not_a_team_database')
                for sql in SCHEMA:
                    conn.execute(sql)
                conn.execute('INSERT INTO team_schema VALUES(1)')
            version = conn.execute('SELECT MAX(version) FROM team_schema').fetchone()[0]
            if version == 1:
                # REPLACE's implicit DELETE skips ordinary DELETE triggers when
                # recursive_triggers is OFF. Schema guards also cover raw clients.
                for sql in INSERT_GUARDS:
                    conn.execute(sql)
                conn.execute('INSERT INTO team_schema VALUES(2)')
                version = 2
            if version == 2:
                from secretary.infrastructure.team_auth_repository import AUTH_MIGRATION
                for sql in AUTH_MIGRATION:
                    conn.execute(sql)
                if conn.execute('PRAGMA foreign_key_check').fetchone():
                    raise sqlite3.IntegrityError('Auth migration foreign key failure')
                conn.execute('INSERT INTO team_schema VALUES(3)')
                version = 3
            if version == 3:
                from secretary.infrastructure.bot_repository import BOT_MIGRATION
                for sql in BOT_MIGRATION:
                    conn.execute(sql)
                if conn.execute('PRAGMA foreign_key_check').fetchone():
                    raise sqlite3.IntegrityError('Bot migration foreign key failure')
                conn.execute('INSERT INTO team_schema VALUES(4)')
                version = 4
            if version == 4:
                from secretary.infrastructure.voice_repository import VOICE_MIGRATION
                for sql in VOICE_MIGRATION:
                    conn.execute(sql)
                if conn.execute('PRAGMA foreign_key_check').fetchone():
                    raise sqlite3.IntegrityError('Voice migration foreign key failure')
                conn.execute('INSERT INTO team_schema VALUES(5)')
                version = 5
            if version == 5:
                from secretary.infrastructure.team_sync_repository import SYNC_MIGRATION
                for sql in SYNC_MIGRATION:
                    conn.execute(sql)
                if conn.execute('PRAGMA foreign_key_check').fetchone():
                    raise sqlite3.IntegrityError('Sync migration foreign key failure')
                conn.execute('INSERT INTO team_schema VALUES(6)')
                version = 6
            if version == 6:
                from secretary.infrastructure.notification_repository import NOTIFICATION_MIGRATION
                for sql in NOTIFICATION_MIGRATION:
                    conn.execute(sql)
                if conn.execute('PRAGMA foreign_key_check').fetchone():
                    raise sqlite3.IntegrityError('Notification migration foreign key failure')
                conn.execute('INSERT INTO team_schema VALUES(7)')
                version = 7
            if version == 7:
                from secretary.infrastructure.team_due_resolution_repository import DUE_RESOLUTION_MIGRATION
                for sql in DUE_RESOLUTION_MIGRATION:
                    conn.execute(sql)
                if conn.execute('PRAGMA foreign_key_check').fetchone():
                    raise sqlite3.IntegrityError('Due resolution migration foreign key failure')
                conn.execute('INSERT INTO team_schema VALUES(8)')
                version = 8
            if version == 8:
                from secretary.infrastructure.voice_repository import VOICE_ABORT_MIGRATION
                for sql in VOICE_ABORT_MIGRATION:
                    conn.execute(sql)
                if conn.execute('PRAGMA foreign_key_check').fetchone():
                    raise sqlite3.IntegrityError('Voice abort migration foreign key failure')
                conn.execute('INSERT INTO team_schema VALUES(9)')
            elif version != SCHEMA_VERSION:
                raise ValueError('unsupported_team_schema_version')

    @contextmanager
    def connection(self):
        from secretary.infrastructure.maintenance_binding import (
            admission, readonly_authority_uri, restore_guard_present, protect_restored_connection,
            RestoredDiagnosticConnection,
        )
        with admission(self.maintenance, self.participant_id, 'sql'):
            guarded = restore_guard_present((self.path,))
            conn = (sqlite3.connect(readonly_authority_uri(self.path), uri=True, timeout=15, isolation_level=None,
                                    factory=RestoredDiagnosticConnection)
                    if guarded else sqlite3.connect(self.path, timeout=15, isolation_level=None))
            conn.row_factory = sqlite3.Row
            try:
                current_guard = protect_restored_connection(conn)
                if current_guard != guarded:
                    raise sqlite3.DatabaseError('maintenance_restore_guard_changed')
                if not guarded:
                    conn.execute('PRAGMA foreign_keys=ON')
                    conn.execute('PRAGMA busy_timeout=15000')
                    conn.execute('PRAGMA journal_mode=WAL')
                yield conn
            finally:
                conn.close()

    @contextmanager
    def transaction(self):
        from secretary.infrastructure.maintenance_binding import protect_restored_connection
        with self.connection() as conn:
            conn.execute('BEGIN' if protect_restored_connection(conn) else 'BEGIN IMMEDIATE')
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
