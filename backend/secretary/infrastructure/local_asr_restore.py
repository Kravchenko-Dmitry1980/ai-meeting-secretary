"""Restore-time invalidation of local execution authority, preserving evidence."""
from __future__ import annotations

import sqlite3

from secretary.infrastructure.restore_quarantine_catalogue import (
    EXPECTED_COLUMNS, LOCAL_ASR_FAMILY_DISPOSITIONS,
)


def quarantine_restored_local_asr(conn: sqlite3.Connection) -> None:
    """Called inside an existing restore transaction, never against live data.

    Full source manifests, review history, plans, qualification receipts and
    attempts remain evidence. A restored unfinished job cannot be made runnable
    by accepting its historical qualification or letting its old lease expire.
    """
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    group = set(LOCAL_ASR_FAMILY_DISPOSITIONS)
    if not tables & group:
        return
    if not group <= tables:
        raise sqlite3.DatabaseError("local_asr_restore_schema_unsupported")
    for table in group:
        columns = tuple(row[1] for row in conn.execute(f'PRAGMA table_info("{table}")'))
        if columns != EXPECTED_COLUMNS["secretary", table]:
            raise sqlite3.DatabaseError("local_asr_restore_schema_unsupported")
    if [row[0] for row in conn.execute("SELECT version FROM local_asr_schema")] != [1]:
        raise sqlite3.DatabaseError("local_asr_restore_schema_unsupported")
    conn.execute("""UPDATE jobs SET status='cancelled',cancel_requested=1,error='local_asr_restore_review_required'
        WHERE id IN (SELECT job_id FROM local_asr_executions
                     WHERE state NOT IN ('applied','cancelled','timed_out','failed','restore_blocked'))""")
    conn.execute("""UPDATE local_asr_executions SET state='restore_blocked',reason='local_asr_restore_review_required'
        WHERE state NOT IN ('applied','cancelled','timed_out','failed','restore_blocked')""")
