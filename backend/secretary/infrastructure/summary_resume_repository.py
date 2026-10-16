"""Explicit processing lineage; historical requests and liabilities remain immutable."""
from __future__ import annotations

import hashlib
import json
import sqlite3

from secretary.domain.summary_context import FrozenSummaryContext
from secretary.infrastructure.assignment_repository import AssignmentRepository
from secretary.infrastructure.database import now, uid


CURRENT_JOB = "NOT EXISTS(SELECT 1 FROM summary_resume_operations r WHERE r.source_job_id=jobs.id)"


def current_job_predicate(conn):
    """Old guarded backups stay readable without creating the new family."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='summary_resume_operations'").fetchone():
        return CURRENT_JOB
    version = conn.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]
    if version is not None and version >= 10:
        raise sqlite3.DatabaseError('Summary continuation evidence is missing')
    return '1'


_ACTIVE = "('queued','running','waiting_config','paused_budget','uncertain')"
_LEAF_CONFLICT = f"""EXISTS(SELECT 1 FROM jobs j WHERE j.meeting_id=NEW.meeting_id
    AND j.version=NEW.version AND j.stage='summarize' AND j.status='uncertain' AND j.id!=NEW.id
    AND NOT EXISTS(SELECT 1 FROM summary_resume_operations r WHERE r.source_job_id=j.id))"""

SUMMARY_RESUME_MIGRATION = (
    """CREATE TABLE summary_resume_operations(
        operation_id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, transcript_version INTEGER NOT NULL,
        source_job_id TEXT NOT NULL UNIQUE, child_job_id TEXT NOT NULL UNIQUE,
        command_hash TEXT NOT NULL, context_hash TEXT NOT NULL,
        acknowledge_possible_duplicate_charge INTEGER NOT NULL CHECK(acknowledge_possible_duplicate_charge=1),
        created_at TEXT NOT NULL,
        CHECK(typeof(transcript_version)='integer' AND transcript_version>=1),
        CHECK(source_job_id!=child_job_id),CHECK(length(command_hash)=64),CHECK(length(context_hash)=64),
        CHECK(length(operation_id)=36 AND substr(operation_id,9,1)='-' AND substr(operation_id,14,1)='-'
            AND substr(operation_id,19,1)='-' AND substr(operation_id,24,1)='-'
            AND length(replace(operation_id,'-',''))=32 AND replace(operation_id,'-','') NOT GLOB '*[^0-9a-f]*'),
        FOREIGN KEY(source_job_id,meeting_id,transcript_version) REFERENCES jobs(id,meeting_id,version),
        FOREIGN KEY(child_job_id,meeting_id,transcript_version) REFERENCES jobs(id,meeting_id,version)
            DEFERRABLE INITIALLY DEFERRED)""",
    "DROP INDEX jobs_one_active",
    f"""CREATE UNIQUE INDEX jobs_one_active ON jobs(meeting_id,stage,IFNULL(chunk_id,''),version)
        WHERE stage!='identify_speakers' AND status IN {_ACTIVE}
        AND (stage!='summarize' OR status!='uncertain')""",
    *tuple(f"""CREATE TRIGGER summary_resume_immutable_{verb.lower()} BEFORE {verb} ON summary_resume_operations
        BEGIN SELECT RAISE(ABORT,'Summary continuation evidence is immutable'); END""" for verb in ('UPDATE','DELETE')),
    """CREATE TRIGGER summary_resume_source_guard BEFORE INSERT ON summary_resume_operations
        WHEN NOT EXISTS(SELECT 1 FROM jobs j JOIN summary_input_contexts c ON c.job_id=j.id
            WHERE j.id=NEW.source_job_id AND j.meeting_id=NEW.meeting_id AND j.version=NEW.transcript_version
            AND j.stage='summarize' AND j.status='uncertain' AND c.context_hash=NEW.context_hash)
        OR EXISTS(SELECT 1 FROM jobs WHERE id=NEW.child_job_id)
        OR EXISTS(SELECT 1 FROM jobs j WHERE j.meeting_id=NEW.meeting_id AND j.id!=NEW.source_job_id
            AND j.status IN ('queued','running','waiting_config','paused_budget','uncertain')
            AND NOT EXISTS(SELECT 1 FROM summary_resume_operations r WHERE r.source_job_id=j.id))
        BEGIN SELECT RAISE(ABORT,'Summary continuation source conflict'); END""",
    """CREATE TRIGGER summary_resume_child_guard BEFORE INSERT ON jobs
        WHEN EXISTS(SELECT 1 FROM summary_resume_operations r WHERE r.child_job_id=NEW.id)
        AND NOT EXISTS(SELECT 1 FROM summary_resume_operations r JOIN jobs s ON s.id=r.source_job_id
            WHERE r.child_job_id=NEW.id AND NEW.meeting_id=r.meeting_id AND NEW.version=r.transcript_version
            AND NEW.stage='summarize' AND NEW.status='queued' AND NEW.chunk_id IS NULL
            AND NEW.attempts=0 AND NEW.cancel_requested=0 AND NEW.provider_job_id IS NULL AND NEW.error IS NULL
            AND NEW.payload IS s.payload)
        BEGIN SELECT RAISE(ABORT,'Summary continuation child conflict'); END""",
    *tuple(f"""CREATE TRIGGER summary_uncertain_leaf_{verb.lower()} BEFORE {verb} ON jobs
        WHEN NEW.stage='summarize' AND NEW.status IN {_ACTIVE} AND {_LEAF_CONFLICT}
        BEGIN SELECT RAISE(ABORT,'Uncertain summary requires explicit continuation'); END""" for verb in ('INSERT','UPDATE')),
    """CREATE TRIGGER summary_resumed_source_frozen BEFORE UPDATE ON jobs
        WHEN EXISTS(SELECT 1 FROM summary_resume_operations r WHERE r.source_job_id=OLD.id)
        AND (NEW.id IS NOT OLD.id OR NEW.stage IS NOT OLD.stage OR NEW.status IS NOT OLD.status
            OR NEW.meeting_id IS NOT OLD.meeting_id OR NEW.version IS NOT OLD.version OR NEW.payload IS NOT OLD.payload)
        BEGIN SELECT RAISE(ABORT,'Historical uncertain summary is immutable'); END""",
    """CREATE TRIGGER summary_resume_child_scope_frozen BEFORE UPDATE ON jobs
        WHEN EXISTS(SELECT 1 FROM summary_resume_operations r WHERE r.child_job_id=OLD.id)
        AND (NEW.id IS NOT OLD.id OR NEW.stage IS NOT OLD.stage OR NEW.meeting_id IS NOT OLD.meeting_id
            OR NEW.version IS NOT OLD.version OR NEW.chunk_id IS NOT NULL)
        BEGIN SELECT RAISE(ABORT,'Summary continuation scope is immutable'); END""",
)


def migrate_summary_resumes(db):
    with db.transaction() as conn:
        if conn.execute('SELECT 1 FROM schema_migrations WHERE version=10').fetchone():
            return
        if not conn.execute('SELECT 1 FROM schema_migrations WHERE version=9').fetchone():
            raise sqlite3.IntegrityError('Summary continuation migration requires schema nine')
        for statement in SUMMARY_RESUME_MIGRATION:
            conn.execute(statement)
        if conn.execute('PRAGMA foreign_key_check').fetchone():
            raise sqlite3.IntegrityError('Summary continuation migration foreign key failure')
        conn.execute('INSERT INTO schema_migrations VALUES(10,?)', (now(),))


class SummaryResumeRepository:
    def __init__(self, db):
        self.db = db
        self.assignments = AssignmentRepository(db)

    def resume(self, meeting_id, command, current_settings):
        source_id, operation_id = str(command.source_job_id), str(command.operation_id)
        command_hash = hashlib.sha256(json.dumps({
            'meeting_id':meeting_id,'source_job_id':source_id,'transcript_version':command.transcript_version,
            'operation_id':operation_id,'acknowledge_possible_duplicate_charge':True,
        },sort_keys=True,separators=(',',':')).encode()).hexdigest()
        with self.db.transaction() as conn:
            existing = conn.execute('SELECT * FROM summary_resume_operations WHERE operation_id=?', (operation_id,)).fetchone()
            if existing:
                if existing['command_hash'] != command_hash:
                    raise ValueError('summary_resume_operation_conflict')
                return dict(conn.execute('SELECT * FROM jobs WHERE id=?', (existing['child_job_id'],)).fetchone())
            if conn.execute('SELECT 1 FROM summary_resume_operations WHERE source_job_id=?', (source_id,)).fetchone():
                raise ValueError('summary_already_resumed')
            meeting = conn.execute('SELECT * FROM meetings WHERE id=?', (meeting_id,)).fetchone()
            source = conn.execute('SELECT * FROM jobs WHERE id=?', (source_id,)).fetchone()
            if meeting is None or source is None:
                raise KeyError('Not found')
            if (source['meeting_id'],source['version'],source['stage'],source['status']) != (
                    meeting_id,command.transcript_version,'summarize','uncertain'):
                raise ValueError('summary_resume_source_conflict')
            if meeting['recording'] or meeting['transcript_version'] != command.transcript_version:
                raise ValueError('summary_resume_scope_changed')
            try:
                payload = json.loads(source['payload'])
                frozen = FrozenSummaryContext.model_validate(payload['summary_context'])
                stored = conn.execute('SELECT payload,context_hash FROM summary_input_contexts WHERE job_id=?', (source_id,)).fetchone()
                current = self.assignments.capture(conn,meeting_id,command.transcript_version,current_settings)
                if (stored is None or stored['context_hash'] != frozen.context_hash
                        or FrozenSummaryContext.model_validate_json(stored['payload']) != frozen
                        or current != frozen or payload['settings'] != frozen.settings.model_dump(mode='json')):
                    raise ValueError
            except (ValueError,TypeError,KeyError):
                raise ValueError('summary_resume_context_changed') from None
            if conn.execute(f"SELECT 1 FROM jobs WHERE meeting_id=? AND id!=? AND status IN {_ACTIVE} AND {CURRENT_JOB}",
                            (meeting_id,source_id)).fetchone():
                raise ValueError('summary_resume_active_processing')
            child_id, stamp = uid(), now()
            conn.execute('INSERT INTO summary_resume_operations VALUES(?,?,?,?,?,?,?,?,?)',
                (operation_id,meeting_id,command.transcript_version,source_id,child_id,command_hash,frozen.context_hash,1,stamp))
            conn.execute("""INSERT INTO jobs(id,meeting_id,stage,status,created_at,updated_at,version,payload)
                VALUES(?,?,'summarize','queued',?,?,?,?)""",
                (child_id,meeting_id,stamp,stamp,command.transcript_version,source['payload']))
            child = dict(conn.execute('SELECT * FROM jobs WHERE id=?', (child_id,)).fetchone())
            self.assignments.bind_job(conn,child)
            conn.execute('INSERT INTO summary_checkpoints(job_id,key,payload) SELECT ?,key,payload FROM summary_checkpoints WHERE job_id=?',
                         (child_id,source_id))
            conn.execute("UPDATE meetings SET status='queued',error=NULL,updated_at=? WHERE id=?", (stamp,meeting_id))
            return child
