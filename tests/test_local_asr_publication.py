"""First transcript publication is additive and rolls back with its fenced owner."""
import json
import hashlib
from pathlib import Path
from types import SimpleNamespace
import wave
import os

import pytest

from secretary.application.local_asr_lifecycle import LocalExecutionError
from secretary.application.local_asr_media import LocalAsrMediaError
from secretary.application.local_asr_execution_protocol import ExecutionContext
from secretary.domain.local_asr import ImportLocalAsrRun
from secretary.infrastructure.database import Database, now
from secretary.infrastructure.local_asr_publication import prepare_initial_audio, publish_execution_result
from secretary.infrastructure.local_asr_repository import initialize_local_asr_schema, LocalAsrRepository


@pytest.fixture
def fresh(tmp_path):
    db = Database(tmp_path / 'test.sqlite3')
    initialize_local_asr_schema(db)
    meeting = db.create_meeting('Synthetic first local transcript')
    attempt = tmp_path / 'local-asr-work' / 'synthetic-attempt'
    attempt.mkdir(parents=True)
    path = attempt / 'input.wav'
    with wave.open(str(path), 'wb') as audio:
        audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\0\0' * 80000)
    input_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    db.update_meeting(meeting['id'], transcript_version=1, media_path=str(path))
    payload = json.loads((Path(__file__).parent / 'fixtures/local_asr/synthetic_run.json').read_text('utf-8'))
    payload['artifacts']['audio_ref'] = 'meeting:' + meeting['id']
    payload['artifacts']['input_sha256'] = input_sha
    payload['result']['input_sha256'] = input_sha
    command = ImportLocalAsrRun.model_validate(payload)
    context = ExecutionContext(meeting['id'], 1, path, input_sha, (), 'synthetic', None, 5000)
    context, command = prepare_initial_audio(SimpleNamespace(data_dir=tmp_path), context, command)
    return db, context, command


def test_first_transcript_and_analysis_publish_together(fresh):
    db, context, command = fresh
    with db.transaction() as conn:
        run_id = publish_execution_result(db, conn, context, command, command.result.input_sha256)
    assert db.segments(context.meeting_id)['total'] == len(command.artifacts.source_segments)
    assert json.loads(db.one('SELECT route FROM meeting_transcript_routes')['route'])['provider'] == 'local'
    assert LocalAsrRepository(db).get_run(context.meeting_id, run_id)['artifacts']['selected_text'] == command.artifacts.selected_text
    assert not db.rows('SELECT * FROM usage')
    assert not db.rows('SELECT * FROM summaries')
    chunk = db.one('SELECT * FROM chunks')
    assert chunk['path'] == str(context.canonical_audio_path)
    assert chunk['sha256'] == context.canonical_audio_sha256
    assert context.canonical_audio_path.is_relative_to(db.path.parent / 'audio')


def test_first_version_is_installed_only_when_publication_succeeds(fresh):
    db, context, command = fresh
    db.update_meeting(context.meeting_id, transcript_version=0)
    with db.transaction() as conn:
        publish_execution_result(db, conn, context, command, command.result.input_sha256)
    assert db.meeting(context.meeting_id)['transcript_version'] == 1


def test_owner_publication_failure_rolls_back_text_route_and_artifact(fresh):
    db, context, command = fresh
    before = db.rows('SELECT * FROM meetings')
    with pytest.raises(LocalExecutionError, match='stale_owner'):
        with db.transaction() as conn:
            publish_execution_result(db, conn, context, command, command.result.input_sha256)
            raise LocalExecutionError('local_asr_stale_owner')
    for table in ('segments', 'chunks', 'meeting_transcript_routes', 'local_asr_runs'):
        assert not db.rows(f'SELECT * FROM {table}')
    assert db.rows('SELECT * FROM meetings') == before


