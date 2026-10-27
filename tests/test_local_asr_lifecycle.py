"""Synthetic lifecycle evidence: no model imports, external service or live DB."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from secretary.application.local_asr_lifecycle import (
    ExecutionPlan, LocalAsrLifecycle, LocalExecutionError, ProcessIdentity, RuntimeQualification,
)
from secretary.infrastructure.database import Database
from secretary.domain.local_asr import ImportLocalAsrRun, model_manifest_digest
from secretary.infrastructure.local_asr_repository import LocalAsrRepository, initialize_local_asr_schema, digest


def make_plan(**overrides):
    fields = dict(input_sha256="1" * 64, source_text_sha256=hashlib.sha256(b"").hexdigest(),
                  profile_digest="3" * 64, model_digest="4" * 64, code_digest="5" * 64,
                  asr_device="cpu", diarization_device="cuda", physical_gpu_id="GPU-synthetic-123",
                  precision="fp32", stages=("diarize", "align_saved_text", "assemble"), deadline_at=10000)
    fields.update(overrides)
    return ExecutionPlan(**fields)


@pytest.fixture
def fixture(tmp_path):
    db = Database(tmp_path / "test.sqlite3")
    meeting = db.create_meeting("Synthetic local lifecycle")
    db.update_meeting(meeting["id"], transcript_version=1, status="completed")
    clock = SimpleNamespace(value=100.0, owner_exited=False)
    life = LocalAsrLifecycle(db, clock=lambda: clock.value, exited=lambda identity: clock.owner_exited)
    life.initialize_schema()
    return SimpleNamespace(db=db, meeting=meeting["id"], clock=clock, life=life)


def qualify(plan):
    return RuntimeQualification(plan.profile_digest, "synthetic-evidence", "synthetic-owner-approval", True, True, True)


def prepared(fixture, **changes):
    plan = make_plan(**changes)
    job = fixture.life.prepare(fixture.meeting, 1, plan, "request-1")
    return job, plan


def running(fixture, plan=None):
    if plan is None:
        job, plan = prepared(fixture)
    else:
        job = fixture.life.prepare(fixture.meeting, 1, plan, "request-1")
    fixture.life.authorize_queue(job["job_id"], qualify(plan))
    claimed = fixture.db.claim()
    assert claimed["id"] == job["job_id"]
    claim = fixture.life.begin_claim(job["job_id"], "worker-A", ProcessIdentity(123, 42.0), lease_seconds=10)
    return job, plan, claim


def seed_publication_source(fixture, meeting_id=None):
    meeting_id = meeting_id or fixture.meeting
    payload = json.loads((Path(__file__).parent / "fixtures/local_asr/synthetic_run.json").read_text(encoding="utf-8"))
    payload["artifacts"]["audio_ref"] = f"meeting:{meeting_id}"
    chunk = fixture.db.add_chunk(meeting_id, dict(sequence=0, channel="import", path="synthetic.wav",
        offset_ms=0, duration_ms=5000, sha256=payload["result"]["input_sha256"]))
    for ordinal, source in enumerate(payload["artifacts"]["source_segments"]):
        source["segment_id"] = f"{meeting_id}-source-{ordinal}"
        payload["artifacts"]["segment_sources"][ordinal]["source_segment_ids"] = [source["segment_id"]]
        fixture.db.execute("INSERT INTO segments VALUES(?,?,?,1,?,?,?,'segment',?,NULL,NULL,'import')",
            (source["segment_id"], meeting_id, chunk["id"], ordinal, source["started_ms"], source["ended_ms"], source["text"]))
    return ImportLocalAsrRun.model_validate(payload)


def manifest_digest(meeting_id, command):
    return digest({"schema_version": "secretary.local-asr-run.v1", "meeting_id": meeting_id,
        "transcript_version": command.transcript_version, "result": command.result.model_dump(mode="json"),
        "artifacts": command.artifacts.model_dump(mode="json")})


@pytest.fixture
def publication(fixture):
    initialize_local_asr_schema(fixture.db)
    command = seed_publication_source(fixture)
    identity = command.artifacts.identity
    plan = make_plan(input_sha256=command.artifacts.input_sha256,
        source_text_sha256=command.artifacts.selected_text_sha256,
        profile_digest=identity.profile_digest, model_digest=model_manifest_digest(identity.models),
        code_digest=identity.code_digest, asr_device=identity.asr_device,
        diarization_device=identity.diarization_device, physical_gpu_id=identity.physical_gpu_id,
        precision=identity.precision)
    job, _, claim = running(fixture, plan)
    return SimpleNamespace(fixture=fixture, command=command, plan=plan, job=job, claim=claim,
        repo=LocalAsrRepository(fixture.db), artifact_digest=manifest_digest(fixture.meeting, command))


def test_prepare_uses_existing_queue_and_is_blocked_without_mutating_meeting(fixture):
    before = fixture.db.meeting(fixture.meeting, internal=True)
    job, plan = prepared(fixture)
    assert job["state"] == "blocked"
    assert job["job_status"] == "waiting_config"
    assert job["reason"] == "local_asr_inference_not_authorized"
    assert fixture.db.claim() is None
    fixture.db.recover()
    assert fixture.db.claim() is None
    assert fixture.life.view(job["job_id"])["state"] == "blocked"
    assert fixture.life.plan(job["job_id"]) == plan
    assert fixture.db.meeting(fixture.meeting, internal=True) == before
    assert fixture.db.rows("SELECT * FROM usage") == []


def test_idempotency_freezes_scope_input_plan_and_request(fixture):
    job, plan = prepared(fixture)
    assert fixture.life.prepare(fixture.meeting, 1, plan, "request-1") == job
    with pytest.raises(LocalExecutionError, match="idempotency_conflict"):
        fixture.life.prepare(fixture.meeting, 1, make_plan(code_digest="9" * 64), "request-1")
    with pytest.raises(LocalExecutionError, match="analysis_already_pending"):
        fixture.life.prepare(fixture.meeting, 1, plan, "request-2")
    other = fixture.db.create_meeting("Same audio different meeting")
    fixture.db.update_meeting(other["id"], transcript_version=1)
    other_job = fixture.life.prepare(other["id"], 1, make_plan(owner_scope="other-owner"), "request-1")
    assert other_job["job_id"] != job["job_id"]
    with pytest.raises(KeyError):
        fixture.life.view(job["job_id"], "other-owner")
    assert len(fixture.db.rows("SELECT * FROM jobs")) == 2


def test_plan_and_checkpoint_tables_are_immutable(fixture):
    job, _ = prepared(fixture)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        fixture.db.execute("UPDATE local_asr_executions SET plan_digest=? WHERE job_id=?", ("0" * 64, job["job_id"]))


@pytest.mark.parametrize("changes", [
    {"stages": ("diarize", "asr_rnnt", "assemble")},
    {"stages": ("diarize", "align_rnnt", "assemble")},
    {"stages": ("diarize", "diarize")},
    {"stages": ()},
    {"ram_reserve_mib": 6655},
    {"commit_reserve_mib": 9215},
    {"cuda_free_vram_mib": 10239},
    {"allocator_budget_mib": 8193},
    {"owned_rss_limit_mib": 4609},
    {"job_commit_limit_mib": 8193},
    {"cpu_threads": True},
    {"deadline_at": float("inf")},
    {"physical_gpu_id": None},
    {"provider": "polza"},
    {"source_text_sha256": None},
])
def test_plan_rejects_unsafe_or_ambiguous_execution(changes):
    with pytest.raises(ValidationError):
        make_plan(**changes)


def test_local_prepare_never_silently_falls_back_to_polza(fixture):
    plan = make_plan(provider="polza", retry_policy="never_repeat_uncertain_paid")
    with pytest.raises(LocalExecutionError, match="no_cloud_fallback"):
        fixture.life.prepare(fixture.meeting, 1, plan, "request-1")
    assert fixture.db.rows("SELECT * FROM jobs") == []


def test_saved_meeting_cannot_be_retranscribed_even_if_caller_omits_source(fixture):
    chunk = fixture.db.add_chunk(fixture.meeting, dict(sequence=0,channel="import",path="synthetic.wav",offset_ms=0,duration_ms=1000,sha256="1" * 64))
    fixture.db.execute("""INSERT INTO segments(id,meeting_id,chunk_id,transcript_version,ordinal,
        timing_precision,text,channel) VALUES('s',?,?,1,0,'unknown','Saved paid text','import')""", (fixture.meeting,chunk["id"]))
    plan = make_plan(source_text_sha256=None, stages=("diarize", "asr_rnnt", "assemble"))
    with pytest.raises(LocalExecutionError, match="must_not_be_retranscribed"):
        fixture.life.prepare(fixture.meeting, 1, plan, "request-1")
    assert fixture.db.rows("SELECT * FROM jobs") == []


def test_saved_text_fingerprint_and_pydantic_copy_bypass_rejected(fixture):
    with pytest.raises(LocalExecutionError, match="saved_text_fingerprint_mismatch"):
        fixture.life.prepare(fixture.meeting, 1, make_plan(source_text_sha256="2" * 64), "request")
    invalid_copy = make_plan().model_copy(update={"stages": ("asr_rnnt",)})
    with pytest.raises(ValidationError, match="must_not_be_retranscribed"):
        fixture.life.prepare(fixture.meeting, 1, invalid_copy, "request")


def test_unwired_worker_defers_accidental_queue_claim_without_changing_frozen_plan(fixture):
    job, plan = prepared(fixture)
    # Legacy external code might incorrectly requeue a waiting row. Even then
    # Worker must route through defer_dispatch, never a provider.
    fixture.db.execute("UPDATE jobs SET status='queued' WHERE id=?", (job["job_id"],))
    fixture.db.claim()
    result = fixture.life.defer_dispatch(job["job_id"])
    assert result["state"] == "blocked"
    assert result["job_status"] == "waiting_config"
    assert result["reason"] == "local_asr_runtime_adapter_unavailable"
    assert fixture.life.plan(job["job_id"]) == plan
    assert fixture.db.claim() is None


def test_defer_dispatch_never_overwrites_owned_attempt(fixture):
    job, _, _ = running(fixture)
    with pytest.raises(LocalExecutionError, match="cannot_defer_owned_execution"):
        fixture.life.defer_dispatch(job["job_id"])
    assert fixture.life.view(job["job_id"])["state"] == "running"


def test_no_unqualified_dispatch_or_implicit_claim(fixture):
    job, plan = prepared(fixture)
    incomplete = RuntimeQualification(plan.profile_digest, "evidence", "approval", True, False, True)
    with pytest.raises(LocalExecutionError, match="qualification_required"):
        fixture.life.authorize_queue(job["job_id"], incomplete)
    with pytest.raises(LocalExecutionError, match="queue_claim_required"):
        fixture.life.begin_claim(job["job_id"], "worker-A", ProcessIdentity(1, 1))
    fixture.life.authorize_queue(job["job_id"], qualify(plan))
    with pytest.raises(LocalExecutionError, match="queue_claim_required"):
        fixture.life.begin_claim(job["job_id"], "worker-A", ProcessIdentity(1, 1))


def test_two_executors_cannot_claim_one_job(fixture):
    job, plan = prepared(fixture)
    fixture.life.authorize_queue(job["job_id"], qualify(plan))
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: fixture.db.claim(), range(2)))
    assert sum(value is not None for value in claims) == 1
    with ThreadPoolExecutor(max_workers=2) as pool:
        def claim(worker):
            try:
                return fixture.life.begin_claim(job["job_id"], worker, ProcessIdentity(123, 42))
            except LocalExecutionError:
                return None
        begun = list(pool.map(claim, ("A", "B")))
    assert sum(value is not None for value in begun) == 1
    assert len(fixture.db.rows("SELECT * FROM local_asr_attempts")) == 1


def test_expired_lease_does_not_duplicate_live_process_and_old_fence_cannot_publish(fixture):
    job, plan, claim = running(fixture)
    fixture.clock.value = 111
    with pytest.raises(LocalExecutionError, match="previous_owner_not_exited"):
        fixture.life.recover_expired(job["job_id"])
    assert fixture.db.claim() is None
    with pytest.raises(LocalExecutionError, match="lease_expired"):
        fixture.life.store_artifact(claim, "6" * 64)
    fixture.clock.owner_exited = True
    assert fixture.life.recover_expired(job["job_id"])["state"] == "queued"
    fixture.db.claim()
    second = fixture.life.begin_claim(job["job_id"], "worker-B", ProcessIdentity(124, 51), lease_seconds=10)
    assert second.fence == claim.fence + 1
    assert second.attempt_id != claim.attempt_id
    with pytest.raises(LocalExecutionError, match="stale_owner"):
        fixture.life.store_artifact(claim, "6" * 64)
    fixture.life.store_artifact(second, "6" * 64)


def test_restarts_are_bounded_and_deadline_never_requeues(fixture):
    job, _, _ = running(fixture)
    fixture.clock.value, fixture.clock.owner_exited = 111, True
    fixture.life.recover_expired(job["job_id"])
    fixture.db.claim()
    fixture.life.begin_claim(job["job_id"], "B", ProcessIdentity(124, 51), lease_seconds=10)
    fixture.clock.value = 122
    assert fixture.life.recover_expired(job["job_id"])["state"] == "failed"
    assert fixture.db.claim() is None


@pytest.mark.parametrize("field", ["input_sha256", "source_text_sha256", "model_digest", "code_digest", "profile_digest", "asr_device", "diarization_device", "precision", "physical_gpu_id", "plan_digest"])
def test_checkpoint_rejects_changed_identity(fixture, field):
    _, plan, claim = running(fixture)
    identity = plan.checkpoint_identity("diarize")
    fixture.life.save_checkpoint(claim, "diarize", identity, {"raw_turns": []})
    assert fixture.life.checkpoint(claim, "diarize", identity) == {"raw_turns": []}
    altered = {**identity, field: "different"}
    with pytest.raises(LocalExecutionError, match="checkpoint_incompatible"):
        fixture.life.checkpoint(claim, "diarize", altered)
    with pytest.raises(LocalExecutionError, match="checkpoint_incompatible"):
        fixture.life.save_checkpoint(claim, "diarize", altered, {"raw_turns": []})


def test_checkpoints_idempotent_not_overwritten(fixture):
    _, plan, claim = running(fixture)
    identity = plan.checkpoint_identity("diarize")
    first = fixture.life.save_checkpoint(claim, "diarize", identity, {"raw_turns": []})
    assert fixture.life.save_checkpoint(claim, "diarize", identity, {"raw_turns": []}) == first
    with pytest.raises(LocalExecutionError, match="checkpoint_conflict"):
        fixture.life.save_checkpoint(claim, "diarize", identity, {"raw_turns": ["new"]})


def test_artifact_write_and_business_application_are_separate_atomic_states(publication):
    fixture, job, claim = publication.fixture, publication.job, publication.claim
    fixture.db.execute("CREATE TABLE synthetic_apply(id TEXT PRIMARY KEY)")
    fixture.life.store_artifact(claim, publication.artifact_digest)
    assert fixture.life.view(job["job_id"])["state"] == "artifact_stored"
    assert fixture.db.job(job["job_id"])["status"] == "running"
    def failed(conn):
        publication.repo.import_run(fixture.meeting, publication.command,
            trusted_input_sha256=publication.plan.input_sha256, conn=conn)
        conn.execute("INSERT INTO synthetic_apply VALUES('run-1')")
        raise RuntimeError("simulated import failure")
    with pytest.raises(RuntimeError):
        fixture.life.apply_atomically(claim, failed)
    assert fixture.db.rows("SELECT * FROM synthetic_apply") == []
    assert publication.repo.list_runs(fixture.meeting) == []
    assert fixture.db.rows("SELECT * FROM local_asr_operations") == []
    assert fixture.life.view(job["job_id"])["state"] == "artifact_stored"
    calls = []
    def succeed(conn):
        calls.append(True)
        conn.execute("INSERT INTO synthetic_apply VALUES('run-1')")
        return publication.repo.import_run(fixture.meeting, publication.command,
            trusted_input_sha256=publication.plan.input_sha256, conn=conn)["run_id"]
    run_id = fixture.life.apply_atomically(claim, succeed)
    assert publication.repo.get_run(fixture.meeting, run_id)["artifact_digest"] == publication.artifact_digest
    assert fixture.life.apply_atomically(claim, succeed) == run_id
    assert calls == [True]
    assert fixture.db.job(job["job_id"])["status"] == "succeeded"
    assert fixture.db.meeting(fixture.meeting)["status"] == "completed"


def test_callback_cannot_fabricate_success_without_a_persisted_run(publication):
    fixture, claim = publication.fixture, publication.claim
    fixture.life.store_artifact(claim, publication.artifact_digest)
    with pytest.raises(LocalExecutionError, match="applied_run_missing"):
        fixture.life.apply_atomically(claim, lambda conn: "invented-run-id")
    assert fixture.db.job(claim.job_id)["status"] == "running"
    assert fixture.life.view(claim.job_id)["state"] == "artifact_stored"


@pytest.mark.parametrize("field,value,reason", [
    ("input_sha256", "9" * 64, "applied_input_mismatch"),
    ("profile_digest", "9" * 64, "applied_profile_mismatch"),
    ("code_digest", "9" * 64, "applied_profile_mismatch"),
    ("precision", "float16", "applied_profile_mismatch"),
    ("asr_device", "cuda", "applied_device_mismatch"),
    ("diarization_device", "cpu", "applied_device_mismatch"),
    ("physical_gpu_id", "GPU-other", "applied_device_mismatch"),
    ("devices_unknown", None, "applied_device_mismatch"),
    ("weights_sha256", "9" * 64, "applied_model_mismatch"),
    ("loader_sha256", "9" * 64, "applied_model_mismatch"),
    ("processor_sha256", "9" * 64, "applied_model_mismatch"),
    ("custom_code_sha256", "9" * 64, "applied_model_mismatch"),
])
def test_real_artifact_with_wrong_provenance_rolls_back_import_and_receipt(publication, field, value, reason):
    fixture, claim = publication.fixture, publication.claim
    payload = publication.command.model_dump(mode="json")
    if field == "input_sha256":
        payload["result"][field] = value
        payload["artifacts"][field] = value
    elif field == "devices_unknown":
        payload["artifacts"]["identity"].update(asr_device=None, diarization_device=None, physical_gpu_id=None)
    elif field in {"weights_sha256", "loader_sha256", "processor_sha256", "custom_code_sha256"}:
        payload["artifacts"]["identity"]["models"][0][field] = value
    else:
        payload["artifacts"]["identity"][field] = value
    command = ImportLocalAsrRun.model_validate(payload)
    # A producer can consistently hash a wrong execution's artifact. Matching
    # the content hash must not replace plan/model/device verification.
    fixture.life.store_artifact(claim, manifest_digest(fixture.meeting, command))
    def publish(conn):
        return publication.repo.import_run(fixture.meeting, command,
            trusted_input_sha256=command.artifacts.input_sha256, conn=conn)["run_id"]
    with pytest.raises(LocalExecutionError, match=reason):
        fixture.life.apply_atomically(claim, publish)
    assert publication.repo.list_runs(fixture.meeting) == []
    assert fixture.db.rows("SELECT * FROM local_asr_operations") == []
    assert fixture.db.job(claim.job_id)["status"] == "running"


@pytest.mark.parametrize("wrong_scope", ["owner", "meeting", "version"])
def test_other_scope_persisted_run_cannot_complete_execution(publication, wrong_scope):
    fixture, claim = publication.fixture, publication.claim
    command = publication.command
    repo = publication.repo
    meeting_id = fixture.meeting
    if wrong_scope == "owner":
        repo = LocalAsrRepository(fixture.db, owner_scope="other-owner")
    elif wrong_scope == "meeting":
        other = fixture.db.create_meeting("Synthetic other scope")
        fixture.db.update_meeting(other["id"], transcript_version=1)
        meeting_id = other["id"]
        command = seed_publication_source(fixture, meeting_id).model_copy(update={"operation_id": "other-operation"})
    else:
        fixture.db.execute("""INSERT INTO segments SELECT id||'-v2',meeting_id,chunk_id,2,ordinal,start_ms,end_ms,
            timing_precision,text,confidence,speaker_id,channel FROM segments WHERE meeting_id=? AND transcript_version=1""", (meeting_id,))
        payload = command.model_dump(mode="json")
        payload["operation_id"] = "other-version"
        payload["transcript_version"] = 2
        for index, source in enumerate(payload["artifacts"]["source_segments"]):
            source["segment_id"] += "-v2"
            payload["artifacts"]["segment_sources"][index]["source_segment_ids"] = [source["segment_id"]]
        command = ImportLocalAsrRun.model_validate(payload)
    fixture.life.store_artifact(claim, manifest_digest(meeting_id, command))
    def publish(conn):
        return repo.import_run(meeting_id, command, trusted_input_sha256=command.artifacts.input_sha256, conn=conn)["run_id"]
    with pytest.raises(LocalExecutionError, match="applied_run_scope_mismatch"):
        fixture.life.apply_atomically(claim, publish)
    assert fixture.db.rows("SELECT * FROM local_asr_runs") == []
    assert fixture.db.rows("SELECT * FROM local_asr_operations") == []


def test_stored_artifact_digest_must_match_the_actual_import(publication):
    fixture, claim = publication.fixture, publication.claim
    fixture.life.store_artifact(claim, "0" * 64)
    def publish(conn):
        return publication.repo.import_run(fixture.meeting, publication.command,
            trusted_input_sha256=publication.plan.input_sha256, conn=conn)["run_id"]
    with pytest.raises(LocalExecutionError, match="applied_artifact_mismatch"):
        fixture.life.apply_atomically(claim, publish)
    assert publication.repo.list_runs(fixture.meeting) == []


def test_cancel_cannot_publish_and_stays_pending_until_cleanup(fixture):
    job, _, claim = running(fixture)
    fixture.life.store_artifact(claim, "6" * 64)
    assert fixture.life.request_cancel(job["job_id"])["state"] == "cancel_requested"
    with pytest.raises(LocalExecutionError, match="cancel_requested"):
        fixture.life.apply_atomically(claim, lambda conn: "never")
    with pytest.raises(LocalExecutionError, match="cleanup_unconfirmed"):
        fixture.life.finish_after_cleanup(claim, cleanup_complete=False, reason="cancelled")
    assert fixture.life.view(job["job_id"])["state"] == "cancel_requested"
    fixture.life.finish_after_cleanup(claim, cleanup_complete=True, reason="cancelled")
    assert fixture.life.view(job["job_id"])["state"] == "cancelled"
    assert fixture.db.meeting(fixture.meeting)["status"] == "completed"


def test_cancel_prepared_job_preserves_existing_meeting_and_scope(fixture):
    job, _ = prepared(fixture)
    with pytest.raises(KeyError):
        fixture.life.request_cancel(job["job_id"], "other-owner")
    assert fixture.life.request_cancel(job["job_id"])["state"] == "cancelled"
    assert fixture.db.claim() is None
    assert fixture.db.meeting(fixture.meeting)["status"] == "completed"


def test_progress_is_monotonic_and_lease_deadline_bounded(fixture):
    job, _, claim = running(fixture)
    fixture.life.heartbeat(claim, "diarize", 0.7)
    with pytest.raises(LocalExecutionError, match="progress_regression"):
        fixture.life.heartbeat(claim, "diarize", 0.1)
    fixture.life.heartbeat(claim, "align_saved_text", 0)
    with pytest.raises(LocalExecutionError, match="progress_regression"):
        fixture.life.heartbeat(claim, "diarize", 1)
    with pytest.raises(LocalExecutionError, match="progress_invalid"):
        fixture.life.heartbeat(claim, "align_saved_text", float("nan"))
    assert fixture.life.view(job["job_id"])["stage"] == "align_saved_text"


def test_execution_resource_profile_uses_frozen_limits_and_total_remaining_deadline():
    plan = make_plan(nemotron_free_vram_mib=3000)
    resource = plan.resource_profile(9950)
    assert resource.deadline_seconds == 50
    assert resource.nemotron_free_vram_mib == 3000
    assert resource.allocator_budget_mib == 8192
    assert resource.owned_rss_limit_mib == 4608
    assert resource.job_commit_limit_mib == 8192
    assert resource.physical_gpu_id == plan.physical_gpu_id
    with pytest.raises(LocalExecutionError, match="deadline_expired"):
        plan.resource_profile(10000)


def test_stale_source_version_prevents_late_activation(fixture):
    job, plan = prepared(fixture)
    fixture.db.update_meeting(fixture.meeting, transcript_version=2)
    with pytest.raises(LocalExecutionError, match="transcript_version_conflict"):
        fixture.life.authorize_queue(job["job_id"], qualify(plan))
    assert fixture.db.claim() is None


def test_current_owner_identity_can_be_checked_without_models():
    from secretary.application.local_asr_lifecycle import process_has_exited
    owner = ProcessIdentity.current()
    assert process_has_exited(owner) is False
    assert process_has_exited(ProcessIdentity(owner.pid, owner.created_at - 1)) is True
