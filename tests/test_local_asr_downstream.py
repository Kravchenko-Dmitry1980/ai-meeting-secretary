"""Applied local transcript evidence feeds existing explicit downstream actions."""
import hashlib
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from secretary.application.local_asr_lifecycle import (
    ExecutionPlan, LocalAsrLifecycle, ProcessIdentity, RuntimeQualification,
)
from secretary.application.local_asr_execution_protocol import ExecutionContext
from secretary.application.worker import Worker
from secretary.domain.local_asr import ImportLocalAsrRun, model_manifest_digest
from secretary.domain.identification import IdentifySpeakers
from secretary.domain.speakers import AddParticipant, ReviewAttribution
from secretary.infrastructure.database import Database
from secretary.infrastructure.local_asr_publication import publish_execution_result
from secretary.infrastructure.local_asr_repository import initialize_local_asr_schema, digest
from secretary.infrastructure.speaker_repository import SpeakerRepository
from secretary.settings import Settings
from test_local_asr_api import synthetic_wav


@pytest.fixture
def applied(tmp_path, request):
    """Exercise the actual fenced transaction with a synthetic owner, no model."""
    settings = Settings(_env_file=None, data_dir=tmp_path, cloud_enabled=False, polza_api_key='',
        local_cost_limits_enabled=False, polza_limits_enabled=False)
    db = Database(tmp_path / 'test.sqlite3')
    initialize_local_asr_schema(db)
    life = LocalAsrLifecycle(db)
    life.initialize_schema()
    meeting = db.create_meeting('Synthetic local downstream')
    option = getattr(request, 'param', 'ordinary')
    mode = option.get('mode', 'ordinary') if isinstance(option, dict) else option
    audio_kind = option.get('audio') if isinstance(option, dict) else None
    audio_path = tmp_path / ('audio' if audio_kind else 'uploads') / meeting['id'] / 'synthetic.wav'
    audio_path.parent.mkdir(parents=True)
    audio_path.write_bytes(b'x'*64 if audio_kind == 'compressed' else synthetic_wav())
    input_sha = hashlib.sha256(audio_path.read_bytes()).hexdigest()
    db.update_meeting(meeting['id'], media_path=str(audio_path))
    db.execute('UPDATE meetings SET processing_mode=? WHERE id=?', (mode, meeting['id']))
    payload = json.loads((Path(__file__).parent / 'fixtures/local_asr/synthetic_run.json').read_text('utf-8'))
    payload['artifacts']['audio_ref'] = 'meeting:' + meeting['id']
    payload['result']['input_sha256'] = payload['artifacts']['input_sha256'] = input_sha
    command = ImportLocalAsrRun.model_validate(payload)
    db.add_chunk(meeting['id'], dict(sequence=0, channel='import', path=audio_path,
        offset_ms=0, duration_ms=command.artifacts.duration_ms, sha256=command.result.input_sha256))
    identity = command.artifacts.identity
    plan = ExecutionPlan(input_sha256=command.result.input_sha256,
        profile_digest=identity.profile_digest, code_digest=identity.code_digest,
        model_digest=model_manifest_digest(identity.models), asr_device=identity.asr_device,
        diarization_device=identity.diarization_device, physical_gpu_id=identity.physical_gpu_id,
        precision=identity.precision, stages=('asr_rnnt', 'validate'), deadline_at=time.time()+60)
    job = life.prepare(meeting['id'], 1, plan, 'synthetic-first-transcript')
    life.authorize_queue(job['job_id'], RuntimeQualification(plan.profile_digest,
        'synthetic-ledger-only', 'synthetic-test-only', True, True, True))
    db.claim()
    claim = life.begin_claim(job['job_id'], 'synthetic-ledger', ProcessIdentity.current())
    workspace = tmp_path / 'local-asr-work' / job['job_id'] / 'synthetic-attempt'
    workspace.mkdir(parents=True)
    copied_input = workspace / 'input.wav'
    copied_input.write_bytes(audio_path.read_bytes())
    (workspace / 'normalized.wav').write_bytes(synthetic_wav())
    context = ExecutionContext(meeting['id'], 1, copied_input, input_sha, (),
        command.operation_id, None, command.artifacts.duration_ms)
    from secretary.infrastructure.local_asr_publication import prepare_initial_audio
    context, command = prepare_initial_audio(settings, context, command)
    manifest = {'schema_version': 'secretary.local-asr-run.v1', 'meeting_id': meeting['id'],
        'transcript_version': 1, 'result': command.result.model_dump(mode='json'),
        'artifacts': command.artifacts.model_dump(mode='json')}
    life.store_artifact(claim, digest(manifest))
    def publish():
        life.apply_atomically(claim, lambda conn: publish_execution_result(
            db, conn, context, command, command.result.input_sha256))
    if not isinstance(option, dict) or option.get('publish', True):
        publish()
    def denied_provider(*args, **kwargs):
        pytest.fail('No paid provider may run in a downstream enqueue test')
    worker = Worker(db, settings, denied_provider, recovery=False)
    return SimpleNamespace(db=db, worker=worker, life=life, mid=meeting['id'], job_id=job['job_id'], command=command,
                           publish=publish, original_path=audio_path)


