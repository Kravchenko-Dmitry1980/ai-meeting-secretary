"""Durable local identification authority. Core inference has no write authority."""
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import threading

from secretary.application.identification import IdentificationInput, SegmentSnapshot, identify_groups
from secretary.application.voice_matching import CalibrationSnapshot
from secretary.domain.identification import IdentificationAccepted, IdentificationState
from secretary.domain.enrollment import EnrollmentFailure
from secretary.domain.speakers import SpeakerConflict, SpeakerNotFound
from secretary.domain.voice import ModelStamp, VoiceError
from secretary.infrastructure.database import now, uid
from secretary.infrastructure.meeting_voice_audio import ChunkAudioSnapshot, MeetingAudioReader
from secretary.infrastructure.speaker_repository import SpeakerRepository, canonical


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class JobCancellation(threading.Event):
    def __init__(self, db, job_id, stopping):
        super().__init__()
        self.db, self.job_id, self.stopping = db, job_id, stopping

    def is_set(self):
        return super().is_set() or self.stopping.is_set() or bool(self.db.job(self.job_id, internal=True)['cancel_requested'])


class AttributionPipeline:
    def __init__(self, db, settings, enrollments, coordinator, *, calibration=None, reader_factory=MeetingAudioReader):
        self.db, self.settings, self.enrollments, self.coordinator = db, settings, enrollments, coordinator
        self.speakers = SpeakerRepository(db)
        self.calibration, self.reader_factory = calibration, reader_factory

    @staticmethod
    def sources(conn, meeting_id, version):
        chunks = [dict(r) for r in conn.execute('SELECT id,meeting_id,sequence,channel,path,offset_ms,duration_ms,sha256 FROM chunks WHERE meeting_id=? ORDER BY sequence,channel,id', (meeting_id,))]
        segments = [dict(r) for r in conn.execute('SELECT * FROM segments WHERE meeting_id=? AND transcript_version=? ORDER BY chunk_id,ordinal,id', (meeting_id, version))]
        return chunks, segments

    def capture(self, meeting_id, version, expected_revision, *, command=None, pipeline=False):
        with self.db.transaction() as conn:
            scope = canonical([meeting_id, version])
            receipt_hash = None
            if command:
                replay, receipt_hash = self.speakers._replay(conn, 'identify_speakers', scope, command, IdentificationAccepted)
                if replay:
                    return replay
            meeting = self.speakers._meeting(conn, meeting_id)
            if meeting['transcript_version'] != version:
                raise SpeakerConflict('transcript_version_changed')
            if meeting['recording']:
                raise SpeakerConflict('finish_recording_before_identification')
            base = self.speakers._attribution(conn, meeting_id, version)
            if base.revision != expected_revision:
                raise SpeakerConflict('attribution_revision_changed')
            route = conn.execute('SELECT route_hash FROM meeting_transcript_routes WHERE meeting_id=? AND transcript_version=?', (meeting_id, version)).fetchone()
            if route is None:
                raise SpeakerConflict('legacy_route_needs_review')
            profiles = [dict(r) for r in conn.execute("SELECT p.* FROM person_profiles p WHERE EXISTS(SELECT 1 FROM meeting_participants mp WHERE mp.meeting_id=? AND mp.person_profile_id=p.id) ORDER BY p.id", (meeting_id,))]
            materials = [dict(r) for r in conn.execute("""SELECT e.id,e.person_profile_id,e.material_version,e.revision,e.consent_confirmed,e.status,
                e.model_id,e.model_revision,p.revision AS profile_revision FROM voice_enrollments e JOIN person_profiles p ON p.id=e.person_profile_id
                WHERE e.status='ready' AND e.consent_confirmed=1 AND e.listening_confirmed=1 AND e.single_speaker_confirmed=1 AND p.enabled=1
                AND EXISTS(SELECT 1 FROM meeting_participants mp WHERE mp.meeting_id=? AND mp.person_profile_id=p.id AND mp.enabled=1)
                ORDER BY e.person_profile_id,e.material_version,e.revision,e.id""", (meeting_id,))]
            chunks, segments = self.sources(conn, meeting_id, version)
            try:
                model = asdict(self.enrollments.model()) if self.enrollments else None
            except (EnrollmentFailure, VoiceError):
                model = None
            calibration = asdict(self.calibration) if self.calibration else None
            snapshot = {'contract': 'meeting-identification-v1', 'meeting_id': meeting_id, 'version': version,
                'route_hash': route['route_hash'], 'expected_revision': base.revision, 'base_run_id': next((i.run_id for i in base.items if i.run_id), None),
                'roster_revision': base.roster_revision, 'roster': [p.model_dump() for p in base.participants],
                'profiles': profiles, 'materials': materials, 'voice_revision': conn.execute('SELECT revision FROM voice_state WHERE id=1').fetchone()[0],
                'chunks': chunks, 'segments': segments, 'audio_hash': digest(chunks), 'segment_hash': digest(segments),
                'model': model, 'calibration': calibration, 'algorithm_revision': 'mean-cosine-v1', 'group_policy': 'scoped-all-observations-v1'}
            key = digest(snapshot)
            existing = conn.execute('SELECT * FROM identification_intents WHERE meeting_id=? AND transcript_version=? AND intent_key=?', (meeting_id, version, key)).fetchone()
            if existing:
                result = IdentificationAccepted(job_id=existing['job_id'], run_id=existing['run_id'], intent_key=key)
                if command and command.retry and existing['outcome'] in ('cancelled','error','runtime_unavailable'):
                    conn.execute("DELETE FROM identification_proposals WHERE intent_id=?", (existing['id'],))
                    conn.execute("UPDATE identification_intents SET outcome='pending',reasons='[]',progress='{}' WHERE id=?", (existing['id'],))
                    conn.execute("UPDATE attribution_runs SET status='pending' WHERE id=? AND status!='published'", (existing['run_id'],))
                    conn.execute("UPDATE jobs SET status='queued',cancel_requested=0,error=NULL WHERE id=?", (existing['job_id'],))
                    conn.execute('UPDATE identification_intents SET pipeline=0 WHERE id=?', (existing['id'],))
            else:
                run = self.speakers._insert_run(conn, base, 'automatic', 'pending', [self.speakers._profile(p).model_dump() for p in profiles])
                conn.execute('UPDATE attribution_runs SET audio_hash=?,model_id=?,model_revision=?,algorithm_version=? WHERE id=?', (
                    snapshot['audio_hash'], model['model_id'] if model else None, model['revision'] if model else None, snapshot['algorithm_revision'], run.id))
                job_id, intent_id, stamp = uid(), uid(), now()
                conn.execute("INSERT INTO jobs(id,meeting_id,stage,status,created_at,updated_at,version,intent_key,payload) VALUES(?,?,'identify_speakers','queued',?,?,?,?,?)", (
                    job_id, meeting_id, stamp, stamp, version, key, canonical({'run_id': run.id, 'intent_id': intent_id})))
                conn.execute('INSERT INTO identification_intents(id,meeting_id,transcript_version,intent_key,run_id,job_id,snapshot,pipeline,created_at) VALUES(?,?,?,?,?,?,?,?,?)', (
                    intent_id, meeting_id, version, key, run.id, job_id, canonical(snapshot), int(pipeline), stamp))
                result = IdentificationAccepted(job_id=job_id, run_id=run.id, intent_key=key)
            if command:
                conn.execute("UPDATE meeting_pipeline_handoffs SET summary_authorized=0,authorization_reason='local_only' WHERE meeting_id=? AND transcript_version=? AND summary_job_id IS NULL", (meeting_id,version))
                conn.execute('UPDATE identification_intents SET pipeline=0 WHERE job_id=?', (result.job_id,))
                self.speakers._receipt(conn, 'identify_speakers', scope, command, receipt_hash, result, [result.run_id, result.job_id], base.revision, meeting_id, version)
            return result

    def current(self, meeting_id, version):
        return self.db.one('SELECT * FROM identification_intents WHERE meeting_id=? AND transcript_version=? AND pipeline=1 ORDER BY created_at,id LIMIT 1', (meeting_id, version))

    def state(self, meeting_id, run_id):
        row = self.db.one('SELECT * FROM identification_intents WHERE meeting_id=? AND run_id=?', (meeting_id, run_id))
        if not row:
            raise SpeakerNotFound('identification_run_not_found')
        snapshot = json.loads(row['snapshot'])
        bypass = self.db.one('SELECT attribution_revision FROM identification_bypasses WHERE meeting_id=? AND transcript_version=?', (meeting_id,row['transcript_version']))
        return IdentificationState(job_id=row['job_id'], run_id=row['run_id'], intent_key=row['intent_key'], transcript_version=row['transcript_version'],
            outcome=row['outcome'], reason_codes=json.loads(row['reasons']), progress=json.loads(row['progress']),
            calibration_revision=snapshot['calibration']['revision'] if snapshot['calibration'] else None,
            attribution_revision=snapshot['expected_revision']+1 if row['outcome']=='completed' else None,
            bypass_acknowledged=bool(bypass),bypass_attribution_revision=bypass['attribution_revision'] if bypass else None)

    def barrier(self, meeting_id, version):
        bypass = self.db.one('SELECT 1 FROM identification_bypasses WHERE meeting_id=? AND transcript_version=?', (meeting_id, version))
        row = self.db.one("SELECT * FROM identification_intents WHERE meeting_id=? AND transcript_version=? AND outcome='completed' ORDER BY created_at DESC,id DESC LIMIT 1", (meeting_id,version))
        voice = self.db.one('SELECT revision FROM voice_state WHERE id=1')['revision']
        return bool(bypass or row and json.loads(row['snapshot'])['voice_revision']==voice)

    def bypass(self, meeting_id, command):
        scope = canonical([meeting_id, command.transcript_version])
        with self.db.transaction() as conn:
            replay, request_hash = self.speakers._replay(conn, 'bypass_identification', scope, command, IdentificationState)
            if replay:
                return replay
            meeting = self.speakers._meeting(conn, meeting_id)
            if meeting['transcript_version'] != command.transcript_version:
                raise SpeakerConflict('transcript_version_changed')
            base = self.speakers._attribution(conn, meeting_id, command.transcript_version)
            if base.revision != command.expected_revision:
                raise SpeakerConflict('attribution_revision_changed')
            row = conn.execute("SELECT * FROM identification_intents WHERE meeting_id=? AND transcript_version=? AND outcome='runtime_unavailable' ORDER BY created_at DESC LIMIT 1", (meeting_id, command.transcript_version)).fetchone()
            if not row:
                raise SpeakerConflict('bypass_requires_runtime_unavailable')
            existing = conn.execute('SELECT 1 FROM identification_bypasses WHERE meeting_id=? AND transcript_version=?', (meeting_id,command.transcript_version)).fetchone()
            revision = base.revision
            if not existing:
                # Explicit bypass publishes a fresh manual/unknown overlay. Old
                # automatic identities remain historical, never silently reused.
                run = self.speakers._insert_run(conn,base,'manual','published',[])
                revision = run.revision
                for item in base.items:
                    manual = item.method=='manual' or 'manual_unset' in item.reason_codes
                    participant,method,score,status,reasons = (item.participant_id,item.method,item.raw_score,item.status,item.reason_codes) if manual else (None,'unknown',None,'unknown',['explicit_runtime_bypass'])
                    conn.execute('INSERT INTO speaker_attributions VALUES(?,?,?,?,?,?,?,?,?,?)', (
                        run.id,item.segment_id,meeting_id,command.transcript_version,participant,method,score,status,canonical(reasons),revision))
                conn.execute('INSERT INTO attribution_state VALUES(?,?,?,?) ON CONFLICT(meeting_id,transcript_version) DO UPDATE SET revision=excluded.revision,run_id=excluded.run_id', (meeting_id,command.transcript_version,revision,run.id))
                from secretary.infrastructure.assignment_repository import revalidate_local
                revalidate_local(conn,self.db,meeting_id,command.transcript_version)
            conn.execute('INSERT INTO identification_bypasses VALUES(?,?,?,?,?,?) ON CONFLICT(meeting_id,transcript_version) DO NOTHING', (
                meeting_id, command.transcript_version, revision, base.roster_revision, command.operation_id, now()))
            self.db.authorize_pipeline(meeting_id,command.transcript_version,explicit=True,reason='bypass',conn=conn)
            snap = json.loads(row['snapshot'])
            result = IdentificationState(job_id=row['job_id'],run_id=row['run_id'],intent_key=row['intent_key'],transcript_version=command.transcript_version,
                outcome='bypassed',reason_codes=['explicit_runtime_bypass'],progress=json.loads(row['progress']),
                calibration_revision=snap['calibration']['revision'] if snap['calibration'] else None,attribution_revision=revision,
                bypass_acknowledged=True,bypass_attribution_revision=revision)
            self.speakers._receipt(conn, 'bypass_identification', scope, command, request_hash, result, [row['run_id']], revision, meeting_id, command.transcript_version)
            return result

    def _guard(self, conn, snapshot):
        meeting = self.speakers._meeting(conn, snapshot['meeting_id'])
        if meeting['transcript_version'] != snapshot['version'] or meeting['recording']:
            raise SpeakerConflict('transcript_version_changed')
        base = self.speakers._attribution(conn, snapshot['meeting_id'], snapshot['version'])
        if base.revision != snapshot['expected_revision'] or base.roster_revision != snapshot['roster_revision']:
            raise SpeakerConflict('identity_revision_changed')
        if conn.execute('SELECT revision FROM voice_state WHERE id=1').fetchone()[0] != snapshot['voice_revision']:
            raise SpeakerConflict('voice_revision_changed')
        chunks, segments = self.sources(conn, snapshot['meeting_id'], snapshot['version'])
        if chunks != snapshot['chunks'] or segments != snapshot['segments']:
            raise SpeakerConflict('source_manifest_changed')
        route = conn.execute('SELECT route_hash FROM meeting_transcript_routes WHERE meeting_id=? AND transcript_version=?', (snapshot['meeting_id'], snapshot['version'])).fetchone()
        if not route or route[0] != snapshot['route_hash'] or [p.model_dump() for p in base.participants] != snapshot['roster']:
            raise SpeakerConflict('snapshot_changed')
        if (asdict(self.calibration) if self.calibration else None) != snapshot['calibration']:
            raise SpeakerConflict('calibration_changed')
        if snapshot['model'] and self.enrollments and asdict(self.enrollments.model()) != snapshot['model']:
            raise SpeakerConflict('model_changed')
        return base

    def _terminal(self, intent, outcome, reasons):
        with self.db.transaction() as conn:
            conn.execute('UPDATE identification_intents SET outcome=?,reasons=? WHERE id=?', (outcome, canonical(reasons), intent['id']))
            conn.execute("UPDATE attribution_runs SET status=? WHERE id=? AND status!='published'", ('stale' if outcome=='stale' else 'failed', intent['run_id']))
            conn.execute('UPDATE jobs SET status=?,error=?,updated_at=? WHERE id=?', ('cancelled' if outcome=='cancelled' else 'failed', ','.join(reasons), now(), intent['job_id']))
            conn.execute('UPDATE meetings SET status=?,error=?,updated_at=? WHERE id=? AND transcript_version=?', (
                'cancelled' if outcome=='cancelled' else 'partial_error', ','.join(reasons), now(), intent['meeting_id'], intent['transcript_version']))

    def execute(self, job, stopping):
        intent = self.db.one('SELECT * FROM identification_intents WHERE job_id=?', (job['id'],))
        if not intent:
            self.db.finish_job(job['id'], 'failed', 'identification_intent_missing')
            return False
        if intent['outcome']=='completed':
            self.db.finish_job(job['id'], 'succeeded')
            return True
        snapshot = json.loads(intent['snapshot'])
        cancel = JobCancellation(self.db, job['id'], stopping)
        try:
            if cancel.is_set():
                raise VoiceError('cancelled')
            with self.db.transaction() as conn:
                self._guard(conn, snapshot)
                conn.execute('DELETE FROM identification_proposals WHERE intent_id=?', (intent['id'],))
                conn.execute("UPDATE meetings SET status='identify_speakers',error=NULL WHERE id=?", (intent['meeting_id'],))
            materials = snapshot['materials']
            with self.coordinator.slot(intent['run_id'], cancel, material_ids=[m['id'] for m in materials]):
                latest = {}
                for material in materials:
                    latest[material['person_profile_id']] = material
                candidates = tuple(self.enrollments.read_candidate(profile_id, m['id'], expected_revision=m['revision'], expected_profile_revision=m['profile_revision'])
                    for profile_id, m in sorted(latest.items()))
                if candidates and snapshot['model'] is None:
                    raise EnrollmentFailure('runtime_metadata_unavailable')
                reader = self.reader_factory(self.settings.data_dir / 'audio', ffmpeg_path=self.settings.ffmpeg_path)
                outcomes = set()
                if not candidates:
                    for s in snapshot['segments']:
                        self._store_proposal(intent, s['id'], None, 'unknown', None, ['no_candidates'], [], 'vg_' + digest([snapshot['meeting_id'],snapshot['version'],s['chunk_id'],s['channel'],s['speaker_id'],s['id'] if s['speaker_id'] is None else None]))
                    self.db.execute('UPDATE identification_intents SET progress=? WHERE id=?', (canonical({'observations_processed':len(snapshot['segments']),'observations_embedded':0,'observations_skipped':len(snapshot['segments']),'observations_total':len(snapshot['segments']),'inference_batches':0}), intent['id']))
                else:
                    core = IdentificationInput(snapshot['meeting_id'], snapshot['version'], ModelStamp(**snapshot['model']),
                        tuple(ChunkAudioSnapshot(c['id'],c['meeting_id'],Path(c['path']),c['sha256'],c['offset_ms'],c['duration_ms'],c['channel']) for c in snapshot['chunks']),
                        tuple(SegmentSnapshot(s['id'],s['meeting_id'],s['transcript_version'],s['chunk_id'],s['speaker_id'],s['channel'],s['start_ms'],s['end_ms'],s['timing_precision'],s['text']) for s in snapshot['segments']),
                        candidates, self.calibration)
                    for group in identify_groups(core, reader, self.enrollments.engine, cancel=cancel):
                        outcomes.add(group.outcome)
                        for proposal in group.proposals:
                            participant = self._participant(snapshot, proposal.proposed_profile_id)
                            reasons = list(proposal.reason_codes)
                            if proposal.proposed_profile_id and participant is None:
                                reasons.append('ambiguous_roster_mapping')
                            reviews = []
                            for c in proposal.candidates:
                                target = self._participant(snapshot,c.profile_id)
                                if target:
                                    reviews.append({'participant_id':target,'material_revision':c.material_revision,'raw_score':c.raw_score})
                            self._store_proposal(intent,proposal.segment_id,participant,proposal.status if participant or not proposal.proposed_profile_id else 'unknown',
                                proposal.raw_score,reasons,reviews,proposal.group_id)
                            if set(reasons) & {'audio_changed','audio_hash_mismatch'}:
                                raise VoiceError('audio_changed')
                        self.db.execute('UPDATE identification_intents SET progress=? WHERE id=?', (canonical({**asdict(group.progress),'observations_total':len(snapshot['segments']),**asdict(reader.counters)}), intent['id']))
                    if 'runtime_unavailable' in outcomes:
                        raise EnrollmentFailure('voice_runtime_unavailable')
                    if 'error' in outcomes:
                        raise VoiceError('voice_engine_error')
                if cancel.is_set():
                    raise VoiceError('cancelled')
                # Reader guards read-time hashes; its final identity check closes the inference-to-publish window.
                reader.revalidate(cancel=cancel)
                with self.db.transaction() as conn:
                    if cancel.is_set():
                        raise VoiceError('cancelled')
                    base = self._guard(conn, snapshot)
                    reader.revalidate(cancel=cancel)
                    proposals = {r['segment_id']:dict(r) for r in conn.execute('SELECT * FROM identification_proposals WHERE intent_id=?', (intent['id'],))}
                    if set(proposals) != {s['id'] for s in snapshot['segments']}:
                        raise VoiceError('incomplete_identification')
                    revision = snapshot['expected_revision']+1
                    for item in base.items:
                        p = proposals[item.segment_id]
                        manual = item.method=='manual' or 'manual_unset' in item.reason_codes
                        participant, method, score, status, reasons = (item.participant_id,item.method,item.raw_score,item.status,item.reason_codes) if manual else (p['participant_id'],p['method'],p['raw_score'],p['status'],json.loads(p['reason_codes']))
                        conn.execute('INSERT INTO speaker_attributions VALUES(?,?,?,?,?,?,?,?,?,?)', (intent['run_id'],item.segment_id,intent['meeting_id'],intent['transcript_version'],participant,method,score,status,canonical(reasons),revision))
                        if not manual:
                            conn.execute('INSERT INTO attribution_proposal_lineage VALUES(?,?,?,?,?,?)', (intent['run_id'],item.segment_id,intent['meeting_id'],intent['transcript_version'],revision,intent['id']))
                    conn.execute("UPDATE attribution_runs SET status='published' WHERE id=?", (intent['run_id'],))
                    conn.execute('INSERT INTO attribution_state VALUES(?,?,?,?) ON CONFLICT(meeting_id,transcript_version) DO UPDATE SET revision=excluded.revision,run_id=excluded.run_id', (intent['meeting_id'],intent['transcript_version'],revision,intent['run_id']))
                    conn.execute("UPDATE identification_intents SET outcome='completed',reasons='[]' WHERE id=?", (intent['id'],))
                    from secretary.infrastructure.assignment_repository import revalidate_local
                    revalidate_local(conn,self.db,intent['meeting_id'],intent['transcript_version'])
                    conn.execute("UPDATE jobs SET status='succeeded',error=NULL,updated_at=? WHERE id=?", (now(),job['id']))
                    conn.execute("UPDATE meetings SET status='identified',error=NULL WHERE id=?", (intent['meeting_id'],))
            return True
        except (SpeakerConflict, EnrollmentFailure, VoiceError) as exc:
            reason = getattr(exc,'reason',str(exc))
            outcome = 'cancelled' if cancel.is_set() or reason in ('cancelled','operation_cancelled') else ('stale' if isinstance(exc,SpeakerConflict) or reason in ('stale_material','stale_voice_snapshot','audio_changed','audio_hash_mismatch') else ('runtime_unavailable' if reason in ('voice_runtime_unavailable','runtime_metadata_unavailable','runtime_missing') else 'error'))
            self._terminal(intent,outcome,[reason])
            return False
        except Exception:
            self._terminal(intent,'error',['identification_failed'])
            return False

    @staticmethod
    def _participant(snapshot, profile_id):
        choices = [p['id'] for p in snapshot['roster'] if p['enabled'] and p['person_profile_id']==profile_id and profile_id is not None]
        return choices[0] if len(choices)==1 else None

    def _store_proposal(self,intent,segment_id,participant,status,score,reasons,candidates,group_id):
        if status not in ('unknown','conflict','proposed') or score is not None and not math.isfinite(score):
            raise VoiceError('invalid_identification_result')
        method = 'voice_embedding' if participant or score is not None else 'unknown'
        self.db.execute('INSERT OR REPLACE INTO identification_proposals VALUES(?,?,?,?,?,?,?,?,?)', (intent['id'],segment_id,group_id,participant,method,status,score,canonical(reasons),canonical(candidates[:5])))
