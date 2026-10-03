"""Transactional lifecycle, immutable generation ledger, deterministic receipts."""
import hashlib
import json
from secretary.domain.enrollment import Enrollment, EnrollmentFailure, EnrollmentRecording
from secretary.infrastructure.database import now, uid
from secretary.infrastructure.speaker_repository import canonical


class EnrollmentRepository:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def public(row):
        if row is None:
            raise EnrollmentFailure('enrollment_not_found', 404)
        return Enrollment(**{k: (json.loads(row[k]) if k == 'reason_codes' else row[k])
                            for k in Enrollment.model_fields})

    @staticmethod
    def profile(conn, profile_id):
        row = conn.execute('SELECT * FROM person_profiles WHERE id=?', (profile_id,)).fetchone()
        if row is None:
            raise EnrollmentFailure('profile_not_found', 404)
        if not row['enabled']:
            raise EnrollmentFailure('profile_disabled')
        return row

    def list(self, profile_id):
        if not self.db.one('SELECT id FROM person_profiles WHERE id=?', (profile_id,)):
            raise EnrollmentFailure('profile_not_found', 404)
        return [self.public(r) for r in self.db.rows('SELECT * FROM voice_enrollments WHERE person_profile_id=? ORDER BY created_at,id', (profile_id,))]

    def get(self, profile_id, enrollment_id):
        return self.public(self.db.one('SELECT * FROM voice_enrollments WHERE id=? AND person_profile_id=?', (enrollment_id, profile_id)))

    @staticmethod
    def reserve(conn, kind, scope, command, *, payload_hash=None):
        digest = hashlib.sha256(canonical([command.model_dump(mode='json'), payload_hash]).encode()).hexdigest()
        row = conn.execute('SELECT * FROM enrollment_commands WHERE operation_id=?', (command.operation_id,)).fetchone()
        if row:
            if (row['kind'], row['scope'], row['request_hash']) != (kind, scope, digest):
                raise EnrollmentFailure('operation_payload_mismatch')
            if row['status'] == 'running':
                raise EnrollmentFailure('operation_in_progress')
            if row['error']:
                raise EnrollmentFailure(row['error'], row['error_status'])
            response = json.loads(row['response'])
            if kind in ('enroll_confirm', 'enroll_delete'):
                profile_id, enrollment_id = json.loads(scope)
                owned = conn.execute('SELECT 1 FROM voice_enrollments WHERE id=? AND person_profile_id=?', (enrollment_id, profile_id)).fetchone()
                if not owned or (response['person_profile_id'], response['id']) != (profile_id, enrollment_id):
                    raise EnrollmentFailure('operation_payload_mismatch')
            return response
        if conn.execute('SELECT 1 FROM speaker_operations WHERE operation_id=?', (command.operation_id,)).fetchone():
            raise EnrollmentFailure('operation_payload_mismatch')
        conn.execute("INSERT INTO enrollment_commands(operation_id,kind,scope,request_hash,status) VALUES(?,?,?,?,'running')", (command.operation_id, kind, scope, digest))
        return None

    def finish(self, operation_id, result=None, error=None, *, error_status=409):
        self.db.execute("UPDATE enrollment_commands SET status='done',response=?,error=?,error_status=? WHERE operation_id=?",
                        (result.model_dump_json() if result else None, error, error_status, operation_id))

    @staticmethod
    def insert(conn, profile_id):
        EnrollmentRepository.profile(conn, profile_id)
        version = conn.execute('SELECT COALESCE(MAX(material_version),0)+1 FROM voice_enrollments WHERE person_profile_id=?', (profile_id,)).fetchone()[0]
        enrollment_id = uid()
        conn.execute("INSERT INTO voice_enrollments(id,person_profile_id,material_version,revision,consent_confirmed,status,created_at) VALUES(?,?,?,0,1,'pending',?)", (enrollment_id, profile_id, version, now()))
        return enrollment_id

    def snapshot(self, profile_id, enrollment_id, statuses=('awaiting_review', 'ready')):
        with self.db.connection() as conn:
            conn.execute('BEGIN')
            profile = self.profile(conn, profile_id)
            row = conn.execute('SELECT * FROM voice_enrollments WHERE id=? AND person_profile_id=?', (enrollment_id, profile_id)).fetchone()
            if row is None:
                raise EnrollmentFailure('enrollment_not_found', 404)
            if not row['consent_confirmed'] or row['status'] not in statuses:
                raise EnrollmentFailure('material_unavailable')
            return dict(row), profile['revision']

    def valid(self, conn, row, profile_revision):
        profile = self.profile(conn, row['person_profile_id'])
        current = conn.execute('SELECT * FROM voice_enrollments WHERE id=? AND person_profile_id=?', (row['id'], row['person_profile_id'])).fetchone()
        if (profile['revision'] != profile_revision or current is None or not current['consent_confirmed']
                or current['revision'] != row['revision'] or current['status'] != row['status'] or current['storage_key'] != row['storage_key']):
            raise EnrollmentFailure('stale_material')
        return current

    def revalidate(self, row, profile_revision):
        with self.db.connection() as conn:
            conn.execute('BEGIN')
            self.valid(conn, row, profile_revision)

    def ledger(self, row, generation):
        self.db.execute("INSERT INTO enrollment_materials VALUES(?,?,?,?,'writing')", (generation, row['id'], row['person_profile_id'], row['revision'] + 1))

    def publish(self, row, profile_revision, generation, model, *, ready=False):
        with self.db.transaction() as conn:
            self.valid(conn, row, profile_revision)
            mapping = conn.execute("SELECT * FROM enrollment_materials WHERE generation=? AND enrollment_id=? AND profile_id=? AND material_revision=? AND state='writing'",
                (generation, row['id'], row['person_profile_id'], row['revision'] + 1)).fetchone()
            if not mapping:
                raise EnrollmentFailure('material_mapping_mismatch')
            conn.execute("UPDATE enrollment_materials SET state='cleanup_pending' WHERE enrollment_id=? AND generation!=?", (row['id'], generation))
            conn.execute("UPDATE enrollment_materials SET state='active' WHERE generation=?", (generation,))
            conn.execute("UPDATE voice_enrollments SET storage_key=?,revision=revision+1,status=?,model_id=?,model_revision=?,reason_codes=?,listening_confirmed=?,single_speaker_confirmed=? WHERE id=?",
                (generation, 'ready' if ready else 'awaiting_review', model.model_id, model.revision,
                 canonical([] if ready else ['listening_required', 'single_speaker_review_required', 'energy_screening_only']), int(ready), int(ready), row['id']))
            conn.execute('UPDATE person_profiles SET revision=revision+1,updated_at=? WHERE id=?', (now(), row['person_profile_id']))
        return self.get(row['person_profile_id'], row['id'])

    def mapping(self, row):
        mapping = self.db.one("SELECT * FROM enrollment_materials WHERE generation=? AND enrollment_id=? AND profile_id=? AND material_revision=? AND state='active'",
            (row['storage_key'], row['id'], row['person_profile_id'], row['revision']))
        if not mapping:
            raise EnrollmentFailure('material_mapping_mismatch')

    def recording(self, row):
        return EnrollmentRecording(recording_id=row['id'], person_profile_id=row['profile_id'], enrollment_id=row['enrollment_id'],
            generation=row['generation'], status=row['status'], disposition=row['disposition'], duration_ms=row['duration_ms'],
            reason_codes=[row['reason']] if row['reason'] else [])

    def voice_snapshot(self):
        """Task4 internal snapshot only; opaque generation must not enter public run DTO."""
        with self.db.connection() as conn:
            conn.execute('BEGIN')
            revision = conn.execute('SELECT revision FROM voice_state WHERE id=1').fetchone()[0]
            rows = conn.execute("SELECT e.id,e.person_profile_id,e.revision,e.storage_key,p.revision AS profile_revision FROM voice_enrollments e JOIN person_profiles p ON p.id=e.person_profile_id WHERE e.status='ready' AND e.consent_confirmed=1 AND p.enabled=1 ORDER BY e.id").fetchall()
            return revision, tuple(dict(r) for r in rows)

    def revalidate_voice_snapshot(self, snapshot, *, conn=None):
        """Task4 passes its BEGIN IMMEDIATE publication connection to close TOCTOU."""
        if conn is None:
            if self.voice_snapshot() != snapshot:
                raise EnrollmentFailure('stale_voice_snapshot')
            return
        current = conn.execute('SELECT revision FROM voice_state WHERE id=1').fetchone()[0]
        if current != snapshot[0]:
            raise EnrollmentFailure('stale_voice_snapshot')
