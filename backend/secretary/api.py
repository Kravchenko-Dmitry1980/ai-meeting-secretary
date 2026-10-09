from __future__ import annotations

import asyncio
import json
import inspect
import hashlib
import os
import secrets
import tempfile
import wave
import ipaddress
import re
from decimal import Decimal
from uuid import UUID
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator, StrictBool
from secretary.domain.identification import IdentifySpeakers, BypassIdentification, IdentificationState

from secretary.application.exporting import export_meeting
from secretary.application.worker import Worker
from secretary.application.speakers import SpeakerService
from secretary.infrastructure.speaker_repository import SpeakerRepository
from secretary.infrastructure.assignment_repository import AssignmentRepository
from secretary.domain.summary_context import AssignmentSnapshot, AssignmentReviewResult, ReviewTaskAssignments
from secretary.domain.assignment_evidence import AssignmentEvidenceError
from secretary.domain.speakers import (
    AddParticipant, AttributionSnapshot, CreateProfile, ParticipantRosterSnapshot,
    PatchParticipant, PatchProfile, PersonProfile, ReviewAttribution, SpeakerError, DTO,
)
from secretary.infrastructure.database import Database, uid
from secretary.settings import Settings, EDITABLE_CONFIG
from secretary.application.enrollments import EnrollmentService
from secretary.domain.enrollment import (Enrollment, EnrollmentFailure, EnrollCommand, ConfirmEnrollment,
    DeleteEnrollment, StartEnrollmentRecording, StopEnrollmentRecording, EnrollmentRecording)
from secretary.infrastructure.voice_resources import CaptureLease, InferenceCoordinator, LeasedCapture
from secretary.domain.voice import MAX_PAYLOAD, VoiceError
from secretary.domain.models import Meeting, AudioChunk, TranscriptSegment, Speaker, Summary, ActionItem, ProcessingJob, UsageRecord
from secretary.domain.cloud_budget import BudgetError, to_micro
from secretary.domain.task_delivery import (DeliveryReceipt, PublicationContext, PublicationPreviewResult,
    PreviewPublications, PublishCommand)
from secretary.domain.team import TeamConflict, TeamForbidden
from secretary.application.publication_evidence import PublicationEvidenceError


class BodyTooLarge(HTTPException):
    def __init__(self):
        super().__init__(413, "Request body exceeds configured limit")


class BodyLimitMiddleware:
    """Bound multipart before parser spooling, including chunked requests."""
    def __init__(self, app, max_upload_bytes):
        self.app, self.max_upload_bytes = app, max_upload_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        enrollment_upload = bool(re.fullmatch(r'/api/v1/participants/[^/]+/enrollments', scope['path']))
        limit = MAX_PAYLOAD + 65536 if enrollment_upload else (self.max_upload_bytes + 65536 if scope["path"].endswith("/upload") else 1024 * 1024)
        headers = dict(scope.get("headers", []))
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            return await JSONResponse({"detail": "Invalid Content-Length"}, 400)(scope, receive, send)
        if length < 0:
            return await JSONResponse({"detail": "Invalid Content-Length"}, 400)(scope, receive, send)
        if length > limit:
            return await JSONResponse({"detail": "Request body exceeds configured limit"}, 413)(scope, receive, send)
        received = 0
        async def bounded_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise BodyTooLarge()
            return message
        try:
            await self.app(scope, bounded_receive, send)
        except BodyTooLarge:
            await JSONResponse({"detail": "Request body exceeds configured limit"}, 413)(scope, receive, send)


class SegmentsPage(BaseModel):
    items: list[TranscriptSegment]
    total: int
    transcript_version: int


class JobAccepted(BaseModel):
    job_id: str


class StatusResponse(BaseModel):
    status: str
    error: str | None = None


class ErrorResponse(BaseModel):
    detail: str


class UsageResponse(BaseModel):
    records: list[UsageRecord]
    estimated_rub: float
    confirmed_rub: float
    unknown_count: int


class ConfigurationResponse(BaseModel):
    key_configured: bool
    stt_model: str
    summary_model: str
    chunk_seconds: int
    request_timeout_seconds: int
    meeting_budget_rub: float
    local_cost_limits_enabled: bool
    allow_unknown_price: bool
    unknown_request_reservation_rub: float | None
    cloud_enabled: bool
    stt_price_rub_per_minute: float | None
    summary_input_rub_per_million: float | None
    summary_output_rub_per_million: float | None
    summary_max_output_tokens: int
    summary_batch_chars: int


