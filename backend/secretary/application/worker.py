from __future__ import annotations

import asyncio
import inspect
import json
import math
from pathlib import Path

from secretary.domain.models import TranscriptSegment
from secretary.infrastructure.database import now, stable_id
from secretary.application.preparation import prepare_audio, cloud_audio
from secretary.application.evidence import validate_summary
from secretary.infrastructure.transcript_routes import TranscriptRoutes, RouteConflict
from secretary.application.attribution import AttributionPipeline
from secretary.domain.speakers import SpeakerConflict
from secretary.infrastructure.assignment_repository import AssignmentRepository
from secretary.domain.summary_context import FrozenSummaryContext


class Worker:
    """One durable worker: atomic claims, per-chunk commits, no ambiguous resubmission."""
    def __init__(self, database, settings, provider_factory, *, recovery=True, enrollments=None, coordinator=None):
        self.db, self.settings, self.provider_factory = database, settings, provider_factory
        self.stopping = asyncio.Event()
        self.task = None
        self.sync_recordings = None
        self.routes = TranscriptRoutes(database, settings)
        self.assignments = AssignmentRepository(database)
        self.attribution = AttributionPipeline(database, settings, enrollments, coordinator) if coordinator else None
        self.local_task = None
        if recovery:
            self.db.recover()

    async def start(self):
        self.task = asyncio.create_task(self.run(), name="secretary-worker")

    async def close(self):
        self.stopping.set()
        if self.local_task:
            # Never abandon the thread/owned process while it holds the shared slot.
            await asyncio.shield(self.local_task)
        if self.task:
            try:
                await asyncio.wait_for(asyncio.shield(self.task), 3)
            except asyncio.TimeoutError:
                self.task.cancel()
                try:
                    await self.task
                except asyncio.CancelledError:
                    pass

    async def run(self):
        while not self.stopping.is_set():
            if self.sync_recordings:
                await asyncio.to_thread(self.sync_recordings)
            if not await self.run_once():
                try:
                    await asyncio.wait_for(self.stopping.wait(), 0.5)
                except asyncio.TimeoutError:
                    pass

    def cancelled(self, job):
        return bool(self.db.job(job["id"], internal=True)["cancel_requested"])

    @staticmethod
    def transcription_completed(job):
        if job["stage"] != "transcribe":
            return False
        if job["status"] == "succeeded":
            return True
        if job["status"] != "cancelled":
            return False
        payload = json.loads(job["payload"]) if job.get("payload") else {}
        # Completion belongs to this exact chunk/version, even when speech was empty.
        return (payload.get("result_committed") is True
                and payload.get("result_chunk_id") == job["chunk_id"]
                and type(payload.get("result_version")) is int
                and payload.get("result_version") == job["version"])

    def error_message(self, exc):
        from pydantic import ValidationError
        if isinstance(exc,ValidationError):
            return 'invalid_typed_evidence'
        message = str(exc)
        if self.settings.key_configured:
            message = message.replace(self.settings.polza_api_key.get_secret_value(), "[redacted]")
        return message[:1500]

    def completed_transcriptions(self, meeting_id, version):
        return [job for job in self.db.rows("SELECT * FROM jobs WHERE meeting_id=? AND stage='transcribe' AND version=? ORDER BY created_at", (meeting_id, version))
                if self.transcription_completed(job)]

    def transcription_needs_resume(self, job):
        return self.transcription_completed(job) and (job["status"] == "cancelled" or bool(job.get("cancel_requested")))

    def resume_committed_transcriptions(self, jobs):
        # Explicit continuation acknowledges the already persisted result. Public
        # succeeded statuses then provide authoritative progress without another POST.
        with self.db.transaction() as conn:
            for job in jobs:
                current = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone())
                if self.transcription_needs_resume(current):
                    conn.execute("UPDATE jobs SET status='succeeded',cancel_requested=0,error=NULL,updated_at=? WHERE id=?", (now(), current["id"]))

    async def run_once(self):
        job = self.db.claim()
        if job is None:
            self.reconcile_local_handoff()
            job = self.db.claim()
            if job is None:
                return False
        reservation = None
        active_summary_receipt = None
        cloud_request_started = False
        payload = json.loads(job["payload"]) if job.get("payload") else {}
        configuration = self.settings.model_copy(update=payload.get("settings", {}))
        try:
            if job['stage'] not in {'prepare','transcribe','identify_speakers','summarize'}:
                self.db.finish_job(job['id'], 'failed', 'unknown_processing_stage')
                return True
            if job['stage'] == 'summarize':
                payload = self.ensure_summary_context(job,payload)
                job = self.db.job(job['id'],internal=True)
                configuration = self.settings.model_copy(update=payload.get('settings',{}))
            if job["stage"] == "prepare":
                await self.prepare(job)
                return True
            if job['stage'] == 'identify_speakers':
                if self.attribution is None:
                    self.db.finish_job(job['id'], 'failed', 'voice_runtime_unavailable')
                    return True
                self.local_task = asyncio.create_task(asyncio.to_thread(self.attribution.execute, job, self.stopping))
                try:
                    completed = await asyncio.shield(self.local_task)
                except asyncio.CancelledError:
                    self.stopping.set()
                    await asyncio.shield(self.local_task)
                    raise
                finally:
                    self.local_task = None
                intent = self.db.one('SELECT pipeline FROM identification_intents WHERE job_id=?', (job['id'],))
                if completed and intent and intent['pipeline']:
                    self.maybe_summary(job['meeting_id'], job['version'])
                return True
            if job['stage'] == 'summarize' and self.db.meeting(job['meeting_id'], internal=True)['processing_mode']=='voice_identification':
                if self.attribution is None or not self.attribution.barrier(job['meeting_id'], job['version']):
                    self.db.finish_job(job['id'], 'failed', 'identification_required')
                    return True
            if job["stage"] == "summarize" and self.db.segments(job["meeting_id"], job["version"], limit=1)["total"] == 0:
                result = {"overview": "Речь не обнаружена", "readable_transcript": "", "decisions": [], "action_items": [], "open_questions": []}
                self.db.save_summary(job, validate_summary(result, job["meeting_id"], job["version"], []))
                return True
            if not self.settings.key_configured or not self.settings.cloud_enabled:
                self.db.finish_job(job["id"], "waiting_config", "Для облачной обработки нужен API-ключ" if not self.settings.key_configured else "Облачная обработка выключена")
                self.db.update_meeting(job["meeting_id"], status="waiting_config", error=None)
                return True
            model = configuration.stt_model if job["stage"] == "transcribe" else configuration.summary_model
            if not model:
                self.db.finish_job(job["id"], "waiting_config", "Выберите модель обработки в настройках")
                self.db.update_meeting(job["meeting_id"], status="waiting_config")
                return True
            if job["stage"] == "transcribe":
                try:
                    previous = self.db.one("SELECT id FROM usage WHERE job_id=? AND status!='released' ORDER BY created_at DESC LIMIT 1", (job["id"],)) if job.get("provider_job_id") else None
                    reservation = previous["id"] if previous else self.db.reserve(job, self.estimate(job, configuration), self.settings.meeting_budget_rub, self.settings.allow_unknown_price, self.settings.unknown_request_reservation_rub, enforce_limits=self.settings.local_cost_limits_enabled)
                except ValueError as exc:
                    self.db.finish_job(job["id"], "paused_budget", str(exc))
                    self.db.update_meeting(job["meeting_id"], status="paused_budget", error=str(exc))
                    return True
            provider = self.provider_factory(configuration)
            try:
                self.db.update_meeting(job["meeting_id"], status=job["stage"], error=None)
                if job["stage"] == "transcribe":
                    chunk = self.db.one("SELECT * FROM chunks WHERE id=?", (job["chunk_id"],))
                    kwargs = {"offset_ms": chunk["offset_ms"], "channel": chunk["channel"]}
                    signature = inspect.signature(provider.transcribe)
                    if "on_provider_job" in signature.parameters:
                        async def durable_provider_id(provider_id):
                            self.db.execute("UPDATE jobs SET provider_job_id=?,updated_at=? WHERE id=?", (provider_id, now(), job["id"]))
                        kwargs["on_provider_job"] = durable_provider_id
                    if "provider_job_id" in signature.parameters:
                        kwargs["provider_job_id"] = job.get("provider_job_id")
                    path = await cloud_audio(self.settings, chunk)
                    if self.cancelled(job):
                        raise asyncio.CancelledError("Cancelled before provider dispatch")
                    cloud_request_started = True
                    result = await provider.transcribe(str(path), **kwargs)
                    self.db.settle(reservation, result.get("usage"))
                    raw_dir = self.settings.data_dir / "transcripts" / job["meeting_id"]
                    raw_dir.mkdir(parents=True, exist_ok=True)
                    raw_path = raw_dir / f"{job['id']}.json"
                    raw_partial = raw_path.with_suffix(".partial")
                    raw_partial.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
                    raw_partial.replace(raw_path)
                    segments, speakers = self.normalize_segments(job, chunk, result)
                    self.db.save_segments(job, segments, speakers)
                    if self.cancelled(job):
                        with self.db.transaction() as conn:
                            # save_segments already committed the whole result and
                            # succeeded status atomically. Persist this marker together
                            # with cancellation, so a restart cannot lose empty success.
                            payload.update(result_committed=True, result_chunk_id=job["chunk_id"], result_version=job["version"])
                            conn.execute("UPDATE jobs SET status='cancelled',payload=?,error=?,updated_at=? WHERE id=?", (
                                json.dumps(payload), "Cancelled after provider completed; available transcript and receipt retained", now(), job["id"]))
                        self.db.update_meeting(job["meeting_id"], status="cancelled", error=None)
                    else:
                        self.maybe_summary(job["meeting_id"], job["version"])
                else:
                    frozen = FrozenSummaryContext.model_validate(payload['summary_context']) if payload.get('summary_context') else None
                    segments = ([s.model_dump(mode='json') for s in frozen.sources] if frozen else
                                payload['legacy_sources'])
                    if not segments:
                        result = {"overview": "Речь не обнаружена", "readable_transcript": "", "decisions": [], "action_items": [], "open_questions": []}
                    else:
                        async def before_request(kind, input_chars, max_output_tokens):
                            nonlocal reservation, active_summary_receipt, cloud_request_started
                            cloud_request_started = False
                            active_summary_receipt = None
                            if self.cancelled(job):
                                raise asyncio.CancelledError("Summary cancelled")
                            inp, out = configuration.summary_input_rub_per_million, configuration.summary_output_rub_per_million
                            estimate = None if inp is None or out is None else ((input_chars + 4096) * 4 * inp + max_output_tokens * out) / 1e6
                            reservation = self.db.reserve(job, estimate, self.settings.meeting_budget_rub, self.settings.allow_unknown_price, self.settings.unknown_request_reservation_rub, enforce_limits=self.settings.local_cost_limits_enabled)
                            cloud_request_started = True
                        async def on_usage(receipt):
                            nonlocal reservation, active_summary_receipt, cloud_request_started
                            active_summary_receipt = receipt
                            if reservation:
                                self.db.settle(reservation, receipt)
                                reservation = None
                            cloud_request_started = False
                        parameters = inspect.signature(provider.summarize).parameters
                        if "before_request" in parameters:
                            kwargs = {"before_request": before_request, "on_usage": on_usage}
                            if 'context' in parameters:
                                kwargs['context'] = frozen
                            if "checkpoint_get" in parameters:
                                async def checkpoint_get(key):
                                    row = self.db.one("SELECT payload FROM summary_checkpoints WHERE job_id=? AND key=?", (job["id"], key))
                                    return json.loads(row["payload"]) if row else None
                                async def checkpoint_put(key, payload):
                                    self.db.execute("INSERT OR REPLACE INTO summary_checkpoints VALUES(?,?,?)", (job["id"], key, json.dumps(payload, ensure_ascii=False)))
                                kwargs.update(checkpoint_get=checkpoint_get, checkpoint_put=checkpoint_put)
                            result = await provider.summarize(segments, **kwargs)
                        else:
                            reservation = self.db.reserve(job, self.estimate(job, configuration), self.settings.meeting_budget_rub, self.settings.allow_unknown_price, self.settings.unknown_request_reservation_rub, enforce_limits=self.settings.local_cost_limits_enabled)
                            cloud_request_started = True
                            result = await provider.summarize(segments)
                            await on_usage(result.get("usage", {}))
                    summary = validate_summary(result, job["meeting_id"], job["version"], segments)
                    if self.cancelled(job):
                        self.db.finish_job(job["id"], "cancelled", "Cancelled after provider completed; cost receipt retained")
                        self.db.update_meeting(job["meeting_id"], status="cancelled", error=None)
                    else:
                        self.db.save_summary(job, summary)
            finally:
                close = getattr(provider, "aclose", None)
                if close:
                    await close()
        except asyncio.CancelledError:
            current_job = self.db.job(job["id"], internal=True)
            if current_job["status"] == "succeeded" or self.transcription_completed(current_job):
                if self.stopping.is_set():
                    raise
                return True
            accepted_id = current_job.get("provider_job_id")
            if job["stage"] == "prepare":
                status = "cancelled" if self.cancelled(job) else "queued"
            elif not cloud_request_started and not accepted_id:
                if reservation:
                    self.db.settle(reservation, rejected=True)
                    reservation = None
                status = "cancelled" if self.cancelled(job) else "queued"
            elif self.cancelled(job) and reservation is None and not accepted_id:
                status = "cancelled"
            else:
                status = "uncertain"
                if reservation:
                    self.db.settle(reservation)
            self.db.finish_job(job["id"], status, "Worker stopped; audio retained")
            if status == "cancelled":
                self.db.update_meeting(job["meeting_id"], status="cancelled", error=None)
            elif status == "uncertain":
                self.db.update_meeting(job["meeting_id"], status="partial_error", error="Worker stopped; accepted request needs reconciliation, audio retained")
            elif status == "queued":
                self.db.update_meeting(job["meeting_id"], status="queued", error=None)
            if self.stopping.is_set():
                raise
        except Exception as exc:
            current_job = self.db.job(job["id"], internal=True)
            if current_job["status"] == "succeeded" or self.transcription_completed(current_job):
                # Cleanup or follow-up failures cannot revoke a durable paid result.
                self.db.update_meeting(job["meeting_id"], status="partial_error", error=self.error_message(exc))
                return True
            uncertain = bool(getattr(exc, "uncertain", False)) or (cloud_request_started and not hasattr(exc, "uncertain"))
            provider_id = getattr(exc, "provider_job_id", None)
            if provider_id:
                self.db.execute("UPDATE jobs SET provider_job_id=? WHERE id=?", (provider_id, job["id"]))
            code = str(getattr(exc, "code", "processing_error"))
            accepted_id = provider_id or job.get("provider_job_id") or self.db.job(job["id"], internal=True).get("provider_job_id")
            configuration_rejected = code == "unsupported_response_format" and not uncertain and not accepted_id
            if code == "provider_job_failed":
                payload["provider_terminal"] = True
                self.db.execute("UPDATE jobs SET payload=? WHERE id=?", (json.dumps(payload), job["id"]))
            # Only explicit rejected statuses release the reservation. Validation failures
            # after a successful response retain settled receipts.
            if reservation:
                state = self.db.one("SELECT status FROM usage WHERE id=?", (reservation,))["status"]
                if state == "reserved":
                    receipts = ([active_summary_receipt] if active_summary_receipt else []) if job["stage"] == "summarize" else getattr(exc, "usage_records", [])
                    if receipts:
                        self.db.settle(reservation, receipts[-1])
                    else:
                        self.db.settle(reservation, rejected=(not uncertain and not accepted_id and (not cloud_request_started or code in {"401", "402", "413", "429", "unauthorized", "payment_required", "payload_too_large", "rate_limit", "authentication", "insufficient_funds", "access_denied", "invalid_request", "not_found", "rate_limited", "price_limit", "unsupported_response_format"})))
            budget_error = isinstance(exc, ValueError) and ("budget" in str(exc).lower() or "Unknown model price" in str(exc) or "unknown cost" in str(exc))
            resumable = bool(provider_id or job.get("provider_job_id")) and bool(getattr(exc, "retryable", False))
            # Summary maps resume from paid checkpoints; only explicit rate rejections are retried.
            rate_retry = job["stage"] in {"transcribe", "summarize"} and not uncertain and code in {"429", "rate_limit", "rate_limited"} and bool(getattr(exc, "retryable", False))
            baseline = payload.get("retry_attempt_baseline", 0) if job["stage"] == "summarize" else 0
            if type(baseline) is not int or not 0 <= baseline < job["attempts"]:
                baseline = 0
            attempts_in_run = job["attempts"] - baseline
            retryable = not uncertain and (resumable or rate_retry) and attempts_in_run < 3 and not self.cancelled(job)
            status = "queued" if retryable else ("paused_budget" if budget_error or code in {"insufficient_funds", "price_limit"} else ("waiting_config" if code == "authentication" or configuration_rejected else ("uncertain" if uncertain or resumable else "failed")))
            if not cloud_request_started and not accepted_id and self.cancelled(job):
                status, retryable = "cancelled", False
            # Provider exceptions are sanitized; never include key/request body in logs.
            message = self.error_message(exc)
            self.db.finish_job(job["id"], status, message)
            if code == "price_limit":
                # One route rejection applies to pending chunks of this attempt too.
                # Keep their audio and require an explicit resume after a tariff change.
                self.db.execute("UPDATE jobs SET status='paused_budget',error=?,updated_at=? WHERE meeting_id=? AND version=? AND stage=? AND status='queued'", (message, now(), job["meeting_id"], job["version"], job["stage"]))
            if configuration_rejected and job["stage"] == "transcribe":
                # Reject the incompatible route once; preserve accepted polls and unrelated attempts.
                with self.db.transaction() as conn:
                    pending = conn.execute("SELECT id,payload FROM jobs WHERE meeting_id=? AND version=? AND stage='transcribe' AND status='queued' AND provider_job_id IS NULL", (job["meeting_id"], job["version"])).fetchall()
                    for candidate in pending:
                        candidate_payload = json.loads(candidate["payload"]) if candidate["payload"] else {}
                        candidate_config = self.settings.model_copy(update=candidate_payload.get("settings", {}))
                        if all(getattr(candidate_config, key) == getattr(configuration, key) for key in self.route_snapshot("transcribe")):
                            conn.execute("UPDATE jobs SET status='waiting_config',error=?,updated_at=? WHERE id=?", (message, now(), candidate["id"]))
            self.db.update_meeting(job["meeting_id"], status=status if status in {"queued", "paused_budget", "waiting_config", "cancelled"} else "partial_error", error=None if status == "cancelled" else message)
            if retryable:
                import random
                await asyncio.sleep(min(30, float(getattr(exc, "retry_after_seconds", None) or 2 ** attempts_in_run)) + random.uniform(0, 0.25))
        return True

    async def prepare(self, job):
        meeting = self.db.meeting(job["meeting_id"], internal=True)
        source = Path(meeting["media_path"])
        payload = json.loads(job["payload"]) if job.get("payload") else {}
        preparation_settings = self.settings.model_copy(update={"chunk_seconds": payload.get("chunk_seconds", self.settings.chunk_seconds)})
        chunks = await prepare_audio(preparation_settings, source, self.settings.data_dir / "audio" / meeting["id"] / "import", cancelled=lambda: self.cancelled(job))
        for chunk in chunks:
            self.db.add_chunk(meeting["id"], chunk)
        self.db.update_meeting(meeting["id"], status="prepared", duration_ms=sum(c["duration_ms"] for c in chunks), error=None)
        self.db.finish_job(job["id"], "succeeded")
        self.enqueue_transcription(meeting["id"])

    def enqueue_transcription(self, meeting_id, *, retry=False, resume=False, new_transcript_version=False):
        meeting = self.db.meeting(meeting_id, internal=True)
        chunks = self.db.chunks(meeting_id)
        if not chunks:
            raise ValueError("Нет подготовленных аудиочанков")
        version, frozen_route = self.routes.bind(meeting_id, explicit=retry or resume or new_transcript_version, new_version=new_transcript_version)
        completed = self.completed_transcriptions(meeting_id, version)
        completed_by_chunk = {job["chunk_id"]: job for job in completed}
        explicit_resume = retry or resume
        resume_cancelled_result = explicit_resume and any(self.transcription_needs_resume(job) for job in completed)
        if retry and not new_transcript_version:
            incomplete = sum(chunk["id"] not in completed_by_chunk for chunk in chunks)
            if incomplete == 0 and not resume_cancelled_result:
                if self.db.one("SELECT 1 FROM jobs WHERE meeting_id=? AND stage='summarize' AND status='running' LIMIT 1", (meeting_id,)):
                    raise ValueError("Итоги сейчас обрабатываются; дождитесь завершения перед новой транскрипцией")
                self.db.execute("UPDATE jobs SET status='cancelled',cancel_requested=1,error='Superseded by a new transcript version' WHERE meeting_id=? AND stage='summarize' AND status IN ('queued','waiting_config','paused_budget')", (meeting_id,))
                version, frozen_route = self.routes.bind(meeting_id, explicit=True, new_version=True)
                completed_by_chunk = {}
        self.db.authorize_pipeline(meeting_id,version,explicit=retry or resume or new_transcript_version,
                                   reason='continue' if retry or resume or new_transcript_version else 'input')
        if resume_cancelled_result:
            self.resume_committed_transcriptions(completed)
        jobs = []
        for chunk in chunks:
            retained = completed_by_chunk.get(chunk["id"])
            if retained:
                if self.transcription_needs_resume(retained) and not explicit_resume:
                    jobs.append(retained)
                continue
            prior = self.db.one("SELECT * FROM jobs WHERE meeting_id=? AND chunk_id=? AND stage='transcribe' AND version=? ORDER BY created_at DESC LIMIT 1", (meeting_id, chunk["id"], version))
            if prior and prior["status"] == "succeeded":
                continue
            if prior:
                jobs.append(self.resume_transcription_job(prior, frozen_route, meeting['processing_mode'], explicit=explicit_resume))
            else:
                request_settings = dict(frozen_route['settings'])
                if explicit_resume and self.settings.stt_model == request_settings['stt_model']:
                    request_settings['stt_price_rub_per_minute'] = self.settings.stt_price_rub_per_minute
                jobs.append(self.db.enqueue(meeting_id, "transcribe", chunk_id=chunk["id"], version=version, payload={"settings": request_settings}))
        if not jobs:
            self.maybe_summary(meeting_id, version)
            previous = self.db.one("SELECT * FROM jobs WHERE meeting_id=? ORDER BY created_at DESC LIMIT 1", (meeting_id,))
            if previous and previous["status"] in {"queued", "running"}:
                self.db.update_meeting(meeting_id, status="queued" if previous["status"] == "queued" else previous["stage"], error=None)
            return previous
        refreshed = [self.db.job(job["id"], internal=True) for job in jobs]
        runnable = [job for job in refreshed if job["status"] in {"queued", "running"} and not job["cancel_requested"]]
        if runnable:
            status = "queued" if any(job["status"] == "queued" for job in runnable) else "transcribe"
            self.db.update_meeting(meeting_id, status=status, error=None)
            return runnable[0]
        # Returning an existing failed/cancelled/ambiguous job is not new work.
        # Preserve the meeting's diagnostic state and never imply a queued submit.
        return refreshed[0]

    def resume_transcription_job(self, prior, frozen_route, processing_mode, *, explicit):
        """Only an explicit safe fresh attempt may change its price ceiling.

        Re-read under the writer lock: a concurrent claim/accepted receipt must
        prevent replacing request settings. Accepted IDs resume polling with
        their original payload; defaults never resume stopped work or repair caps.
        """
        with self.db.transaction() as conn:
            job = dict(conn.execute('SELECT * FROM jobs WHERE id=?',(prior['id'],)).fetchone())
            payload = json.loads(job['payload'] or '{}')
            if (job['status']=='waiting_config' and not job.get('provider_job_id')
                    and frozen_route['settings']['stt_model'] != self.routes.configured(processing_mode)['settings']['stt_model']):
                raise RouteConflict('route_change_required: use new_transcript_version=true; all STT may be billed again')
            if job['status'] in {'running','succeeded'} or self.transcription_completed(job) or payload.get('result_committed'):
                return job
            if not explicit:
                return job
            if job.get('provider_job_id'):
                if not payload.get('provider_terminal'):
                    conn.execute("UPDATE jobs SET status='queued',cancel_requested=0,updated_at=? WHERE id=?",(now(),job['id']))
                return dict(conn.execute('SELECT * FROM jobs WHERE id=?',(job['id'],)).fetchone())
            if job['status']=='uncertain' or conn.execute("SELECT 1 FROM usage WHERE job_id=? AND status!='released' LIMIT 1",(job['id'],)).fetchone():
                return job
            request_settings = dict(payload.get('settings') or frozen_route['settings'])
            if request_settings.get('stt_model') != frozen_route['settings']['stt_model']:
                raise RouteConflict('legacy_route_needs_review')
            if self.settings.stt_model == request_settings['stt_model']:
                request_settings['stt_price_rub_per_minute'] = self.settings.stt_price_rub_per_minute
            payload['settings'] = request_settings
            conn.execute("UPDATE jobs SET status='queued',cancel_requested=0,error=NULL,payload=?,updated_at=? WHERE id=?",
                (json.dumps(payload),now(),job['id']))
            return dict(conn.execute('SELECT * FROM jobs WHERE id=?',(job['id'],)).fetchone())

    def enqueue_summary(self, meeting_id, *, retry=False, automatic=False, regenerate_summary=False):
        if regenerate_summary and (retry or automatic):
            raise ValueError('regenerate_summary_conflicts_with_retry')
        meeting = self.db.meeting(meeting_id, internal=True)
        if meeting["recording"]:
            raise ValueError("Завершите запись перед созданием итогов")
        chunks = self.db.chunks(meeting_id)
        completed = self.completed_transcriptions(meeting_id, meeting["transcript_version"])
        completed_chunks = {job["chunk_id"] for job in completed}
        pending = sum(chunk["id"] not in completed_chunks for chunk in chunks)
        if not chunks or pending:
            raise ValueError("Итоги доступны после полной транскрипции; часть аудио ещё не обработана")
        if not automatic:
            self.resume_committed_transcriptions(completed)
        elif any(self.transcription_needs_resume(job) for job in completed):
            return None
        if meeting['processing_mode']=='voice_identification' and (self.attribution is None or not self.attribution.barrier(meeting_id, meeting['transcript_version'])):
            raise ValueError('identification_required')
        if self.db.summary(meeting_id) and not retry and not regenerate_summary:
            return self.db.one("SELECT * FROM jobs WHERE meeting_id=? AND stage='summarize' AND status='succeeded' ORDER BY created_at DESC LIMIT 1", (meeting_id,))
        if automatic:
            return self.first_summary_handoff(meeting_id,meeting['transcript_version'])
        prior = self.db.one("SELECT * FROM jobs WHERE meeting_id=? AND stage='summarize' AND version=? ORDER BY created_at DESC,id DESC LIMIT 1", (meeting_id,meeting['transcript_version']))
        if prior:
            if regenerate_summary:
                if prior['status'] in ('queued','running','uncertain'):
                    raise ValueError('summary_regeneration_requires_stopped_job')
                return self.regenerate_summary_job(meeting_id,meeting['transcript_version'])
            if prior['status'] in ('queued','running','uncertain') or prior['status'] in ('failed','cancelled') and not retry:
                return prior
            if prior['status']=='succeeded':
                if not retry:
                    return prior
                # An explicit new summary of a completed version is the existing
                # paid retry action; first-handoff ownership remains historical.
                return self.regenerate_summary_job(meeting_id,meeting['transcript_version'])
            payload = json.loads(prior["payload"]) if prior.get("payload") else {}
            # Frozen paid input and settings survive explicit stopped resume.
            if not payload.get('summary_context') and not self.db.one('SELECT 1 FROM usage WHERE job_id=?',(prior['id'],)) and not self.db.one('SELECT 1 FROM summary_checkpoints WHERE job_id=?',(prior['id'],)):
                payload.setdefault('settings',self.route_snapshot('summarize'))
            if retry:
                # Only explicit resume of a stopped job starts a fresh bounded retry series.
                # Preserve lifetime attempts and paid checkpoints on this same job.
                payload["retry_attempt_baseline"] = prior["attempts"]
            self.db.execute("UPDATE jobs SET status='queued',cancel_requested=0,payload=? WHERE id=?", (json.dumps(payload), prior["id"]))
            return self.db.job(prior['id'],internal=True)
        self.db.authorize_pipeline(meeting_id,meeting['transcript_version'],explicit=True,reason='summary')
        return self.first_summary_handoff(meeting_id,meeting['transcript_version'])

    def regenerate_summary_job(self, meeting_id, version):
        from secretary.infrastructure.database import uid
        with self.db.transaction() as conn:
            if conn.execute('SELECT transcript_version FROM meetings WHERE id=?',(meeting_id,)).fetchone()[0] != version:
                raise ValueError('transcript_version_changed')
            active = conn.execute("SELECT status FROM jobs WHERE meeting_id=? AND version=? AND stage='summarize' AND status IN ('queued','running','uncertain')",(meeting_id,version)).fetchone()
            if active:
                raise ValueError('summary_regeneration_requires_stopped_job')
            conn.execute("UPDATE jobs SET status='cancelled',cancel_requested=1,error='Superseded by explicit summary regeneration' WHERE meeting_id=? AND version=? AND stage='summarize' AND status IN ('waiting_config','paused_budget')",(meeting_id,version))
            payload = self.summary_reference_payload(meeting_id,version,conn=conn)
            job_id,stamp = uid(),now()
            conn.execute("INSERT INTO jobs(id,meeting_id,stage,status,created_at,updated_at,version,payload) VALUES(?,?,'summarize','queued',?,?,?,?)",(job_id,meeting_id,stamp,stamp,version,json.dumps(payload)))
            result = dict(conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone())
            self.assignments.bind_job(conn,result)
            return result

    def summary_reference_payload(self, meeting_id, version, *, conn=None):
        if conn is None:
            with self.db.connection() as connection:
                connection.execute('BEGIN')
                return self.summary_reference_payload(meeting_id,version,conn=connection)
        frozen = self.assignments.capture(conn,meeting_id,version,self.route_snapshot('summarize'))
        return {'settings':frozen.settings.model_dump(mode='json'),'summary_contract':frozen.contract,
            'summary_context':frozen.model_dump(mode='json'),'attribution_run_id':frozen.attribution_run_id,
            'attribution_revision':frozen.attribution_revision,'roster_revision':frozen.roster_revision}

    def ensure_summary_context(self, job, payload):
        """Bind fresh direct jobs; keep reconstructable accepted legacy contract v1."""
        with self.db.transaction() as conn:
            if payload.get('summary_context'):
                frozen = FrozenSummaryContext.model_validate(payload['summary_context'])
                stored = conn.execute('SELECT context_hash FROM summary_input_contexts WHERE job_id=?',(job['id'],)).fetchone()
                if stored is None:
                    self.assignments.bind_job(conn,job)
                elif stored['context_hash'] != frozen.context_hash:
                    raise ValueError('summary_context_mismatch')
                return payload
            paid = conn.execute("SELECT 1 FROM usage WHERE job_id=? AND status IN ('confirmed','unknown') LIMIT 1",(job['id'],)).fetchone()
            checkpoints = conn.execute('SELECT key,payload FROM summary_checkpoints WHERE job_id=?',(job['id'],)).fetchall()
            if paid or checkpoints or payload.get('summary_contract')=='legacy-v1':
                required = {'summary_model','summary_batch_chars','summary_max_output_tokens'}
                if not required <= set(payload.get('settings',{})) or (paid and not checkpoints):
                    raise ValueError('legacy_summary_context_needs_review')
                payload.setdefault('legacy_sources',self.assignments.sources(conn,job['meeting_id'],job['version']))
                from secretary.infrastructure.polza import PolzaClient, ProviderError
                try:
                    legacy_provider = PolzaClient(self.settings.model_copy(update=payload['settings']))
                    legacy_provider.validate_legacy_checkpoints(payload['legacy_sources'],
                        {row['key']:json.loads(row['payload']) for row in checkpoints})
                except (ValueError, TypeError, KeyError, ProviderError) as exc:
                    raise ValueError('legacy_summary_context_needs_review') from exc
                payload['summary_contract']='legacy-v1'
            else:
                settings = payload.get('settings') or self.route_snapshot('summarize')
                frozen = self.assignments.capture(conn,job['meeting_id'],job['version'],settings)
                payload.update(summary_context=frozen.model_dump(mode='json'),summary_contract=frozen.contract,
                               settings=frozen.settings.model_dump(mode='json'))
            conn.execute('UPDATE jobs SET payload=? WHERE id=?',(json.dumps(payload),job['id']))
            if payload.get('summary_context'):
                self.assignments.bind_job(conn,{**job,'payload':json.dumps(payload)})
            elif not conn.execute('SELECT 1 FROM legacy_summary_inputs WHERE job_id=?',(job['id'],)).fetchone():
                conn.execute('INSERT INTO legacy_summary_inputs VALUES(?,?)',(job['id'],json.dumps(payload)))
            return payload

    def first_summary_handoff(self, meeting_id, version):
        """One atomic first enqueue, never resume/replace any existing paid state."""
        with self.db.transaction() as conn:
            handoff = conn.execute('SELECT * FROM meeting_pipeline_handoffs WHERE meeting_id=? AND transcript_version=?', (meeting_id,version)).fetchone()
            if not handoff or not handoff['summary_authorized']:
                return None
            if handoff['summary_job_id']:
                return dict(conn.execute('SELECT * FROM jobs WHERE id=?', (handoff['summary_job_id'],)).fetchone())
            prior = conn.execute("SELECT * FROM jobs WHERE meeting_id=? AND version=? AND stage='summarize' ORDER BY created_at,id LIMIT 1", (meeting_id,version)).fetchone()
            if prior:
                job_id = prior['id']
            else:
                from secretary.infrastructure.database import uid
                job_id,stamp = uid(),now()
                payload = self.summary_reference_payload(meeting_id,version,conn=conn)
                conn.execute("INSERT INTO jobs(id,meeting_id,stage,status,created_at,updated_at,version,payload) VALUES(?,?,'summarize','queued',?,?,?,?)", (job_id,meeting_id,stamp,stamp,version,json.dumps(payload)))
                self.assignments.bind_job(conn,dict(conn.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()))
            conn.execute('UPDATE meeting_pipeline_handoffs SET summary_job_id=? WHERE meeting_id=? AND transcript_version=?', (job_id,meeting_id,version))
            return dict(conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())

    def enqueue_prepare(self, meeting_id, *, retry=False):
        meeting = self.db.meeting(meeting_id, internal=True)
        if not meeting["media_path"] or meeting["media_path"].startswith("uploading:"):
            raise ValueError("Нет сохранённого исходного файла для подготовки")
        prior = self.db.one("SELECT * FROM jobs WHERE meeting_id=? AND stage='prepare' ORDER BY created_at DESC LIMIT 1", (meeting_id,))
        if prior and prior["status"] in {"queued", "running"}:
            return prior
        if prior and not retry:
            return prior
        if self.db.one("SELECT 1 FROM chunks WHERE meeting_id=? AND status='transcribed' LIMIT 1", (meeting_id,)):
            raise ValueError("Подготовленное аудио уже обработано; повторите только нужный облачный этап")
        payload = json.loads(prior["payload"]) if prior and prior.get("payload") else {"chunk_seconds": self.settings.chunk_seconds}
        self.routes.bind(meeting_id, explicit=retry)
        self.db.update_meeting(meeting_id, status="preparing", error=None)
        return self.db.enqueue(meeting_id, "prepare", payload=payload)

    def maybe_summary(self, meeting_id, version):
        meeting = self.db.meeting(meeting_id, internal=True)
        if meeting['transcript_version'] != version or meeting["recording"] or self.db.summary(meeting_id):
            return
        if any(self.transcription_needs_resume(job)
               for job in self.completed_transcriptions(meeting_id, version)):
            return  # Only explicit continuation acknowledges cancelled paid results.
        try:
            if meeting['processing_mode']=='voice_identification':
                self._require_complete(meeting_id, version)
                if self.attribution is None:
                    return
                handoff = self.db.one('SELECT summary_authorized FROM meeting_pipeline_handoffs WHERE meeting_id=? AND transcript_version=?', (meeting_id,version))
                if not handoff or not handoff['summary_authorized']:
                    return
                if not self.attribution.barrier(meeting_id,version):
                    if self.attribution.current(meeting_id,version) is None:
                        revision = self.attribution.speakers.get_attribution(meeting_id,version).revision
                        self.attribution.capture(meeting_id,version,revision,pipeline=True)
                    return
            self.enqueue_summary(meeting_id,automatic=True)
        except ValueError:
            pass

    def _require_complete(self, meeting_id, version):
        meeting = self.db.meeting(meeting_id,internal=True)
        chunks = self.db.chunks(meeting_id)
        completed = self.completed_transcriptions(meeting_id, version)
        if meeting['recording'] or meeting['transcript_version']!=version or not chunks or {c['id'] for c in chunks}-{j['chunk_id'] for j in completed}:
            raise ValueError('identification_requires_complete_transcript')

    def reconcile_local_handoff(self):
        # Only explicit new-mode routes, never legacy/history migration. Close the
        # save-STT->intent and publish->summary crash windows without cloud replay.
        if self.attribution is None:
            return
        meetings = self.db.rows("""SELECT m.id,m.transcript_version FROM meetings m
            JOIN meeting_transcript_routes r ON r.meeting_id=m.id AND r.transcript_version=m.transcript_version
            JOIN meeting_pipeline_handoffs h ON h.meeting_id=m.id AND h.transcript_version=m.transcript_version
            WHERE m.processing_mode='voice_identification' AND m.recording=0 AND h.summary_authorized=1 AND h.summary_job_id IS NULL""")
        for meeting in meetings:
            existing = self.attribution.current(meeting['id'],meeting['transcript_version'])
            if existing is None or existing['outcome']=='completed':
                self.maybe_summary(meeting['id'],meeting['transcript_version'])

    def enqueue_identification(self, meeting_id, command):
        if self.attribution is None:
            raise ValueError('voice_runtime_unavailable')
        # Replay precedes mutable-version/revision/completion guards.
        from secretary.domain.identification import IdentificationAccepted
        from secretary.infrastructure.speaker_repository import canonical
        with self.db.connection() as conn:
            replay, _ = self.attribution.speakers._replay(conn, 'identify_speakers', canonical([meeting_id,command.transcript_version]), command, IdentificationAccepted)
            if replay:
                return replay
        self._require_complete(meeting_id,command.transcript_version)
        result = self.attribution.capture(meeting_id,command.transcript_version,command.expected_revision,command=command,pipeline=False)
        return result

    def route_snapshot(self, stage):
        keys = ("stt_model", "stt_price_rub_per_minute") if stage == "transcribe" else ("summary_model", "summary_input_rub_per_million", "summary_output_rub_per_million", "summary_batch_chars", "summary_max_output_tokens", "local_cost_limits_enabled")
        return {key: getattr(self.settings, key) for key in keys}

    def estimate(self, job, configuration=None):
        configuration = configuration or self.settings
        if job["stage"] == "transcribe":
            chunk = self.db.one("SELECT duration_ms FROM chunks WHERE id=?", (job["chunk_id"],))
            price = configuration.stt_price_rub_per_minute
            return None if price is None else math.ceil(chunk["duration_ms"] / 60000) * price
        input_price = configuration.summary_input_rub_per_million
        output_price = configuration.summary_output_rub_per_million
        if input_price is None or output_price is None:
            return None
        text = self.db.rows("SELECT LENGTH(text) AS n FROM segments WHERE meeting_id=? AND transcript_version=?", (job["meeting_id"], job["version"]))
        characters = sum(r["n"] for r in text)
        batches = max(1, math.ceil(characters / configuration.summary_batch_chars))
        # Conservative upper estimate: <= one token per UTF-8 byte, source IDs/prompts
        # and a final merge. Confirmed receipt remains distinct from this reservation.
        input_tokens = characters * 4 + len(text) * 160 + batches * 2000
        output_tokens = configuration.summary_max_output_tokens * (batches + (1 if batches > 1 else 0))
        input_tokens += output_tokens if batches > 1 else 0
        return input_tokens / 1e6 * input_price + output_tokens / 1e6 * output_price

    def normalize_segments(self, job, chunk, result):
        if not isinstance(result.get("segments"), list):
            raise ValueError("Provider transcription result has no valid segment list")
        segments, speakers, labels = [], [], {}
        for ordinal, raw in enumerate(result["segments"]):
            text = raw.get("text", "").strip()
            if not text:
                continue
            label = raw.get("speaker_label")
            speaker_id = None
            if label is not None:
                label = str(label)
                speaker_id = stable_id(chunk["id"], "speaker", label)
                if label not in labels:
                    speakers.append({"id": speaker_id, "meeting_id": job["meeting_id"], "chunk_id": chunk["id"], "provider_label": label, "display_name": None})
                    labels[label] = speaker_id
            precision = raw.get("timing_precision", "unknown")
            start = chunk["offset_ms"] if precision == "chunk" else raw.get("start_ms")
            end = chunk["offset_ms"] + chunk["duration_ms"] if precision == "chunk" else raw.get("end_ms")
            segment = TranscriptSegment(id=stable_id(chunk["id"], job["version"], ordinal), meeting_id=job["meeting_id"], chunk_id=chunk["id"], transcript_version=job["version"], ordinal=ordinal, start_ms=start, end_ms=end, timing_precision=precision, text=text, confidence=raw.get("confidence"), speaker_id=speaker_id, channel=chunk["channel"])
            for point in (segment.start_ms, segment.end_ms):
                if point is not None and not chunk["offset_ms"] <= point <= chunk["offset_ms"] + chunk["duration_ms"]:
                    raise ValueError("Provider segment time is outside its source chunk")
            if segment.start_ms is not None and segment.end_ms is not None:
                duplicate = any(prior["text"] == text and prior["start_ms"] is not None and prior["end_ms"] is not None and max(prior["start_ms"], segment.start_ms) < min(prior["end_ms"], segment.end_ms) for prior in segments)
                if duplicate:
                    continue  # raw response preserves repeated provider provenance
            segments.append(segment.model_dump())
        return segments, speakers

