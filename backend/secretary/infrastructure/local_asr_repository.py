"""Additive, scoped storage for immutable local-ASR evidence and manual overlays.

No inference, network or paid business-object mutation is reachable here. The
caller supplies a server-verified media digest and never accepts it from JSON.
"""
from __future__ import annotations

from contextlib import nullcontext
import hashlib
import json
import sqlite3

from secretary.domain.local_asr import (
    ImportLocalAsrRun, LocalAsrConflict, LocalAsrNotFound, LocalAsrError,
    ReviewLocalAsrCluster, ReviewLocalAsrSegment, SourceSegment,
)
from secretary.infrastructure.database import Database, now, uid, stable_id


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


LOCAL_ASR_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS local_asr_schema(version INTEGER PRIMARY KEY,applied_at TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS local_asr_runs(
        id TEXT PRIMARY KEY,owner_scope TEXT NOT NULL,meeting_id TEXT NOT NULL REFERENCES meetings(id),
        transcript_version INTEGER NOT NULL,revision INTEGER NOT NULL,artifact_digest TEXT NOT NULL,
        manifest TEXT NOT NULL CHECK(json_valid(manifest)),created_at TEXT NOT NULL,
        UNIQUE(owner_scope,meeting_id,transcript_version,artifact_digest),
        UNIQUE(owner_scope,meeting_id,transcript_version,revision))""",
    """CREATE TABLE IF NOT EXISTS local_asr_review_state(
        run_id TEXT PRIMARY KEY REFERENCES local_asr_runs(id),revision INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS local_asr_review_events(
        run_id TEXT NOT NULL REFERENCES local_asr_runs(id),segment_ordinal INTEGER NOT NULL,
        revision INTEGER NOT NULL,participant_id TEXT,person_id TEXT,status TEXT NOT NULL,
        source TEXT NOT NULL,speaker_cluster_id TEXT NOT NULL,confirmed_at TEXT NOT NULL,
        PRIMARY KEY(run_id,segment_ordinal,revision))""",
    """CREATE TABLE IF NOT EXISTS local_asr_operations(
        operation_id TEXT PRIMARY KEY,owner_scope TEXT NOT NULL,kind TEXT NOT NULL,scope TEXT NOT NULL,
        request_hash TEXT NOT NULL,response TEXT NOT NULL CHECK(json_valid(response)),created_at TEXT NOT NULL)""",
    """CREATE TRIGGER IF NOT EXISTS local_asr_run_no_update BEFORE UPDATE ON local_asr_runs
        BEGIN SELECT RAISE(ABORT,'Local ASR manifests are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS local_asr_run_no_delete BEFORE DELETE ON local_asr_runs
        BEGIN SELECT RAISE(ABORT,'Local ASR manifests are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS local_asr_review_no_update BEFORE UPDATE ON local_asr_review_events
        BEGIN SELECT RAISE(ABORT,'Local ASR review history is immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS local_asr_review_no_delete BEFORE DELETE ON local_asr_review_events
        BEGIN SELECT RAISE(ABORT,'Local ASR review history is immutable'); END""",
)


def initialize_local_asr_schema(db: Database):
    """Explicit additive migration, called only at an authorized app startup."""
    with db.transaction() as conn:
        for statement in LOCAL_ASR_SCHEMA:
            conn.execute(statement)
        if conn.execute("PRAGMA foreign_key_check").fetchone():
            raise sqlite3.IntegrityError("Local ASR schema foreign-key validation failed")
        conn.execute("INSERT OR IGNORE INTO local_asr_schema VALUES(1,?)", (now(),))


class LocalAsrRepository:
    def __init__(self, db: Database, *, owner_scope: str = "local-owner"):
        if not owner_scope or len(owner_scope) > 200:
            raise ValueError("Server-resolved owner scope required")
        self.db, self.owner_scope = db, owner_scope

    @staticmethod
    def _meeting(conn, meeting_id):
        item = conn.execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone()
        if item is None:
            raise LocalAsrNotFound("Meeting not found")
        return item

    def _run(self, conn, meeting_id, run_id):
        row = conn.execute("SELECT * FROM local_asr_runs WHERE id=? AND meeting_id=? AND owner_scope=?",
                           (run_id, meeting_id, self.owner_scope)).fetchone()
        if row is None:
            raise LocalAsrNotFound("Analysis run not found")
        return row

    def _operation(self, conn, command, kind, scope):
        request_hash = digest(command.model_dump(mode="json"))
        row = conn.execute("SELECT * FROM local_asr_operations WHERE operation_id=?", (command.operation_id,)).fetchone()
        if row is None:
            return None, request_hash
        if (row["owner_scope"], row["kind"], row["scope"], row["request_hash"]) != (self.owner_scope, kind, scope, request_hash):
            raise LocalAsrConflict("operation_id already used with different scope or content")
        return json.loads(row["response"]), request_hash

    def _receipt(self, conn, command, kind, scope, request_hash, response):
        conn.execute("INSERT INTO local_asr_operations VALUES(?,?,?,?,?,?,?)",
                     (command.operation_id, self.owner_scope, kind, scope, request_hash, canonical(response), now()))

    @staticmethod
    def source_snapshot(conn, meeting_id, version):
        rows = conn.execute("""SELECT s.id AS segment_id,s.text,s.start_ms AS started_ms,s.end_ms AS ended_ms,s.channel
            FROM segments s JOIN chunks c ON c.id=s.chunk_id
            WHERE s.meeting_id=? AND s.transcript_version=?
            ORDER BY COALESCE(s.start_ms,c.offset_ms),c.channel,c.sequence,s.ordinal""", (meeting_id, version)).fetchall()
        return [SourceSegment(**dict(row)).model_dump(mode="json") for row in rows]

    def import_run(self, meeting_id: str, command: ImportLocalAsrRun, *, trusted_input_sha256: str,
                   conn: sqlite3.Connection | None = None):
        """Validate authoritative source, then atomically publish evidence only.

        Passing a connection joins the existing job owner's fenced transaction.
        No path or authentication token is accepted in the immutable manifest.
        """
        # Pydantic model_copy bypasses validators; never trust an in-memory object
        # supplied by another adapter more than its serialized representation.
        command = ImportLocalAsrRun.model_validate(command.model_dump(mode="json"))
        if trusted_input_sha256 != command.result.input_sha256:
            raise LocalAsrConflict("Input digest differs from server-verified media")
        if command.artifacts.audio_ref != f"meeting:{meeting_id}":
            raise LocalAsrConflict("Audio reference belongs to another meeting")
        scope = canonical([meeting_id, command.transcript_version])
        with (self.db.transaction() if conn is None else nullcontext(conn)) as connection:
            self._meeting(connection, meeting_id)
            replay, request_hash = self._operation(connection, command, "import", scope)
            if replay is not None:
                return replay
            actual = self.source_snapshot(connection, meeting_id, command.transcript_version)
            expected = [item.model_dump(mode="json") for item in command.artifacts.source_segments]
            if not actual or actual != expected:
                raise LocalAsrConflict("Source transcript changed or does not match the stored version")
            manifest = {"schema_version": "secretary.local-asr-run.v1", "meeting_id": meeting_id,
                        "transcript_version": command.transcript_version,
                        "result": command.result.model_dump(mode="json"),
                        "artifacts": command.artifacts.model_dump(mode="json")}
            artifact_digest = digest(manifest)
            previous = connection.execute("""SELECT id FROM local_asr_runs WHERE owner_scope=? AND meeting_id=?
                AND transcript_version=? AND artifact_digest=?""",
                (self.owner_scope, meeting_id, command.transcript_version, artifact_digest)).fetchone()
            if previous is not None:
                response = self._read(connection, meeting_id, previous[0])
            else:
                revision = connection.execute("""SELECT COALESCE(MAX(revision),0)+1 FROM local_asr_runs
                    WHERE owner_scope=? AND meeting_id=? AND transcript_version=?""",
                    (self.owner_scope, meeting_id, command.transcript_version)).fetchone()[0]
                run_id = uid()
                connection.execute("INSERT INTO local_asr_runs VALUES(?,?,?,?,?,?,?,?)",
                    (run_id, self.owner_scope, meeting_id, command.transcript_version, revision,
                     artifact_digest, canonical(manifest), now()))
                connection.execute("INSERT INTO local_asr_review_state VALUES(?,0)", (run_id,))
                response = self._read(connection, meeting_id, run_id)
            self._receipt(connection, command, "import", scope, request_hash, response)
            return response

    @staticmethod
    def _summary(row, review_revision):
        return {"run_id": row["id"], "meeting_id": row["meeting_id"], "transcript_version": row["transcript_version"],
                "revision": row["revision"], "created_at": row["created_at"], "artifact_digest": row["artifact_digest"],
                "review_revision": review_revision}

    @staticmethod
    def _reviews(conn, run_id):
        return [dict(row) for row in conn.execute("""SELECT e.segment_ordinal,e.participant_id,e.person_id,e.status,
            e.source,e.speaker_cluster_id,e.revision AS review_revision,e.confirmed_at FROM local_asr_review_events e
            WHERE e.run_id=? AND e.revision=(SELECT MAX(x.revision) FROM local_asr_review_events x
                WHERE x.run_id=e.run_id AND x.segment_ordinal=e.segment_ordinal) ORDER BY e.segment_ordinal""", (run_id,))]

    def _read(self, conn, meeting_id, run_id):
        row = self._run(conn, meeting_id, run_id)
        manifest = json.loads(row["manifest"])
        if digest(manifest) != row["artifact_digest"]:
            raise LocalAsrConflict("Stored artifact digest verification failed")
        revision = conn.execute("SELECT revision FROM local_asr_review_state WHERE run_id=?", (run_id,)).fetchone()[0]
        return {**self._summary(row, revision), **manifest, "reviews": self._reviews(conn, run_id),
                "source_decisions": [{"source_segment_id": source_id,
                    "participant_id": decision["participant_id"], "status": decision["status"], "method": decision["method"]}
                    for source_id, decision in self._source_overrides(conn, meeting_id, row["transcript_version"]).items()],
                "clusters": [{"speaker_label": label, "speaker_cluster_id": stable_id(run_id, label), "kind": kind}
                             for label, kind in manifest["artifacts"]["speaker_kinds"].items()]}

    def list_runs(self, meeting_id):
        with self.db.connection() as conn:
            conn.execute("BEGIN")
            self._meeting(conn, meeting_id)
            return [self._summary(row, row["review_revision"]) for row in conn.execute("""SELECT r.*,s.revision AS review_revision
                FROM local_asr_runs r JOIN local_asr_review_state s ON s.run_id=r.id
                WHERE r.meeting_id=? AND r.owner_scope=? ORDER BY r.created_at,r.revision""", (meeting_id, self.owner_scope))]

    def get_run(self, meeting_id, run_id):
        with self.db.connection() as conn:
            conn.execute("BEGIN")
            return self._read(conn, meeting_id, run_id)

    def export_run(self, meeting_id, run_id):
        return self.get_run(meeting_id, run_id)

    @staticmethod
    def _source_overrides(conn, meeting_id, version):
        rows = conn.execute("""SELECT a.segment_id,a.participant_id,a.status,a.method FROM attribution_state st
            JOIN speaker_attributions a ON a.run_id=st.run_id
            WHERE st.meeting_id=? AND st.transcript_version=? AND (a.method='manual' OR a.status='confirmed')""",
            (meeting_id, version)).fetchall()
        return {row["segment_id"]: dict(row) for row in rows}

    @staticmethod
    def _covered_by_turns(item, turns):
        """Every millisecond in a review sample must have independent support.

        A tiny intersection cannot certify a long gap as a single clean voice.
        Overlapping/touching turns of the same speaker form a continuous union.
        """
        boundary = item["started_ms"]
        intervals = sorted((turn["started_ms"], turn["ended_ms"]) for turn in turns
                           if turn["speaker_label"] == item["speaker_label"])
        for start, end in intervals:
            if end <= boundary:
                continue
            if start > boundary:
                return False
            boundary = max(boundary, end)
            if boundary >= item["ended_ms"]:
                return True
        return False

    def _preview(self, conn, meeting_id, run_id, speaker_label):
        run = self._read(conn, meeting_id, run_id)
        kinds = run["artifacts"]["speaker_kinds"]
        if speaker_label not in kinds:
            raise LocalAsrNotFound("Speaker cluster not found")
        raw = run["result"]
        sources = {item["segment_id"]: item for item in run["artifacts"]["source_segments"]}
        mappings = {item["ordinal"]: item["source_segment_ids"] for item in run["artifacts"]["segment_sources"]}
        previous = self._source_overrides(conn, meeting_id, run["transcript_version"])
        reviews = {item["segment_ordinal"]: item for item in run["reviews"]}
        segments, eligible, excluded = [], [], []
        for item in raw["segment_items"]:
            if item["speaker_label"] != speaker_label:
                continue
            ordinal = item["ordinal"]
            source_ids = mappings[ordinal]
            channels = {sources[s]["channel"] for s in source_ids}
            channel = next(iter(channels)) if len(channels) == 1 else None
            segments.append({**item, "source_segment_ids": source_ids, "channel": channel})
            reason = None
            if kinds[speaker_label] != "single":
                reason = "unknown_or_composite"
            elif channel is None:
                reason = "ambiguous_channel"
            elif any(source_id in previous for source_id in source_ids):
                reason = "existing_manual_source_decision"
            elif ordinal in reviews:
                reason = "individual_override" if reviews[ordinal]["source"] == "manual_segment" else "previous_confirmation"
            elif not any(turn["speaker_label"] == speaker_label and turn["started_ms"] < item["ended_ms"]
                         and item["started_ms"] < turn["ended_ms"] for turn in raw["speaker_turns"]):
                reason = "no_independent_turn_evidence"
            elif not self._covered_by_turns(item, raw["speaker_turns"]):
                reason = "incomplete_turn_coverage"
            elif any(turn["speaker_label"] != speaker_label and turn["started_ms"] < item["ended_ms"]
                     and item["started_ms"] < turn["ended_ms"] for turn in raw["speaker_turns"]):
                reason = "overlap_or_mixed_turn"
            if reason:
                excluded.append({"segment_ordinal": ordinal, "reason": reason})
            else:
                eligible.append(ordinal)
        # Spread listening samples across a cluster, not just adjacent utterances.
        sample_indices = sorted({0, len(eligible) // 2, len(eligible) - 1}) if eligible else []
        sample_ordinals = {eligible[index] for index in sample_indices}
        samples = [{"segment_ordinal": item["ordinal"], "started_ms": item["started_ms"],
                    "ended_ms": item["ended_ms"], "source_segment_ids": item["source_segment_ids"], "channel": item["channel"]}
                   for item in segments if item["ordinal"] in sample_ordinals]
        return {"run_id": run_id, "speaker_label": speaker_label, "speaker_cluster_id": stable_id(run_id, speaker_label),
                "review_revision": run["review_revision"], "shown_segment_ordinals": [item["ordinal"] for item in segments],
                "eligible_segment_ordinals": eligible, "excluded_segments": excluded, "samples": samples, "segments": segments}

    def cluster_preview(self, meeting_id, run_id, speaker_label):
        with self.db.connection() as conn:
            conn.execute("BEGIN")
            return self._preview(conn, meeting_id, run_id, speaker_label)

    @staticmethod
    def _participant(conn, meeting_id, participant_id):
        row = conn.execute("SELECT * FROM meeting_participants WHERE id=? AND meeting_id=? AND enabled=1",
                           (participant_id, meeting_id)).fetchone()
        if row is None:
            raise LocalAsrConflict("Selected participant is not enabled in this meeting")
        return row

    @staticmethod
    def _append_review(conn, run_id, ordinal, revision, participant, source, label, stamp):
        conn.execute("INSERT INTO local_asr_review_events VALUES(?,?,?,?,?,?,?,?,?)", (
            run_id, ordinal, revision, participant["id"] if participant else None,
            participant["person_profile_id"] if participant else None,
            "confirmed" if participant else "excluded", source, stable_id(run_id, label), stamp))

    def review_cluster(self, meeting_id, run_id, command: ReviewLocalAsrCluster):
        command = ReviewLocalAsrCluster.model_validate(command.model_dump(mode="json"))
        scope = canonical([meeting_id, run_id])
        with self.db.transaction() as conn:
            self._run(conn, meeting_id, run_id)
            replay, request_hash = self._operation(conn, command, "cluster_review", scope)
            if replay is not None:
                return replay
            preview = self._preview(conn, meeting_id, run_id, command.speaker_label)
            if command.expected_review_revision != preview["review_revision"]:
                raise LocalAsrConflict("Review revision changed; refresh the shown set")
            if command.shown_segment_ordinals != preview["shown_segment_ordinals"]:
                raise LocalAsrConflict("Shown segment set differs from the current cluster")
            eligible = set(preview["eligible_segment_ordinals"])
            excluded = set(command.excluded_segment_ordinals)
            intended = set(command.shown_segment_ordinals) - excluded
            if not intended or not intended <= eligible:
                raise LocalAsrConflict("Review includes excluded, mixed or previously confirmed segments")
            participant = self._participant(conn, meeting_id, command.participant_id)
            revision, stamp = preview["review_revision"] + 1, now()
            for ordinal in sorted(intended):
                self._append_review(conn, run_id, ordinal, revision, participant, "manual_cluster", command.speaker_label, stamp)
            # Explicit user exclusions become persistent individual overrides;
            # automatic safety exclusions are not manual user decisions.
            for ordinal in sorted(excluded & eligible):
                self._append_review(conn, run_id, ordinal, revision, None, "manual_segment", command.speaker_label, stamp)
            conn.execute("UPDATE local_asr_review_state SET revision=? WHERE run_id=?", (revision, run_id))
            response = self._read(conn, meeting_id, run_id)
            self._receipt(conn, command, "cluster_review", scope, request_hash, response)
            return response

    def review_segment(self, meeting_id, run_id, command: ReviewLocalAsrSegment):
        command = ReviewLocalAsrSegment.model_validate(command.model_dump(mode="json"))
        scope = canonical([meeting_id, run_id])
        with self.db.transaction() as conn:
            run = self._read(conn, meeting_id, run_id)
            replay, request_hash = self._operation(conn, command, "segment_review", scope)
            if replay is not None:
                return replay
            if command.expected_review_revision != run["review_revision"]:
                raise LocalAsrConflict("Review revision changed")
            segments = run["result"]["segment_items"]
            if command.segment_ordinal >= len(segments):
                raise LocalAsrNotFound("Segment not found")
            segment = segments[command.segment_ordinal]
            if command.participant_id is not None:
                preview = self._preview(conn, meeting_id, run_id, segment["speaker_label"])
                unsafe = next((item["reason"] for item in preview["excluded_segments"]
                               if item["segment_ordinal"] == command.segment_ordinal), None)
                # An explicit individual review may replace this run's overlay,
                # but cannot erase existing source confirmations or ambiguity.
                if unsafe not in (None, "individual_override", "previous_confirmation"):
                    raise LocalAsrConflict("Segment cannot be assigned to a single person: " + unsafe)
            participant = self._participant(conn, meeting_id, command.participant_id) if command.participant_id else None
            revision = run["review_revision"] + 1
            self._append_review(conn, run_id, command.segment_ordinal, revision, participant,
                                "manual_segment", segment["speaker_label"], now())
            conn.execute("UPDATE local_asr_review_state SET revision=? WHERE run_id=?", (revision, run_id))
            response = self._read(conn, meeting_id, run_id)
            self._receipt(conn, command, "segment_review", scope, request_hash, response)
            return response