def snapshot(case):
    return {name: case.db.rows(f'SELECT * FROM {name} ORDER BY rowid') for name in (
        'meetings', 'jobs', 'segments', 'meeting_transcript_routes', 'meeting_pipeline_handoffs', 'usage')}


@pytest.mark.parametrize('kwargs', [{}, {'retry': True}, {'resume': True}, {'new_transcript_version': True}])
def test_local_route_rejects_cloud_retranscription_before_any_mutation(applied, kwargs):
    before = snapshot(applied)
    with pytest.raises(ValueError, match='local_asr_cloud_retranscription_requires_new_route'):
        applied.worker.enqueue_transcription(applied.mid, **kwargs)
    assert snapshot(applied) == before


@pytest.mark.parametrize('applied', [{'publish': False}], indirect=True)
@pytest.mark.parametrize('new_version', [False, True])
def test_local_publication_between_precheck_and_bind_cannot_create_cloud_route(applied, monkeypatch, new_version):
    original = applied.worker.routes.bind
    published = []
    def interleaved_bind(*args, **kwargs):
        applied.publish()
        published.append(snapshot(applied))
        return original(*args, **kwargs)
    monkeypatch.setattr(applied.worker.routes, 'bind', interleaved_bind)
    with pytest.raises(ValueError, match='local_asr_cloud_retranscription_requires_new_route'):
        applied.worker.enqueue_transcription(applied.mid, new_transcript_version=new_version)
    assert published and snapshot(applied) == published[0]


def test_explicit_summary_recognizes_applied_local_transcript_without_cloud_stt(applied):
    assert applied.db.rows("SELECT * FROM jobs WHERE stage='transcribe'") == []
    assert applied.db.rows('SELECT * FROM meeting_pipeline_handoffs') == []
    assert applied.db.rows("SELECT * FROM jobs WHERE stage='summarize'") == []
    applied.worker._require_complete(applied.mid, 1)
    job = applied.worker.enqueue_summary(applied.mid)
    assert job['stage'] == 'summarize' and job['status'] == 'queued'
    assert applied.worker.enqueue_summary(applied.mid)['id'] == job['id']
    assert applied.db.rows('SELECT * FROM usage') == []
    assert applied.db.rows("SELECT * FROM jobs WHERE stage='transcribe'") == []


@pytest.mark.parametrize('applied', [{'audio': 'compressed'}], indirect=True)
def test_canonical_audio_receipt_binds_distinct_normalized_hash_and_retains_original(applied):
    chunk = applied.db.chunks(applied.mid)[0]
    canonical = Path(chunk['path'])
    assert canonical != applied.original_path
    assert chunk['sha256'] == hashlib.sha256(canonical.read_bytes()).hexdigest()
    assert chunk['sha256'] != applied.command.result.input_sha256
    assert hashlib.sha256(applied.original_path.read_bytes()).hexdigest() == applied.command.result.input_sha256
    assert applied.db.meeting(applied.mid, internal=True)['media_path'] == str(applied.original_path)
    applied.worker._require_complete(applied.mid, 1, require_voice_audio=True)
    assert applied.worker.enqueue_summary(applied.mid)['stage'] == 'summarize'


@pytest.mark.parametrize('corrupt', ['not_applied', 'job_failed', 'artifact_digest', 'source_text', 'source_time',
                                    'chunk_sha', 'chunk_duration', 'extra_chunk', 'version'])
