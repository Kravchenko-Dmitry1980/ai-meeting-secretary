"""Local enrollment lifecycle. Synchronous methods run outside the HTTP event loop."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import threading

from secretary.domain.enrollment import Enrollment, EnrollmentFailure
from secretary.domain.voice import ModelStamp, PrivateMaterial, VoiceError, canonical_uuid, MAX_PAYLOAD
from secretary.infrastructure.database import uid, now
from secretary.infrastructure.enrollment_repository import EnrollmentRepository
from secretary.infrastructure.maintenance_executor import MaintenanceExecutor
from secretary.infrastructure.voice_audio import EnrollmentDecoder, wav_bytes
from secretary.infrastructure.voice_engine import LocalVoiceEngine
from secretary.infrastructure.voice_store import VoiceStore, _current_user_owned
from secretary.infrastructure.speaker_repository import canonical
from secretary.application.voice_matching import CandidateMaterial


class EnrollmentService:
    def __init__(self, settings, db, capture, coordinator, *, store=None, decoder=None, engine=None, model=None):
        self.settings, self.db, self.capture, self.coordinator = settings, db, capture, coordinator
        self.repository = EnrollmentRepository(db)
        self.store = store or VoiceStore(settings.data_dir)
        self.store.partial_observer = self._track_partial
        self.decoder = decoder or EnrollmentDecoder()
        self.engine = engine or LocalVoiceEngine(settings.project_dir)
        self._model = model
        self._locks = {}
        self._lock = threading.Lock()
        self._events = {}
        self._closed = False
        self._condition = threading.Condition(self._lock)
        self._executor = MaintenanceExecutor(
            ThreadPoolExecutor(max_workers=1, thread_name_prefix='SecretaryEnrollment'),
            maintenance=getattr(db, 'maintenance', None), participant_id=getattr(db, 'participant_id', None))

    def lock(self, enrollment_id):
        with self._lock:
            return self._locks.setdefault(enrollment_id, threading.RLock())

    def _track_partial(self, profile_id, generation, basename, identity):
        self.db.execute('INSERT INTO enrollment_partials VALUES(?,?,?,?,?)',
                        (generation, profile_id, basename, str(identity[0]), str(identity[1])))

    def model(self):
        # Reviewed metadata only for encrypted draft; no runtime/native initialization.
        if self._model is not None:
            return self._model
        try:
            manifest = json.loads((self.settings.project_dir / 'config/voice-runtime/model-manifest.json').read_text('utf-8'))
            weights = next(f for f in manifest['files'] if f['name'] == 'embedding_model.ckpt')
            return ModelStamp(manifest['repository'], manifest['revision'], weights['sha256'])
        except (OSError, ValueError, KeyError, StopIteration):
            raise EnrollmentFailure('runtime_metadata_unavailable') from None

    def staging(self, recording_id, *, create=False):
        canonical_uuid(recording_id)
        base = self.settings.data_dir.absolute() / 'voice' / 'staging'
        path = base / recording_id
        for parent in (*reversed(path.parents), path):
            if parent.exists():
                stat = parent.lstat()
                if parent.is_symlink() or getattr(stat, 'st_file_attributes', 0) & 0x400:
                    raise EnrollmentFailure('private_path_invalid')
                if parent == self.settings.data_dir.absolute() or self.settings.data_dir.absolute() in parent.parents:
                    _current_user_owned(parent)
        if path.resolve().parent != base.resolve():
            raise EnrollmentFailure('private_path_invalid')
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def remove_staging(self, recording_id):
        path = self.staging(recording_id)
        if not path.exists():
            return
        # Own single recording folder only; never traverse a child directory/reparse.
        try:
            for item in path.iterdir():
                st = item.lstat()
                if item.is_symlink() or getattr(st, 'st_file_attributes', 0) & 0x400 or not item.is_file() or st.st_nlink != 1:
                    raise EnrollmentFailure('private_path_invalid')
                _current_user_owned(item)
                item.unlink()
            path.rmdir()
        except OSError:
            raise EnrollmentFailure('private_cleanup_pending') from None

    def cleanup(self, enrollment_id):
        pending = False
        for row in self.db.rows("SELECT * FROM enrollment_materials WHERE enrollment_id=? AND state!='active'", (enrollment_id,)):
            try:
                self.cleanup_partials(row['generation'])
                self.store.delete(row['profile_id'], row['generation'])
                self.db.execute('DELETE FROM enrollment_materials WHERE generation=?', (row['generation'],))
            except VoiceError:
                pending = True
                self.db.execute("UPDATE enrollment_materials SET state='cleanup_pending' WHERE generation=?", (row['generation'],))
        for record in self.db.rows("SELECT * FROM enrollment_recordings WHERE enrollment_id=? AND status NOT IN ('starting','recording','stopping')", (enrollment_id,)):
            try:
                if self.capture.lease.owner == (self.capture.namespace, record['id']):
                    raise EnrollmentFailure('capture_cleanup_pending')
                self.remove_staging(record['id'])
                if record['status'] == 'cleanup_pending':
                    self.db.execute("UPDATE enrollment_recordings SET status='interrupted',reason='recording_interrupted' WHERE id=?", (record['id'],))
            except (EnrollmentFailure, VoiceError):
                pending = True
                self.db.execute("UPDATE enrollment_recordings SET status='cleanup_pending',reason='private_cleanup_pending' WHERE id=?", (record['id'],))
        if pending:
            self.db.execute("UPDATE voice_enrollments SET status='cleanup_pending',reason_codes='[\"private_cleanup_pending\"]' WHERE id=? AND consent_confirmed=0", (enrollment_id,))
        else:
            self.db.execute("UPDATE voice_enrollments SET status='revoked',reason_codes='[]' WHERE id=? AND status='cleanup_pending' AND consent_confirmed=0", (enrollment_id,))
        return not pending

    def cleanup_partials(self, generation):
        for partial in self.db.rows('SELECT * FROM enrollment_partials WHERE generation=?', (generation,)):
            self.store.cleanup_tracked_partial(partial['profile_id'], generation, partial['basename'],
                                               (int(partial['device']), int(partial['inode'])))
            self.db.execute('DELETE FROM enrollment_partials WHERE generation=? AND basename=?', (generation, partial['basename']))

    def _material(self, row):
        self.repository.mapping(row)
        material = self.store.read(row['person_profile_id'], row['storage_key'])
        # PrivateMaterial.enrollment_id means immutable generation UUID, not public id.
        if (material.profile_id, material.enrollment_id, material.material_revision) != (row['person_profile_id'], row['storage_key'], row['revision']):
            raise EnrollmentFailure('material_mapping_mismatch')
        if (material.model.model_id, material.model.revision) != (row['model_id'], row['model_revision']):
            raise EnrollmentFailure('material_mapping_mismatch')
        return material

    def _publish_audio(self, profile_id, enrollment_id, payload, *, cancel=None):
        row, profile_revision = self.repository.snapshot(profile_id, enrollment_id, ('pending',))
        cancel = cancel or threading.Event()
        with self.coordinator.slot(enrollment_id, cancel):
            prepared = self.decoder.decode(payload, cancel=cancel)
        generation = uid()
        self.repository.ledger(row, generation)
        try:
            material = PrivateMaterial(profile_id, generation, row['revision'] + 1, self.model(), prepared, ())
            self.store.write(material)
            self.cleanup_partials(generation)
            return self.repository.publish(row, profile_revision, generation, material.model)
        finally:
            self.cleanup(enrollment_id)

    def upload(self, profile_id, payload, command):
        if self._closed:
            raise EnrollmentFailure('service_stopping')
        if not command.consent_confirmed:
            raise EnrollmentFailure('consent_required', 422)
        if not isinstance(payload, bytes) or len(payload) > MAX_PAYLOAD:
            raise EnrollmentFailure('upload_too_large', 413)
        with self.db.transaction() as conn:
            replay = self.repository.reserve(conn, 'enroll_upload', profile_id, command, payload_hash=hashlib.sha256(payload).hexdigest())
            if replay:
                return Enrollment(**replay)
            enrollment_id = self.repository.insert(conn, profile_id)
        cancel = threading.Event()
        with self._condition:
            self._events.setdefault(enrollment_id, []).append(cancel)
        try:
            with self.lock(enrollment_id):
                result = self._publish_audio(profile_id, enrollment_id, payload, cancel=cancel)
            self.repository.finish(command.operation_id, result)
            return result
        except (VoiceError, EnrollmentFailure) as exc:
            self.db.execute("UPDATE voice_enrollments SET status='failed',reason_codes=? WHERE id=? AND status='pending'", (json.dumps([exc.reason]), enrollment_id))
            status = getattr(exc, 'status', 422)
            self.repository.finish(command.operation_id, error=exc.reason, error_status=status)
            raise EnrollmentFailure(exc.reason, status) from None
        finally:
            with self._condition:
                self._events[enrollment_id].remove(cancel)
                self._condition.notify_all()

    def confirm(self, profile_id, enrollment_id, command):
        if self._closed:
            raise EnrollmentFailure('service_stopping')
        if not command.listening_confirmed or not command.single_speaker_confirmed:
            raise EnrollmentFailure('human_review_required', 422)
        with self.db.transaction() as conn:
            replay = self.repository.reserve(conn, 'enroll_confirm', canonical([profile_id, enrollment_id]), command)
            if replay:
                return Enrollment(**replay)
        cancel = threading.Event()
        acquired = False
        with self._lock:
            self._events.setdefault(enrollment_id, []).append(cancel)
        try:
            with self.coordinator.slot(enrollment_id, cancel), self.lock(enrollment_id):
                acquired = True
                row, profile_revision = self.repository.snapshot(profile_id, enrollment_id, ('awaiting_review',))
                if row['revision'] != command.expected_revision:
                    raise EnrollmentFailure('stale_material')
                material = self._material(row)
                self.repository.revalidate(row, profile_revision)
                clips = tuple(material.audio.pcm16[e.start_sample*2:e.end_sample*2] for e in material.audio.excerpts)
                batch = self.engine.embed(clips, cancel=cancel)
                if cancel.is_set():
                    raise EnrollmentFailure('operation_cancelled')
                generation = uid()
                self.repository.ledger(row, generation)
                ready = replace(material, enrollment_id=generation, material_revision=row['revision']+1, model=batch.model, embeddings=batch.vectors)
                self.store.write(ready)
                self.cleanup_partials(generation)
                result = self.repository.publish(row, profile_revision, generation, batch.model, ready=True)
                self.repository.finish(command.operation_id, result)
                return result
        except (VoiceError, EnrollmentFailure) as exc:
            status = getattr(exc, 'status', 409)
            self.repository.finish(command.operation_id, error=exc.reason, error_status=status)
            raise EnrollmentFailure(exc.reason, status) from None
        finally:
            try:
                if acquired:
                    with self.lock(enrollment_id):
                        self.cleanup(enrollment_id)
            finally:
                with self._condition:
                    self._events[enrollment_id].remove(cancel)
                    self._condition.notify_all()

    def preview(self, profile_id, enrollment_id, *, expected_revision):
        with self.lock(enrollment_id):
            row, profile_revision = self.repository.snapshot(profile_id, enrollment_id)
            if row['revision'] != expected_revision:
                raise EnrollmentFailure('stale_material')
            material = self._material(row)
            content = wav_bytes(material.audio.pcm16)
            self.repository.revalidate(row, profile_revision)
            return content

    def read_candidate(self, profile_id, enrollment_id, *, expected_revision, expected_profile_revision):
        """Task4 only: vectors/model/revisions, no WAV or opaque generation in result."""
        with self.lock(enrollment_id):
            row, profile_revision = self.repository.snapshot(profile_id, enrollment_id, ('ready',))
            if row['revision'] != expected_revision or profile_revision != expected_profile_revision:
                raise EnrollmentFailure('stale_material')
            material = self._material(row)
            if not material.embeddings:
                raise EnrollmentFailure('material_unavailable')
            self.repository.revalidate(row, profile_revision)
            return CandidateMaterial(profile_id, row['revision'], material.model, material.embeddings)

    def delete(self, profile_id, enrollment_id, command):
        with self.db.transaction() as conn:
            replay = self.repository.reserve(conn, 'enroll_delete', canonical([profile_id, enrollment_id]), command)
            if replay:
                return Enrollment(**replay)
            row = conn.execute('SELECT * FROM voice_enrollments WHERE id=? AND person_profile_id=?', (enrollment_id, profile_id)).fetchone()
            if row is None:
                raise EnrollmentFailure('enrollment_not_found', 404)
            if row['revision'] != command.expected_revision:
                raise EnrollmentFailure('stale_material')
            conn.execute("UPDATE voice_enrollments SET consent_confirmed=0,revision=revision+1,status='revoked',storage_key=NULL,revoked_at=?,reason_codes='[]' WHERE id=?", (now(), enrollment_id))
            conn.execute("UPDATE enrollment_materials SET state='cleanup_pending' WHERE enrollment_id=?", (enrollment_id,))
            conn.execute('UPDATE person_profiles SET revision=revision+1 WHERE id=?', (profile_id,))
            from secretary.infrastructure.assignment_repository import revalidate_voice_overlays
            revalidate_voice_overlays(conn,self.db)
        self.coordinator.cancel_material(enrollment_id)
        with self._lock:
            for event in self._events.get(enrollment_id, ()):
                event.set()
        for record in self.db.rows("SELECT * FROM enrollment_recordings WHERE enrollment_id=? AND status IN ('starting','recording','stopping')", (enrollment_id,)):
            self.db.execute("UPDATE enrollment_recordings SET disposition='cancel' WHERE id=?", (record['id'],))
            try:
                stopped = self.capture.stop(record['id'])
                if not stopped.get('native_closed', self.capture.legacy and not stopped.get('recording', False)):
                    raise EnrollmentFailure('capture_cleanup_pending')
                self.db.execute("UPDATE enrollment_recordings SET status='cancelled' WHERE id=?", (record['id'],))
            except Exception:
                self.db.execute("UPDATE enrollment_recordings SET status='cleanup_pending',reason='capture_cleanup_pending' WHERE id=?", (record['id'],))
        lock = self.lock(enrollment_id)
        if lock.acquire(timeout=2):
            try:
                self.cleanup(enrollment_id)
            finally:
                lock.release()
        else:
            self.db.execute("UPDATE voice_enrollments SET status='cleanup_pending',reason_codes='[\"worker_cleanup_pending\"]' WHERE id=?", (enrollment_id,))
        if not self.coordinator.wait_material(enrollment_id, 0):
            self.db.execute("UPDATE voice_enrollments SET status='cleanup_pending',reason_codes='[\"worker_cleanup_pending\"]' WHERE id=?", (enrollment_id,))
            if not self._closed:
                self._executor.submit(self._finish_revocation, enrollment_id)
        result = self.repository.get(profile_id, enrollment_id)
        self.repository.finish(command.operation_id, result)
        return result

    def _finish_revocation(self, enrollment_id):
        if self.coordinator.wait_material(enrollment_id, 125):
            with self.lock(enrollment_id):
                self.cleanup(enrollment_id)

    def start_recording(self, profile_id, command):
        if self._closed:
            raise EnrollmentFailure('service_stopping')
        if not command.consent_confirmed:
            raise EnrollmentFailure('consent_required', 422)
        # Replay before lease acquisition; first execution acquires before DB/device mutation.
        with self.db.transaction() as conn:
            existing = conn.execute('SELECT 1 FROM enrollment_commands WHERE operation_id=?', (command.operation_id,)).fetchone()
            if existing:
                from secretary.domain.enrollment import EnrollmentRecording
                return EnrollmentRecording(**self.repository.reserve(conn, 'enroll_start', profile_id, command))
            self.repository.profile(conn, profile_id)
        recording_id = uid()
        self.capture.claim(recording_id)
        try:
            with self.db.transaction() as conn:
                self.repository.reserve(conn, 'enroll_start', profile_id, command)
                enrollment_id = self.repository.insert(conn, profile_id)
                conn.execute("INSERT INTO enrollment_recordings(id,profile_id,enrollment_id,generation,status,created_at) VALUES(?,?,?,1,'starting',?)",
                    (recording_id, profile_id, enrollment_id, now()))
            def finished(snapshot):
                # No capture.state/stop/recover calls here: stop may be joining us.
                self.db.execute("UPDATE enrollment_recordings SET status=?,duration_ms=?,reason=? WHERE id=?", (
                    'processing' if snapshot.get('native_closed', True) else 'cleanup_pending', min(snapshot.get('duration_ms', 0), 30000),
                    ('capture_interrupted' if snapshot.get('error') else None) if snapshot.get('native_closed', True) else 'capture_cleanup_pending', recording_id))
                if snapshot.get('native_closed', True) and not self._closed:
                    self._executor.submit(self._finish_recording, recording_id, snapshot.get('chunks', []))
            result = self.capture.start(recording_id, command.microphone_id, None, lambda chunk: None, lambda error: None, finished)
            if result.get('recording'):
                self.db.execute("UPDATE enrollment_recordings SET status='recording' WHERE id=? AND status='starting'", (recording_id,))
            elif result.get('error'):
                raise EnrollmentFailure('capture_start_failed')
            response = self.recording_state(profile_id, recording_id)
            self.repository.finish(command.operation_id, response)
            return response
        except Exception:
            pending = self.capture.lease.owner == (self.capture.namespace, recording_id)
            reason = 'capture_cleanup_pending' if pending else 'capture_start_failed'
            if self.db.one('SELECT id FROM enrollment_recordings WHERE id=?', (recording_id,)):
                self.db.execute("UPDATE enrollment_recordings SET status=?,reason=? WHERE id=? AND status='starting'", (
                    'cleanup_pending' if pending else 'failed', reason, recording_id))
                self.repository.finish(command.operation_id, error=reason)
            # If no device start was attempted, native closure is already confirmed.
            else:
                self.capture._release(recording_id, {'native_closed': True})
            raise EnrollmentFailure(reason) from None

    def recording_state(self, profile_id, recording_id=None):
        row = self.db.one('SELECT * FROM enrollment_recordings WHERE profile_id=?' + (' AND id=?' if recording_id else '') + ' ORDER BY created_at DESC LIMIT 1',
                          (profile_id, recording_id) if recording_id else (profile_id,))
        if row is None:
            raise EnrollmentFailure('recording_not_found', 404)
        if row['status'] in ('starting', 'recording', 'stopping'):
            actual = self.capture.state(row['id'])
            row['duration_ms'] = min(max(int(actual.get('duration_ms', 0)), 0), 30000)
        return self.repository.recording(row)

    def stop_recording(self, profile_id, command):
        with self.db.transaction() as conn:
            replay = self.repository.reserve(conn, 'enroll_stop', profile_id, command)
            if replay:
                from secretary.domain.enrollment import EnrollmentRecording
                return EnrollmentRecording(**replay)
            row = conn.execute('SELECT * FROM enrollment_recordings WHERE id=? AND profile_id=?', (command.recording_id, profile_id)).fetchone()
            if row is None:
                raise EnrollmentFailure('recording_not_found', 404)
            if row['generation'] != command.generation:
                raise EnrollmentFailure('stale_recording')
            owns_pending = row['status'] == 'cleanup_pending' and self.capture.lease.owner == (self.capture.namespace, row['id'])
            if row['status'] not in ('starting','recording','stopping') and not owns_pending:
                if row['disposition'] != command.disposition:
                    raise EnrollmentFailure('recording_already_stopped')
                result = self.repository.recording(row)
                conn.execute("UPDATE enrollment_commands SET status='done',response=? WHERE operation_id=?", (result.model_dump_json(), command.operation_id))
                return result
            conn.execute("UPDATE enrollment_recordings SET disposition=?,status='stopping' WHERE id=?", (command.disposition, row['id']))
        try:
            snapshot = self.capture.stop(row['id'])
            if not snapshot.get('native_closed', self.capture.legacy and not snapshot.get('recording', False)):
                raise EnrollmentFailure('capture_cleanup_pending')
            # Native callback normally owns completion; injected callback-less fakes use this path.
            current = self.recording_state(profile_id, row['id'])
            if current.status == 'stopping':
                self.db.execute("UPDATE enrollment_recordings SET status='processing' WHERE id=?", (row['id'],))
                self._executor.submit(self._finish_recording, row['id'], snapshot.get('chunks', []))
            result = self.recording_state(profile_id, row['id'])
            self.repository.finish(command.operation_id, result)
            return result
        except Exception:
            self.db.execute("UPDATE enrollment_recordings SET status='cleanup_pending',reason='capture_cleanup_pending' WHERE id=?", (row['id'],))
            self.repository.finish(command.operation_id, error='capture_cleanup_pending')
            raise EnrollmentFailure('capture_cleanup_pending') from None

    def _finish_recording(self, recording_id, chunks):
        row = self.db.one('SELECT * FROM enrollment_recordings WHERE id=?', (recording_id,))
        enrollment_id = row['enrollment_id']
        with self.lock(enrollment_id):
            try:
                if row['disposition'] == 'cancel':
                    self.db.execute("UPDATE voice_enrollments SET status='cancelled',consent_confirmed=0,revision=revision+1 WHERE id=? AND status='pending'", (enrollment_id,))
                    self.db.execute("UPDATE enrollment_recordings SET status='cancelled' WHERE id=?", (recording_id,))
                    return
                folder = self.staging(recording_id)
                if len(chunks) != 1:
                    raise EnrollmentFailure('capture_material_invalid')
                path = Path(chunks[0]['path'])
                st = path.lstat()
                if path.resolve().parent != folder.resolve() or path.is_symlink() or getattr(st, 'st_file_attributes', 0) & 0x400 or st.st_nlink != 1 or st.st_size > MAX_PAYLOAD:
                    raise EnrollmentFailure('private_path_invalid')
                _current_user_owned(path)
                result = self._publish_audio(row['profile_id'], enrollment_id, path.read_bytes())
                if row['reason'] == 'capture_interrupted':
                    self.db.execute('UPDATE voice_enrollments SET reason_codes=? WHERE id=?',
                                    (json.dumps([*result.reason_codes, 'capture_interrupted']), enrollment_id))
                self.db.execute("UPDATE enrollment_recordings SET status=? WHERE id=?", (result.status, recording_id))
            except (EnrollmentFailure, VoiceError, OSError):
                self.db.execute("UPDATE voice_enrollments SET status='failed',reason_codes='[\"capture_material_invalid\"]' WHERE id=? AND status='pending'", (enrollment_id,))
                self.db.execute("UPDATE enrollment_recordings SET status='failed',reason='capture_material_invalid' WHERE id=?", (recording_id,))
            finally:
                self.cleanup(enrollment_id)

    def recover(self):
        # Bounded crash policy: discard incomplete capture plaintext; never auto-ready.
        self.db.execute("UPDATE enrollment_commands SET status='done',error='operation_interrupted' WHERE status='running'")
        self.db.execute("UPDATE enrollment_materials SET state='cleanup_pending' WHERE state='writing'")
        for row in self.db.rows("SELECT generation,enrollment_id FROM enrollment_materials WHERE state='active'"):
            try:
                self.cleanup_partials(row['generation'])
            except VoiceError:
                self.db.execute("UPDATE voice_enrollments SET reason_codes='[\"private_cleanup_pending\"]' WHERE id=?", (row['enrollment_id'],))
        self.db.execute("UPDATE enrollment_recordings SET status='interrupted',reason='recording_interrupted' WHERE status IN ('starting','recording','stopping','processing')")
        self.db.execute("UPDATE voice_enrollments SET status='interrupted',reason_codes='[\"operation_interrupted\"]' WHERE status='pending'")
        for row in self.db.rows('SELECT id FROM voice_enrollments'):
            with self.lock(row['id']):
                self.cleanup(row['id'])

    def close(self):
        self._closed = True
        self.coordinator.cancel()
        with self._lock:
            for events in self._events.values():
                for event in events:
                    event.set()
        self._executor.shutdown(wait=True, cancel_futures=True)
        # Bounded3a engine120s supervises its own process cleanup. Keep slot until return.
        import time
        deadline = time.monotonic() + 125
        with self._condition:
            while any(self._events.values()):
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise EnrollmentFailure('worker_cleanup_pending')
                self._condition.wait(min(remaining, 1))
        # Queued completion tasks cancelled at shutdown must not leave plaintext drafts.
        self.recover()
