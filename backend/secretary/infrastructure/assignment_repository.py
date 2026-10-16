"""Append-only assignment snapshots and atomic human review of local evidence."""
from __future__ import annotations

import json

from secretary.application.assignments import (
    locally_revalidate, project_summary, resolve_summary, review_batch, snapshot,
)
from secretary.application.summary_context import assignment_context, freeze_summary_context
from secretary.domain.summary_context import AssignmentReviewResult, AssignmentSnapshot, FrozenSummaryContext
from secretary.domain.speakers import SpeakerConflict, SpeakerNotFound
from secretary.infrastructure.database import now, uid, MEETING_FIELDS
from secretary.infrastructure.speaker_repository import SpeakerRepository, canonical


ASSIGNMENT_MIGRATION = (
    """CREATE TABLE summary_input_contexts(job_id TEXT PRIMARY KEY REFERENCES jobs(id),
        meeting_id TEXT NOT NULL REFERENCES meetings(id),transcript_version INTEGER NOT NULL,
        contract TEXT NOT NULL,payload TEXT NOT NULL CHECK(json_valid(payload)),context_hash TEXT NOT NULL)""",
    """CREATE TABLE summary_publications(job_id TEXT PRIMARY KEY REFERENCES jobs(id),
        meeting_id TEXT NOT NULL,summary_version INTEGER NOT NULL,
        FOREIGN KEY(meeting_id,summary_version) REFERENCES summaries(meeting_id,summary_version))""",
    """CREATE TABLE legacy_summary_inputs(job_id TEXT PRIMARY KEY REFERENCES jobs(id),payload TEXT NOT NULL CHECK(json_valid(payload)))""",
    """CREATE TABLE assignment_snapshots(meeting_id TEXT NOT NULL,summary_version INTEGER NOT NULL,
        revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>=0),payload TEXT NOT NULL CHECK(json_valid(payload)),
        PRIMARY KEY(meeting_id,summary_version,revision),
        FOREIGN KEY(meeting_id,summary_version) REFERENCES summaries(meeting_id,summary_version))""",
    """CREATE TABLE task_assignment_state(meeting_id TEXT NOT NULL,summary_version INTEGER NOT NULL,revision INTEGER NOT NULL,
        PRIMARY KEY(meeting_id,summary_version),
        FOREIGN KEY(meeting_id,summary_version,revision) REFERENCES assignment_snapshots(meeting_id,summary_version,revision))""",
    *tuple(f"CREATE TRIGGER {table}_immutable_{verb.lower()} BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT,'Summary evidence is immutable'); END"
           for table in ('summary_input_contexts','summary_publications','legacy_summary_inputs','assignment_snapshots','task_assignments')
           for verb in ('UPDATE','DELETE')),
    """CREATE TRIGGER frozen_summary_job_payload BEFORE UPDATE OF payload ON jobs
        WHEN (EXISTS(SELECT 1 FROM summary_input_contexts WHERE job_id=OLD.id) OR EXISTS(SELECT 1 FROM legacy_summary_inputs WHERE job_id=OLD.id))
        AND (json_extract(NEW.payload,'$.summary_context') IS NOT json_extract(OLD.payload,'$.summary_context')
             OR json_extract(NEW.payload,'$.settings') IS NOT json_extract(OLD.payload,'$.settings')
             OR json_extract(NEW.payload,'$.legacy_sources') IS NOT json_extract(OLD.payload,'$.legacy_sources')
             OR json_extract(NEW.payload,'$.summary_contract') IS NOT json_extract(OLD.payload,'$.summary_contract'))
        BEGIN SELECT RAISE(ABORT,'Paid summary input is immutable'); END""",
)


