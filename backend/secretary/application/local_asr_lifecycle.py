"""Fenced execution metadata attached to Secretary's existing durable jobs.

This module has no polling loop, model loader or HTTP client. Preparation is
blocked by default. The existing queue remains the only job claimant; a future
qualified adapter must explicitly authorize and dispatch this stage.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
import sqlite3
import time
from typing import Callable, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from secretary.infrastructure.database import now


class LocalExecutionError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


class ExecutionPlan(BaseModel):
    """Server-derived, immutable identity; never accept caller device/permission."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["secretary.local-asr.execution.v1"] = "secretary.local-asr.execution.v1"
    application: str = Field(default="secretary", min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    owner_scope: str = Field(default="local-owner", min_length=1, max_length=128)
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_text_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    provider: Literal["local", "polza"] = "local"
    profile: Literal["nemotron-ctc"] = "nemotron-ctc"
    profile_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    code_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    asr_device: Literal["cpu", "cuda"]
    diarization_device: Literal["cpu", "cuda"]
    physical_gpu_id: str | None = Field(default=None, min_length=1, max_length=128)
    precision: str = Field(min_length=1, max_length=64)
    stages: tuple[Literal["normalize", "diarize", "asr_rnnt", "align_rnnt", "align_saved_text", "assemble", "validate"], ...]
    cpu_threads: int = Field(default=4, ge=1, le=128, strict=True)
    ram_reserve_mib: int = Field(default=6656, ge=6656, strict=True)
    commit_reserve_mib: int = Field(default=9216, ge=9216, strict=True)
    cuda_free_vram_mib: int = Field(default=10240, ge=10240, strict=True)
    allocator_budget_mib: int = Field(default=8192, ge=1, le=8192, strict=True)
    nemotron_free_vram_mib: int | None = Field(default=None, ge=1, strict=True)
    owned_rss_limit_mib: int = Field(default=4608, ge=1, le=4608, strict=True)
    job_commit_limit_mib: int = Field(default=8192, ge=1, le=8192, strict=True)
    deadline_at: float = Field(gt=0, allow_inf_nan=False)
    max_attempts: int = Field(default=2, ge=1, le=3, strict=True)
    retry_policy: Literal["local_bounded_after_exit", "never_repeat_uncertain_paid"] = "local_bounded_after_exit"

    @model_validator(mode="after")
    def validate_plan(self):
        if not self.stages or len(self.stages) != len(set(self.stages)):
            raise ValueError("local_asr_stages_invalid")
        if self.source_text_sha256 is not None and ("asr_rnnt" in self.stages or "align_rnnt" in self.stages):
            raise ValueError("saved_text_must_not_be_retranscribed")
        if "align_saved_text" in self.stages and self.source_text_sha256 is None:
            raise ValueError("saved_text_fingerprint_required")
        if (self.asr_device == "cuda" or self.diarization_device == "cuda") and not self.physical_gpu_id:
            raise ValueError("physical_gpu_identity_required")
        if self.provider == "polza" and self.retry_policy != "never_repeat_uncertain_paid":
            raise ValueError("paid_retry_policy_invalid")
        if self.provider == "local" and self.retry_policy != "local_bounded_after_exit":
            raise ValueError("local_retry_policy_invalid")
        return self

    @property
    def digest(self) -> str:
        return _digest(self.model_dump(mode="json"))

    def checkpoint_identity(self, stage: str) -> dict:
        if stage not in self.stages:
            raise LocalExecutionError("local_asr_stage_not_in_plan")
        return {"plan_digest": self.digest, "input_sha256": self.input_sha256,
                "source_text_sha256": self.source_text_sha256,
                "model_digest": self.model_digest, "code_digest": self.code_digest,
                "profile_digest": self.profile_digest, "asr_device": self.asr_device,
                "diarization_device": self.diarization_device, "physical_gpu_id": self.physical_gpu_id,
                "precision": self.precision, "stage": stage}

    def resource_profile(self, clock: float):
        """Use the same frozen limits and the remaining total job deadline."""
        from secretary.infrastructure.local_asr_resources import ResourceProfile
        remaining = math.floor(self.deadline_at - clock)
        if remaining <= 0:
            raise LocalExecutionError("local_asr_deadline_expired")
        return ResourceProfile(asr_device=self.asr_device, diarization_device=self.diarization_device,
            physical_gpu_id=self.physical_gpu_id, cpu_threads=self.cpu_threads,
            ram_reserve_mib=self.ram_reserve_mib, commit_reserve_mib=self.commit_reserve_mib,
            cuda_free_vram_mib=self.cuda_free_vram_mib, allocator_budget_mib=self.allocator_budget_mib,
            nemotron_free_vram_mib=self.nemotron_free_vram_mib, owned_rss_limit_mib=self.owned_rss_limit_mib,
            job_commit_limit_mib=self.job_commit_limit_mib,
            deadline_seconds=remaining)


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    created_at: float

    @classmethod
    def current(cls):
        import psutil
        return cls(os.getpid(), psutil.Process(os.getpid()).create_time())


def process_has_exited(identity: ProcessIdentity) -> bool:
    """Fail closed on permissions/errors; PID reuse proves only old identity exited."""
    import psutil
    try:
        process = psutil.Process(identity.pid)
        return process.create_time() != identity.created_at or not process.is_running()
    except psutil.NoSuchProcess:
        return True
    except (psutil.AccessDenied, OSError):
        return False


@dataclass(frozen=True)
class ExecutionClaim:
    job_id: str
    owner_scope: str
    worker_id: str
    process: ProcessIdentity
    attempt_id: str
    fence: int


@dataclass(frozen=True)
class RuntimeQualification:
    """Operator-side evidence, not a request DTO or automatic approval.

    The current API never constructs this. Activation requires a separately
    reviewed runtime with verified process containment and shared admission.
    """
    profile_digest: str
    evidence_id: str
    owner_authorization_id: str
    source_verified: bool
    process_containment_verified: bool
    shared_admission_verified: bool


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS local_asr_executions(
        job_id TEXT PRIMARY KEY REFERENCES jobs(id), application TEXT NOT NULL,
        owner_scope TEXT NOT NULL, request_id TEXT NOT NULL, request_digest TEXT NOT NULL,
        plan TEXT NOT NULL CHECK(json_valid(plan)), plan_digest TEXT NOT NULL,
        state TEXT NOT NULL, reason TEXT, worker_id TEXT, owner_pid INTEGER, owner_started REAL,
        attempt_id TEXT, fence INTEGER NOT NULL DEFAULT 0, lease_until REAL,
        attempt_count INTEGER NOT NULL DEFAULT 0, stage TEXT, progress REAL NOT NULL DEFAULT 0,
        artifact_digest TEXT, result_run_id TEXT, qualification TEXT,
        UNIQUE(application,owner_scope,request_id))""",
    """CREATE TRIGGER IF NOT EXISTS local_asr_plan_immutable BEFORE UPDATE OF
        application,owner_scope,request_id,request_digest,plan,plan_digest,job_id ON local_asr_executions
        BEGIN SELECT RAISE(ABORT,'Local ASR execution plan is immutable'); END""",
    """CREATE TABLE IF NOT EXISTS local_asr_attempts(
        attempt_id TEXT PRIMARY KEY,job_id TEXT NOT NULL REFERENCES jobs(id),fence INTEGER NOT NULL,
        worker_id TEXT NOT NULL,owner_pid INTEGER NOT NULL,owner_started REAL NOT NULL,
        state TEXT NOT NULL,started_at REAL NOT NULL,ended_at REAL,reason TEXT,
        UNIQUE(job_id,fence))""",
    """CREATE TABLE IF NOT EXISTS local_asr_checkpoints(
        job_id TEXT NOT NULL REFERENCES jobs(id),stage TEXT NOT NULL,identity TEXT NOT NULL,
        payload TEXT NOT NULL CHECK(json_valid(payload)),payload_digest TEXT NOT NULL,
        PRIMARY KEY(job_id,stage))""",
    """CREATE TRIGGER IF NOT EXISTS local_asr_checkpoint_immutable BEFORE UPDATE ON local_asr_checkpoints
        BEGIN SELECT RAISE(ABORT,'Local ASR checkpoints are immutable'); END""",
)


class LocalAsrLifecycle:
    def __init__(self, database, *, clock: Callable[[], float] = time.time,
                 exited: Callable[[ProcessIdentity], bool] = process_has_exited):
        self.db, self.clock, self.exited = database, clock, exited

    def initialize_schema(self) -> None:
        """Explicit startup migration; construction/read has no migration side effect."""
        with self.db.transaction() as conn:
            for statement in SCHEMA:
                conn.execute(statement)

    def prepare(self, meeting_id: str, transcript_version: int, plan: ExecutionPlan,
                request_id: str) -> dict:
        if not isinstance(plan, ExecutionPlan):
            raise LocalExecutionError("local_asr_plan_required")
        # Revalidate copies constructed with model_copy(update=...), which does
        # not run Pydantic validators itself.
        plan = ExecutionPlan.model_validate(plan.model_dump(mode="json"))
        if plan.provider != "local":
            raise LocalExecutionError("local_asr_no_cloud_fallback")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
            raise LocalExecutionError("local_asr_request_id_invalid")
        identity = _digest({"meeting_id": meeting_id, "version": transcript_version,
                            "plan": plan.model_dump(mode="json")})
        with self.db.transaction() as conn:
            prior = conn.execute("SELECT job_id,request_digest FROM local_asr_executions WHERE application=? AND owner_scope=? AND request_id=?",
                                 (plan.application, plan.owner_scope, request_id)).fetchone()
            if prior:
                if prior["request_digest"] != identity:
                    raise LocalExecutionError("local_asr_idempotency_conflict")
                return self._view(conn, prior["job_id"], plan.owner_scope)
            meeting = conn.execute("SELECT transcript_version,recording FROM meetings WHERE id=?", (meeting_id,)).fetchone()
            if not meeting:
                raise KeyError(meeting_id)
            initial_version = (meeting["transcript_version"] == 0 and transcript_version == 1
                               and plan.source_text_sha256 is None)
            if type(transcript_version) is not int or transcript_version < 1 or (meeting["transcript_version"] != transcript_version and not initial_version):
                raise LocalExecutionError("local_asr_transcript_version_conflict")
            if meeting["recording"]:
                raise LocalExecutionError("local_asr_recording_in_progress")
            from secretary.infrastructure.local_asr_repository import LocalAsrRepository
            from secretary.domain.local_asr import text_fingerprint
            text_rows = LocalAsrRepository.source_snapshot(conn, meeting_id, transcript_version)
            if text_rows and ("asr_rnnt" in plan.stages or "align_rnnt" in plan.stages or plan.source_text_sha256 is None):
                raise LocalExecutionError("saved_text_must_not_be_retranscribed")
            if plan.source_text_sha256 is not None and plan.source_text_sha256 != text_fingerprint("\n".join(row["text"] for row in text_rows)):
                raise LocalExecutionError("local_asr_saved_text_fingerprint_mismatch")
            active = conn.execute("SELECT 1 FROM jobs WHERE meeting_id=? AND version=? AND stage='local_asr' AND status IN ('queued','running','waiting_config','paused_budget','uncertain')", (meeting_id, transcript_version)).fetchone()
            if active:
                raise LocalExecutionError("local_asr_analysis_already_pending")
            job_id, stamp = str(uuid4()), now()
            reason = "local_asr_inference_not_authorized"
            if conn.execute("""SELECT 1 FROM jobs WHERE meeting_id=? AND stage='local_asr'
                    AND version=? AND status IN ('queued','running','waiting_config','paused_budget','uncertain')""",
                    (meeting_id, transcript_version)).fetchone():
                raise LocalExecutionError("local_asr_execution_already_active")
            conn.execute("""INSERT INTO jobs(id,meeting_id,stage,status,created_at,updated_at,version,payload,error)
                VALUES(?,?,'local_asr','waiting_config',?,?,?,?,?)""",
                         (job_id, meeting_id, stamp, stamp, transcript_version,
                          _canonical({"execution_schema": plan.schema_version}), reason))
            conn.execute("""INSERT INTO local_asr_executions(job_id,application,owner_scope,request_id,
                request_digest,plan,plan_digest,state,reason) VALUES(?,?,?,?,?,?,?,'blocked',?)""",
                         (job_id, plan.application, plan.owner_scope, request_id, identity,
                          _canonical(plan.model_dump(mode="json")), plan.digest, reason))
            return self._view(conn, job_id, plan.owner_scope)

    @staticmethod
    def _row(conn, job_id: str, owner_scope: str):
        row = conn.execute("""SELECT e.*,j.status AS job_status,j.cancel_requested,j.meeting_id,j.version
            FROM local_asr_executions e JOIN jobs j ON j.id=e.job_id
            WHERE e.job_id=? AND e.owner_scope=?""", (job_id, owner_scope)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return dict(row)

    @classmethod
    def _view(cls, conn, job_id: str, owner_scope: str) -> dict:
        row = cls._row(conn, job_id, owner_scope)
        return {key: row[key] for key in ("job_id", "meeting_id", "version", "state", "reason", "job_status",
                "cancel_requested", "attempt_count", "stage", "progress", "artifact_digest", "result_run_id", "plan_digest")}

    def view(self, job_id: str, owner_scope: str = "local-owner") -> dict:
        with self.db.connection() as conn:
            return self._view(conn, job_id, owner_scope)

    def plan(self, job_id: str, owner_scope: str = "local-owner") -> ExecutionPlan:
        with self.db.connection() as conn:
            return ExecutionPlan.model_validate_json(self._row(conn, job_id, owner_scope)["plan"])

    @staticmethod
    def _validate_current_source(conn, row, plan):
        from secretary.infrastructure.local_asr_repository import LocalAsrRepository
        from secretary.domain.local_asr import text_fingerprint
        version = conn.execute("SELECT transcript_version FROM meetings WHERE id=?", (row["meeting_id"],)).fetchone()
        initial_version = (version and version["transcript_version"] == 0 and row["version"] == 1
                           and plan.source_text_sha256 is None)
        if not version or (version["transcript_version"] != row["version"] and not initial_version):
            raise LocalExecutionError("local_asr_transcript_version_conflict")
        if plan.source_text_sha256 is not None:
            snapshot = LocalAsrRepository.source_snapshot(conn, row["meeting_id"], row["version"])
            if text_fingerprint("\n".join(item["text"] for item in snapshot)) != plan.source_text_sha256:
                raise LocalExecutionError("local_asr_saved_text_fingerprint_mismatch")

    def defer_dispatch(self, job_id: str, owner_scope: str = "local-owner") -> dict:
        """Existing Worker has no qualified model adapter in this phase.

        This handles an accidentally queued row without permitting it to reach
        the paid provider branch or erasing a previously owned attempt.
        """
        with self.db.transaction() as conn:
            row = self._row(conn, job_id, owner_scope)
            if row["job_status"] != "running" or row["attempt_count"] != 0 or row["state"] not in {"queued", "blocked"}:
                raise LocalExecutionError("local_asr_cannot_defer_owned_execution")
            reason = "local_asr_runtime_adapter_unavailable"
            conn.execute("UPDATE local_asr_executions SET state='blocked',reason=? WHERE job_id=?", (reason, job_id))
            conn.execute("UPDATE jobs SET status='waiting_config',error=?,updated_at=? WHERE id=?", (reason, now(), job_id))
            return self._view(conn, job_id, owner_scope)

    def authorize_queue(self, job_id: str, qualification: RuntimeQualification,
                        owner_scope: str = "local-owner") -> dict:
        """Queue only a runtime whose host-side qualification was revalidated."""
        if not isinstance(qualification, RuntimeQualification) or not (
            qualification.evidence_id and qualification.owner_authorization_id and qualification.source_verified
            and qualification.process_containment_verified and qualification.shared_admission_verified
        ):
            raise LocalExecutionError("local_asr_qualification_required")
        with self.db.transaction() as conn:
            row = self._row(conn, job_id, owner_scope)
            plan = ExecutionPlan.model_validate_json(row["plan"])
            self._validate_current_source(conn, row, plan)
            if qualification.profile_digest != plan.profile_digest:
                raise LocalExecutionError("local_asr_qualification_profile_mismatch")
            if row["state"] not in {"blocked", "waiting_resources"} or row["cancel_requested"]:
                raise LocalExecutionError("local_asr_not_activatable")
            if self.clock() >= plan.deadline_at:
                raise LocalExecutionError("local_asr_deadline_expired")
            conn.execute("UPDATE local_asr_executions SET state='queued',reason=NULL,qualification=? WHERE job_id=?",
                         (_canonical(qualification.__dict__), job_id))
            conn.execute("UPDATE jobs SET status='queued',error=NULL,updated_at=? WHERE id=?", (now(), job_id))
            return self._view(conn, job_id, owner_scope)

    def reject_activation(self, job_id: str, reason: str, owner_scope: str = 'local-owner') -> dict:
        """Keep a denied operator activation visible without creating an attempt."""
        with self.db.transaction() as conn:
            row = self._row(conn, job_id, owner_scope)
            if row['state'] not in {'blocked', 'waiting_resources'} or row['job_status'] != 'waiting_config':
                raise LocalExecutionError('local_asr_not_activatable')
            if row['owner_pid'] is not None and not self.exited(ProcessIdentity(row['owner_pid'], row['owner_started'])):
                raise LocalExecutionError('local_asr_previous_owner_not_exited')
            conn.execute("UPDATE local_asr_executions SET state='blocked',reason=? WHERE job_id=?", (reason, job_id))
            conn.execute("UPDATE jobs SET error=?,updated_at=? WHERE id=?", (reason, now(), job_id))
            return self._view(conn, job_id, owner_scope)

    @staticmethod
    def _lease_seconds(value):
        if type(value) is not int or not 1 <= value <= 3600:
            raise LocalExecutionError("local_asr_lease_invalid")

    def begin_claim(self, job_id: str, worker_id: str, process: ProcessIdentity, *,
                    owner_scope: str = "local-owner", lease_seconds: int = 60) -> ExecutionClaim:
        """Called only after db.claim(); never scans or claims the queue itself."""
        self._lease_seconds(lease_seconds)
        if not worker_id or not isinstance(process, ProcessIdentity) or process.pid <= 0 or not math.isfinite(process.created_at):
            raise LocalExecutionError("local_asr_process_identity_invalid")
        with self.db.transaction() as conn:
            row = self._row(conn, job_id, owner_scope)
            plan = ExecutionPlan.model_validate_json(row["plan"])
            self._validate_current_source(conn, row, plan)
            if row["job_status"] != "running" or row["state"] != "queued" or not row["qualification"] or row["cancel_requested"]:
                raise LocalExecutionError("local_asr_queue_claim_required")
            if self.clock() >= plan.deadline_at:
                raise LocalExecutionError("local_asr_deadline_expired")
            if row["attempt_count"] >= plan.max_attempts:
                raise LocalExecutionError("local_asr_attempt_limit")
            if row["owner_pid"] is not None and not self.exited(ProcessIdentity(row["owner_pid"], row["owner_started"])):
                raise LocalExecutionError("local_asr_previous_owner_not_exited")
            attempt, fence = str(uuid4()), row["fence"] + 1
            conn.execute("""UPDATE local_asr_executions SET state='running',worker_id=?,owner_pid=?,owner_started=?,
                attempt_id=?,fence=?,lease_until=?,attempt_count=attempt_count+1,reason=NULL WHERE job_id=?""",
                         (worker_id, process.pid, process.created_at, attempt, fence,
                          min(self.clock() + lease_seconds, plan.deadline_at), job_id))
            conn.execute("INSERT INTO local_asr_attempts VALUES(?,?,?,?,?,?,'running',?,NULL,NULL)",
                         (attempt, job_id, fence, worker_id, process.pid, process.created_at, self.clock()))
            return ExecutionClaim(job_id, owner_scope, worker_id, process, attempt, fence)

    def _owned(self, conn, claim: ExecutionClaim, *, allow_cancel=False, allow_expired=False):
        row = self._row(conn, claim.job_id, claim.owner_scope)
        if (row["worker_id"], row["owner_pid"], row["owner_started"], row["attempt_id"], row["fence"]) != (
            claim.worker_id, claim.process.pid, claim.process.created_at, claim.attempt_id, claim.fence
        ):
            raise LocalExecutionError("local_asr_stale_owner")
        if row["cancel_requested"] and not allow_cancel:
            raise LocalExecutionError("local_asr_cancel_requested")
        if not allow_expired and (row["lease_until"] is None or self.clock() >= row["lease_until"]):
            raise LocalExecutionError("local_asr_lease_expired")
        plan = ExecutionPlan.model_validate_json(row["plan"])
        if not allow_expired and self.clock() >= plan.deadline_at:
            raise LocalExecutionError("local_asr_deadline_expired")
        return row, plan

    def heartbeat(self, claim: ExecutionClaim, stage: str, progress: float, *, lease_seconds=60):
        self._lease_seconds(lease_seconds)
        if isinstance(progress, bool) or not isinstance(progress, (int, float)) or not math.isfinite(progress) or not 0 <= progress <= 1:
            raise LocalExecutionError("local_asr_progress_invalid")
        with self.db.transaction() as conn:
            row, plan = self._owned(conn, claim)
            if row["state"] != "running" or stage not in plan.stages:
                raise LocalExecutionError("local_asr_stage_not_running")
            if row["stage"] and (plan.stages.index(stage) < plan.stages.index(row["stage"]) or (stage == row["stage"] and progress < row["progress"])):
                raise LocalExecutionError("local_asr_progress_regression")
            conn.execute("UPDATE local_asr_executions SET stage=?,progress=?,lease_until=? WHERE job_id=?",
                         (stage, progress, min(self.clock() + lease_seconds, plan.deadline_at), claim.job_id))

    def save_checkpoint(self, claim: ExecutionClaim, stage: str, identity: dict, payload: dict):
        encoded, digest = _canonical(payload), _digest(payload)
        with self.db.transaction() as conn:
            row, plan = self._owned(conn, claim)
            if row["state"] != "running" or identity != plan.checkpoint_identity(stage):
                raise LocalExecutionError("local_asr_checkpoint_incompatible")
            prior = conn.execute("SELECT identity,payload_digest FROM local_asr_checkpoints WHERE job_id=? AND stage=?", (claim.job_id, stage)).fetchone()
            if prior:
                if prior["identity"] != _canonical(identity) or prior["payload_digest"] != digest:
                    raise LocalExecutionError("local_asr_checkpoint_conflict")
                return digest
            conn.execute("INSERT INTO local_asr_checkpoints VALUES(?,?,?,?,?)", (claim.job_id, stage, _canonical(identity), encoded, digest))
            return digest

    def checkpoint(self, claim: ExecutionClaim, stage: str, expected_identity: dict) -> dict | None:
        with self.db.connection() as conn:
            _, plan = self._owned(conn, claim)
            if expected_identity != plan.checkpoint_identity(stage):
                raise LocalExecutionError("local_asr_checkpoint_incompatible")
            record = conn.execute("SELECT * FROM local_asr_checkpoints WHERE job_id=? AND stage=?", (claim.job_id, stage)).fetchone()
            if not record:
                return None
            payload = json.loads(record["payload"])
            if record["identity"] != _canonical(expected_identity) or _digest(payload) != record["payload_digest"]:
                raise LocalExecutionError("local_asr_checkpoint_corrupt")
            return payload

    def store_artifact(self, claim: ExecutionClaim, artifact_digest: str):
        if len(artifact_digest) != 64 or any(c not in "0123456789abcdef" for c in artifact_digest):
            raise LocalExecutionError("local_asr_artifact_digest_invalid")
        with self.db.transaction() as conn:
            row, _ = self._owned(conn, claim)
            if row["state"] == "artifact_stored" and row["artifact_digest"] == artifact_digest:
                return
            if row["state"] != "running":
                raise LocalExecutionError("local_asr_not_running")
            conn.execute("UPDATE local_asr_executions SET state='artifact_stored',artifact_digest=? WHERE job_id=?", (artifact_digest, claim.job_id))

    def apply_atomically(self, claim: ExecutionClaim, apply: Callable[[sqlite3.Connection], str]) -> str:
        """Import the run and record application in the same SQLite transaction."""
        with self.db.transaction() as conn:
            row, plan = self._owned(conn, claim)
            if row["state"] == "applied":
                return row["result_run_id"]
            if row["state"] != "artifact_stored":
                raise LocalExecutionError("local_asr_artifact_required")
            self._validate_current_source(conn, row, plan)
            run_id = apply(conn)
            if not isinstance(run_id, str) or not run_id:
                raise LocalExecutionError("local_asr_applied_run_id_required")
            self._validate_applied_run(conn, row, plan, run_id)
            # Re-check after callback, including cancellation and deadline.
            self._owned(conn, claim)
            self._validate_current_source(conn, row, plan)
            conn.execute("UPDATE local_asr_executions SET state='applied',result_run_id=?,progress=1 WHERE job_id=?", (run_id, claim.job_id))
            conn.execute("UPDATE local_asr_attempts SET state='completed',ended_at=? WHERE attempt_id=?", (self.clock(), claim.attempt_id))
            conn.execute("UPDATE jobs SET status='succeeded',error=NULL,updated_at=? WHERE id=?", (now(), claim.job_id))
            return run_id

    @staticmethod
    def _validate_applied_run(conn, execution, plan: ExecutionPlan, run_id: str) -> None:
        """A callback return value is not publication evidence.

        Verify the immutable row actually written in this transaction, including
        all provenance needed to distinguish CPU and CUDA execution. Failed
        validation rolls back the callback's import and its idempotency receipt.
        """
        from pydantic import ValidationError
        from secretary.domain.local_asr import ImportLocalAsrRun, model_manifest_digest
        from secretary.infrastructure.local_asr_repository import LocalAsrRepository
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='local_asr_runs'").fetchone()
        record = conn.execute("SELECT * FROM local_asr_runs WHERE id=?", (run_id,)).fetchone() if exists else None
        if record is None:
            raise LocalExecutionError("local_asr_applied_run_missing")
        if (record["owner_scope"], record["meeting_id"], record["transcript_version"]) != (
            execution["owner_scope"], execution["meeting_id"], execution["version"]
        ):
            raise LocalExecutionError("local_asr_applied_run_scope_mismatch")
        try:
            manifest = json.loads(record["manifest"])
            if (set(manifest) != {"schema_version", "meeting_id", "transcript_version", "result", "artifacts"}
                    or manifest["schema_version"] != "secretary.local-asr-run.v1"
                    or manifest["meeting_id"] != execution["meeting_id"]
                    or manifest["transcript_version"] != execution["version"]):
                raise LocalExecutionError("local_asr_applied_manifest_invalid")
            artifact_digest = _digest(manifest)
            if artifact_digest != record["artifact_digest"] or artifact_digest != execution["artifact_digest"]:
                raise LocalExecutionError("local_asr_applied_artifact_mismatch")
            command = ImportLocalAsrRun.model_validate({"operation_id": "lifecycle-validation",
                "transcript_version": manifest["transcript_version"], "result": manifest["result"],
                "artifacts": manifest["artifacts"]})
        except (KeyError, TypeError, ValidationError, json.JSONDecodeError):
            raise LocalExecutionError("local_asr_applied_manifest_invalid") from None
        sidecar, identity = command.artifacts, command.artifacts.identity
        source = LocalAsrRepository.source_snapshot(conn, execution["meeting_id"], execution["version"])
        if (sidecar.input_sha256 != plan.input_sha256 or command.result.input_sha256 != plan.input_sha256
                or sidecar.audio_ref != f"meeting:{execution['meeting_id']}"
                or (plan.source_text_sha256 is not None and sidecar.selected_text_sha256 != plan.source_text_sha256)
                or [item.model_dump(mode="json") for item in sidecar.source_segments] != source):
            raise LocalExecutionError("local_asr_applied_input_mismatch")
        if (identity.profile, identity.profile_digest, identity.code_digest, identity.precision) != (
            plan.profile, plan.profile_digest, plan.code_digest, plan.precision
        ):
            raise LocalExecutionError("local_asr_applied_profile_mismatch")
        if (identity.asr_device, identity.diarization_device, identity.physical_gpu_id) != (
            plan.asr_device, plan.diarization_device, plan.physical_gpu_id
        ):
            raise LocalExecutionError("local_asr_applied_device_mismatch")
        if model_manifest_digest(identity.models) != plan.model_digest:
            raise LocalExecutionError("local_asr_applied_model_mismatch")

    def request_cancel(self, job_id: str, owner_scope: str = "local-owner") -> dict:
        """No meeting mutation and no inference/payment cancellation claim."""
        with self.db.transaction() as conn:
            row = self._row(conn, job_id, owner_scope)
            if row["state"] in {"applied", "cancelled", "timed_out", "failed"}:
                return self._view(conn, job_id, owner_scope)
            running = row["state"] in {"running", "artifact_stored", "cancel_requested"}
            state = "cancel_requested" if running else "cancelled"
            conn.execute("UPDATE local_asr_executions SET state=?,reason='cancel_requested' WHERE job_id=?", (state, job_id))
            conn.execute("UPDATE jobs SET cancel_requested=1,status=?,updated_at=? WHERE id=?",
                         ("running" if running else "cancelled", now(), job_id))
            return self._view(conn, job_id, owner_scope)

    def finish_after_cleanup(self, claim: ExecutionClaim, *, cleanup_complete: bool, reason: str):
        """Called by owned runtime only after its process-tree cleanup returns."""
        if cleanup_complete is not True:
            raise LocalExecutionError("local_asr_cleanup_unconfirmed")
        if reason not in {"cancelled", "timed_out", "failed"}:
            raise LocalExecutionError("local_asr_terminal_reason_invalid")
        with self.db.transaction() as conn:
            row, _ = self._owned(conn, claim, allow_cancel=True, allow_expired=True)
            if row["state"] in {"applied", "cancelled", "timed_out", "failed"}:
                raise LocalExecutionError("local_asr_already_terminal")
            conn.execute("UPDATE local_asr_executions SET state=?,reason=?,lease_until=NULL WHERE job_id=?", (reason, reason, claim.job_id))
            conn.execute("UPDATE local_asr_attempts SET state=?,ended_at=?,reason=? WHERE attempt_id=?", (reason, self.clock(), reason, claim.attempt_id))
            conn.execute("UPDATE jobs SET status=?,error=?,updated_at=? WHERE id=?", ("cancelled" if reason == "cancelled" else "failed", reason, now(), claim.job_id))

    def block_before_start(self, job_id: str, reason: str, *, waiting_resources=False,
                           owner_scope: str = "local-owner") -> dict:
        """A queue claim is not a native attempt; no model process was started."""
        with self.db.transaction() as conn:
            row = self._row(conn, job_id, owner_scope)
            if row["job_status"] != "running" or row["state"] not in {"queued", "blocked"}:
                raise LocalExecutionError("local_asr_cannot_block_owned_execution")
            if row['owner_pid'] is not None and not self.exited(ProcessIdentity(row['owner_pid'], row['owner_started'])):
                raise LocalExecutionError('local_asr_previous_owner_not_exited')
            if row['attempt_id'] is not None:
                previous = conn.execute('SELECT state,ended_at FROM local_asr_attempts WHERE attempt_id=?',
                                        (row['attempt_id'],)).fetchone()
                if not previous or previous['ended_at'] is None or previous['state'] not in {'interrupted','waiting_resources'}:
                    raise LocalExecutionError('local_asr_previous_cleanup_unverified')
            state = "cancelled" if row["cancel_requested"] else ("waiting_resources" if waiting_resources else "blocked")
            conn.execute("UPDATE local_asr_executions SET state=?,reason=? WHERE job_id=?", (state, reason, job_id))
            conn.execute("UPDATE jobs SET status=?,error=?,attempts=MAX(0,attempts-1),updated_at=? WHERE id=?",
                         ("cancelled" if state == "cancelled" else "waiting_config", reason, now(), job_id))
            return self._view(conn, job_id, owner_scope)

    def retry_after_native_cleanup(self, claim: ExecutionClaim, receipt) -> dict:
        """Bounded local restart requires real native proof and old owner exit."""
        from secretary.infrastructure.local_asr_process import StageProcessReceipt, StageProcessStopped
        if not isinstance(receipt, (StageProcessReceipt, StageProcessStopped)) or receipt.cleanup_confirmed is not True:
            raise LocalExecutionError("local_asr_cleanup_unconfirmed")
        if not self.exited(claim.process):
            raise LocalExecutionError("local_asr_previous_owner_not_exited")
        with self.db.transaction() as conn:
            row, plan = self._owned(conn, claim, allow_cancel=True, allow_expired=True)
            if row["state"] not in {"running", "artifact_stored", "cancel_requested"}:
                raise LocalExecutionError("local_asr_not_recoverable")
            if row["cancel_requested"]:
                state, status, reason = "cancelled", "cancelled", "cancelled_after_owner_exit"
            elif self.clock() >= plan.deadline_at:
                state, status, reason = "timed_out", "failed", "local_asr_deadline_expired"
            elif row["attempt_count"] >= plan.max_attempts:
                state, status, reason = "failed", "failed", "local_asr_attempt_limit"
            else:
                state, status, reason = "queued", "queued", "local_asr_owner_exit_verified"
            conn.execute("UPDATE local_asr_attempts SET state='interrupted',ended_at=?,reason=? WHERE attempt_id=?",
                         (self.clock(), reason, claim.attempt_id))
            conn.execute("UPDATE local_asr_executions SET state=?,reason=?,lease_until=NULL,stage=NULL,progress=0 WHERE job_id=?",
                         (state, reason, claim.job_id))
            conn.execute("UPDATE jobs SET status=?,error=?,updated_at=? WHERE id=?", (status, reason, now(), claim.job_id))
            return self._view(conn, claim.job_id, claim.owner_scope)

    def wait_after_native_cleanup(self, claim: ExecutionClaim, receipt, reason: str) -> dict:
        """Resource exhaustion pauses owned work after exhaustive native exit."""
        from secretary.infrastructure.local_asr_process import StageProcessReceipt, StageProcessStopped
        if not isinstance(receipt, (StageProcessReceipt, StageProcessStopped)) or receipt.cleanup_confirmed is not True:
            raise LocalExecutionError("local_asr_cleanup_unconfirmed")
        if not self.exited(claim.process):
            raise LocalExecutionError("local_asr_previous_owner_not_exited")
        with self.db.transaction() as conn:
            row, _ = self._owned(conn, claim, allow_cancel=True, allow_expired=True)
            if row["state"] not in {"running", "artifact_stored"} or row["cancel_requested"]:
                raise LocalExecutionError("local_asr_not_waitable")
            conn.execute("UPDATE local_asr_attempts SET state='waiting_resources',ended_at=?,reason=? WHERE attempt_id=?",
                         (self.clock(), reason, claim.attempt_id))
            conn.execute("UPDATE local_asr_executions SET state='waiting_resources',reason=?,lease_until=NULL,stage=NULL,progress=0 WHERE job_id=?",
                         (reason, claim.job_id))
            conn.execute("UPDATE jobs SET status='waiting_config',error=?,updated_at=? WHERE id=?", (reason, now(), claim.job_id))
            return self._view(conn, claim.job_id, claim.owner_scope)

    def recover_expired(self, job_id: str, owner_scope: str = "local-owner") -> dict:
        """Lease expiry alone never grants another model process ownership."""
        with self.db.transaction() as conn:
            row = self._row(conn, job_id, owner_scope)
            plan = ExecutionPlan.model_validate_json(row["plan"])
            if row["state"] not in {"running", "artifact_stored", "cancel_requested"} or row["lease_until"] is None or self.clock() < row["lease_until"]:
                raise LocalExecutionError("local_asr_not_recoverable")
            if not self.exited(ProcessIdentity(row["owner_pid"], row["owner_started"])):
                raise LocalExecutionError("local_asr_previous_owner_not_exited")
            # The qualified adapter must provide an owner identity whose exit
            # proves contained descendants exited (e.g. Windows Job Object).
            if not row["qualification"] or not json.loads(row["qualification"])["process_containment_verified"]:
                raise LocalExecutionError("local_asr_containment_unverified")
            if row["cancel_requested"]:
                state, status, reason = "cancelled", "cancelled", "cancelled_after_owner_exit"
            elif self.clock() >= plan.deadline_at:
                state, status, reason = "timed_out", "failed", "local_asr_deadline_expired"
            elif row["attempt_count"] >= plan.max_attempts:
                state, status, reason = "failed", "failed", "local_asr_attempt_limit"
            else:
                state, status, reason = "queued", "queued", "local_asr_owner_exit_verified"
            conn.execute("UPDATE local_asr_attempts SET state='interrupted',ended_at=?,reason=? WHERE attempt_id=?", (self.clock(), reason, row["attempt_id"]))
            conn.execute("UPDATE local_asr_executions SET state=?,reason=?,lease_until=NULL,stage=NULL,progress=0 WHERE job_id=?", (state, reason, job_id))
            conn.execute("UPDATE jobs SET status=?,error=?,updated_at=? WHERE id=?", (status, reason, now(), job_id))
            return self._view(conn, job_id, owner_scope)

    def recover_unowned_queue(self, job_id: str, owner_scope: str = 'local-owner') -> dict:
        """Exclusive-startup-only repair before any worker can enter preflight."""
        with self.db.transaction() as conn:
            row=self._row(conn,job_id,owner_scope)
            if not (row['state']=='queued' and row['job_status']=='running' and row['owner_pid'] is None
                    and row['attempt_id'] is None and row['lease_until'] is None and row['attempt_count']==0):
                raise LocalExecutionError('local_asr_not_unowned_claim')
            plan=ExecutionPlan.model_validate_json(row['plan'])
            if row['cancel_requested']:state,status,reason='cancelled','cancelled','cancel_requested'
            elif self.clock()>=plan.deadline_at:state,status,reason='timed_out','failed','local_asr_deadline_expired'
            else:state,status,reason='queued','queued','local_asr_unowned_claim_recovered'
            conn.execute('UPDATE local_asr_executions SET state=?,reason=? WHERE job_id=?',(state,reason,job_id))
            conn.execute('UPDATE jobs SET status=?,error=?,attempts=MAX(0,attempts-1),updated_at=? WHERE id=?',
                         (status,reason,now(),job_id))
            return self._view(conn,job_id,owner_scope)
