from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4, uuid5, NAMESPACE_URL


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def uid() -> str:
    return str(uuid4())


def stable_id(*parts) -> str:
    return str(uuid5(NAMESPACE_URL, "/".join(map(str, parts))))


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meetings(id TEXT PRIMARY KEY,title TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,transcript_version INTEGER NOT NULL DEFAULT 0,duration_ms INTEGER,error TEXT,media_path TEXT,recording INTEGER NOT NULL DEFAULT 0,auto_process INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS chunks(id TEXT PRIMARY KEY,meeting_id TEXT NOT NULL REFERENCES meetings(id),sequence INTEGER NOT NULL,channel TEXT NOT NULL,path TEXT NOT NULL,offset_ms INTEGER NOT NULL,duration_ms INTEGER NOT NULL,status TEXT NOT NULL,sha256 TEXT NOT NULL,UNIQUE(meeting_id,sequence,channel));
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,meeting_id TEXT NOT NULL REFERENCES meetings(id),stage TEXT NOT NULL,status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,chunk_id TEXT,version INTEGER NOT NULL DEFAULT 0,provider_job_id TEXT,cancel_requested INTEGER NOT NULL DEFAULT 0,payload TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_active ON jobs(meeting_id,stage,IFNULL(chunk_id,''),version) WHERE status IN ('queued','running','waiting_config','paused_budget','uncertain');
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status,created_at);
CREATE TABLE IF NOT EXISTS segments(id TEXT PRIMARY KEY,meeting_id TEXT NOT NULL REFERENCES meetings(id),chunk_id TEXT NOT NULL,transcript_version INTEGER NOT NULL,ordinal INTEGER NOT NULL,start_ms INTEGER,end_ms INTEGER,timing_precision TEXT NOT NULL,text TEXT NOT NULL,confidence REAL,speaker_id TEXT,channel TEXT NOT NULL,UNIQUE(chunk_id,transcript_version,ordinal));
CREATE INDEX IF NOT EXISTS segments_meeting ON segments(meeting_id,transcript_version);
CREATE TABLE IF NOT EXISTS speakers(id TEXT PRIMARY KEY,meeting_id TEXT NOT NULL,chunk_id TEXT NOT NULL,provider_label TEXT NOT NULL,display_name TEXT,UNIQUE(chunk_id,provider_label));
CREATE TABLE IF NOT EXISTS summaries(meeting_id TEXT NOT NULL,transcript_version INTEGER NOT NULL,summary_version INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(meeting_id,summary_version));
CREATE TABLE IF NOT EXISTS usage(id TEXT PRIMARY KEY,meeting_id TEXT NOT NULL,job_id TEXT NOT NULL,kind TEXT NOT NULL,estimated_rub REAL,confirmed_rub REAL,status TEXT NOT NULL,provider_request_id TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS configuration(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS summary_checkpoints(job_id TEXT NOT NULL,key TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(job_id,key));
"""

MEETING_FIELDS = "id,title,status,created_at,updated_at,transcript_version,duration_ms,COALESCE(capture_error,error) AS error,processing_mode"
JOB_FIELDS = "id,meeting_id,stage,status,attempts,error,created_at,updated_at,version,chunk_id,intent_key,(SELECT run_id FROM identification_intents i WHERE i.job_id=jobs.id) AS run_id,(SELECT outcome FROM identification_intents i WHERE i.job_id=jobs.id) AS local_outcome"

IDENTIFICATION_MIGRATION = (
    "ALTER TABLE meetings ADD COLUMN processing_mode TEXT NOT NULL DEFAULT 'ordinary' CHECK(processing_mode IN ('ordinary','voice_identification'))",
    "ALTER TABLE jobs ADD COLUMN intent_key TEXT",
    "DROP INDEX jobs_one_active",
    "CREATE UNIQUE INDEX jobs_one_active ON jobs(meeting_id,stage,IFNULL(chunk_id,''),version) WHERE stage!='identify_speakers' AND status IN ('queued','running','waiting_config','paused_budget','uncertain')",
    "CREATE UNIQUE INDEX jobs_identification_intent ON jobs(meeting_id,version,intent_key) WHERE stage='identify_speakers'",
    """CREATE TABLE meeting_transcript_routes(meeting_id TEXT NOT NULL REFERENCES meetings(id),
        transcript_version INTEGER NOT NULL CHECK(transcript_version>=1),processing_mode TEXT NOT NULL,
        route TEXT NOT NULL CHECK(json_valid(route)),route_hash TEXT NOT NULL,created_at TEXT NOT NULL,
        PRIMARY KEY(meeting_id,transcript_version))""",
    "CREATE TRIGGER transcript_route_no_update BEFORE UPDATE ON meeting_transcript_routes BEGIN SELECT RAISE(ABORT,'Transcript routes are immutable'); END",
    "CREATE TRIGGER transcript_route_no_delete BEFORE DELETE ON meeting_transcript_routes BEGIN SELECT RAISE(ABORT,'Transcript routes are immutable'); END",
    """CREATE TRIGGER meeting_mode_frozen BEFORE UPDATE OF processing_mode ON meetings
        WHEN NEW.processing_mode!=OLD.processing_mode AND EXISTS(SELECT 1 FROM meeting_transcript_routes WHERE meeting_id=OLD.id)
        BEGIN SELECT RAISE(ABORT,'Processing mode is bound'); END""",
    """CREATE TABLE identification_intents(id TEXT PRIMARY KEY,meeting_id TEXT NOT NULL REFERENCES meetings(id),
        transcript_version INTEGER NOT NULL,intent_key TEXT NOT NULL,run_id TEXT NOT NULL UNIQUE REFERENCES attribution_runs(id),
        job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),snapshot TEXT NOT NULL CHECK(json_valid(snapshot)),
        outcome TEXT NOT NULL DEFAULT 'pending',reasons TEXT NOT NULL DEFAULT '[]',progress TEXT NOT NULL DEFAULT '{}',
        pipeline INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,UNIQUE(meeting_id,transcript_version,intent_key))""",
    """CREATE TABLE identification_proposals(intent_id TEXT NOT NULL REFERENCES identification_intents(id),
        segment_id TEXT NOT NULL,group_id TEXT NOT NULL,participant_id TEXT,method TEXT NOT NULL,status TEXT NOT NULL,
        raw_score REAL,reason_codes TEXT NOT NULL,review_candidates TEXT NOT NULL,
        PRIMARY KEY(intent_id,segment_id))""",
    """CREATE TRIGGER identification_snapshot_frozen BEFORE UPDATE OF snapshot,meeting_id,transcript_version,intent_key,run_id,job_id ON identification_intents
        BEGIN SELECT RAISE(ABORT,'Identification snapshots are immutable'); END""",
    """CREATE TABLE identification_bypasses(meeting_id TEXT NOT NULL REFERENCES meetings(id),transcript_version INTEGER NOT NULL,
        attribution_revision INTEGER NOT NULL,roster_revision INTEGER NOT NULL,operation_id TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,PRIMARY KEY(meeting_id,transcript_version))""",
    "CREATE TRIGGER voice_profile_insert AFTER INSERT ON person_profiles BEGIN UPDATE voice_state SET revision=revision+1 WHERE id=1; END",
    "CREATE TRIGGER voice_enrollment_insert AFTER INSERT ON voice_enrollments BEGIN UPDATE voice_state SET revision=revision+1 WHERE id=1; END",
    "CREATE UNIQUE INDEX jobs_identity_scope ON jobs(id,meeting_id,version)",
    """CREATE TABLE meeting_pipeline_handoffs(meeting_id TEXT NOT NULL REFERENCES meetings(id),transcript_version INTEGER NOT NULL,
        summary_authorized INTEGER NOT NULL CHECK(summary_authorized IN (0,1)),authorization_reason TEXT NOT NULL,
        summary_job_id TEXT,created_at TEXT NOT NULL,PRIMARY KEY(meeting_id,transcript_version),
        FOREIGN KEY(summary_job_id,meeting_id,transcript_version) REFERENCES jobs(id,meeting_id,version))""",
    """CREATE TRIGGER first_summary_job_frozen BEFORE UPDATE OF summary_job_id ON meeting_pipeline_handoffs
        WHEN OLD.summary_job_id IS NOT NULL AND NEW.summary_job_id IS NOT OLD.summary_job_id
        BEGIN SELECT RAISE(ABORT,'First summary association is immutable'); END""",
    "CREATE UNIQUE INDEX identification_intent_scope ON identification_intents(id,meeting_id,transcript_version)",
    """CREATE TABLE attribution_proposal_lineage(run_id TEXT NOT NULL,segment_id TEXT NOT NULL,meeting_id TEXT NOT NULL,
        transcript_version INTEGER NOT NULL,revision INTEGER NOT NULL,source_intent_id TEXT NOT NULL,PRIMARY KEY(run_id,segment_id),
        FOREIGN KEY(run_id,meeting_id,transcript_version,revision) REFERENCES attribution_runs(id,meeting_id,transcript_version,revision),
        FOREIGN KEY(segment_id,meeting_id,transcript_version) REFERENCES segments(id,meeting_id,transcript_version),
        FOREIGN KEY(source_intent_id,meeting_id,transcript_version) REFERENCES identification_intents(id,meeting_id,transcript_version),
        FOREIGN KEY(source_intent_id,segment_id) REFERENCES identification_proposals(intent_id,segment_id))""",
    "CREATE TRIGGER proposal_lineage_no_update BEFORE UPDATE ON attribution_proposal_lineage BEGIN SELECT RAISE(ABORT,'Proposal lineage is immutable'); END",
    "CREATE TRIGGER proposal_lineage_no_delete BEFORE DELETE ON attribution_proposal_lineage BEGIN SELECT RAISE(ABORT,'Proposal lineage is immutable'); END",
)

# Executed statement by statement inside one explicit transaction. Never use
# executescript here: sqlite3 may commit the transaction before running its DDL.
SPEAKER_MIGRATION = (
    "CREATE UNIQUE INDEX segments_identity_scope ON segments(id,meeting_id,transcript_version)",
    """CREATE TABLE person_profiles(
        id TEXT PRIMARY KEY,display_name TEXT NOT NULL CHECK(length(trim(display_name))>0),
        aliases TEXT NOT NULL CHECK(json_valid(aliases) AND json_type(aliases)='array'),
        enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>=0),
        created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""",
    """CREATE TABLE voice_enrollments(
        id TEXT PRIMARY KEY,person_profile_id TEXT NOT NULL REFERENCES person_profiles(id),
        material_version INTEGER NOT NULL CHECK(typeof(material_version)='integer' AND material_version>=0),
        revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>=0),
        consent_confirmed INTEGER NOT NULL CHECK(consent_confirmed IN (0,1)),
        status TEXT NOT NULL CHECK(status IN ('pending','awaiting_review','ready','revoked','failed','interrupted','cancelled','cleanup_pending')),
        model_id TEXT,model_revision TEXT,storage_key TEXT,private_material TEXT CHECK(private_material IS NULL OR json_valid(private_material)),
        created_at TEXT NOT NULL,revoked_at TEXT,
        CHECK(status!='ready' OR consent_confirmed=1),UNIQUE(person_profile_id,material_version))""",
    """CREATE TABLE meeting_participants(
        id TEXT PRIMARY KEY,meeting_id TEXT NOT NULL REFERENCES meetings(id),
        person_profile_id TEXT REFERENCES person_profiles(id),
        display_name TEXT NOT NULL CHECK(length(trim(display_name))>0),
        aliases TEXT NOT NULL CHECK(json_valid(aliases) AND json_type(aliases)='array'),
        enabled INTEGER NOT NULL CHECK(enabled IN (0,1)),profile_revision INTEGER CHECK(profile_revision IS NULL OR (typeof(profile_revision)='integer' AND profile_revision>=0)),
        created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(id,meeting_id),
        CHECK((person_profile_id IS NULL AND profile_revision IS NULL) OR
              (person_profile_id IS NOT NULL AND profile_revision IS NOT NULL)))""",
    "CREATE UNIQUE INDEX meeting_profile_once ON meeting_participants(meeting_id,person_profile_id) WHERE person_profile_id IS NOT NULL",
    """CREATE TABLE participant_roster_state(
        meeting_id TEXT PRIMARY KEY REFERENCES meetings(id),revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>=0))""",
    """CREATE TABLE attribution_runs(
        id TEXT PRIMARY KEY,meeting_id TEXT NOT NULL REFERENCES meetings(id),
        transcript_version INTEGER NOT NULL CHECK(typeof(transcript_version)='integer' AND transcript_version>=0),
        expected_revision INTEGER NOT NULL CHECK(typeof(expected_revision)='integer' AND expected_revision>=0),
        roster_revision INTEGER NOT NULL CHECK(typeof(roster_revision)='integer' AND roster_revision>=0),revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>=0),
        kind TEXT NOT NULL CHECK(kind IN ('manual','automatic')),
        status TEXT NOT NULL CHECK(status IN ('pending','published','stale','failed')),
        profile_snapshot TEXT NOT NULL CHECK(json_valid(profile_snapshot) AND json_type(profile_snapshot)='array'),
        roster_snapshot TEXT NOT NULL CHECK(json_valid(roster_snapshot) AND json_type(roster_snapshot)='array'),
        audio_hash TEXT,model_id TEXT,model_revision TEXT,algorithm_version TEXT,created_at TEXT NOT NULL,
        UNIQUE(id,meeting_id,transcript_version,revision),
        CHECK(kind!='manual' OR (audio_hash IS NULL AND model_id IS NULL AND model_revision IS NULL)))""",
    """CREATE TABLE speaker_attributions(
        run_id TEXT NOT NULL,segment_id TEXT NOT NULL,meeting_id TEXT NOT NULL,
        transcript_version INTEGER NOT NULL CHECK(typeof(transcript_version)='integer' AND transcript_version>=0),
        participant_id TEXT,method TEXT NOT NULL CHECK(method IN ('voice_embedding','provider_reference','manual','unknown')),
        raw_score REAL CHECK(raw_score IS NULL OR (raw_score>=-1.7976931348623157e308 AND raw_score<=1.7976931348623157e308)),
        status TEXT NOT NULL CHECK(status IN ('proposed','confirmed','unknown','conflict')),
        reason_codes TEXT NOT NULL CHECK(json_valid(reason_codes) AND json_type(reason_codes)='array'),
        revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>=0),PRIMARY KEY(run_id,segment_id),
        FOREIGN KEY(run_id,meeting_id,transcript_version,revision) REFERENCES attribution_runs(id,meeting_id,transcript_version,revision),
        FOREIGN KEY(segment_id,meeting_id,transcript_version) REFERENCES segments(id,meeting_id,transcript_version),
        FOREIGN KEY(participant_id,meeting_id) REFERENCES meeting_participants(id,meeting_id),
        CHECK(status!='confirmed' OR participant_id IS NOT NULL),
        CHECK(method!='unknown' OR (participant_id IS NULL AND raw_score IS NULL)))""",
    """CREATE TABLE attribution_state(
        meeting_id TEXT NOT NULL REFERENCES meetings(id),transcript_version INTEGER NOT NULL CHECK(typeof(transcript_version)='integer' AND transcript_version>=0),
        revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>=0),run_id TEXT NOT NULL,
        PRIMARY KEY(meeting_id,transcript_version),
        FOREIGN KEY(run_id,meeting_id,transcript_version,revision) REFERENCES attribution_runs(id,meeting_id,transcript_version,revision))""",
    """CREATE TABLE task_assignments(
        meeting_id TEXT NOT NULL,summary_version INTEGER NOT NULL CHECK(typeof(summary_version)='integer' AND summary_version>=0),action_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>=0),participant_id TEXT,
        basis TEXT NOT NULL CHECK(basis IN ('named_person','self_commitment','manual','unknown')),
        source_segment_ids TEXT NOT NULL CHECK(json_valid(source_segment_ids) AND json_type(source_segment_ids)='array'),
        evidence_quote TEXT NOT NULL,attribution_revision INTEGER NOT NULL CHECK(typeof(attribution_revision)='integer' AND attribution_revision>=0),
        roster_revision INTEGER NOT NULL CHECK(typeof(roster_revision)='integer' AND roster_revision>=0),
        status TEXT NOT NULL CHECK(status IN ('proposed','confirmed','needs_review')),named_owner_text TEXT,
        PRIMARY KEY(meeting_id,summary_version,action_id,revision),
        FOREIGN KEY(meeting_id,summary_version) REFERENCES summaries(meeting_id,summary_version),
        FOREIGN KEY(participant_id,meeting_id) REFERENCES meeting_participants(id,meeting_id),
        CHECK(status!='confirmed' OR participant_id IS NOT NULL))""",
    """CREATE TABLE speaker_operations(
        operation_id TEXT PRIMARY KEY,kind TEXT NOT NULL,scope TEXT NOT NULL,request_hash TEXT NOT NULL,
        response TEXT NOT NULL CHECK(json_valid(response)),created_at TEXT NOT NULL)""",
    """CREATE TABLE speaker_audit(
        id TEXT PRIMARY KEY,operation_id TEXT NOT NULL REFERENCES speaker_operations(operation_id),
        kind TEXT NOT NULL,meeting_id TEXT REFERENCES meetings(id),transcript_version INTEGER,
        resource_ids TEXT NOT NULL CHECK(json_valid(resource_ids)),revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision>=0),created_at TEXT NOT NULL)""",
    "CREATE TRIGGER speaker_attribution_no_update BEFORE UPDATE ON speaker_attributions BEGIN SELECT RAISE(ABORT,'Attribution snapshots are immutable'); END",
    "CREATE TRIGGER speaker_attribution_no_delete BEFORE DELETE ON speaker_attributions BEGIN SELECT RAISE(ABORT,'Attribution snapshots are immutable'); END",
    "CREATE TRIGGER published_attribution_run_no_update BEFORE UPDATE ON attribution_runs WHEN OLD.status='published' BEGIN SELECT RAISE(ABORT,'Published attribution runs are immutable'); END",
)


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.executescript(SCHEMA)
            conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES (1,?)", (now(),))
            columns = {row[1] for row in conn.execute("PRAGMA table_info(usage)")}
            if "reserved_rub" not in columns:
                conn.execute("ALTER TABLE usage ADD COLUMN reserved_rub REAL")
            conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES (2,?)", (now(),))
            meeting_columns = {row[1] for row in conn.execute("PRAGMA table_info(meetings)")}
            if "capture_error" not in meeting_columns:
                conn.execute("ALTER TABLE meetings ADD COLUMN capture_error TEXT")
            conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES (3,?)", (now(),))
        self._migrate_speakers()
        self._migrate_enrollments()
        self._migrate_enrollment_receipts()
        self._migrate_identification()
        self._migrate_assignments()

    def _migrate_assignments(self):
        from secretary.infrastructure.assignment_repository import ASSIGNMENT_MIGRATION
        with self.transaction() as conn:
            if conn.execute('SELECT 1 FROM schema_migrations WHERE version=8').fetchone():
                return
            for statement in ASSIGNMENT_MIGRATION:
                conn.execute(statement)
            if conn.execute('PRAGMA foreign_key_check').fetchone():
                raise sqlite3.IntegrityError('Assignment migration foreign key failure')
            conn.execute('INSERT INTO schema_migrations VALUES(8,?)',(now(),))

    def _migrate_identification(self):
        with self.transaction() as conn:
            if conn.execute('SELECT 1 FROM schema_migrations WHERE version=7').fetchone():
                return
            for statement in IDENTIFICATION_MIGRATION:
                conn.execute(statement)
            if conn.execute('PRAGMA foreign_key_check').fetchone():
                raise sqlite3.IntegrityError('Identification migration foreign key failure')
            conn.execute('INSERT INTO schema_migrations VALUES(7,?)', (now(),))

    def _migrate_enrollment_receipts(self):
        """Schema6 compatibility with existing schema5 enrollment receipts."""
        with self.transaction() as conn:
            columns = {row[1] for row in conn.execute('PRAGMA table_info(enrollment_commands)')}
            if 'error_status' not in columns:
                conn.execute('ALTER TABLE enrollment_commands ADD COLUMN error_status INTEGER NOT NULL DEFAULT 409 CHECK(error_status BETWEEN 400 AND 599)')
            # Successful old receipts contain independently checkable owner metadata.
            # Ambiguous old failure scopes cannot establish which profile was requested.
            conn.execute("""UPDATE enrollment_commands SET scope=json_array(json_extract(response,'$.person_profile_id'),scope)
                WHERE kind IN ('enroll_confirm','enroll_delete') AND error IS NULL AND status='done'
                AND json_valid(response) AND json_extract(response,'$.id')=scope
                AND EXISTS(SELECT 1 FROM voice_enrollments e WHERE e.id=enrollment_commands.scope
                    AND e.person_profile_id=json_extract(enrollment_commands.response,'$.person_profile_id'))""")
            conn.execute('INSERT OR IGNORE INTO schema_migrations VALUES(6,?)', (now(),))

    def _migrate_enrollments(self):
        with self.transaction() as conn:
            if conn.execute("SELECT 1 FROM schema_migrations WHERE version=5").fetchone():
                return
            for statement in (
                "ALTER TABLE voice_enrollments ADD COLUMN reason_codes TEXT NOT NULL DEFAULT '[]'",
                "ALTER TABLE voice_enrollments ADD COLUMN listening_confirmed INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE voice_enrollments ADD COLUMN single_speaker_confirmed INTEGER NOT NULL DEFAULT 0",
                "CREATE UNIQUE INDEX enrollment_generation_once ON voice_enrollments(storage_key) WHERE storage_key IS NOT NULL",
                "CREATE TABLE voice_state(id INTEGER PRIMARY KEY CHECK(id=1),revision INTEGER NOT NULL)",
                "INSERT INTO voice_state VALUES(1,0)",
                """CREATE TABLE enrollment_materials(generation TEXT PRIMARY KEY,
                    enrollment_id TEXT NOT NULL REFERENCES voice_enrollments(id),
                    profile_id TEXT NOT NULL REFERENCES person_profiles(id),material_revision INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('writing','active','cleanup_pending')))""",
                """CREATE TABLE enrollment_recordings(id TEXT PRIMARY KEY,profile_id TEXT NOT NULL REFERENCES person_profiles(id),
                    enrollment_id TEXT NOT NULL REFERENCES voice_enrollments(id),generation INTEGER NOT NULL,
                    status TEXT NOT NULL,disposition TEXT NOT NULL DEFAULT 'review',duration_ms INTEGER NOT NULL DEFAULT 0,
                    reason TEXT,created_at TEXT NOT NULL)""",
                """CREATE TABLE enrollment_partials(generation TEXT NOT NULL REFERENCES enrollment_materials(generation),
                    profile_id TEXT NOT NULL,basename TEXT NOT NULL,device TEXT NOT NULL,inode TEXT NOT NULL,
                    PRIMARY KEY(generation,basename))""",
                """CREATE TABLE enrollment_commands(operation_id TEXT PRIMARY KEY,kind TEXT NOT NULL,scope TEXT NOT NULL,
                    request_hash TEXT NOT NULL,status TEXT NOT NULL,response TEXT,error TEXT)""",
                """CREATE TRIGGER enrollment_operation_unique BEFORE INSERT ON enrollment_commands
                    WHEN EXISTS(SELECT 1 FROM speaker_operations WHERE operation_id=NEW.operation_id)
                    BEGIN SELECT RAISE(ABORT,'Operation already used'); END""",
                """CREATE TRIGGER speaker_operation_unique BEFORE INSERT ON speaker_operations
                    WHEN EXISTS(SELECT 1 FROM enrollment_commands WHERE operation_id=NEW.operation_id)
                    BEGIN SELECT RAISE(ABORT,'Operation already used'); END""",
                """CREATE TRIGGER voice_profile_revision AFTER UPDATE ON person_profiles
                    BEGIN UPDATE voice_state SET revision=revision+1 WHERE id=1; END""",
                """CREATE TRIGGER voice_material_revision AFTER UPDATE ON voice_enrollments
                    BEGIN UPDATE voice_state SET revision=revision+1 WHERE id=1; END""",
            ):
                conn.execute(statement)
            conn.execute("INSERT INTO schema_migrations VALUES(5,?)", (now(),))

    def _migrate_speakers(self):
        with self.transaction() as conn:
            if conn.execute("SELECT 1 FROM schema_migrations WHERE version=4").fetchone():
                return
            for statement in SPEAKER_MIGRATION:
                conn.execute(statement)
            if conn.execute("PRAGMA foreign_key_check").fetchone():
                raise sqlite3.IntegrityError("Foreign key validation failed during speaker migration")
            conn.execute("INSERT INTO schema_migrations VALUES (4,?)", (now(),))

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def rows(self, query, args=()):
        with self.connection() as conn:
            return [dict(r) for r in conn.execute(query, args).fetchall()]

    def one(self, query, args=()):
        rows = self.rows(query, args)
        return rows[0] if rows else None

    def execute(self, query, args=()):
        with self.connection() as conn:
            return conn.execute(query, args).rowcount

    def create_meeting(self, title, processing_mode='ordinary'):
        meeting_id, stamp = uid(), now()
        self.execute("INSERT INTO meetings(id,title,status,created_at,updated_at,processing_mode) VALUES(?,?,'new',?,?,?)", (meeting_id, title, stamp, stamp, processing_mode))
        return self.meeting(meeting_id)

    def list_meetings(self):
        return self.rows(f"SELECT {MEETING_FIELDS} FROM meetings ORDER BY created_at DESC")

    def meeting(self, meeting_id, internal=False):
        item = self.one(f"SELECT {'*' if internal else MEETING_FIELDS} FROM meetings WHERE id=?", (meeting_id,))
        if item is None:
            raise KeyError(meeting_id)
        return item

    def update_meeting(self, meeting_id, **fields):
        allowed = {"title", "status", "error", "capture_error", "duration_ms", "media_path", "recording", "auto_process", "transcript_version"}
        if fields.keys() - allowed:
            raise ValueError("Unsupported meeting field")
        fields["updated_at"] = now()
        self.execute("UPDATE meetings SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?", (*fields.values(), meeting_id))

    def add_chunk(self, meeting_id, chunk):
        chunk_id = stable_id(meeting_id, chunk["sequence"], chunk["channel"])
        self.execute("INSERT OR IGNORE INTO chunks VALUES(?,?,?,?,?,?,?,'ready',?)", (chunk_id, meeting_id, chunk["sequence"], chunk["channel"], str(chunk["path"]), chunk["offset_ms"], chunk["duration_ms"], chunk["sha256"]))
        return self.one("SELECT * FROM chunks WHERE id=?", (chunk_id,))

    def chunks(self, meeting_id):
        return self.rows("SELECT * FROM chunks WHERE meeting_id=? ORDER BY sequence,channel", (meeting_id,))

    def ensure_version(self, meeting_id):
        with self.transaction() as conn:
            row = conn.execute("SELECT transcript_version FROM meetings WHERE id=?", (meeting_id,)).fetchone()
            if row is None:
                raise KeyError(meeting_id)
            version = max(1, row[0])
            conn.execute("UPDATE meetings SET transcript_version=?,updated_at=? WHERE id=?", (version, now(), meeting_id))
            return version

    def authorize_pipeline(self, meeting_id, version, *, explicit=False, reason='input', conn=None):
        if conn is None:
            with self.transaction() as connection:
                return self.authorize_pipeline(meeting_id,version,explicit=explicit,reason=reason,conn=connection)
        conn.execute('''INSERT INTO meeting_pipeline_handoffs VALUES(?,?,1,?,NULL,?)
            ON CONFLICT(meeting_id,transcript_version) DO UPDATE SET
            summary_authorized=CASE WHEN ? THEN 1 ELSE summary_authorized END,
            authorization_reason=CASE WHEN ? THEN excluded.authorization_reason ELSE authorization_reason END''',
            (meeting_id,version,reason,now(),int(explicit),int(explicit)))

    def enqueue(self, meeting_id, stage, *, chunk_id=None, version=0, payload=None, intent_key=None):
        if stage == 'identify_speakers' and not intent_key:
            raise ValueError('Identification requires a durable intent')
        if stage != 'identify_speakers' and intent_key is not None:
            raise ValueError('Intent key is local-only')
        with self.transaction() as conn:
            if stage == 'identify_speakers':
                existing = conn.execute("SELECT * FROM jobs WHERE meeting_id=? AND stage=? AND version=? AND intent_key=?", (meeting_id, stage, version, intent_key)).fetchone()
            else:
                existing = conn.execute("SELECT * FROM jobs WHERE meeting_id=? AND stage=? AND IFNULL(chunk_id,'')=? AND version=? AND status IN ('queued','running','waiting_config','paused_budget','uncertain')", (meeting_id, stage, chunk_id or "", version)).fetchone()
            if existing:
                return dict(existing)
            job_id, stamp = uid(), now()
            conn.execute("INSERT INTO jobs(id,meeting_id,stage,status,created_at,updated_at,chunk_id,version,payload,intent_key) VALUES(?,?,?,'queued',?,?,?,?,?,?)", (job_id, meeting_id, stage, stamp, stamp, chunk_id, version, json.dumps(payload) if payload else None, intent_key))
            result = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            if stage == 'summarize':
                from secretary.infrastructure.assignment_repository import AssignmentRepository
                AssignmentRepository.bind_job(conn,result)
            return result

    def job(self, job_id, internal=False):
        item = self.one(f"SELECT {'*' if internal else JOB_FIELDS} FROM jobs WHERE id=?", (job_id,))
        if item is None:
            raise KeyError(job_id)
        return item

    def jobs(self, meeting_id):
        return self.rows(f"SELECT {JOB_FIELDS} FROM jobs WHERE meeting_id=? ORDER BY created_at", (meeting_id,))

    def claim(self):
        with self.transaction() as conn:
            item = conn.execute("SELECT * FROM jobs WHERE status='queued' AND cancel_requested=0 ORDER BY created_at LIMIT 1").fetchone()
            if not item:
                return None
            conn.execute("UPDATE jobs SET status='running',attempts=attempts+1,updated_at=? WHERE id=? AND status='queued'", (now(), item["id"]))
            return dict(conn.execute("SELECT * FROM jobs WHERE id=?", (item["id"],)).fetchone())

    def finish_job(self, job_id, status, error=None):
        self.execute("UPDATE jobs SET status=?,error=?,updated_at=? WHERE id=?", (status, error, now(), job_id))

    def cancel(self, job_id):
        self.job(job_id)
        self.execute("UPDATE jobs SET cancel_requested=CASE WHEN status='succeeded' THEN cancel_requested ELSE 1 END,status=CASE WHEN status IN ('running','uncertain','succeeded') THEN status ELSE 'cancelled' END,updated_at=? WHERE id=?", (now(), job_id))
        job = self.job(job_id, internal=True)
        if job["status"] == "cancelled":
            self.update_meeting(job["meeting_id"], status="cancelled", error=None)
            if job['stage']=='identify_speakers':
                with self.transaction() as conn:
                    conn.execute("UPDATE identification_intents SET outcome='cancelled',reasons='[\"cancelled\"]' WHERE job_id=? AND outcome!='completed'", (job_id,))
                    conn.execute("UPDATE attribution_runs SET status='failed' WHERE id IN (SELECT run_id FROM identification_intents WHERE job_id=?) AND status!='published'", (job_id,))
        return self.job(job_id)

    def recover(self):
        with self.transaction() as conn:
            conn.execute("UPDATE jobs SET status=CASE WHEN cancel_requested=1 THEN 'cancelled' ELSE 'queued' END,error='Local work resumed after restart',updated_at=? WHERE status='running' AND stage IN ('prepare','identify_speakers')", (now(),))
            conn.execute("UPDATE jobs SET status='uncertain',error='Process stopped during cloud request; reconciliation required before retry',updated_at=? WHERE status='running' AND stage IN ('transcribe','summarize')", (now(),))
            conn.execute("UPDATE identification_intents SET outcome='cancelled',reasons='[\"cancelled\"]' WHERE outcome='pending' AND job_id IN (SELECT id FROM jobs WHERE stage='identify_speakers' AND status='cancelled')")
            conn.execute("UPDATE attribution_runs SET status='failed' WHERE status='pending' AND id IN (SELECT run_id FROM identification_intents WHERE outcome='cancelled')")
            conn.execute("UPDATE usage SET status='unknown' WHERE status='reserved' AND job_id IN (SELECT id FROM jobs WHERE status='uncertain')")
            interrupted = conn.execute("SELECT id FROM meetings WHERE recording=1").fetchall()
            conn.execute("UPDATE meetings SET recording=0,status='interrupted',capture_error='Recording interrupted; completed audio chunks preserved',updated_at=? WHERE recording=1", (now(),))
            conn.execute("UPDATE meetings SET media_path=NULL,status='new',error='Interrupted upload; please upload again',updated_at=? WHERE media_path LIKE 'uploading:%'", (now(),))
            return [r[0] for r in interrupted]

    def save_segments(self, job, segments, speakers):
        with self.transaction() as conn:
            for speaker in speakers:
                conn.execute("INSERT OR IGNORE INTO speakers VALUES(?,?,?,?,?)", tuple(speaker[k] for k in ("id", "meeting_id", "chunk_id", "provider_label", "display_name")))
            for segment in segments:
                conn.execute("INSERT OR IGNORE INTO segments VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", tuple(segment[k] for k in ("id", "meeting_id", "chunk_id", "transcript_version", "ordinal", "start_ms", "end_ms", "timing_precision", "text", "confidence", "speaker_id", "channel")))
            conn.execute("UPDATE chunks SET status='transcribed' WHERE id=?", (job["chunk_id"],))
            conn.execute("UPDATE jobs SET status='succeeded',error=NULL,updated_at=? WHERE id=?", (now(), job["id"]))

    def segments(self, meeting_id, version=None, offset=0, limit=100):
        version = version if version is not None else self.meeting(meeting_id)["transcript_version"]
        total = self.one("SELECT COUNT(*) AS n FROM segments WHERE meeting_id=? AND transcript_version=?", (meeting_id, version))["n"]
        items = self.rows("SELECT s.* FROM segments s JOIN chunks c ON c.id=s.chunk_id WHERE s.meeting_id=? AND s.transcript_version=? ORDER BY COALESCE(s.start_ms,c.offset_ms),c.channel,c.sequence,s.ordinal LIMIT ? OFFSET ?", (meeting_id, version, limit, offset))
        return {"items": items, "total": total, "transcript_version": version}

    def save_summary(self, job, summary):
        with self.transaction() as conn:
            prior = conn.execute('SELECT meeting_id,summary_version FROM summary_publications WHERE job_id=?',(job['id'],)).fetchone()
            if prior:
                return json.loads(conn.execute('SELECT payload FROM summaries WHERE meeting_id=? AND summary_version=?',
                    (prior['meeting_id'],prior['summary_version'])).fetchone()[0])
            current = conn.execute("SELECT transcript_version FROM meetings WHERE id=?", (job["meeting_id"],)).fetchone()[0]
            if current != job["version"]:
                conn.execute("UPDATE jobs SET status='cancelled',error='Transcript version changed; stale summary not published',updated_at=? WHERE id=?", (now(), job["id"]))
                return None
            existing = conn.execute("SELECT MAX(summary_version) FROM summaries WHERE meeting_id=?", (job["meeting_id"],)).fetchone()[0] or 0
            summary["summary_version"] = existing + 1
            conn.execute("INSERT INTO summaries VALUES(?,?,?,?)", (job["meeting_id"], job["version"], summary["summary_version"], json.dumps(summary, ensure_ascii=False)))
            from secretary.infrastructure.assignment_repository import AssignmentRepository
            AssignmentRepository(self).publish(conn,job,summary)
            conn.execute("UPDATE jobs SET status='succeeded',error=NULL,updated_at=? WHERE id=?", (now(), job["id"]))
            conn.execute("UPDATE meetings SET status=CASE WHEN capture_error IS NULL THEN 'ready' ELSE 'partial_ready' END,error=NULL,updated_at=? WHERE id=?", (now(), job["meeting_id"]))
        return summary

    def summary(self, meeting_id):
        item = self.one("SELECT payload FROM summaries WHERE meeting_id=? AND transcript_version=(SELECT transcript_version FROM meetings WHERE id=?) ORDER BY summary_version DESC LIMIT 1", (meeting_id, meeting_id))
        return json.loads(item["payload"]) if item else None

    def reserve(self, job, estimate, budget, allow_unknown, unknown_reservation=None, *, enforce_limits=True):
        with self.transaction() as conn:
            reserved = estimate if estimate is not None else unknown_reservation
            if enforce_limits:
                rows = conn.execute("SELECT estimated_rub,confirmed_rub,status,reserved_rub FROM usage WHERE meeting_id=? AND status!='released'", (job["meeting_id"],)).fetchall()
                known = sum(r["confirmed_rub"] if r["confirmed_rub"] is not None else (r["estimated_rub"] if r["estimated_rub"] is not None else (r["reserved_rub"] if r["reserved_rub"] is not None else budget)) for r in rows)
                unknown = any(r["estimated_rub"] is None and r["confirmed_rub"] is None and r["reserved_rub"] is None for r in rows)
                if estimate is None and not allow_unknown:
                    raise ValueError("Unknown model price; verify price or explicitly allow unknown cost")
                if estimate is None and (unknown_reservation is None or unknown_reservation <= 0):
                    raise ValueError("Unknown model price requires an explicit positive per-request RUB reservation")
                if unknown and not allow_unknown:
                    raise ValueError("Previous request has unknown cost; reconcile provider receipt")
                if known >= budget or known + reserved > budget:
                    raise ValueError("Meeting budget exhausted (confirmed, estimated and in-flight requests included)")
            usage_id = uid()
            conn.execute("INSERT INTO usage(id,meeting_id,job_id,kind,estimated_rub,confirmed_rub,status,provider_request_id,created_at,reserved_rub) VALUES(?,?,?,?,?,NULL,'reserved',NULL,?,?)", (usage_id, job["meeting_id"], job["id"], job["stage"], estimate, now(), reserved))
            return usage_id

    def reserve_upload(self, meeting_id, token):
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone()
            if row is None:
                raise KeyError(meeting_id)
            if row["recording"] or row["media_path"] or conn.execute("SELECT 1 FROM chunks WHERE meeting_id=? LIMIT 1", (meeting_id,)).fetchone():
                raise ValueError("Создайте отдельную встречу для новой записи")
            conn.execute("UPDATE meetings SET media_path=?,status='uploading',updated_at=? WHERE id=?", ("uploading:" + token, now(), meeting_id))

    def settle(self, usage_id, usage=None, *, rejected=False):
        usage = usage or {}
        confirmed = usage.get("confirmed_rub")
        existing = self.one("SELECT confirmed_rub,provider_request_id FROM usage WHERE id=?", (usage_id,))
        if confirmed is None and existing:
            confirmed = existing["confirmed_rub"]
        status = "released" if rejected else ("confirmed" if confirmed is not None else "unknown")
        request_id = usage.get("provider_request_id") or (existing["provider_request_id"] if existing else None)
        self.execute("UPDATE usage SET confirmed_rub=?,status=?,provider_request_id=? WHERE id=?", (confirmed, status, request_id, usage_id))

    def configuration(self):
        return {r["key"]: json.loads(r["value"]) for r in self.rows("SELECT * FROM configuration")}

    def set_configuration(self, changes):
        with self.transaction() as conn:
            for key, value in changes.items():
                conn.execute("INSERT INTO configuration VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