class AssignmentRepository:
    def __init__(self, db):
        self.db = db
        self.speakers = SpeakerRepository(db)

    @staticmethod
    def sources(conn, meeting_id, version):
        return [dict(row) for row in conn.execute("""SELECT s.* FROM segments s JOIN chunks c ON c.id=s.chunk_id
            WHERE s.meeting_id=? AND s.transcript_version=?
            ORDER BY COALESCE(s.start_ms,c.offset_ms),c.channel,c.sequence,s.ordinal,s.id""", (meeting_id,version))]

    def capture(self, conn, meeting_id, version, settings):
        attribution = self.speakers._attribution(conn, meeting_id, version).model_dump(mode='json')
        state = conn.execute('SELECT run_id FROM attribution_state WHERE meeting_id=? AND transcript_version=?', (meeting_id,version)).fetchone()
        attribution['run_id'] = state['run_id'] if state else None
        return freeze_summary_context(meeting_id=meeting_id,transcript_version=version,settings=settings,
            segments=self.sources(conn,meeting_id,version),attribution=attribution)

    def context(self, conn, summary):
        # These settings are not provider input: the assignment-context hash
        # contains only evidence, identity and explicit meeting roster.
        frozen = self.capture(conn,summary['meeting_id'],summary['transcript_version'],
            {'summary_model':'','summary_batch_chars':12000,'summary_max_output_tokens':8192})
        return assignment_context(frozen,summary['summary_version'])

    @staticmethod
    def bind_job(conn, job):
        payload = json.loads(job['payload']) if job.get('payload') else {}
        if 'summary_context' not in payload:
            return
        frozen = FrozenSummaryContext.model_validate(payload['summary_context'])
        if (frozen.meeting_id,frozen.transcript_version) != (job['meeting_id'],job['version']):
            raise SpeakerConflict('summary_context_scope_mismatch')
        conn.execute('INSERT INTO summary_input_contexts VALUES(?,?,?,?,?,?)',
            (job['id'],job['meeting_id'],job['version'],frozen.contract,frozen.model_dump_json(),frozen.context_hash))

    @staticmethod
    def _summary(conn, meeting_id, summary_version=None):
        if not conn.execute('SELECT 1 FROM meetings WHERE id=?',(meeting_id,)).fetchone():
            raise SpeakerNotFound('Meeting not found')
        if summary_version is None:
            row = conn.execute('''SELECT payload FROM summaries WHERE meeting_id=?
                AND transcript_version=(SELECT transcript_version FROM meetings WHERE id=?) ORDER BY summary_version DESC LIMIT 1''', (meeting_id,meeting_id)).fetchone()
        else:
            row = conn.execute('SELECT payload FROM summaries WHERE meeting_id=? AND summary_version=?',(meeting_id,summary_version)).fetchone()
        if row is None:
            raise SpeakerNotFound('Summary not found')
        return json.loads(row['payload'])

    @staticmethod
    def _state(conn, meeting_id, summary_version, revision=None):
        if revision is None:
            row = conn.execute('''SELECT s.payload FROM assignment_snapshots s JOIN task_assignment_state t
                USING(meeting_id,summary_version,revision) WHERE meeting_id=? AND summary_version=?''',(meeting_id,summary_version)).fetchone()
        else:
            row = conn.execute('SELECT payload FROM assignment_snapshots WHERE meeting_id=? AND summary_version=? AND revision=?',
                (meeting_id,summary_version,revision)).fetchone()
        return AssignmentSnapshot.model_validate_json(row['payload']) if row else None

    def _view(self, conn, summary, revision=None):
        state = self._state(conn,summary['meeting_id'],summary['summary_version'],revision)
        if state is not None and revision is not None:
            return state,None  # immutable historical proof does not need live source reads
        context = self.context(conn,summary)
        if state is None:
            if revision not in (None,0):
                raise SpeakerNotFound('Assignment revision not found')
            state = resolve_summary(summary,context,context,captured_context_hash=None)
        elif revision is None:
            current = locally_revalidate(state,context)
            # Read-only stale view keeps actual durable CAS revision. Mutation
            # hooks publish revalidation; GET never seeds or advances state.
            state = snapshot(context,current.items,revision=state.revision,captured_context_hash=state.captured_context_hash)
        return state,context

    def get(self, meeting_id, summary_version=None, revision=None):
        with self.db.connection() as conn:
            conn.execute('BEGIN')
            summary = self._summary(conn,meeting_id,summary_version)
            return self._view(conn,summary,revision)[0]

    def publication_source(self, conn, scope):
        """Read current publication evidence inside the caller's source transaction."""
        if not conn.in_transaction:
            raise SpeakerConflict('publication_source_transaction_required')
        meeting = self.speakers._meeting(conn, scope.meeting_id)
        if meeting['transcript_version'] != scope.transcript_version:
            raise SpeakerConflict('summary_scope_changed')
        latest = conn.execute('''SELECT MAX(summary_version) FROM summaries
            WHERE meeting_id=? AND transcript_version=?''',
            (scope.meeting_id, scope.transcript_version)).fetchone()[0]
        if latest != scope.summary_version:
            raise SpeakerConflict('summary_scope_changed')
        summary = self._summary(conn, scope.meeting_id)
        if (summary['meeting_id'], summary['transcript_version'], summary['summary_version']) != (
                scope.meeting_id, scope.transcript_version, scope.summary_version):
            raise SpeakerConflict('summary_scope_changed')
        return self._view(conn, summary)

    def projected(self, meeting_id):
        with self.db.connection() as conn:
            conn.execute('BEGIN')
            summary = self._summary(conn,meeting_id)
            state,context = self._view(conn,summary)
            return project_summary(summary,state,context)

    @staticmethod
    def persist(conn, state):
        conn.execute('INSERT INTO assignment_snapshots VALUES(?,?,?,?)',
            (state.meeting_id,state.summary_version,state.revision,state.model_dump_json()))
        for item in state.items:
            conn.execute('INSERT INTO task_assignments VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', (
                state.meeting_id,state.summary_version,item.action.action_id,state.revision,item.participant_id,
                item.basis,canonical(item.action.source_segment_ids),item.action.evidence_quote,
                item.attribution_revision,item.roster_revision,item.status,item.action.named_owner_text))
        conn.execute('''INSERT INTO task_assignment_state VALUES(?,?,?) ON CONFLICT(meeting_id,summary_version)
            DO UPDATE SET revision=excluded.revision''',(state.meeting_id,state.summary_version,state.revision))

    def review(self, meeting_id, command):
        scope = canonical([meeting_id,command.transcript_version,command.summary_version])
        with self.db.transaction() as conn:
            receipt,digest = self.speakers._replay(conn,'review_assignments',scope,command,AssignmentSnapshot)
            if receipt is not None:
                summary = self._summary(conn,meeting_id,command.summary_version)
                current,_ = self._view(conn,summary)
                meeting = self.speakers._meeting(conn,meeting_id)
                latest = conn.execute('SELECT MAX(summary_version) FROM summaries WHERE meeting_id=? AND transcript_version=?',
                    (meeting_id,meeting['transcript_version'])).fetchone()[0]
                scope_stale = (meeting['transcript_version'],latest) != (command.transcript_version,command.summary_version)
                return AssignmentReviewResult(operation_id=command.operation_id,receipt=receipt,current=current,
                    replayed=True,receipt_stale=scope_stale or receipt != current)
            meeting = self.speakers._meeting(conn,meeting_id)
            if meeting['transcript_version'] != command.transcript_version:
                raise SpeakerConflict('summary_scope_changed')
            summary = self._summary(conn,meeting_id)
            if (summary['transcript_version'],summary['summary_version']) != (command.transcript_version,command.summary_version):
                raise SpeakerConflict('summary_scope_changed')
            state,context = self._view(conn,summary)
            if (command.expected_revision,command.attribution_revision,command.roster_revision) != (
                    state.revision,context.attribution_revision,context.roster_revision):
                raise SpeakerConflict('assignment_revision_changed')
            result = review_batch(state,command,context)
            if self._state(conn,meeting_id,summary['summary_version']) is None:
                self.persist(conn,state)  # legacy seed only inside human mutation
            self.persist(conn,result)
            self.speakers._receipt(conn,'review_assignments',scope,command,digest,result,
                [change.action_id for change in command.changes],result.revision,meeting_id,summary['transcript_version'])
            return AssignmentReviewResult(operation_id=command.operation_id,receipt=result,current=result)

    def revalidate(self, conn, meeting_id, version=None):
        row = conn.execute('''SELECT s.payload FROM summaries s JOIN task_assignment_state t
            ON s.meeting_id=t.meeting_id AND s.summary_version=t.summary_version WHERE s.meeting_id=?
            AND s.transcript_version=(SELECT transcript_version FROM meetings WHERE id=?) ORDER BY s.summary_version DESC LIMIT 1''',(meeting_id,meeting_id)).fetchone()
        if row is None:
            return
        summary = json.loads(row['payload'])
        if version is not None and summary['transcript_version'] != version:
            return
        old = self._state(conn,meeting_id,summary['summary_version'])
        if old is None:
            return  # legacy remains read-only until a human decision
        current = locally_revalidate(old,self.context(conn,summary))
        if current != old:
            self.persist(conn,current)

    def publish(self, conn, job, summary):
        record = conn.execute('SELECT payload FROM summary_input_contexts WHERE job_id=?',(job['id'],)).fetchone()
        current = self.context(conn,summary)
        frozen = FrozenSummaryContext.model_validate_json(record['payload']) if record else None
        captured = assignment_context(frozen,summary['summary_version']) if frozen else current
        old = conn.execute('SELECT payload FROM summaries WHERE meeting_id=? AND summary_version<? ORDER BY summary_version DESC LIMIT 1',
            (job['meeting_id'],summary['summary_version'])).fetchone()
        previous = self._state(conn,job['meeting_id'],json.loads(old['payload'])['summary_version']) if old else None
        state = resolve_summary(summary,captured,current,captured_context_hash=frozen.context_hash if frozen else None,previous=previous)
        self.persist(conn,state)
        conn.execute('INSERT INTO summary_publications VALUES(?,?,?)',(job['id'],job['meeting_id'],summary['summary_version']))

    def export_snapshot(self, meeting_id):
        with self.db.connection() as conn:
            conn.execute('BEGIN')
            self.speakers._meeting(conn,meeting_id)  # scoped existence guard
            meeting = dict(conn.execute(f'SELECT {MEETING_FIELDS} FROM meetings WHERE id=?',(meeting_id,)).fetchone())
            segments = self.sources(conn,meeting_id,meeting['transcript_version'])
            row = conn.execute('SELECT payload FROM summaries WHERE meeting_id=? AND transcript_version=? ORDER BY summary_version DESC LIMIT 1',
                (meeting_id,meeting['transcript_version'])).fetchone()
            if row is None:
                return meeting,segments,None,None,None,None
            raw = json.loads(row['payload'])
            state,context = self._view(conn,raw)
            paid = conn.execute('''SELECT c.payload FROM summary_input_contexts c JOIN summary_publications p USING(job_id)
                WHERE p.meeting_id=? AND p.summary_version=?''',(meeting_id,raw['summary_version'])).fetchone()
            return meeting,segments,project_summary(raw,state,context),state,raw,json.loads(paid['payload']) if paid else None


def revalidate_local(conn, db, meeting_id, version=None):
    AssignmentRepository(db).revalidate(conn,meeting_id,version)


def revalidate_voice_overlays(conn, db):
    # Current summaries only; historical raw summaries and global roster names
    # are never rewritten. No provider/worker/queue is reachable from this hook.
    for row in conn.execute('''SELECT DISTINCT t.meeting_id FROM task_assignment_state t JOIN summaries s
        ON s.meeting_id=t.meeting_id AND s.summary_version=t.summary_version JOIN meetings m ON m.id=s.meeting_id
        WHERE s.transcript_version=m.transcript_version''').fetchall():
        revalidate_local(conn,db,row['meeting_id'])
