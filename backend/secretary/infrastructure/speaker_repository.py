from __future__ import annotations

import hashlib
import json

from secretary.domain.speakers import (
    AddParticipant, AttributionRun, AttributionSnapshot, CreateProfile,
    MeetingParticipant, ParticipantRosterSnapshot, PatchParticipant, PatchProfile,
    PersonProfile, ReviewAttribution, SpeakerConflict, SpeakerError, SpeakerNotFound,
    TaskAssignment,
)
from secretary.infrastructure.database import Database, now, uid


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class SpeakerRepository:
    """Local identity commands serialized by SQLite BEGIN IMMEDIATE.

    Operation IDs are globally unique across command kinds and scopes. Receipts
    contain the original response (identity metadata and source IDs, no speech).
    """

    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def _profile(row):
        if row is None:
            raise SpeakerNotFound("Profile not found")
        data = dict(row)
        data["aliases"] = json.loads(data["aliases"])
        data["enabled"] = bool(data["enabled"])
        return PersonProfile(**data)

    @staticmethod
    def _participant(row):
        data = dict(row)
        data["aliases"] = json.loads(data["aliases"])
        data["enabled"] = bool(data["enabled"])
        return MeetingParticipant(**data)

    @staticmethod
    def _meeting(conn, meeting_id):
        meeting = conn.execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone()
        if meeting is None:
            raise SpeakerNotFound("Meeting not found")
        return meeting

    @staticmethod
    def _replay(conn, kind, scope, command, model):
        digest = hashlib.sha256(canonical(command.model_dump(mode="json")).encode()).hexdigest()
        if conn.execute('SELECT 1 FROM enrollment_commands WHERE operation_id=?', (command.operation_id,)).fetchone():
            raise SpeakerConflict('operation_id already used with a different request or scope')
        old = conn.execute("SELECT * FROM speaker_operations WHERE operation_id=?", (command.operation_id,)).fetchone()
        if old:
            if (old["kind"], old["scope"], old["request_hash"]) != (kind, scope, digest):
                raise SpeakerConflict("operation_id already used with a different request or scope")
            return model.model_validate_json(old["response"]), digest
        return None, digest

    @staticmethod
    def _receipt(conn, kind, scope, command, digest, response, resource_ids, revision, meeting_id=None, version=None):
        stamp = now()
        conn.execute("INSERT INTO speaker_operations VALUES(?,?,?,?,?,?)", (
            command.operation_id, kind, scope, digest, response.model_dump_json(), stamp))
        conn.execute("INSERT INTO speaker_audit VALUES(?,?,?,?,?,?,?,?)", (
            uid(), command.operation_id, kind, meeting_id, version, canonical(resource_ids), revision, stamp))

    def list_profiles(self) -> list[PersonProfile]:
        return [self._profile(row) for row in self.db.rows("SELECT * FROM person_profiles ORDER BY created_at,id")]

    def create_profile(self, command: CreateProfile) -> PersonProfile:
        with self.db.transaction() as conn:
            replay, digest = self._replay(conn, "create_profile", "profiles", command, PersonProfile)
            if replay is not None:
                return replay
            profile_id, stamp = uid(), now()
            conn.execute("INSERT INTO person_profiles VALUES(?,?,?,?,0,?,?)", (
                profile_id, command.display_name, canonical(command.aliases), int(command.enabled), stamp, stamp))
            result = self._profile(conn.execute("SELECT * FROM person_profiles WHERE id=?", (profile_id,)).fetchone())
            self._receipt(conn, "create_profile", "profiles", command, digest, result, [profile_id], result.revision)
            return result

    def patch_profile(self, profile_id: str, command: PatchProfile) -> PersonProfile:
        with self.db.transaction() as conn:
            replay, digest = self._replay(conn, "patch_profile", profile_id, command, PersonProfile)
            if replay is not None:
                return replay
            profile = self._profile(conn.execute("SELECT * FROM person_profiles WHERE id=?", (profile_id,)).fetchone())
            if command.expected_revision != profile.revision:
                raise SpeakerConflict("Profile revision changed")
            edits = command.model_dump(exclude_unset=True, exclude={"expected_revision", "operation_id"})
            updated = profile.model_copy(update={**edits, "revision": profile.revision + 1, "updated_at": now()})
            conn.execute("UPDATE person_profiles SET display_name=?,aliases=?,enabled=?,revision=?,updated_at=? WHERE id=?", (
                updated.display_name, canonical(updated.aliases), int(updated.enabled), updated.revision, updated.updated_at, profile_id))
            from secretary.infrastructure.assignment_repository import revalidate_voice_overlays
            revalidate_voice_overlays(conn,self.db)
            self._receipt(conn, "patch_profile", profile_id, command, digest, updated, [profile_id], updated.revision)
            return updated

    def _roster(self, conn, meeting_id) -> ParticipantRosterSnapshot:
        self._meeting(conn, meeting_id)
        state = conn.execute("SELECT revision FROM participant_roster_state WHERE meeting_id=?", (meeting_id,)).fetchone()
        people = conn.execute("SELECT * FROM meeting_participants WHERE meeting_id=? ORDER BY created_at,id", (meeting_id,)).fetchall()
        return ParticipantRosterSnapshot(meeting_id=meeting_id, roster_revision=state[0] if state else 0,
                                         participants=[self._participant(row) for row in people])

    def get_roster(self, meeting_id: str) -> ParticipantRosterSnapshot:
        with self.db.connection() as conn:
            conn.execute("BEGIN")
            return self._roster(conn, meeting_id)

    @staticmethod
    def _bump_roster(conn, meeting_id, revision):
        conn.execute("INSERT INTO participant_roster_state VALUES(?,?) ON CONFLICT(meeting_id) DO UPDATE SET revision=excluded.revision",
                     (meeting_id, revision + 1))

    def add_participant(self, meeting_id: str, command: AddParticipant) -> ParticipantRosterSnapshot:
        with self.db.transaction() as conn:
            replay, digest = self._replay(conn, "add_participant", meeting_id, command, ParticipantRosterSnapshot)
            if replay is not None:
                return replay
            roster = self._roster(conn, meeting_id)
            if command.expected_roster_revision != roster.roster_revision:
                raise SpeakerConflict("Roster revision changed")
            profile = None
            if command.person_profile_id:
                profile = self._profile(conn.execute("SELECT * FROM person_profiles WHERE id=?", (command.person_profile_id,)).fetchone())
                if any(person.person_profile_id == profile.id for person in roster.participants):
                    raise SpeakerConflict("Profile already in meeting roster")
            participant_id, stamp = uid(), now()
            conn.execute("INSERT INTO meeting_participants VALUES(?,?,?,?,?,?,?,?,?)", (
                participant_id, meeting_id, profile.id if profile else None,
                profile.display_name if profile else command.display_name,
                canonical(profile.aliases if profile else command.aliases or []),
                int(command.enabled and (profile.enabled if profile else True)),
                profile.revision if profile else None, stamp, stamp))
            self._bump_roster(conn, meeting_id, roster.roster_revision)
            from secretary.infrastructure.assignment_repository import revalidate_local
            revalidate_local(conn,self.db,meeting_id)
            result = self._roster(conn, meeting_id)
            self._receipt(conn, "add_participant", meeting_id, command, digest, result, [participant_id], result.roster_revision, meeting_id)
            return result

    def patch_participant(self, meeting_id: str, participant_id: str, command: PatchParticipant) -> ParticipantRosterSnapshot:
        scope = canonical([meeting_id, participant_id])
        with self.db.transaction() as conn:
            replay, digest = self._replay(conn, "patch_participant", scope, command, ParticipantRosterSnapshot)
            if replay is not None:
                return replay
            roster = self._roster(conn, meeting_id)
            if command.expected_roster_revision != roster.roster_revision:
                raise SpeakerConflict("Roster revision changed")
            person = next((person for person in roster.participants if person.id == participant_id), None)
            if person is None:
                raise SpeakerNotFound("Meeting participant not found")
            edits = command.model_dump(exclude_unset=True, exclude={"expected_roster_revision", "operation_id", "apply_profile"})
            if command.apply_profile:
                if person.person_profile_id is None:
                    raise SpeakerError("Guest has no profile to apply")
                profile = self._profile(conn.execute("SELECT * FROM person_profiles WHERE id=?", (person.person_profile_id,)).fetchone())
                edits = {"display_name": profile.display_name, "aliases": profile.aliases,
                         "enabled": profile.enabled, "profile_revision": profile.revision, **edits}
            updated = person.model_copy(update={**edits, "updated_at": now()})
            conn.execute("UPDATE meeting_participants SET display_name=?,aliases=?,enabled=?,profile_revision=?,updated_at=? WHERE id=? AND meeting_id=?", (
                updated.display_name, canonical(updated.aliases), int(updated.enabled), updated.profile_revision,
                updated.updated_at, participant_id, meeting_id))
            self._bump_roster(conn, meeting_id, roster.roster_revision)
            from secretary.infrastructure.assignment_repository import revalidate_local
            revalidate_local(conn,self.db,meeting_id)
            result = self._roster(conn, meeting_id)
            self._receipt(conn, "patch_participant", scope, command, digest, result, [participant_id], result.roster_revision, meeting_id)
            return result

    def _attribution(self, conn, meeting_id, version=None) -> AttributionSnapshot:
        meeting = self._meeting(conn, meeting_id)
        version = meeting["transcript_version"] if version is None else version
        if version != meeting["transcript_version"] and not conn.execute(
                "SELECT 1 FROM segments WHERE meeting_id=? AND transcript_version=? LIMIT 1", (meeting_id, version)).fetchone():
            raise SpeakerNotFound("Transcript version not found")
        roster = self._roster(conn, meeting_id)
        state = conn.execute("SELECT * FROM attribution_state WHERE meeting_id=? AND transcript_version=?", (meeting_id, version)).fetchone()
        revision, run_id = (state["revision"], state["run_id"]) if state else (0, None)
        rows = conn.execute("""SELECT s.id AS segment_id,s.meeting_id,s.transcript_version,s.chunk_id,s.speaker_id,
            p.provider_label,s.channel,s.ordinal,s.start_ms,s.end_ms,
            a.participant_id,a.method,a.raw_score,a.status,a.reason_codes,a.run_id
            FROM segments s LEFT JOIN speakers p ON p.id=s.speaker_id AND p.meeting_id=s.meeting_id AND p.chunk_id=s.chunk_id
            LEFT JOIN speaker_attributions a ON a.segment_id=s.id AND a.run_id=?
            WHERE s.meeting_id=? AND s.transcript_version=? ORDER BY s.chunk_id,s.ordinal,s.id""", (run_id, meeting_id, version)).fetchall()
        items = []
        proposal_rows = {r['segment_id']:dict(r) for r in conn.execute('''SELECT p.* FROM attribution_proposal_lineage l
            JOIN identification_proposals p ON p.intent_id=l.source_intent_id AND p.segment_id=l.segment_id
            WHERE l.run_id=? AND l.meeting_id=? AND l.transcript_version=? AND l.revision=?''', (run_id,meeting_id,version,revision))}
        for row in rows:
            item = dict(row)
            item.update(method=item["method"] or "unknown", status=item["status"] or "unknown",
                        reason_codes=json.loads(item["reason_codes"]) if item["reason_codes"] else [], revision=revision)
            proposal = proposal_rows.get(item['segment_id'])
            if proposal and item['method'] != 'manual':
                item.update(group_id=proposal['group_id'],review_candidates=json.loads(proposal['review_candidates']))
            items.append(item)
        prior = conn.execute("SELECT snapshot FROM identification_intents WHERE meeting_id=? AND transcript_version=? AND outcome='completed' ORDER BY created_at DESC,id DESC LIMIT 1", (meeting_id,version)).fetchone()
        captured = json.loads(prior['snapshot']) if prior else None
        stale = bool(captured and any(i['method']=='voice_embedding' for i in items) and (
            captured['voice_revision'] != conn.execute('SELECT revision FROM voice_state WHERE id=1').fetchone()[0]
            or captured['roster_revision'] != roster.roster_revision))
        bypass = conn.execute('SELECT 1 FROM identification_bypasses WHERE meeting_id=? AND transcript_version=?', (meeting_id,version)).fetchone()
        return AttributionSnapshot(**roster.model_dump(), transcript_version=version, revision=revision, items=items,
            automatic_overlay_stale=stale,reason_codes=(['automatic_overlay_requires_review'] if stale else [])+(['explicit_runtime_bypass'] if bypass else []))

    def get_attribution(self, meeting_id: str, transcript_version: int | None = None) -> AttributionSnapshot:
        with self.db.connection() as conn:
            conn.execute("BEGIN")
            return self._attribution(conn, meeting_id, transcript_version)

    @staticmethod
    def _insert_run(conn, snapshot, kind, status, profile_snapshot):
        run = AttributionRun(id=uid(), meeting_id=snapshot.meeting_id, transcript_version=snapshot.transcript_version,
                             expected_revision=snapshot.revision, roster_revision=snapshot.roster_revision,
                             revision=snapshot.revision + 1, kind=kind, status=status,
                             profile_snapshot=profile_snapshot, roster_snapshot=snapshot.participants, created_at=now())
        conn.execute("INSERT INTO attribution_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            run.id, run.meeting_id, run.transcript_version, run.expected_revision, run.roster_revision, run.revision,
            run.kind, run.status, canonical(run.profile_snapshot), canonical([person.model_dump() for person in run.roster_snapshot]), run.audio_hash, run.model_id,
            run.model_revision, run.algorithm_version, run.created_at))
        return run

    def review_attribution(self, meeting_id: str, command: ReviewAttribution) -> AttributionSnapshot:
        scope = canonical([meeting_id, command.transcript_version])
        with self.db.transaction() as conn:
            replay, digest = self._replay(conn, "review_attribution", scope, command, AttributionSnapshot)
            if replay is not None:
                return replay
            meeting = self._meeting(conn, meeting_id)
            if command.transcript_version != meeting["transcript_version"]:
                raise SpeakerConflict("Only current transcript version can be changed")
            previous = self._attribution(conn, meeting_id, command.transcript_version)
            if previous.revision != command.expected_revision:
                raise SpeakerConflict("Attribution revision changed")
            changes = {change.segment_id: change.participant_id for change in command.changes}
            if changes.keys() - {item.segment_id for item in previous.items}:
                raise SpeakerNotFound("Segment not found in meeting/transcript version")
            participants = {person.id: person for person in previous.participants}
            for participant_id in changes.values():
                if participant_id is not None and participant_id not in participants:
                    raise SpeakerNotFound("Meeting participant not found")
                if participant_id is not None and not participants[participant_id].enabled:
                    raise SpeakerError("Disabled participant cannot be assigned")
            profiles = [self._profile(row).model_dump() for row in conn.execute("""SELECT p.* FROM person_profiles p
                WHERE EXISTS(SELECT 1 FROM meeting_participants mp WHERE mp.meeting_id=? AND mp.person_profile_id=p.id) ORDER BY p.id""", (meeting_id,))]
            run = self._insert_run(conn, previous, "manual", "published", profiles)
            for item in previous.items:
                if item.segment_id in changes:
                    participant_id = changes[item.segment_id]
                    method, status = "manual", "confirmed" if participant_id else "unknown"
                    score, reasons = None, ["manual_override" if participant_id else "manual_unset"]
                else:
                    participant_id, method, status, score, reasons = item.participant_id, item.method, item.status, item.raw_score, item.reason_codes
                conn.execute("INSERT INTO speaker_attributions VALUES(?,?,?,?,?,?,?,?,?,?)", (
                    run.id, item.segment_id, meeting_id, command.transcript_version,
                    participant_id, method, score, status, canonical(reasons), run.revision))
                if item.segment_id not in changes and method!='manual':
                    conn.execute('''INSERT INTO attribution_proposal_lineage
                        SELECT ?,segment_id,meeting_id,transcript_version,?,source_intent_id FROM attribution_proposal_lineage
                        WHERE run_id=? AND segment_id=? AND meeting_id=? AND transcript_version=? AND revision=?''',
                        (run.id,run.revision,item.run_id,item.segment_id,meeting_id,command.transcript_version,previous.revision))
            conn.execute("INSERT INTO attribution_state VALUES(?,?,?,?) ON CONFLICT(meeting_id,transcript_version) DO UPDATE SET revision=excluded.revision,run_id=excluded.run_id",
                         (meeting_id, command.transcript_version, run.revision, run.id))
            from secretary.infrastructure.assignment_repository import revalidate_local
            revalidate_local(conn,self.db,meeting_id,command.transcript_version)
            result = self._attribution(conn, meeting_id, command.transcript_version)
            self._receipt(conn, "review_attribution", scope, command, digest, result, [run.id, *changes.keys()], result.revision, meeting_id, command.transcript_version)
            return result

    def create_attribution_run(self, meeting_id: str, transcript_version: int, expected_revision: int) -> AttributionRun:
        """Capture a pending automatic run; this does not publish/infer identities.

        Task3 must validate captured revisions and enrollment versions again at
        publication. Profiles include only public metadata; no voice material.
        """
        with self.db.transaction() as conn:
            meeting = self._meeting(conn, meeting_id)
            if meeting["transcript_version"] != transcript_version:
                raise SpeakerConflict("Only current transcript version can be changed")
            snapshot = self._attribution(conn, meeting_id, transcript_version)
            if snapshot.revision != expected_revision:
                raise SpeakerConflict("Attribution revision changed")
            profiles = [self._profile(row).model_dump() for row in conn.execute("SELECT * FROM person_profiles ORDER BY id")]
            return self._insert_run(conn, snapshot, "automatic", "pending", profiles)

    def list_task_assignments(self, meeting_id: str, summary_version: int, revision: int) -> list[TaskAssignment]:
        """Read an explicit assignment snapshot; no derivation or seeding here."""
        self.db.meeting(meeting_id)
        rows = self.db.rows("SELECT * FROM task_assignments WHERE meeting_id=? AND summary_version=? AND revision=? ORDER BY action_id",
                            (meeting_id, summary_version, revision))
        for row in rows:
            row["source_segment_ids"] = json.loads(row["source_segment_ids"])
        return [TaskAssignment(**row) for row in rows]
