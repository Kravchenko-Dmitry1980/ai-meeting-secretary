"""Publish a local execution inside the existing lifecycle's fenced transaction."""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

from secretary.application.local_asr_lifecycle import LocalExecutionError
from secretary.application.local_asr_media import _lock_copy_directories, lock_verified_input_copy
from secretary.domain.local_asr import ImportLocalAsrRun, RuntimeReceipt, seconds_to_ms
from secretary.domain.voice import VoiceError
from secretary.infrastructure.database import now, stable_id
from secretary.infrastructure.local_asr_repository import LocalAsrRepository, canonical, digest
from secretary.infrastructure.meeting_voice_audio import MeetingAudioReader


_AUDIO_LIMIT = 512 * 1024 * 1024


def prepare_initial_audio(settings, context, command: ImportLocalAsrRun):
    """Persist checked normalized PCM before the fenced first-text transaction.

    An interrupted transaction may leave an unreferenced content-addressed file;
    it never replaces the original upload or another meeting's audio.
    """
    if context.source_segments:
        return context, command
    root = Path(settings.data_dir).absolute()
    source = context.input_path.parent / 'normalized.wav'
    if not source.exists():
        # Supports an already canonical PCM input (including synthetic tests).
        # The production runtime separately requires its normalize output.
        source = context.input_path
    source = source.absolute()
    if not source.is_relative_to(root / 'local-asr-work'):
        raise LocalExecutionError('local_asr_canonical_audio_path_invalid')
    info = source.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= _AUDIO_LIMIT:
        raise LocalExecutionError('local_asr_canonical_audio_invalid')
    with source.open('rb') as stream:
        audio_sha = hashlib.file_digest(stream, 'sha256').hexdigest()
    with lock_verified_input_copy(source, expected_sha256=audio_sha, max_bytes=_AUDIO_LIMIT):
        try:
            with source.open('rb') as audio:
                _, frames, channels, rate = MeetingAudioReader._layout(audio, info.st_size, None)
                if (channels, rate) != (1, 16000):
                    raise LocalExecutionError('local_asr_canonical_audio_invalid')
                if seconds_to_ms(frames / 16000) != command.artifacts.duration_ms:
                    raise LocalExecutionError('local_asr_canonical_audio_duration_mismatch')
        except (VoiceError, EOFError):
            raise LocalExecutionError('local_asr_canonical_audio_invalid') from None
        folder = root / 'audio' / context.meeting_id / 'local-asr'
        # Create one directory at a time while its checked parent is held.
        parent = root
        for part in folder.relative_to(root).parts:
            with _lock_copy_directories(root, parent):
                parent = parent / part
                parent.mkdir(exist_ok=True)
        target = folder / (audio_sha + '.wav')
        with _lock_copy_directories(root, folder):
            if not target.exists():
                partial = folder / ('.partial-' + uuid.uuid4().hex + '.wav')
                with source.open('rb') as src, partial.open('xb') as dst:
                    while block := src.read(1024 * 1024):
                        dst.write(block)
                    dst.flush()
                    os.fsync(dst.fileno())
                with lock_verified_input_copy(partial, expected_sha256=audio_sha, max_bytes=_AUDIO_LIMIT):
                    pass
                try:
                    # Windows rename refuses replacement if another attempt
                    # installed this digest. Interrupted copies remain partial.
                    os.rename(partial, target)
                except FileExistsError:
                    pass
            with lock_verified_input_copy(target, expected_sha256=audio_sha, max_bytes=_AUDIO_LIMIT):
                pass
    receipt = RuntimeReceipt(stage='canonical_audio', status='succeeded', receipt_sha256=digest({
        'schema_version': 'secretary.local-asr.canonical-audio.v1',
        'input_sha256': context.input_sha256, 'audio_sha256': audio_sha,
        'duration_ms': command.artifacts.duration_ms, 'sample_rate_hz': 16000,
        'channels': 1, 'sample_width_bytes': 2}))
    if any(item.stage == 'canonical_audio' for item in command.artifacts.runtime_receipts):
        raise LocalExecutionError('local_asr_canonical_audio_receipt_conflict')
    artifacts = command.artifacts.model_copy(update={
        'runtime_receipts': [*command.artifacts.runtime_receipts, receipt]})
    command = ImportLocalAsrRun.model_validate(command.model_copy(update={'artifacts': artifacts}).model_dump(mode='json'))
    return replace(context, canonical_audio_path=target, canonical_audio_sha256=audio_sha), command


