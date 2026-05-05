import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import select
from sqlalchemy import delete

from app.infrastructure.db.models import Meeting
from app.infrastructure.db.models import MeetingSummary
from app.infrastructure.db.models import ProcessingJob
from app.infrastructure.db.models import Speaker
from app.infrastructure.db.models import TaskItem
from app.infrastructure.db.models import Transcript
from app.infrastructure.db.models import TranscriptSegment
from app.infrastructure.db.session import SessionLocal
from app.infrastructure.queue.celery_app import celery_app
from app.services.alignment_service import align_stt_with_diarization
from app.services.diarization_service import DiarizationInterval
from app.services.diarization_service import diarize_audio_file
from app.services.media_service import prepare_audio_file
from app.services.memory_service import build_active_tasks_context
from app.services.memory_service import extract_task_items
from app.services.memory_service import save_task_items
from app.services.summary_service import summarize_transcript_text
from app.services.task_extraction_service import extract_tasks_from_transcript
from app.services.transcript_cleanup_service import build_readable_transcript
from app.services.transcript_cleanup_service import clean_transcript_segments
from app.services.transcription_service import transcribe_audio_file

logger = logging.getLogger(__name__)


def measure(stage_name, fn, metrics: dict):
    start = time.perf_counter()
    try:
        result = fn()
        success = True
        return result
    except Exception:
        success = False
        raise
    finally:
        duration = (time.perf_counter() - start) * 1000
        metrics[stage_name] = {
            "duration_ms": round(duration, 2),
            "success": success,
        }


def _update_job_and_meeting(
    *,
    db,
    meeting_id: str,
    stage: str,
    job_status: str,
    meeting_status: str,
) -> None:
    job = db.scalar(
        select(ProcessingJob).where(ProcessingJob.meeting_id == meeting_id)
    )
    meeting = db.scalar(select(Meeting).where(Meeting.id == meeting_id))
    if job is not None:
        job.stage = stage
        job.status = job_status
    if meeting is not None:
        meeting.status = meeting_status
    db.commit()


