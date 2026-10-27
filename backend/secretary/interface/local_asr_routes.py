"""Local, additive ASR evidence API. This router cannot call model runtimes.

The app's localhost/Origin/CSRF middleware is authoritative. The owner scope and
future qualified profile factory come from the server, never request JSON.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from secretary.application.local_asr_lifecycle import ExecutionPlan, LocalAsrLifecycle, LocalExecutionError
from secretary.application.local_asr_media import LocalAsrMediaError, verified_meeting_input
from secretary.domain.local_asr import (
    ImportLocalAsrRun, LocalAsrConflict, LocalAsrError, LocalAsrNotFound,
    ReviewLocalAsrCluster, ReviewLocalAsrSegment, text_fingerprint,
)
from secretary.domain.speakers import DTO, Identifier
from secretary.infrastructure.local_asr_repository import LocalAsrRepository


class PrepareLocalAsrJob(DTO):
    operation_id: Identifier
    transcript_version: int = Field(strict=True, ge=1)


def create_local_asr_router(db, settings, *, plan_factory=None, queue_prepared=None):
    repository = LocalAsrRepository(db)
    lifecycle = LocalAsrLifecycle(db)
    router = APIRouter(prefix="/api/v1/meetings/{meeting_id}/local-asr", tags=["local-asr"])

    def invoke(action, *args, **kwargs):
        try:
            return action(*args, **kwargs)
        except (LocalAsrNotFound, KeyError):
            raise HTTPException(404, "Analysis or meeting not found") from None
        except (LocalAsrConflict, LocalExecutionError) as exc:
            raise HTTPException(409, str(exc)) from None
        except (LocalAsrError, LocalAsrMediaError) as exc:
            raise HTTPException(422, str(exc)) from None

    @router.get("/runs")
    def runs(meeting_id: str):
        return invoke(repository.list_runs, meeting_id)

    @router.post("/runs", status_code=201)
    async def import_run(meeting_id: str, command: ImportLocalAsrRun):
        # Hashing is local I/O in a worker thread, not a blocking event-loop task.
        verified = await run_in_threadpool(invoke, verified_meeting_input, db, settings.data_dir,
                                          meeting_id, max_bytes=settings.max_upload_bytes)
        return await run_in_threadpool(invoke, repository.import_run, meeting_id, command,
                                       trusted_input_sha256=verified)

    @router.get("/runs/{run_id}")
    def read_run(meeting_id: str, run_id: str):
        return invoke(repository.get_run, meeting_id, run_id)

    @router.get("/runs/{run_id}/export")
    def export_run(meeting_id: str, run_id: str):
        result = invoke(repository.export_run, meeting_id, run_id)
        # A fixed filename avoids reflecting path/header input. The JSON carries
        # the immutable run ID and independent reviews, never a service token.
        return JSONResponse(result, headers={"Content-Disposition": 'attachment; filename="local-asr-analysis.json"'})

    @router.get("/runs/{run_id}/clusters/{speaker_label}/preview")
    def preview(meeting_id: str, run_id: str, speaker_label: str):
        return invoke(repository.cluster_preview, meeting_id, run_id, speaker_label)

    @router.post("/runs/{run_id}/cluster-reviews")
    def review_cluster(meeting_id: str, run_id: str, command: ReviewLocalAsrCluster):
        return invoke(repository.review_cluster, meeting_id, run_id, command)

    @router.post("/runs/{run_id}/segment-reviews")
    def review_segment(meeting_id: str, run_id: str, command: ReviewLocalAsrSegment):
        return invoke(repository.review_segment, meeting_id, run_id, command)

    @router.post("/jobs", status_code=202)
    async def prepare(meeting_id: str, command: PrepareLocalAsrJob):
        invoke(db.meeting, meeting_id)
        prior = db.one("""SELECT e.job_id,j.meeting_id,j.version FROM local_asr_executions e
            JOIN jobs j ON j.id=e.job_id WHERE e.request_id=? AND e.owner_scope=? AND e.application='secretary'""",
            (command.operation_id, repository.owner_scope))
        if prior:
            if prior["meeting_id"] != meeting_id or prior["version"] != command.transcript_version:
                raise HTTPException(409, "local_asr_idempotency_conflict")
            return invoke(lifecycle.view, prior["job_id"], repository.owner_scope)
        if plan_factory is None:
            raise HTTPException(503, "local_asr_profile_bundle_unavailable")
        verified = await run_in_threadpool(invoke, verified_meeting_input, db, settings.data_dir,
                                          meeting_id, max_bytes=settings.max_upload_bytes)
        with db.connection() as connection:
            source = repository.source_snapshot(connection, meeting_id, command.transcript_version)
        if not source and queue_prepared is None:
            raise HTTPException(409, "local_asr_saved_transcript_required")
        if not source:
            from secretary.infrastructure.local_asr_publication import assert_initial_transcript_available
            with db.connection() as connection:
                invoke(assert_initial_transcript_available, connection, meeting_id, command.transcript_version)
        text_hash = text_fingerprint("\n".join(item["text"] for item in source)) if source else None
        # A private code seam, intentionally absent from settings and request DTO.
        plan = await run_in_threadpool(invoke, plan_factory, meeting_id, command.transcript_version, verified, text_hash)
        if not isinstance(plan, ExecutionPlan):
            raise HTTPException(503, "local_asr_profile_bundle_unavailable")
        if (plan.application != "secretary" or plan.owner_scope != repository.owner_scope
                or plan.provider != "local" or plan.input_sha256 != verified
                or plan.source_text_sha256 != text_hash):
            raise HTTPException(409, "local_asr_profile_scope_mismatch")
        prepared = invoke(lifecycle.prepare, meeting_id, command.transcript_version, plan, command.operation_id)
        if queue_prepared is not None:
            return await run_in_threadpool(invoke, queue_prepared, prepared["job_id"])
        return prepared

    def scoped_job(meeting_id, job_id):
        result = invoke(lifecycle.view, job_id, repository.owner_scope)
        if result["meeting_id"] != meeting_id:
            raise HTTPException(404, "Analysis job not found")
        return result

    @router.get("/jobs/{job_id}")
    def job(meeting_id: str, job_id: str):
        return scoped_job(meeting_id, job_id)

    @router.post("/jobs/{job_id}/cancel")
    def cancel(meeting_id: str, job_id: str):
        scoped_job(meeting_id, job_id)
        return invoke(lifecycle.request_cancel, job_id, repository.owner_scope)

    @router.post("/jobs/{job_id}/resume", status_code=202)
    async def resume(meeting_id: str, job_id: str):
        current = scoped_job(meeting_id, job_id)
        if queue_prepared is None:
            raise HTTPException(503, "local_asr_profile_bundle_unavailable")
        if current["state"] not in {"blocked", "waiting_resources"}:
            raise HTTPException(409, "local_asr_not_activatable")
        return await run_in_threadpool(invoke, queue_prepared, job_id)

    return router