def completed_local_transcript(db, meeting_id: str, version: int) -> bool:
    """Read a complete applied first-local transcript from one DB snapshot.

    A route or imported analysis alone is insufficient. Reuse the lifecycle's
    manifest/provenance/source validation and require the canonical whole-input
    chunk. Speaker review decisions are intentionally outside this text proof.
    This grants no summary authorization and creates no cloud transcription job.
    """
    from secretary.application.local_asr_lifecycle import ExecutionPlan, LocalAsrLifecycle
    with db.connection() as conn:
        conn.execute('BEGIN')
        meeting = conn.execute('SELECT * FROM meetings WHERE id=?', (meeting_id,)).fetchone()
        if meeting is None or meeting['recording'] or meeting['transcript_version'] != version:
            return False
        record = conn.execute('SELECT * FROM meeting_transcript_routes WHERE meeting_id=? AND transcript_version=?',
                              (meeting_id, version)).fetchone()
        if record is None:
            return False
        try:
            route = json.loads(record['route'])
            if (not isinstance(route, dict) or route.get('provider') != 'local'
                    or digest(route) != record['route_hash']
                    or record['processing_mode'] != meeting['processing_mode']):
                return False
        except (ValueError, TypeError):
            return False
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'local_asr_executions', 'local_asr_runs'} <= tables:
            return False
        executions = conn.execute("""SELECT e.*,j.meeting_id,j.version FROM local_asr_executions e
            JOIN jobs j ON j.id=e.job_id WHERE e.application='secretary' AND e.owner_scope='local-owner'
            AND e.state='applied' AND j.status='succeeded' AND j.stage='local_asr'
            AND j.meeting_id=? AND j.version=?""", (meeting_id, version)).fetchall()
        chunks = conn.execute('SELECT * FROM chunks WHERE meeting_id=?', (meeting_id,)).fetchall()
        if len(chunks) != 1:
            return False
        chunk = chunks[0]
        for execution in executions:
            try:
                plan = ExecutionPlan.model_validate_json(execution['plan'])
                if (plan.provider != 'local' or plan.source_text_sha256 is not None
                        or plan.digest != execution['plan_digest']
                        or route != {'provider': 'local', 'adapter_revision': 'secretary-portable-asr-v1',
                                     'profile_digest': plan.profile_digest, 'code_digest': plan.code_digest}):
                    continue
                LocalAsrLifecycle._validate_applied_run(conn, execution, plan, execution['result_run_id'])
                run = conn.execute('SELECT manifest FROM local_asr_runs WHERE id=?',
                                   (execution['result_run_id'],)).fetchone()
                artifacts = json.loads(run['manifest'])['artifacts']
                canonical_receipts = [receipt for receipt in artifacts['runtime_receipts']
                                      if receipt['stage'] == 'canonical_audio']
                canonical_digest = digest({'schema_version': 'secretary.local-asr.canonical-audio.v1',
                    'input_sha256': plan.input_sha256, 'audio_sha256': chunk['sha256'],
                    'duration_ms': artifacts['duration_ms'], 'sample_rate_hz': 16000,
                    'channels': 1, 'sample_width_bytes': 2})
                if (not artifacts['source_segments'] or chunk['status'] != 'transcribed'
                        or chunk['offset_ms'] != 0 or chunk['channel'] != 'import'
                        or len(canonical_receipts) != 1
                        or canonical_receipts[0]['status'] != 'succeeded'
                        or canonical_receipts[0]['receipt_sha256'] != canonical_digest
                        or chunk['duration_ms'] != artifacts['duration_ms']
                        or meeting['duration_ms'] != artifacts['duration_ms']):
                    continue
                source_chunks = {row[0] for row in conn.execute(
                    'SELECT DISTINCT chunk_id FROM segments WHERE meeting_id=? AND transcript_version=?',
                    (meeting_id, version))}
                if source_chunks == {chunk['id']}:
                    return True
            except (LocalExecutionError, ValueError, TypeError, KeyError):
                continue
        return False


def assert_initial_transcript_available(conn, meeting_id: str, version: int) -> dict:
    """A first local transcript cannot replace a cloud route or paid history."""
    row = conn.execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone()
    if row is None:
        raise KeyError(meeting_id)
    meeting = dict(row)
    if meeting["recording"] or not (meeting["transcript_version"] == version
                                    or (meeting["transcript_version"] == 0 and version == 1)):
        raise LocalExecutionError("local_asr_transcript_version_conflict")
    for table in ("segments", "meeting_transcript_routes", "summaries", "usage"):
        if conn.execute(f"SELECT 1 FROM {table} WHERE meeting_id=? LIMIT 1", (meeting_id,)).fetchone():
            raise LocalExecutionError("local_asr_initial_transcript_requires_unused_meeting")
    if conn.execute("""SELECT 1 FROM jobs WHERE meeting_id=? AND
            (stage IN ('transcribe','summarize','identify_speakers') OR
             (stage!='local_asr' AND status IN ('queued','running','waiting_config','paused_budget','uncertain')))
            LIMIT 1""", (meeting_id,)).fetchone():
        raise LocalExecutionError("local_asr_initial_transcript_has_other_work")
    return meeting