@pytest.mark.parametrize('history', ['route', 'summary', 'usage', 'cloud_job', 'active_prepare'])
def test_initial_transcript_refuses_prior_paid_or_processing_history(fresh, history):
    db, context, command = fresh
    mid = context.meeting_id
    if history == 'route':
        db.execute("INSERT INTO meeting_transcript_routes VALUES(?,1,'ordinary','{}','saved',?)", (mid, now()))
    elif history == 'summary':
        db.execute("INSERT INTO summaries VALUES(?,1,1,'{}')", (mid,))
    elif history == 'usage':
        db.execute("INSERT INTO usage(id,meeting_id,job_id,kind,status,created_at) VALUES('usage',?,'job','stt','unknown',?)", (mid, now()))
    else:
        db.execute("INSERT INTO jobs(id,meeting_id,stage,status,created_at,updated_at,version) VALUES('other',?,?,?, ?,?,1)",
                   (mid, 'transcribe' if history == 'cloud_job' else 'prepare',
                    'uncertain' if history == 'cloud_job' else 'queued', now(), now()))
    with pytest.raises(LocalExecutionError):
        with db.transaction() as conn:
            publish_execution_result(db, conn, context, command, command.result.input_sha256)
    assert not db.rows('SELECT * FROM segments')
    assert not db.rows('SELECT * FROM local_asr_runs')


def test_publisher_rechecks_scope_before_any_mutation(fresh):
    db, context, command = fresh
    with pytest.raises(LocalExecutionError, match='scope_mismatch'):
        with db.transaction() as conn:
            publish_execution_result(db, conn, context, command, '0' * 64)
    assert not db.rows('SELECT * FROM chunks')


def test_canonical_audio_tamper_refuses_publication(fresh):
    db, context, command = fresh
    context.canonical_audio_path.write_bytes(b'changed after validation')
    with pytest.raises(ValueError, match='hash_mismatch'):
        with db.transaction() as conn:
            publish_execution_result(db, conn, context, command, command.result.input_sha256)
    assert not db.rows('SELECT * FROM segments')


def test_normalized_audio_has_independent_hash_and_preserves_original(fresh):
    db, context, command = fresh
    command = command.model_copy(update={'artifacts': command.artifacts.model_copy(update={
        'runtime_receipts': [r for r in command.artifacts.runtime_receipts if r.stage != 'canonical_audio']})})
    normalized = context.input_path.parent / 'normalized.wav'
    with wave.open(str(normalized), 'wb') as audio:
        audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\1\0' * 80000)
    context, command = prepare_initial_audio(SimpleNamespace(data_dir=db.path.parent), context, command)
    assert context.canonical_audio_sha256 != context.input_sha256
    with db.transaction() as conn:
        publish_execution_result(db, conn, context, command, command.result.input_sha256)
    assert db.one('SELECT media_path FROM meetings WHERE id=?', (context.meeting_id,))['media_path'] == str(context.input_path)
    assert hashlib.sha256(context.input_path.read_bytes()).hexdigest() == context.input_sha256
    assert db.one('SELECT * FROM chunks')['sha256'] == context.canonical_audio_sha256


def test_canonical_audio_rejects_riff_trailing_bytes_before_copy(fresh):
    db, context, command = fresh
    normalized = context.input_path.parent / 'normalized.wav'
    normalized.write_bytes(context.input_path.read_bytes() + b'trailing bytes')
    with pytest.raises(LocalExecutionError, match='canonical_audio_invalid'):
        prepare_initial_audio(SimpleNamespace(data_dir=db.path.parent), context, command)
    assert not db.rows('SELECT * FROM chunks')


def test_interrupted_audio_copy_does_not_poison_retry(fresh, monkeypatch):
    db, context, command = fresh
    command = command.model_copy(update={'artifacts': command.artifacts.model_copy(update={
        'runtime_receipts': [r for r in command.artifacts.runtime_receipts if r.stage != 'canonical_audio']})})
    normalized = context.input_path.parent / 'normalized.wav'
    with wave.open(str(normalized), 'wb') as audio:
        audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\2\0' * 80000)
    sha = hashlib.sha256(normalized.read_bytes()).hexdigest()
    target = context.canonical_audio_path.with_name(sha + '.wav')
    def interrupted(fd):
        raise OSError('synthetic interrupted flush')
    with monkeypatch.context() as fault:
        fault.setattr(os, 'fsync', interrupted)
        with pytest.raises(LocalAsrMediaError, match='input_lock_unavailable'):
            prepare_initial_audio(SimpleNamespace(data_dir=db.path.parent), context, command)
    assert not target.exists()
    updated, _ = prepare_initial_audio(SimpleNamespace(data_dir=db.path.parent), context, command)
    assert updated.canonical_audio_path == target
    assert hashlib.sha256(target.read_bytes()).hexdigest() == sha
