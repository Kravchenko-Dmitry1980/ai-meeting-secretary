"""A diagnostic connection cannot mutate another authority through ATTACH."""
from contextlib import closing
import hashlib
import sqlite3
from uuid import uuid4

import pytest

from secretary.domain.cloud_budget import BudgetError
from secretary.infrastructure.budget_repository import BudgetRepository
from secretary.infrastructure.database import Database
from secretary.infrastructure.team_database import TeamDatabase
from secretary.infrastructure.team_backup import _restore_guard


@pytest.mark.parametrize('repository_type', [Database, TeamDatabase, BudgetRepository])
@pytest.mark.parametrize('callback', [None, lambda *args: sqlite3.SQLITE_OK], ids=['cleared', 'allow-all'])
def test_clearing_diagnostic_authorizer_cannot_write_an_attached_restored_authority(tmp_path, repository_type, callback):
    path = tmp_path / 'main.sqlite3'
    repository = repository_type(path)
    target = tmp_path / 'second-authority.sqlite3'
    with closing(sqlite3.connect(target)) as conn, conn:
        conn.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY,status TEXT)')
        conn.execute("INSERT INTO jobs VALUES('legacy-job','queued')")
        _restore_guard(conn, str(uuid4()), 'b' * 64)
    with closing(sqlite3.connect(path)) as conn, conn:
        _restore_guard(conn, str(uuid4()), 'a' * 64)
    before = hashlib.sha256(target.read_bytes()).hexdigest()
    open_connection = repository.transaction if repository_type is BudgetRepository else repository.connection
    denied = False
    try:
        with open_connection() as conn:
            conn.set_authorizer(callback)
            conn.execute('ATTACH DATABASE ? AS secondary', (str(target),))
            conn.execute("UPDATE secondary.jobs SET status='running' WHERE id='legacy-job'")
            conn.commit()
    except (sqlite3.DatabaseError, BudgetError):
        denied = True
    assert hashlib.sha256(target.read_bytes()).hexdigest() == before, 'diagnostics changed a second restored authority'
    assert denied, 'diagnostic authorizer removal enabled cross-authority write'