def publish_execution_result(db, conn, context, command: ImportLocalAsrRun, trusted_sha: str) -> str:
    with ExitStack() as locks:
        if not context.source_segments:
            path = context.canonical_audio_path
            if (path is None or context.canonical_audio_sha256 is None
                    or not path.absolute().is_relative_to(db.path.parent.absolute() / 'audio' / context.meeting_id)):
                raise LocalExecutionError('local_asr_canonical_audio_required')
            locks.enter_context(_lock_copy_directories(db.path.parent.absolute(), path.parent))
            locks.enter_context(lock_verified_input_copy(path,
                expected_sha256=context.canonical_audio_sha256, max_bytes=_AUDIO_LIMIT))
        return _publish_execution_result(db, conn, context, command, trusted_sha)


def _publish_execution_result(db, conn, context, command: ImportLocalAsrRun, trusted_sha: str) -> str:
    """The caller validates plan/provenance and commits or rolls back everything.

    Existing text is never rewritten. A new transcript is installed only into an
    unused meeting; the repository's strict source comparison still runs after
    insertion. Fencing failure rolls back text, route and analysis together.
    """
    command = ImportLocalAsrRun.model_validate(command.model_dump(mode="json"))
    if (command.transcript_version != context.transcript_version
            or command.artifacts.audio_ref != f"meeting:{context.meeting_id}"
            or command.artifacts.input_sha256 != trusted_sha
            or command.result.input_sha256 != trusted_sha):
        raise LocalExecutionError("local_asr_publication_scope_mismatch")
    repository = LocalAsrRepository(db)
    if not context.source_segments:
        meeting = assert_initial_transcript_available(conn, context.meeting_id, context.transcript_version)
        sources = command.artifacts.source_segments
        if not sources or any(source.channel != "import" for source in sources):
            raise LocalExecutionError("local_asr_initial_source_invalid")
        chunks = conn.execute('SELECT * FROM chunks WHERE meeting_id=?', (context.meeting_id,)).fetchall()
        if (len(chunks) > 1 or (chunks and (chunks[0]['sha256'] != trusted_sha
                or chunks[0]['offset_ms'] != 0 or chunks[0]['channel'] != 'import'
                or chunks[0]['duration_ms'] != command.artifacts.duration_ms))):
            raise LocalExecutionError('local_asr_initial_chunk_conflict')
        if not chunks:
            chunk_id = stable_id(context.meeting_id, "local-asr-original", trusted_sha)
            conn.execute("INSERT INTO chunks VALUES(?,?,?,'import',?,0,?,'transcribed',?)",
                         (chunk_id, context.meeting_id, 0, str(context.canonical_audio_path),
                          command.artifacts.duration_ms, context.canonical_audio_sha256))
        else:
            chunk_id = chunks[0]['id']
            conn.execute("UPDATE chunks SET status='transcribed',path=?,sha256=? WHERE id=?",
                         (str(context.canonical_audio_path), context.canonical_audio_sha256, chunk_id))
        for ordinal, source in enumerate(sources):
            conn.execute("""INSERT INTO segments(id,meeting_id,chunk_id,transcript_version,ordinal,
                start_ms,end_ms,timing_precision,text,confidence,speaker_id,channel)
                VALUES(?,?,?,?,?,?,?,'segment',?,NULL,NULL,'import')""",
                (source.segment_id, context.meeting_id, chunk_id, context.transcript_version,
                 ordinal, source.started_ms, source.ended_ms, source.text))
        route = {"provider": "local", "adapter_revision": "secretary-portable-asr-v1",
                 "profile_digest": command.artifacts.identity.profile_digest,
                 "code_digest": command.artifacts.identity.code_digest}
        conn.execute("INSERT INTO meeting_transcript_routes VALUES(?,?,?,?,?,?)",
                     (context.meeting_id, context.transcript_version, meeting["processing_mode"],
                      canonical(route), digest(route), now()))
        conn.execute("UPDATE meetings SET status='ready',transcript_version=?,duration_ms=?,error=NULL,updated_at=? WHERE id=?",
                     (context.transcript_version, command.artifacts.duration_ms, now(), context.meeting_id))
    result = repository.import_run(context.meeting_id, command, trusted_input_sha256=trusted_sha, conn=conn)
    return result["run_id"]