def test_unverified_local_completion_cannot_authorize_summary(applied, corrupt):
    if corrupt == 'not_applied':
        applied.db.execute("UPDATE local_asr_executions SET state='artifact_stored' WHERE job_id=?", (applied.job_id,))
    elif corrupt == 'job_failed':
        applied.db.execute("UPDATE jobs SET status='failed' WHERE id=?", (applied.job_id,))
    elif corrupt == 'artifact_digest':
        applied.db.execute("UPDATE local_asr_executions SET artifact_digest=? WHERE job_id=?", ('0'*64, applied.job_id))
    elif corrupt == 'source_text':
        applied.db.execute("UPDATE segments SET text='changed' WHERE meeting_id=?", (applied.mid,))
    elif corrupt == 'source_time':
        applied.db.execute("UPDATE segments SET end_ms=end_ms+1 WHERE meeting_id=?", (applied.mid,))
    elif corrupt == 'chunk_sha':
        applied.db.execute("UPDATE chunks SET sha256=? WHERE meeting_id=?", ('2'*64, applied.mid))
    elif corrupt == 'chunk_duration':
        applied.db.execute("UPDATE chunks SET duration_ms=duration_ms+1 WHERE meeting_id=?", (applied.mid,))
    elif corrupt == 'extra_chunk':
        applied.db.add_chunk(applied.mid, dict(sequence=3, channel='import', path=Path('extra.wav'),
            offset_ms=5000, duration_ms=1000, sha256='1'*64))
    else:
        applied.db.update_meeting(applied.mid, transcript_version=2)
    before = snapshot(applied)
    with pytest.raises(ValueError):
        applied.worker.enqueue_summary(applied.mid)
    assert snapshot(applied) == before


def test_automatic_summary_is_not_authorized_by_local_publication(applied):
    applied.worker.maybe_summary(applied.mid, 1)
    assert applied.db.rows("SELECT * FROM jobs WHERE stage='summarize'") == []
    assert applied.db.rows('SELECT * FROM meeting_pipeline_handoffs') == []


@pytest.mark.parametrize('applied', ['voice_identification'], indirect=True)
def test_local_completeness_does_not_bypass_voice_identification_barrier(applied):
    applied.worker._require_complete(applied.mid, 1)
    before = snapshot(applied)
    with pytest.raises(ValueError, match='identification_required'):
        applied.worker.enqueue_summary(applied.mid)
    assert snapshot(applied) == before


@pytest.mark.parametrize('applied', ['voice_identification', {'mode': 'voice_identification', 'audio': 'compressed'}], indirect=True)
def test_local_voice_preflight_rejects_unsupported_audio_before_intent_or_job(applied):
    # Corrupt only the audio locator after an otherwise valid canonical
    # publication; the preflight must reject this before creating any intent.
    applied.db.execute('UPDATE chunks SET path=? WHERE meeting_id=?', (str(applied.original_path), applied.mid))
    def forbidden(*args, **kwargs):
        pytest.fail('Unsupported local audio must not reach identification capture')
    applied.worker.attribution = SimpleNamespace(speakers=SpeakerRepository(applied.db), capture=forbidden)
    before = snapshot(applied)
    with pytest.raises(ValueError, match='local_asr_voice_canonical_audio_required'):
        applied.worker.enqueue_identification(applied.mid, IdentifySpeakers(
            transcript_version=1, expected_revision=0, operation_id='explicit-identity'))
    assert snapshot(applied) == before
    assert applied.db.rows('SELECT * FROM identification_intents') == []


@pytest.mark.parametrize('applied', [{'mode': 'voice_identification', 'audio': 'pcm'}], indirect=True)
def test_local_voice_preflight_preserves_existing_pcm_audio_boundary(applied):
    applied.worker._require_complete(applied.mid, 1, require_voice_audio=True)
    assert applied.db.rows('SELECT * FROM identification_intents') == []


def test_manual_identity_decisions_survive_local_completeness_and_summary_enqueue(applied):
    speakers = SpeakerRepository(applied.db)
    participant = speakers.add_participant(applied.mid, AddParticipant(operation_id='manual-person',
        expected_roster_revision=0, display_name='Synthetic participant')).participants[0].id
    speakers.review_attribution(applied.mid, ReviewAttribution(operation_id='manual-review',
        transcript_version=1, expected_revision=0,
        changes=[{'segment_id': applied.command.artifacts.source_segments[0].segment_id,
                  'participant_id': participant}]))
    decisions = applied.db.rows('SELECT * FROM speaker_attributions ORDER BY rowid')
    assert any(row['participant_id'] == participant and row['method'] == 'manual' for row in decisions)
    applied.worker._require_complete(applied.mid, 1)
    applied.worker.enqueue_summary(applied.mid)
    assert applied.db.rows('SELECT * FROM speaker_attributions ORDER BY rowid') == decisions
