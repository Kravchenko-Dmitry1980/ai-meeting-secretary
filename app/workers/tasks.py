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
from app.services.summary_service import summarize_transcript_text
from app.services.task_extraction_service import extract_tasks_from_transcript
from app.services.transcript_cleanup_service import build_readable_transcript
from app.services.transcript_cleanup_service import clean_transcript_segments
from app.services.transcription_service import transcribe_audio_file


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


@celery_app.task(name="app.workers.tasks.process_meeting_pipeline")
def process_meeting_pipeline(meeting_id: str) -> None:
    db = SessionLocal()
    try:
        meeting = db.scalar(select(Meeting).where(Meeting.id == meeting_id))
        if meeting is None:
            raise ValueError(f"Meeting not found: {meeting_id}")

        _update_job_and_meeting(
            db=db,
            meeting_id=meeting_id,
            stage="uploaded",
            job_status="in_progress",
            meeting_status="processing",
        )

        audio_file_path = prepare_audio_file(meeting.media_file_path)
        _update_job_and_meeting(
            db=db,
            meeting_id=meeting_id,
            stage="audio_ready",
            job_status="in_progress",
            meeting_status="processing",
        )

        transcript_text, stt_segments = transcribe_audio_file(audio_file_path)
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

        try:
            diarization_intervals = diarize_audio_file(audio_file_path)
        except Exception:  # noqa: BLE001
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

        llm_input_text = transcript.full_text.strip() or transcript_text.strip()
        summary_text = summarize_transcript_text(llm_input_text)
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

        extracted_tasks = extract_tasks_from_transcript(llm_input_text)
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

        _update_job_and_meeting(
            db=db,
            meeting_id=meeting_id,
            stage="done",
            job_status="done",
            meeting_status="done",
        )
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
        raise
    finally:
        db.close()