@celery_app.task(
    bind=True,
    name="app.workers.tasks.process_meeting_pipeline",
    autoretry_for=(RuntimeError, TimeoutError, ConnectionError),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=2,
    soft_time_limit=5400,
    time_limit=5700,
)
def process_meeting_pipeline(self, meeting_id: str) -> None:
    db = SessionLocal()
    prepared_audio_path: str | None = None
    meeting: Meeting | None = None
    metrics = {}
    pipeline_start = time.perf_counter()
    try:
        meeting = db.scalar(select(Meeting).where(Meeting.id == meeting_id))
        if meeting is None:
            raise ValueError(f"Meeting not found: {meeting_id}")
        job = db.scalar(
            select(ProcessingJob).where(ProcessingJob.meeting_id == meeting_id)
        )
        if job is not None and job.status == "done":
            logger.info(
                "pipeline_skip_done meeting_id=%s task_id=%s",
                meeting_id,
                self.request.id,
            )
            return
        if job is not None and job.status == "in_progress" and job.stage != "failed":
            logger.info(
                "pipeline_skip_in_progress meeting_id=%s task_id=%s stage=%s",
                meeting_id,
                self.request.id,
                job.stage,
            )
            return

        _update_job_and_meeting(
            db=db,
            meeting_id=meeting_id,
            stage="uploaded",
            job_status="in_progress",
            meeting_status="processing",
        )

        logger.info("pipeline_stage_start stage=audio_ready meeting_id=%s", meeting_id)
        prepared_audio_path = measure(
            "preprocess",
            lambda: prepare_audio_file(meeting.media_file_path),
            metrics,
        )
        audio_file_path = prepared_audio_path
        _update_job_and_meeting(
            db=db,
            meeting_id=meeting_id,
            stage="audio_ready",
            job_status="in_progress",
            meeting_status="processing",
        )
        logger.info("pipeline_stage_end stage=audio_ready meeting_id=%s", meeting_id)

        logger.info("pipeline_stage_start stage=transcribed meeting_id=%s", meeting_id)
        logger.info("pipeline_stage_start stage=diarized meeting_id=%s", meeting_id)
        diarization_error: Exception | None = None
        with ThreadPoolExecutor(max_workers=2) as executor:
            stt_future = executor.submit(
                lambda: measure(
                    "stt",
                    lambda: transcribe_audio_file(audio_file_path),
                    metrics,
                )
            )
            diarization_future = executor.submit(
                lambda: measure(
                    "diarization",
                    lambda: diarize_audio_file(audio_file_path),
                    metrics,
                )
            )
            transcript_text, stt_segments = stt_future.result()
            try:
                diarization_intervals = diarization_future.result()
            except Exception as exc:  # noqa: BLE001
                diarization_error = exc
        if not transcript_text.strip() and stt_segments:
            transcript_text = " ".join(
                segment.text.strip()
                for segment in stt_segments
                if segment.text.strip()
            )
        transcript = Transcript(
            meeting_id=meeting_id,
            provider="faster-whisper",
            language="auto",
            full_text=transcript_text,
        )
        db.add(transcript)
        db.commit()

        _update_job_and_meeting(
            db=db,
            meeting_id=meeting_id,
            stage="transcribed",
            job_status="in_progress",
            meeting_status="processing",
        )
        logger.info("pipeline_stage_end stage=transcribed meeting_id=%s", meeting_id)

        if diarization_error is not None:
            logger.exception(
                "pipeline_stage_fallback stage=diarized_default_single_speaker meeting_id=%s",
                meeting_id,
                exc_info=diarization_error,
            )
            diarization_intervals = []
            for segment in stt_segments:
                diarization_intervals.append(
                    DiarizationInterval(
                        speaker_label="SPEAKER_01",
                        start_sec=float(segment.start_sec),
                        end_sec=float(segment.end_sec),
                    )
                )
        _update_job_and_meeting(
            db=db,
            meeting_id=meeting_id,
            stage="diarized",
            job_status="in_progress",
            meeting_status="processing",
        )
        logger.info("pipeline_stage_end stage=diarized meeting_id=%s", meeting_id)

        logger.info("pipeline_stage_start stage=segmented meeting_id=%s", meeting_id)
        aligned_segments = align_stt_with_diarization(
            stt_segments,
            diarization_intervals,
        )
        cleaned_segments = clean_transcript_segments(aligned_segments)
        persisted_segments = cleaned_segments or aligned_segments
        speaker_map: dict[str, Speaker] = {}
        db.execute(
            delete(TranscriptSegment).where(
                TranscriptSegment.transcript_id == transcript.id
            )
        )
        db.execute(delete(Speaker).where(Speaker.meeting_id == meeting_id))
        db.commit()
        for segment in persisted_segments:
            speaker = speaker_map.get(segment.speaker)
            if speaker is None:
                speaker = Speaker(
                    meeting_id=meeting_id,
                    speaker_label=segment.speaker,
                )
                db.add(speaker)
                db.flush()
                speaker_map[segment.speaker] = speaker
            db.add(
                TranscriptSegment(
                    transcript_id=transcript.id,
                    speaker_id=speaker.id,
                    start_sec=float(segment.start),
                    end_sec=float(segment.end),
                    text=segment.text_clean or segment.text_raw,
                )
            )
        db.commit()
        transcript.full_text = (
            build_readable_transcript(persisted_segments).strip() or transcript.full_text
        )
        db.commit()
        _update_job_and_meeting(
            db=db,
            meeting_id=meeting_id,
            stage="segmented",
            job_status="in_progress",
            meeting_status="processing",
        )
        logger.info("pipeline_stage_end stage=segmented meeting_id=%s", meeting_id)

        llm_base_text = transcript.full_text.strip() or transcript_text.strip()
        user_id = (meeting.owner_id if meeting and meeting.owner_id else "").strip() or "default_user"
        memory_context = ""
        try:
            memory_context = build_active_tasks_context(user_id=user_id, db=db, limit=10)
        except Exception:  # noqa: BLE001
            logger.exception("memory_context_load_failed meeting_id=%s", meeting_id)
            memory_context = ""
        if memory_context:
            llm_input_text = (
                f"Context from previous meetings:\n{memory_context}\n\n"
                f"Current user request/transcript:\n{llm_base_text}"
            )
        else:
            llm_input_text = llm_base_text
        logger.info("pipeline_stage_start stage=summarized meeting_id=%s", meeting_id)
        summary_text = measure(
            "llm_summary",
            lambda: summarize_transcript_text(llm_input_text),
            metrics,
        )
        summary = db.scalar(
            select(MeetingSummary).where(MeetingSummary.meeting_id == meeting_id)
        )
        if summary is None:
            summary = MeetingSummary(
                meeting_id=meeting_id,
                summary_text=summary_text,
            )
            db.add(summary)
        else:
            summary.summary_text = summary_text
        db.commit()

        _update_job_and_meeting(
            db=db,
            meeting_id=meeting_id,
            stage="summarized",
            job_status="in_progress",
            meeting_status="processing",
        )
        logger.info("pipeline_stage_end stage=summarized meeting_id=%s", meeting_id)
        try:
            memory_source_text = f"{llm_base_text}\n\n{summary_text}".strip()
            memory_task_items = extract_task_items(memory_source_text)
            save_task_items(
                user_id=user_id,
                task_items=memory_task_items,
                db=db,
                source="llm",
                meeting_id=meeting_id,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "memory_extract_or_save_failed meeting_id=%s",
                meeting_id,
            )

        logger.info("pipeline_stage_start stage=tasks_extracted meeting_id=%s", meeting_id)
        extracted_tasks = measure(
            "llm_extract_tasks",
            lambda: extract_tasks_from_transcript(llm_input_text),
            metrics,
        )
        db.execute(delete(TaskItem).where(TaskItem.meeting_id == meeting_id))
        for extracted_task in extracted_tasks:
            db.add(
                TaskItem(
                    meeting_id=meeting_id,
                    description=extracted_task.description,
                    due_date=extracted_task.due_date,
                    priority=extracted_task.priority,
                    assignee_speaker_label=(
                        extracted_task.assignee_speaker_label
                    ),
                    source_quote=extracted_task.source_quote,
                    confidence=extracted_task.confidence,
                )
            )
        db.commit()
        _update_job_and_meeting(
            db=db,
            meeting_id=meeting_id,
            stage="tasks_extracted",
            job_status="in_progress",
            meeting_status="processing",
        )
        logger.info("pipeline_stage_end stage=tasks_extracted meeting_id=%s", meeting_id)

        metrics["total_ms"] = round(
            (time.perf_counter() - pipeline_start) * 1000, 2
        )
        job_for_metrics = db.scalar(
            select(ProcessingJob).where(ProcessingJob.meeting_id == meeting_id)
        )
        if job_for_metrics is not None and hasattr(job_for_metrics, "metrics_json"):
            job_for_metrics.metrics_json = metrics
            db.commit()
        logger.info("pipeline_metrics %s", json.dumps(metrics))

        logger.info("pipeline_stage_start stage=done meeting_id=%s", meeting_id)
        _update_job_and_meeting(
            db=db,
            meeting_id=meeting_id,
            stage="done",
            job_status="done",
            meeting_status="done",
        )
        logger.info("pipeline_stage_end stage=done meeting_id=%s", meeting_id)
    except Exception as exc:  # noqa: BLE001
        job = db.scalar(
            select(ProcessingJob).where(ProcessingJob.meeting_id == meeting_id)
        )
        meeting = db.scalar(select(Meeting).where(Meeting.id == meeting_id))
        if job is not None:
            job.stage = "failed"
            job.status = "failed"
            job.error = str(exc)
        if meeting is not None:
            meeting.status = "failed"
        db.commit()
        logger.exception("pipeline_failed meeting_id=%s error=%s", meeting_id, str(exc))
        raise
    finally:
        if prepared_audio_path:
            prepared_path_obj = Path(prepared_audio_path)
            source_path_obj = Path(meeting.media_file_path) if meeting else None
            if (
                source_path_obj is not None
                and prepared_path_obj != source_path_obj
                and prepared_path_obj.exists()
            ):
                prepared_path_obj.unlink(missing_ok=True)
        db.close()