class CreateMeeting(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(default="Новая встреча", min_length=1, max_length=250)
    processing_mode: Literal['ordinary','voice_identification'] = 'ordinary'
    cloud_budget_scope_id: UUID | None = None


class CreateBudgetScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: UUID
    cap_rub: Decimal = Field(gt=0, le=3000, allow_inf_nan=False)


class ProcessRequest(DTO):
    model_config = ConfigDict(extra="forbid")
    stage: Literal["prepare", "transcribe", "identify_speakers", "summarize"] = "transcribe"
    retry: StrictBool = False
    new_transcript_version: StrictBool = False
    regenerate_summary: StrictBool = Field(default=False,description='Explicit new paid summary using latest frozen context; summarize-only, cannot combine retry')
    transcript_version: int | None = Field(default=None,ge=0,strict=True)
    expected_revision: int | None = Field(default=None,ge=0,strict=True)
    operation_id: str | None = Field(default=None,min_length=1,max_length=128)

    @model_validator(mode='after')
    def stage_fields(self):
        if self.regenerate_summary and (self.stage != 'summarize' or self.retry):
            raise ValueError('regenerate_summary is summarize-only and cannot combine retry')
        local = {'transcript_version','expected_revision','operation_id'}
        if self.stage=='identify_speakers':
            if any(getattr(self,k) is None for k in local) or self.new_transcript_version:
                raise ValueError('Identification requires transcript_version, expected_revision, operation_id and no new_transcript_version')
        elif self.model_fields_set & local:
            raise ValueError('Identification fields are local-only')
        if self.new_transcript_version and self.stage!='transcribe':
            raise ValueError('new_transcript_version is transcribe-only; it may repeat paid STT')
        return self


class RecordingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    microphone_id: str | None = None
    system_id: str | None = None
    auto_process: bool = True


def local_url(value):
    try:
        parsed = urlsplit(value)
        _ = parsed.port
        return parsed.scheme in {"http", "https"} and parsed.hostname in {"127.0.0.1", "localhost", "::1"} and not parsed.username and not parsed.password and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment
    except ValueError:
        return False


def create_app(settings: Settings | None = None, *, provider_factory=None, capture=None, run_worker=True,
               enrollment_capture=None, voice_store=None, enrollment_decoder=None, voice_engine=None, voice_model=None,
               task_publications_factory=None, team_auth_factory=None, publication_worker_factory=None,
               database_path=None, billing_path=None, maintenance=None, participant_id=None,
               outbound_enabled=True, enforce_single_instance=True) -> FastAPI:
    settings = settings or Settings()
    from secretary.infrastructure.maintenance_binding import restore_guard_present
    from secretary.team_settings import RuntimeConfigurationError
    secretary_path = database_path or settings.data_dir / "secretary.sqlite3"
    effective_billing_path = billing_path if billing_path is not None else settings.data_dir / 'billing.sqlite3'
    if restore_guard_present((secretary_path, effective_billing_path)):
        raise RuntimeConfigurationError('team_restore_reconciliation_required')
    instance_lock = None
    if enforce_single_instance:
        from secretary.infrastructure.instance_lock import DataDirectoryLock, DataDirectoryLockError
        try:
            instance_lock = DataDirectoryLock(settings.data_dir)
        except DataDirectoryLockError as exc:
            raise RuntimeConfigurationError(exc.code) from None
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    project_tmp = settings.data_dir / "tmp"
    project_tmp.mkdir(exist_ok=True)
    tempfile.tempdir = str(project_tmp)  # only Secretary's own process; no Windows setting
    db = Database(secretary_path, maintenance=maintenance, participant_id=participant_id)
    speaker_service = SpeakerService(SpeakerRepository(db))
    stored = db.configuration()
    settings = Settings(**{**settings.model_dump(), **stored}, _env_file=None)
    if not outbound_enabled:
        settings = settings.model_copy(update={'cloud_enabled': False})
    csrf_token = secrets.token_urlsafe(32)
    if provider_factory is None:
        from secretary.infrastructure.polza import build_polza_client
        provider_factory = lambda configuration: build_polza_client(configuration.model_copy(update={'cloud_enabled': False}) if not outbound_enabled else configuration, billing_path=billing_path,
            maintenance=maintenance, participant_id=participant_id)
    from secretary.infrastructure.cloud_budget_factory import build_monthly_budget
    cloud_budget = build_monthly_budget(settings, billing_path=billing_path, maintenance=maintenance, participant_id=participant_id)
    from secretary.infrastructure.audio import AudioCapture
    legacy_capture = capture is not None and not isinstance(capture, AudioCapture)
    if capture is None:
        capture = AudioCapture(settings)
    capture_lease, inference = CaptureLease(), InferenceCoordinator()
    capture = LeasedCapture(capture, capture_lease, 'meeting', legacy=legacy_capture,
        maintenance=maintenance, participant_id=participant_id)
    legacy_enrollment = enrollment_capture is not None and not isinstance(enrollment_capture, AudioCapture)
    if enrollment_capture is None:
        # Factory is called at explicit start only; native module remains lazy.
        enrollment_capture = AudioCapture(settings, folder_factory=lambda identifier: enrollment_service.staging(identifier, create=True),
                                           max_seconds=30, max_channels=2, chunk_seconds=30)
    enrollment_capture = LeasedCapture(enrollment_capture, capture_lease, 'enrollment', legacy=legacy_enrollment,
        maintenance=maintenance, participant_id=participant_id)
    enrollment_service = EnrollmentService(settings, db, enrollment_capture, inference,
        store=voice_store, decoder=enrollment_decoder, engine=voice_engine, model=voice_model)
    recovery = getattr(capture, "recover", None)
    def recover_storage():
        db.recover()
        if recovery:
            for saved_meeting in db.list_meetings():
                if (settings.data_dir / "audio" / saved_meeting["id"] / "capture.json").exists():
                    try:
                        recovery(saved_meeting["id"], on_chunk=lambda chunk, mid=saved_meeting["id"]: db.add_chunk(mid, chunk))
                    except Exception:
                        db.update_meeting(saved_meeting["id"], status="partial_error", capture_error="Capture recovery needs inspection; original files retained")
    worker = Worker(db, settings, provider_factory, recovery=False, enrollments=enrollment_service, coordinator=inference)

    @asynccontextmanager
    async def lifespan(app):
        publication_worker, publication_task = None, None
        try:
            await asyncio.to_thread(enrollment_service.recover)
            if run_worker:
                await asyncio.to_thread(recover_storage)
                await worker.start()
            publication_worker = publication_worker_factory(app.state.task_publications) if publication_worker_factory and outbound_enabled else None
            publication_task = asyncio.create_task(publication_worker.run(), name='secretary-publications') if publication_worker else None
            app.state.publication_worker, app.state.publication_task = publication_worker, publication_task
            yield
        finally:
            # All close paths execute even if one driver fails; no lease release on timeout.
            try:
                await asyncio.to_thread(capture.close)
            finally:
                try:
                    await asyncio.to_thread(enrollment_capture.close)
                finally:
                    try:
                        await asyncio.to_thread(enrollment_service.close)
                    finally:
                        try:
                            await worker.close()
                        finally:
                            try:
                                if publication_worker is not None:
                                    publication_worker.stop()
                                    await asyncio.shield(publication_task)
                            finally:
                                if instance_lock is not None:
                                    instance_lock.release()

    app = FastAPI(title="Secretary", version="0.1.0", lifespan=lifespan)
    app.state.instance_lock = instance_lock
    app.add_middleware(BodyLimitMiddleware, max_upload_bytes=settings.max_upload_bytes)
    if maintenance is not None:
        from secretary.interface.maintenance_middleware import MaintenanceMiddleware
        app.add_middleware(MaintenanceMiddleware, maintenance=maintenance, participant_id=participant_id)
    app.state.db, app.state.worker, app.state.settings, app.state.capture = db, worker, settings, capture
    app.state.speakers = speaker_service
    assignment_service = AssignmentRepository(db)
    app.state.assignments = assignment_service
    app.state.task_publications = task_publications_factory(db) if task_publications_factory else None
    from secretary.interface.local_team_routes import create_local_team_router
    team_auth = team_auth_factory(db) if team_auth_factory else None
    app.include_router(create_local_team_router(
        team_auth[0] if team_auth is not None else None,
        actor_id=team_auth[1] if team_auth is not None else ''))
    app.state.cloud_budget = cloud_budget
    app.state.maintenance, app.state.maintenance_participant = maintenance, participant_id
    app.state.publication_worker, app.state.publication_task = None, None
    from secretary.infrastructure.polza_history import PolzaHistoryClient
    app.state.cloud_budget_history = PolzaHistoryClient(settings)
    app.state.enrollments, app.state.voice_inference, app.state.capture_lease = enrollment_service, inference, capture_lease
    app.state.enrollment_capture = enrollment_capture
    audio_locks: dict[str, asyncio.Lock] = {}

    @app.middleware("http")
    async def local_security(request, call_next):
        host = request.headers.get("host", "")
        if not local_url("http://" + host):
            return JSONResponse({"detail": "Localhost Host required"}, 403)
        origin = request.headers.get("origin")
        if origin:
            if not local_url(origin):
                return JSONResponse({"detail": "Trusted localhost Origin required"}, 403)
            origin_url, host_url = urlsplit(origin), urlsplit("http://" + host)
            origin_port = origin_url.port or (443 if origin_url.scheme == "https" else 80)
            host_port = host_url.port or 80
            if origin_url.scheme != request.url.scheme or origin_port not in {host_port, 5173}:
                return JSONResponse({"detail": "Origin must match Secretary or the trusted Vite development port"}, 403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if not secrets.compare_digest(request.headers.get("x-secretary-token", ""), csrf_token):
                return JSONResponse({"detail": "X-Secretary-Token required; obtain /api/v1/session"}, 403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        if origin:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Access-Control-Allow-Methods"] = "GET,POST,PATCH,DELETE,OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type,X-Secretary-Token"
        return response

    @app.options("/{path:path}")
    async def preflight(path: str):
        return Response(status_code=204)

    @app.exception_handler(EnrollmentFailure)
    async def enrollment_failure(request, exc):
        return JSONResponse({'detail': exc.reason, 'reason_codes': [exc.reason]}, status_code=exc.status)

    @app.exception_handler(VoiceError)
    async def private_voice_failure(request, exc):
        return JSONResponse({'detail': exc.reason, 'reason_codes': [exc.reason]}, status_code=409)

    @app.exception_handler(BudgetError)
    async def budget_failure(request, exc):
        status = 404 if exc.code == "unknown_budget_scope" else 409
        return JSONResponse({"detail": exc.code, "reason_codes": [exc.code]}, status_code=status)

    from secretary.infrastructure.team_maintenance import MaintenanceBlocked

    @app.exception_handler(MaintenanceBlocked)
    async def maintenance_failure(request, exc):
        return JSONResponse({'detail': 'maintenance_blocked'}, status_code=503,
            headers={'Cache-Control': 'no-store', 'Retry-After': '5'})

    @app.get("/api/v1/cloud-budget")
    async def monthly_budget_snapshot():
        return cloud_budget.snapshot().public()

    @app.post("/api/v1/cloud-budget/refresh")
    async def refresh_monthly_budget():
        # An explicit owner action also authorizes switching the active key identity.
        usage = await cloud_budget.account_reader.read_key_usage()
        cloud_budget.refresh_account(usage)
        return cloud_budget.snapshot().public()

    @app.post("/api/v1/cloud-budget/scopes", status_code=201)
    async def create_budget_scope(body: CreateBudgetScope):
        return cloud_budget.create_scope(str(body.operation_id), to_micro(body.cap_rub))

    @app.get("/api/v1/cloud-budget/scopes/{scope_id}")
    async def budget_scope_snapshot(scope_id: UUID):
        return cloud_budget.scope_snapshot(str(scope_id))

    @app.post("/api/v1/cloud-budget/operations/{operation_id}/reconcile")
    async def reconcile_cloud_operation(operation_id: UUID):
        reservation = cloud_budget.reservation(str(operation_id))
        if reservation.status in {"confirmed", "released"}:
            return {"operation_id": str(operation_id), "status": reservation.status,
                    "budget": cloud_budget.snapshot().public()}
        history = app.state.cloud_budget_history
        if history.key_tag != reservation.key_tag:
            raise BudgetError("monthly_budget_key_changed")
        identifier = reservation.provider_request_id or reservation.provider_job_id
        if not identifier:
            raise BudgetError("monthly_budget_receipt_identity_unavailable")
        receipt = await history.read_receipt(identifier)
        if receipt["provider_request_id"] != identifier:
            raise BudgetError("budget_receipt_identity_conflict")
        if receipt["status"] in {"completed", "failed"}:
            cloud_budget.settle(str(operation_id), receipt)
        return {"operation_id": str(operation_id), "status": cloud_budget.reservation(str(operation_id)).status,
                "budget": cloud_budget.snapshot().public()}

    @app.exception_handler(AssignmentEvidenceError)
    async def assignment_evidence_error(request, exc):
        reason = str(exc)
        return JSONResponse({'detail':reason,'reason_codes':[reason]},status_code=422)

    @app.get('/api/v1/meetings/{meeting_id}/task-assignments',response_model=AssignmentSnapshot)
    async def task_assignments(meeting_id: str, summary_version: int | None = Query(default=None,ge=0),
                               revision: int | None = Query(default=None,ge=0)):
        return assignment_service.get(meeting_id,summary_version,revision)

    @app.patch('/api/v1/meetings/{meeting_id}/task-assignments',response_model=AssignmentReviewResult)
    async def review_task_assignments(meeting_id: str, body: ReviewTaskAssignments):
        return assignment_service.review(meeting_id,body)

    @app.exception_handler(TeamConflict)
    async def team_conflict(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=409)

    @app.exception_handler(TeamForbidden)
    async def team_forbidden(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=403)

    @app.exception_handler(PublicationEvidenceError)
    async def publication_evidence_error(request, exc):
        return JSONResponse({'detail': exc.code, 'reason_codes': [exc.code]}, status_code=422)

    def publications():
        service = app.state.task_publications
        if service is None:
            raise HTTPException(503, 'team_publication_not_configured')
        return service

    publication_errors = {403: {'model': ErrorResponse}, 404: {'model': ErrorResponse},
                          409: {'model': ErrorResponse}, 503: {'model': ErrorResponse}}

    @app.get('/api/v1/meetings/{meeting_id}/task-publications/context', response_model=PublicationContext,
             responses=publication_errors)
    async def task_publication_context(meeting_id: str):
        return await asyncio.to_thread(publications().context, meeting_id)

    @app.post('/api/v1/meetings/{meeting_id}/task-publications/preview', response_model=PublicationPreviewResult,
              responses=publication_errors)
    async def preview_task_publications(meeting_id: str, body: PreviewPublications):
        return await asyncio.to_thread(publications().preview, meeting_id, body)

    @app.post('/api/v1/meetings/{meeting_id}/task-publications', response_model=DeliveryReceipt, status_code=202,
              responses=publication_errors)
    async def confirm_task_publications(meeting_id: str, body: PublishCommand):
        return await asyncio.to_thread(publications().confirm, meeting_id, body)

    @app.get('/api/v1/meetings/{meeting_id}/task-publications', response_model=list[DeliveryReceipt],
             responses=publication_errors)
    async def list_task_publications(meeting_id: str):
        return await asyncio.to_thread(publications().list, meeting_id)

    @app.get('/api/v1/meetings/{meeting_id}/task-publications/{operation_id}', response_model=DeliveryReceipt,
             responses=publication_errors)
    async def read_task_publication(meeting_id: str, operation_id: UUID):
        try:
            return await asyncio.to_thread(publications().read, meeting_id, str(operation_id))
        except TeamForbidden as exc:
            if str(exc) == 'publication_command_unavailable':
                raise HTTPException(404, 'publication_command_unavailable') from None
            raise

    @app.get('/api/v1/participants/{person_id}/enrollments', response_model=list[Enrollment])
    async def enrollments(person_id: str):
        return await asyncio.to_thread(enrollment_service.repository.list, person_id)

    @app.post('/api/v1/participants/{person_id}/enrollments', response_model=Enrollment, status_code=201)
    async def upload_enrollment(person_id: str, file: UploadFile = File(...),
                                consent_confirmed: str = Form(...), operation_id: str = Form(...)):
        try:
            if consent_confirmed not in ('true', 'false'):
                raise EnrollmentFailure('invalid_consent', 422)
            try:
                command = EnrollCommand(consent_confirmed=consent_confirmed == 'true', operation_id=operation_id)
            except ValidationError:
                raise EnrollmentFailure('invalid_enrollment_command', 422) from None
            if not command.consent_confirmed:
                raise EnrollmentFailure('consent_required', 422)
            payload = await file.read(MAX_PAYLOAD + 1)
            if len(payload) > MAX_PAYLOAD:
                raise EnrollmentFailure('upload_too_large', 413)
            return await asyncio.to_thread(enrollment_service.upload, person_id, payload, command)
        finally:
            await file.close()

    @app.post('/api/v1/participants/{person_id}/enrollment-recording/start', response_model=EnrollmentRecording)
    async def enrollment_recording_start(person_id: str, body: StartEnrollmentRecording):
        return await asyncio.to_thread(enrollment_service.start_recording, person_id, body)

    @app.post('/api/v1/participants/{person_id}/enrollment-recording/stop', response_model=EnrollmentRecording)
    async def enrollment_recording_stop(person_id: str, body: StopEnrollmentRecording):
        return await asyncio.to_thread(enrollment_service.stop_recording, person_id, body)

    @app.get('/api/v1/participants/{person_id}/enrollment-recording', response_model=EnrollmentRecording)
    async def enrollment_recording_state(person_id: str, recording_id: str | None = None):
        return await asyncio.to_thread(enrollment_service.recording_state, person_id, recording_id)

    @app.post('/api/v1/participants/{person_id}/enrollments/{enrollment_id}/confirm', response_model=Enrollment)
    async def confirm_enrollment(person_id: str, enrollment_id: str, body: ConfirmEnrollment):
        return await asyncio.to_thread(enrollment_service.confirm, person_id, enrollment_id, body)

    @app.delete('/api/v1/participants/{person_id}/enrollments/{enrollment_id}', response_model=Enrollment)
    async def delete_enrollment(person_id: str, enrollment_id: str, body: DeleteEnrollment):
        return await asyncio.to_thread(enrollment_service.delete, person_id, enrollment_id, body)

    @app.get('/api/v1/participants/{person_id}/enrollments/{enrollment_id}/audio-preview', response_class=Response)
    async def enrollment_preview(person_id: str, enrollment_id: str, request: Request, expected_revision: int = Query(ge=0)):
        try:
            local_peer = request.client and ipaddress.ip_address(request.client.host).is_loopback
        except ValueError:
            local_peer = False
        if not local_peer:
            raise EnrollmentFailure('loopback_peer_required', 403)
        content = await asyncio.to_thread(enrollment_service.preview, person_id, enrollment_id, expected_revision=expected_revision)
        headers = {'Accept-Ranges': 'bytes', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                   'ETag': f'"{enrollment_id}:{expected_revision}"'}
        value = request.headers.get('range')
        # If-Range only matches this strong metadata ETag; dates/weak/other values get full200.
        if value and request.headers.get('if-range', headers['ETag']) == headers['ETag']:
            matched = re.fullmatch(r'bytes=(\d*)-(\d*)', value)
            if not matched or not any(matched.groups()):
                raise EnrollmentFailure('invalid_range', 400)
            first, last = matched.groups()
            try:
                if first:
                    start, end = int(first), int(last) if last else len(content)-1
                    if last and end < start:
                        raise EnrollmentFailure('invalid_range', 400)
                else:
                    suffix = int(last)
                    start, end = max(0, len(content)-suffix), len(content)-1
                    if suffix == 0:
                        start = len(content)
            except ValueError:
                raise EnrollmentFailure('invalid_range', 400) from None
            if start >= len(content):
                return Response(status_code=416, headers={**headers, 'Content-Range': f'bytes */{len(content)}', 'Content-Length': '0'})
            end = min(end, len(content)-1)
            headers['Content-Range'] = f'bytes {start}-{end}/{len(content)}'
            content = content[start:end+1]
            return Response(content, status_code=206, media_type='audio/wav', headers=headers)
        return Response(content, media_type='audio/wav', headers=headers)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"detail": "Not found"}, status_code=404)

    @app.exception_handler(SpeakerError)
    async def speaker_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Reject malformed Unicode/nonfinite values without echoing raw input
        # or exception context, which may itself fail UTF-8 JSON serialization.
        errors = [{field: error[field] for field in ("loc", "msg", "type")} for error in exc.errors()]
        content = json.dumps({"detail": errors}, ensure_ascii=True, allow_nan=False)
        return Response(content=content, status_code=422, media_type="application/json")

    @app.get("/api/v1/participants", response_model=list[PersonProfile])
    async def participant_profiles():
        return speaker_service.list_profiles()

    @app.post("/api/v1/participants", status_code=201, response_model=PersonProfile)
    async def create_participant_profile(body: CreateProfile):
        return speaker_service.create_profile(body)

    @app.patch("/api/v1/participants/{profile_id}", response_model=PersonProfile)
    async def patch_participant_profile(profile_id: str, body: PatchProfile):
        result = speaker_service.patch_profile(profile_id, body)
        if body.enabled is False:
            inference.cancel()
        return result

    @app.get("/api/v1/meetings/{meeting_id}/participants", response_model=ParticipantRosterSnapshot)
    async def participant_roster(meeting_id: str):
        return speaker_service.get_roster(meeting_id)

    @app.post("/api/v1/meetings/{meeting_id}/participants", status_code=201, response_model=ParticipantRosterSnapshot)
    async def add_meeting_participant(meeting_id: str, body: AddParticipant):
        return speaker_service.add_participant(meeting_id, body)

    @app.patch("/api/v1/meetings/{meeting_id}/participants/{participant_id}", response_model=ParticipantRosterSnapshot)
    async def patch_meeting_participant(meeting_id: str, participant_id: str, body: PatchParticipant):
        return speaker_service.patch_participant(meeting_id, participant_id, body)

    @app.get("/api/v1/meetings/{meeting_id}/attribution", response_model=AttributionSnapshot)
    async def attribution(meeting_id: str, version: int | None = Query(default=None, ge=0)):
        return speaker_service.get_attribution(meeting_id, version)

    @app.patch("/api/v1/meetings/{meeting_id}/attribution", response_model=AttributionSnapshot)
    async def review_attribution(meeting_id: str, body: ReviewAttribution):
        return speaker_service.review_attribution(meeting_id, body)

    @app.get("/health")
    async def health():
        if maintenance is not None:
            processing = ('not_configured' if not run_worker else
                'healthy' if worker.task is not None and not worker.task.done() else 'degraded')
            publication = ('not_configured' if app.state.task_publications is None or not outbound_enabled else
                'healthy' if app.state.publication_task is not None and not app.state.publication_task.done() else 'degraded')
            return {'status': 'ok', 'components': {
                'processing': {'state': processing}, 'publication': {'state': publication},
                'capture': {'state': 'healthy', 'recording': capture_lease.owner is not None, 'device_qualified': False},
                'cloud': {'state': 'not_configured' if not settings.key_configured or not outbound_enabled else 'healthy', 'live_qualified': False},
                'maintenance': {'state': 'healthy' if maintenance.status()['mode'] == 'open' else 'degraded'},
            }}
        return {"status": "ok"}

    @app.get("/api/v1/session")
    async def session():
        return {"csrf_token": csrf_token}

    @app.get("/api/v1/meetings", response_model=list[Meeting])
    async def meetings():
        return db.list_meetings()

    @app.post("/api/v1/meetings", status_code=201, response_model=Meeting)
    async def create_meeting(body: CreateMeeting):
        if body.cloud_budget_scope_id:
            cloud_budget.scope_snapshot(str(body.cloud_budget_scope_id))
        meeting = db.create_meeting(body.title.strip() or "Новая встреча",body.processing_mode)
        if body.cloud_budget_scope_id:
            cloud_budget.bind_meeting_scope(meeting["id"], str(body.cloud_budget_scope_id))
        return meeting

    @app.get("/api/v1/meetings/{meeting_id}", response_model=Meeting)
    async def meeting(meeting_id: str):
        return db.meeting(meeting_id)

    @app.post("/api/v1/meetings/{meeting_id}/upload", status_code=202, response_model=JobAccepted)
    async def upload(meeting_id: str, file: UploadFile = File(...)):
        token = uid()
        try:
            db.reserve_upload(meeting_id, token)
        except ValueError as exc:
            await file.close()
            raise HTTPException(409, str(exc)) from None
        upload_dir = settings.data_dir / "uploads" / meeting_id
        upload_dir.mkdir(parents=True, exist_ok=True)
        temporary = upload_dir / f"{token}.partial"
        final = upload_dir / "original.media"
        size = 0
        try:
            with temporary.open("wb") as stream:
                while part := await file.read(1024 * 1024):
                    size += len(part)
                    if size > settings.max_upload_bytes:
                        raise HTTPException(413, "Recording exceeds configured upload size")
                    stream.write(part)
            if size == 0:
                raise HTTPException(422, "Empty recording")
            temporary.replace(final)
        except BaseException:
            temporary.unlink(missing_ok=True)
            db.update_meeting(meeting_id, media_path=None, status="new")
            raise
        finally:
            await file.close()
        db.update_meeting(meeting_id, media_path=str(final), status="preparing", error=None)
        version, _ = worker.routes.bind(meeting_id)
        db.authorize_pipeline(meeting_id,version)
        job = db.enqueue(meeting_id, "prepare",version=version, payload={"chunk_seconds": settings.chunk_seconds})
        return {"job_id": job["id"]}

    @app.get("/api/v1/meetings/{meeting_id}/chunks", response_model=list[AudioChunk])
    async def chunks(meeting_id: str):
        db.meeting(meeting_id)
        return db.chunks(meeting_id)

    @app.get("/api/v1/audio/devices")
    async def devices():
        return await asyncio.to_thread(capture.list_devices)

    @app.get("/api/v1/meetings/{meeting_id}/recording")
    async def recording_state(meeting_id: str):
        db.meeting(meeting_id)
        return capture.state(meeting_id)

    @app.post("/api/v1/meetings/{meeting_id}/recording/start")
    async def recording_start(meeting_id: str, body: RecordingRequest):
        meeting = db.meeting(meeting_id, internal=True)
        if meeting["media_path"] or db.chunks(meeting_id) or meeting["recording"]:
            raise HTTPException(409, "Для новой записи создайте новую встречу")
        def on_chunk(chunk):
            persisted = db.add_chunk(meeting_id, chunk)
            if body.auto_process:
                db.enqueue(meeting_id, "transcribe", chunk_id=persisted["id"], version=version, payload={"settings": frozen_route['settings']})
            db.update_meeting(meeting_id, duration_ms=max(c["offset_ms"] + c["duration_ms"] for c in db.chunks(meeting_id)))
        def on_error(error):
            db.update_meeting(meeting_id, recording=0, status="partial_error", capture_error=str(error)[:1500])
        def on_finish(snapshot):
            for chunk in snapshot.get("chunks", []):
                db.add_chunk(meeting_id, chunk)
            chunks = db.chunks(meeting_id)
            failed = snapshot.get("status") == "failed" or snapshot.get("error")
            duration = max((chunk["offset_ms"] + chunk["duration_ms"] for chunk in chunks), default=0)
            db.update_meeting(meeting_id, recording=0, status="partial_error" if failed else "recorded", duration_ms=duration, capture_error=snapshot.get("error") if failed else None)
            if body.auto_process and chunks:
                worker.enqueue_transcription(meeting_id)
        # Claim before DB mutation so a sample recording causes no phantom meeting job.
        capture.claim(meeting_id)
        try:
            version, frozen_route = worker.routes.bind(meeting_id)
            if body.auto_process:
                db.authorize_pipeline(meeting_id,version)
        except BaseException:
            capture._release(meeting_id, {'native_closed':True})
            raise
        db.update_meeting(meeting_id, recording=1, auto_process=int(body.auto_process), status="recording", error=None)
        try:
            kwargs = {"on_finish": on_finish} if "on_finish" in inspect.signature(capture.start).parameters else {}
            result = await asyncio.to_thread(capture.start, meeting_id, body.microphone_id, body.system_id, on_chunk, on_error, **kwargs)
            if result.get("error") or result.get("status") in {"failed", "error", "unavailable"}:
                db.update_meeting(meeting_id, recording=0, status="partial_error", error=result.get("error", "Audio capture failed"))
            return result
        except Exception as exc:
            db.update_meeting(meeting_id, recording=0, status="partial_error", error=str(exc))
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/v1/meetings/{meeting_id}/recording/stop")
    async def recording_stop(meeting_id: str):
        meeting = db.meeting(meeting_id, internal=True)
        result = await asyncio.to_thread(capture.stop, meeting_id)
        if recovery:
            await asyncio.to_thread(recovery, meeting_id, on_chunk=lambda chunk: db.add_chunk(meeting_id, chunk))
        failed = result.get("status") == "failed" or result.get("error")
        db.update_meeting(meeting_id, recording=0, status="partial_error" if failed else "recorded", capture_error=result.get("error") if failed else meeting.get("capture_error"))
        if meeting["auto_process"] and db.chunks(meeting_id):
            job = worker.enqueue_transcription(meeting_id)
            if job:
                result["job_id"] = job["id"]
            worker.maybe_summary(meeting_id, db.meeting(meeting_id)["transcript_version"])
        return result

    def sync_recordings():
        for item in db.rows("SELECT * FROM meetings WHERE recording=1"):
            actual = capture.state(item["id"])
            if actual.get("status") in {"stopped", "failed", "idle"} and not actual.get("recording"):
                if recovery:
                    recovery(item["id"], on_chunk=lambda chunk, mid=item["id"]: db.add_chunk(mid, chunk))
                failed = actual.get("status") == "failed" or actual.get("error")
                db.update_meeting(item["id"], recording=0, status="partial_error" if failed else "recorded", capture_error=actual.get("error") if failed else item.get("capture_error"))
                if item["auto_process"] and db.chunks(item["id"]):
                    worker.enqueue_transcription(item["id"])
    worker.sync_recordings = sync_recordings

    @app.post("/api/v1/meetings/{meeting_id}/process", status_code=202, response_model=JobAccepted)
    async def process(meeting_id: str, body: ProcessRequest):
        db.meeting(meeting_id)
        try:
            if body.stage == "prepare":
                job = worker.enqueue_prepare(meeting_id, retry=body.retry)
            elif body.stage == "summarize":
                job = worker.enqueue_summary(meeting_id, retry=body.retry,regenerate_summary=body.regenerate_summary)
            elif body.stage=='identify_speakers':
                result = worker.enqueue_identification(meeting_id,IdentifySpeakers(transcript_version=body.transcript_version,expected_revision=body.expected_revision,operation_id=body.operation_id,retry=body.retry))
                job = db.job(result.job_id,internal=True)
            elif body.stage=='transcribe':
                job = worker.enqueue_transcription(meeting_id, retry=body.retry, resume=True,new_transcript_version=body.new_transcript_version)
            else:
                raise ValueError('unknown_processing_stage')
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        if not job:
            raise HTTPException(409, "No processing work available")
        current = db.job(job["id"])
        if current["status"] == "uncertain":
            raise HTTPException(409, "Провайдер мог принять оплаченный запрос. Сверьте расход и статус перед повторной отправкой; автоматический повтор заблокирован")
        if current["status"] in {"failed", "cancelled"} and body.stage!='identify_speakers':
            raise HTTPException(409, "Этап завершился ошибкой или отменён; для явного повтора передайте retry=true")
        return {"job_id": job["id"]}

    @app.get('/api/v1/meetings/{meeting_id}/identification-runs/{run_id}',response_model=IdentificationState)
    async def identification_state(meeting_id: str, run_id: str):
        return worker.attribution.state(meeting_id,run_id)

    @app.post('/api/v1/meetings/{meeting_id}/identification-bypass',response_model=IdentificationState)
    async def identification_bypass(meeting_id: str, body: BypassIdentification):
        result = worker.attribution.bypass(meeting_id,body)
        worker.maybe_summary(meeting_id,body.transcript_version)
        return result

    @app.get("/api/v1/meetings/{meeting_id}/jobs", response_model=list[ProcessingJob])
    async def jobs(meeting_id: str):
        db.meeting(meeting_id)
        return db.jobs(meeting_id)

    @app.post("/api/v1/jobs/{job_id}/cancel", response_model=ProcessingJob)
    async def cancel(job_id: str):
        result = db.cancel(job_id)
        intent = db.one('SELECT run_id FROM identification_intents WHERE job_id=?', (job_id,))
        if intent:
            inference.cancel(intent['run_id'])
        return result

    @app.get("/api/v1/meetings/{meeting_id}/segments", response_model=SegmentsPage)
    async def segments(meeting_id: str, offset: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=1000), segment_id: str | None = None, version: int | None = Query(default=None,ge=0)):
        meeting = db.meeting(meeting_id)
        version = meeting['transcript_version'] if version is None else version
        if segment_id:
            item = db.one("SELECT * FROM segments WHERE id=? AND meeting_id=? AND transcript_version=?", (segment_id, meeting_id, version))
            return {"items": [item] if item else [], "total": int(item is not None), "transcript_version": version}
        return db.segments(meeting_id,version=version,offset=offset,limit=limit)

    @app.get("/api/v1/meetings/{meeting_id}/speakers", response_model=list[Speaker])
    async def speakers(meeting_id: str, version: int | None = Query(default=None, ge=0)):
        meeting = db.meeting(meeting_id)
        version = meeting["transcript_version"] if version is None else version
        return db.rows("""SELECT p.* FROM speakers p WHERE p.meeting_id=? AND EXISTS(
            SELECT 1 FROM segments s WHERE s.speaker_id=p.id AND s.meeting_id=p.meeting_id
            AND s.chunk_id=p.chunk_id AND s.transcript_version=?) ORDER BY p.chunk_id,p.provider_label""", (meeting_id, version))

    @app.get("/api/v1/meetings/{meeting_id}/summary", response_model=Summary,
             responses={202: {"model": StatusResponse, "description": "Итоги ещё не готовы; текущее состояние обработки"}})
    async def summary(meeting_id: str):
        meeting = db.meeting(meeting_id)
        result = db.summary(meeting_id)
        if result:
            return assignment_service.projected(meeting_id)
        job = db.one("SELECT status,error FROM jobs WHERE meeting_id=? AND stage='summarize' ORDER BY created_at DESC LIMIT 1", (meeting_id,))
        return JSONResponse(job or {"status": meeting["status"]}, status_code=202)

    @app.get("/api/v1/meetings/{meeting_id}/tasks", response_model=list[ActionItem],
             responses={202: {"model": StatusResponse, "description": "Задачи ещё не готовы; текущее состояние обработки"}})
    async def tasks(meeting_id: str):
        meeting = db.meeting(meeting_id)
        result = db.summary(meeting_id)
        if result:
            return assignment_service.projected(meeting_id)["action_items"]
        return JSONResponse({"status": meeting["status"]}, status_code=202)

    @app.get("/api/v1/meetings/{meeting_id}/events", response_class=StreamingResponse,
             responses={200: {"description": "SSE: события status с meeting/jobs/segment_count и комментарии heartbeat",
                              "content": {"text/event-stream": {"schema": {"type": "string"}}}}})
    async def events(meeting_id: str, request: Request):
        db.meeting(meeting_id)
        async def stream():
            previous = None
            while not await request.is_disconnected():
                if maintenance is not None and maintenance.status()['mode'] != 'open':
                    break
                snapshot = json.dumps({"meeting": db.meeting(meeting_id), "jobs": db.jobs(meeting_id), "segment_count": db.segments(meeting_id, limit=1)["total"]}, ensure_ascii=False)
                if snapshot != previous:
                    yield "event: status\ndata: " + snapshot + "\n\n"
                    previous = snapshot
                else:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(1)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    @app.get("/api/v1/meetings/{meeting_id}/audio", response_class=Response,
             responses={
                 200: {"description": "WAV выбранного канала", "content": {"audio/wav": {"schema": {"type": "string", "format": "binary"}}}},
                 206: {"description": "Запрошенный диапазон WAV или несколько диапазонов",
                       "content": {"audio/wav": {"schema": {"type": "string", "format": "binary"}},
                                   "multipart/byteranges": {"schema": {"type": "string", "format": "binary"}}},
                       "headers": {"Content-Range": {"schema": {"type": "string"}}}},
                 400: {"description": "Некорректный заголовок Range", "content": {"text/plain": {"schema": {"type": "string"}}}},
                 404: {"model": ErrorResponse, "description": "Встреча, канал или файл записи не найден"},
                 409: {"model": ErrorResponse, "description": "Аудиофрагменты нельзя объединить без искажения записи"},
                 416: {"description": "Запрошенный диапазон вне файла", "content": {"text/plain": {"schema": {"type": "string"}}}},
             })
    async def audio(meeting_id: str, channel: Literal["import", "microphone", "system", "mixed"] | None = None):
        db.meeting(meeting_id)
        parts = db.chunks(meeting_id)
        if not parts:
            raise HTTPException(404, "Аудио ещё не подготовлено")
        # Import produces one channel; live has independent channels. Prefer system
        # for playback, microphone if unavailable. Original independent files remain.
        channels = {part["channel"] for part in parts}
        selected = channel or ("import" if "import" in channels else ("system" if "system" in channels else "microphone"))
        if selected not in channels:
            raise HTTPException(404, "Audio channel unavailable")
        selected_parts = sorted((part for part in parts if part["channel"] == selected), key=lambda part: (part["offset_ms"], part["sequence"]))
        paths = [Path(part["path"]) for part in selected_parts]
        missing_audio = "Файл записи не найден. Восстановите папку данных из резервной копии или импортируйте исходную запись в новую встречу."
        try:
            if any(not path.is_file() for path in paths):
                raise FileNotFoundError
            source_sizes = [path.stat().st_size for path in paths]
        except FileNotFoundError:
            raise HTTPException(404, missing_audio) from None
        if len(paths) == 1 and selected_parts[0]["offset_ms"] == 0:
            return FileResponse(paths[0], media_type="audio/wav", headers={"X-Secretary-Audio-Channel": selected})
        snapshot = [[part["id"], part["sha256"], part["offset_ms"], part["duration_ms"], str(path), size] for part, path, size in zip(selected_parts, paths, source_sizes)]
        signature = hashlib.sha256(json.dumps(["timeline-v1", selected, snapshot], separators=(",", ":")).encode()).hexdigest()[:32]
        combined = settings.data_dir / "audio" / meeting_id / f"playback-{selected}-{signature}.wav"
        async with audio_locks.setdefault(meeting_id + selected, asyncio.Lock()):
            if not combined.exists():
                def concatenate():
                    # The cache basename already includes a hash. Repeating it
                    # in a temporary name can exceed Windows' path limit.
                    partial = combined.with_name(f".{uid()}.partial")
                    with wave.open(str(paths[0]), "rb") as first:
                        params = first.getparams()
                    try:
                        with wave.open(str(partial), "wb") as output:
                            output.setparams(params)
                            written_frames = 0
                            frame_size = params.nchannels * params.sampwidth
                            silence = b"\0" * (32768 * frame_size)
                            for part, path in zip(selected_parts, paths):
                                with wave.open(str(path), "rb") as source:
                                    if source.getparams()[:3] != params[:3]:
                                        raise ValueError("Incompatible channel audio formats")
                                    target_frame = round(part["offset_ms"] * params.framerate / 1000)
                                    if written_frames - target_frame > max(1, params.framerate // 1000):
                                        raise ValueError("Overlapping source audio chunks cannot form a faithful playback timeline")
                                    gap = max(0, target_frame - written_frames)
                                    while gap:
                                        count = min(gap, 32768)
                                        output.writeframesraw(silence[:count * frame_size])
                                        written_frames += count
                                        gap -= count
                                    while data := source.readframes(32768):
                                        output.writeframesraw(data)
                                        written_frames += len(data) // frame_size
                        # Atomic create-if-absent; never replace a WAV held by a player.
                        # A second process producing this exact snapshot can only reuse it.
                        try:
                            os.link(partial, combined)
                        except FileExistsError:
                            pass
                    finally:
                        partial.unlink(missing_ok=True)
                try:
                    await asyncio.to_thread(concatenate)
                except FileNotFoundError:
                    raise HTTPException(404, missing_audio) from None
                except ValueError as exc:
                    raise HTTPException(409, str(exc)) from None
        return FileResponse(combined, media_type="audio/wav", headers={"X-Secretary-Audio-Channel": selected})

    @app.get("/api/v1/meetings/{meeting_id}/export", response_class=Response,
             responses={200: {"description": "Документ в формате, выбранном параметром format",
                              "headers": {"Content-Disposition": {"schema": {"type": "string"}}},
                              "content": {
                                  "text/plain": {"schema": {"type": "string"}},
                                  "text/markdown": {"schema": {"type": "string"}},
                                  "application/json": {"schema": {"type": "object"}},
                                  "application/vnd.openxmlformats-officedocument.wordprocessingml.document": {"schema": {"type": "string", "format": "binary"}},
                              }}})
    async def export(meeting_id: str, format: Literal["txt", "md", "json", "docx"] = "md"):
        meeting = db.meeting(meeting_id)
        saved_meeting,segments,summary,assignments,raw,context = assignment_service.export_snapshot(meeting_id)
        content, media = export_meeting(saved_meeting,segments,summary,format,assignment_snapshot=assignments,raw_summary=raw,summary_context=context)
        return Response(content, media_type=media, headers={"Content-Disposition": f'attachment; filename="meeting-{meeting_id}.{format}"'})

    @app.get("/api/v1/config", response_model=ConfigurationResponse)
    async def config():
        return settings.public()

    @app.patch("/api/v1/config", response_model=ConfigurationResponse)
    async def edit_config(changes: dict):
        if changes.keys() - EDITABLE_CONFIG:
            raise HTTPException(422, "Only non-secret configuration may be edited")
        if not outbound_enabled and "cloud_enabled" in changes and changes["cloud_enabled"] is not False:
            raise HTTPException(409, "runtime_outbound_disabled")
        changes = dict(changes)
        for field in ("stt_model", "summary_model"):
            if field in changes and isinstance(changes[field], str) and not changes[field].strip():
                raise HTTPException(422, "Название модели не должно быть пустым или состоять только из пробелов")
        if changes.get("stt_model", settings.stt_model) != settings.stt_model and "stt_price_rub_per_minute" not in changes:
            changes["stt_price_rub_per_minute"] = None
        if changes.get("summary_model", settings.summary_model) != settings.summary_model:
            for tariff in ("summary_input_rub_per_million", "summary_output_rub_per_million"):
                if tariff not in changes:
                    changes[tariff] = None
        try:
            validated = Settings(**{**settings.model_dump(), **changes}, _env_file=None)
        except ValidationError as exc:
            raise HTTPException(422, str(exc)) from None
        db.set_configuration({k: getattr(validated, k) for k in changes})
        for key in changes:
            setattr(settings, key, getattr(validated, key))
        return settings.public()

    @app.get("/api/v1/models")
    async def models():
        path = settings.project_dir / "config" / "model_catalog.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return {"models": [], "source": "not_available", "updated_at": None}

    @app.get("/api/v1/usage", response_model=UsageResponse)
    async def usage(meeting_id: str | None = None):
        records = db.rows("SELECT id,meeting_id,job_id,kind,estimated_rub,confirmed_rub,status,provider_request_id FROM usage" + (" WHERE meeting_id=?" if meeting_id else ""), (meeting_id,) if meeting_id else ())
        return {"records": records, "estimated_rub": sum(r["estimated_rub"] or 0 for r in records if r["status"] != "released"), "confirmed_rub": sum(r["confirmed_rub"] or 0 for r in records), "unknown_count": sum(r["status"] == "unknown" or (r["status"] == "reserved" and r["estimated_rub"] is None) for r in records)}

    frontend = settings.project_dir / "frontend" / "dist"
    if frontend.exists():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
    else:
        @app.get("/")
        async def build_required():
            return JSONResponse({"status": "frontend_build_required", "message": "Build frontend with npm run build"}, status_code=503)
    return app
