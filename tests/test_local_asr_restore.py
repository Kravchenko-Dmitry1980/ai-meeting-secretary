"""Synthetic backup/restore retains ASR evidence and invalidates execution grants."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from secretary.application.local_asr_lifecycle import (
    ExecutionPlan, LocalAsrLifecycle, LocalExecutionError, ProcessIdentity, RuntimeQualification,
)
from secretary.domain.local_asr import ImportLocalAsrRun, ReviewLocalAsrCluster
from secretary.domain.restore_quarantine import Origin, RestoreQuarantineError, key_digest
from secretary.domain.speakers import AddParticipant
from secretary.infrastructure.database import Database
from secretary.infrastructure.local_asr_repository import LocalAsrRepository, initialize_local_asr_schema
from secretary.infrastructure.local_backup import create_backup, restore_backup
from secretary.infrastructure.restore_quarantine_catalogue import LOCAL_ASR_FAMILY_DISPOSITIONS
from secretary.infrastructure.speaker_repository import SpeakerRepository
from secretary.infrastructure.team_restore_preview import reconciliation_preview
from secretary.infrastructure.team_restore_quarantine import collect_quarantine
from test_team_backup import full_fixture
from test_team_restore_preview import copy, files


def populate(db):
    initialize_local_asr_schema(db)
    life = LocalAsrLifecycle(db, clock=lambda: 100.0, exited=lambda identity: True)
    life.initialize_schema()
    meeting = db.create_meeting("Synthetic restore ASR evidence")["id"]
    db.update_meeting(meeting, transcript_version=1, duration_ms=5000, status="ready")
    folder = db.path.parent / "audio" / meeting
    folder.mkdir(parents=True)
    media = folder / "synthetic.wav"
    media.write_bytes(b"synthetic local bytes; no inference input")
    media_hash = hashlib.sha256(media.read_bytes()).hexdigest()
    chunk = db.add_chunk(meeting, {"sequence": 0, "channel": "import", "path": str(media),
        "offset_ms": 0, "duration_ms": 5000, "sha256": media_hash})["id"]
    payload = json.loads((Path(__file__).parent / "fixtures/local_asr/synthetic_run.json").read_text("utf-8"))
    payload["result"]["input_sha256"] = media_hash
    payload["artifacts"]["input_sha256"] = media_hash
    payload["artifacts"]["audio_ref"] = f"meeting:{meeting}"
    for index, item in enumerate(payload["artifacts"]["source_segments"]):
        db.execute("INSERT INTO segments VALUES(?,?,?,1,?,?,?,'segment',?,NULL,NULL,'import')",
                   (item["segment_id"], meeting, chunk, index, item["started_ms"], item["ended_ms"], item["text"]))
    person = SpeakerRepository(db).add_participant(meeting, AddParticipant(
        operation_id="roster-restore", expected_roster_revision=0, display_name="Synthetic person")).participants[0].id
    repo = LocalAsrRepository(db)
    command = ImportLocalAsrRun.model_validate(payload)
    run = repo.import_run(meeting, command, trusted_input_sha256=media_hash)
    run = repo.review_cluster(meeting, run["run_id"], ReviewLocalAsrCluster(
        operation_id="review-restore", expected_review_revision=0, speaker_label="Спикер 1",
        shown_segment_ordinals=[0, 1, 2], excluded_segment_ordinals=[1], participant_id=person,
        listening_confirmed=True))
    plan = ExecutionPlan(input_sha256=media_hash, source_text_sha256=command.artifacts.selected_text_sha256,
        profile_digest="3" * 64, model_digest="4" * 64, code_digest="5" * 64,
        asr_device="cpu", diarization_device="cuda", physical_gpu_id="GPU-synthetic-restore",
        precision="fp32", stages=("diarize", "align_saved_text", "assemble"), deadline_at=10000)
    job = life.prepare(meeting, 1, plan, "restore-request")
    qualification = RuntimeQualification(plan.profile_digest, "synthetic-evidence", "synthetic-authorization", True, True, True)
    life.authorize_queue(job["job_id"], qualification)
    assert db.claim()["id"] == job["job_id"]
    claim = life.begin_claim(job["job_id"], "synthetic-worker", ProcessIdentity(123, 42.0))
    life.save_checkpoint(claim, "diarize", plan.checkpoint_identity("diarize"), {"synthetic": True})
    frozen_plan = db.one("SELECT plan,qualification FROM local_asr_executions WHERE job_id=?", (job["job_id"],))
    return meeting, run, job["job_id"], qualification, frozen_plan


def assert_retained(db, meeting, run, job_id, qualification, frozen_plan):
    repo = LocalAsrRepository(db)
    assert repo.get_run(meeting, run["run_id"]) == run
    assert db.one("SELECT plan,qualification FROM local_asr_executions WHERE job_id=?", (job_id,)) == frozen_plan
    life = LocalAsrLifecycle(db, clock=lambda: 200.0, exited=lambda identity: True)
    state = life.view(job_id)
    assert state["state"] == "restore_blocked"
    assert state["job_status"] == "cancelled" and state["cancel_requested"] == 1
    assert len(db.rows("SELECT * FROM local_asr_attempts")) == 1
    assert len(db.rows("SELECT * FROM local_asr_checkpoints")) == 1
    return life


def test_local_backup_keeps_artifacts_reviews_and_cannot_reactivate_old_qualification(tmp_path):
    source = tmp_path / "source"
    db = Database(source / "secretary.sqlite3")
    saved = populate(db)
    before = LocalAsrLifecycle(db).view(saved[2])
    archive = tmp_path / "archive"
    create_backup(source, archive)
    restore_backup(archive, tmp_path / "restored", restore_root=tmp_path)
    restored = Database(tmp_path / "restored/secretary.sqlite3")
    life = assert_retained(restored, *saved)
    with pytest.raises(LocalExecutionError, match="not_activatable"):
        life.authorize_queue(saved[2], saved[3])
    with pytest.raises(LocalExecutionError, match="not_recoverable"):
        life.recover_expired(saved[2])
    assert restored.claim() is None
    assert LocalAsrLifecycle(db).view(saved[2]) == before


def test_team_backup_classifies_all_asr_rows_and_blocks_restored_writes(tmp_path):
    with full_fixture(tmp_path) as (backup, _, service, _, sources):
        db = Database(sources["secretary"])
        saved = populate(db)
        archived = service.backup(tmp_path / "backups/asr")
        assert archived.full_recovery
        restored = backup.restore_backup(archived.destination, tmp_path / "restored", restore_root=tmp_path)
        before = files(restored.destination)
        reopened = Database(restored.databases["secretary"])
        life = assert_retained(reopened, *saved)
        preview = reconciliation_preview(archived.destination, restored.destination)
        quarantine = collect_quarantine(archived.destination, restored.destination,
                                        expected_preview_sha256=preview["preview_sha256"])
        assert set(LOCAL_ASR_FAMILY_DISPOSITIONS) <= set(quarantine.coverage["secretary"])
        for family, disposition in LOCAL_ASR_FAMILY_DISPOSITIONS.items():
            entries = [item for item in quarantine.entries if item.origin.family == family]
            assert entries and all(item.disposition == disposition for item in entries)
        execution = Origin("secretary", "local_asr_executions",
                           key_digest("secretary", "local_asr_executions", (saved[2],)))
        with pytest.raises(RestoreQuarantineError, match="restore_legacy_work_blocked"):
            quarantine.assert_no_legacy_authority((execution,))
        with pytest.raises(LocalExecutionError, match="not_activatable"):
            life.authorize_queue(saved[2], saved[3])
        with pytest.raises(sqlite3.DatabaseError):
            reopened.execute("UPDATE local_asr_executions SET state='queued' WHERE job_id=?", (saved[2],))
        assert reopened.claim() is None
        assert files(restored.destination) == before
        assert saved[0] not in json.dumps(quarantine.report())
        assert saved[1]["artifacts"]["selected_text"] not in json.dumps(quarantine.report())


@pytest.mark.parametrize("change", ["partial", "version", "column"])
def test_optional_asr_extension_is_closed_and_never_guessed(copy, change):
    from secretary.infrastructure.local_asr_repository import LOCAL_ASR_SCHEMA
    from secretary.application.local_asr_lifecycle import SCHEMA
    with closing(sqlite3.connect(copy.restored.databases["secretary"])) as conn, conn:
        for statement in (*LOCAL_ASR_SCHEMA, *SCHEMA):
            conn.execute(statement)
        conn.execute("INSERT INTO local_asr_schema VALUES(1,'synthetic')")
        if change == "partial":
            conn.execute("DROP TABLE local_asr_checkpoints")
        elif change == "version":
            conn.execute("UPDATE local_asr_schema SET version=2")
        else:
            conn.execute("ALTER TABLE local_asr_runs ADD COLUMN hidden_execution_authority TEXT")
    before = files(copy.restored.destination)
    preview = reconciliation_preview(copy.backup, copy.restored.destination)
    with pytest.raises(RestoreQuarantineError, match="restore_quarantine_catalogue_unsupported"):
        collect_quarantine(copy.backup, copy.restored.destination, expected_preview_sha256=preview["preview_sha256"])
    assert files(copy.restored.destination) == before
