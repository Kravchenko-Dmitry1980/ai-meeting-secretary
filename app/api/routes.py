import uuid
from pathlib import Path

from fastapi import APIRouter
from fastapi import Depends
from fastapi import File
from fastapi import Header
from fastapi import HTTPException
from fastapi import UploadFile
from redis import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.schemas import HealthResponse
from app.api.schemas import MeetingStatusResponse
from app.api.schemas import SegmentsResponse
from app.api.schemas import SummaryResponse
from app.api.schemas import TasksResponse
from app.api.schemas import TranscriptResponse
from app.api.schemas import UploadMeetingResponse
from app.infrastructure.config.settings import settings
from app.infrastructure.db.models import Meeting
from app.infrastructure.db.models import MeetingSummary
from app.infrastructure.db.models import ProcessingJob
from app.infrastructure.db.models import Speaker
from app.infrastructure.db.models import TaskItem
from app.infrastructure.db.models import Transcript
from app.infrastructure.db.models import TranscriptSegment
from app.infrastructure.db.models import User
from app.infrastructure.db.session import get_db_session
from app.infrastructure.queue.celery_app import celery_app
from app.workers.tasks import process_meeting_pipeline

router = APIRouter()
ALLOWED_UPLOAD_EXTENSIONS = {".mp3", ".wav", ".mp4", ".mkv"}
UPLOAD_CHUNK_SIZE_BYTES = 1024 * 1024


def get_current_user(
    x_user_email: str | None = Header(default=None, alias="X-User-Email"),
    db: Session = Depends(get_db_session),
) -> User:
    if not x_user_email:
        raise HTTPException(status_code=401, detail="Missing X-User-Email header")

    user = db.scalar(select(User).where(User.email == x_user_email))
    if user is None:
        raise HTTPException(status_code=403, detail="User not found")
    return user


def _get_user_meeting_or_404(db: Session, meeting_id: str, user_id: str) -> Meeting:
    meeting = db.scalar(
        select(Meeting).where(Meeting.id == meeting_id, Meeting.owner_id == user_id)
    )
    if meeting is not None:
        return meeting
    raise HTTPException(status_code=404, detail="Meeting not found")


@router.get("/health", response_model=HealthResponse)
def healthcheck(db: Session = Depends(get_db_session)) -> HealthResponse:
    db.execute(select(1))
    redis_client = Redis.from_url(settings.redis_url)
    redis_client.ping()
    return HealthResponse(status="ok", db="ok", redis="ok")


@router.post("/api/v1/meetings/upload", response_model=UploadMeetingResponse)
def upload_meeting_file(
    file: UploadFile = File(...),
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
) -> UploadMeetingResponse:
    storage_dir = Path(settings.storage_path)
    storage_dir.mkdir(parents=True, exist_ok=True)

    meeting_id = str(uuid.uuid4())
    file_extension = Path(file.filename or "meeting.bin").suffix.lower()
    if file_extension not in ALLOWED_UPLOAD_EXTENSIONS:
        raise HTTPException(status_code=415, detail="Unsupported file extension")

    target_name = f"{meeting_id}{file_extension}"
    target_path = storage_dir / target_name

    max_upload_size_bytes = settings.max_upload_size_mb * 1024 * 1024
    uploaded_size = 0
    with target_path.open("wb") as target_file:
        while True:
            chunk = file.file.read(UPLOAD_CHUNK_SIZE_BYTES)
            if not chunk:
                break
            uploaded_size += len(chunk)
            if uploaded_size > max_upload_size_bytes:
                target_file.close()
                target_path.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="File too large")
            target_file.write(chunk)

    meeting = Meeting(
        id=meeting_id,
        owner_id=current_user.id,
        title=file.filename or "Uploaded meeting",
        source_type="upload",
        status="uploaded",
        media_file_path=str(target_path),
    )
    db.add(meeting)
    db.commit()

    processing_job = ProcessingJob(
        meeting_id=meeting_id,
        stage="uploaded",
        status="queued",
    )
    db.add(processing_job)
    db.commit()
    db.refresh(processing_job)

    celery_app.send_task(
        process_meeting_pipeline.name,
        args=[meeting_id],
        queue="meetings",
    )
    return UploadMeetingResponse(
        meeting_id=meeting_id,
        processing_job_id=processing_job.id,
        status="queued",
    )


@router.get("/api/v1/meetings/{meeting_id}", response_model=MeetingStatusResponse)
def get_meeting_status(
    meeting_id: str,
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
) -> MeetingStatusResponse:
    meeting = _get_user_meeting_or_404(db, meeting_id, current_user.id)

    job = db.scalar(
        select(ProcessingJob).where(ProcessingJob.meeting_id == meeting_id)
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Processing job not found")

    return MeetingStatusResponse(
        meeting_id=meeting.id,
        meeting_status=meeting.status,
        job_status=job.status,
        stage=job.stage,
        error=job.error,
    )


@router.get(
    "/api/v1/meetings/{meeting_id}/transcript",
    response_model=TranscriptResponse,
)
def get_meeting_transcript(
    meeting_id: str,
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
) -> TranscriptResponse:
    _get_user_meeting_or_404(db, meeting_id, current_user.id)
    transcript = db.scalar(
        select(Transcript)
        .where(Transcript.meeting_id == meeting_id)
        .order_by(Transcript.created_at.desc())
    )
    if transcript is None:
        raise HTTPException(status_code=404, detail="Transcript not found")

    return TranscriptResponse(
        meeting_id=meeting_id,
        transcript=transcript.full_text,
        provider=transcript.provider,
        language=transcript.language,
    )


@router.get("/api/v1/meetings/{meeting_id}/summary", response_model=SummaryResponse)
def get_meeting_summary(
    meeting_id: str,
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
) -> SummaryResponse:
    _get_user_meeting_or_404(db, meeting_id, current_user.id)
    summary = db.scalar(
        select(MeetingSummary)
        .where(MeetingSummary.meeting_id == meeting_id)
        .order_by(MeetingSummary.created_at.desc())
    )
    if summary is None:
        raise HTTPException(status_code=404, detail="Summary not found")

    return SummaryResponse(meeting_id=meeting_id, summary=summary.summary_text)


@router.get(
    "/api/v1/meetings/{meeting_id}/segments",
    response_model=SegmentsResponse,
)
def get_meeting_segments(
    meeting_id: str,
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
) -> SegmentsResponse:
    _get_user_meeting_or_404(db, meeting_id, current_user.id)

    rows = db.execute(
        select(
            TranscriptSegment.start_sec,
            TranscriptSegment.end_sec,
            TranscriptSegment.text,
            Speaker.speaker_label,
        )
        .join(Transcript, Transcript.id == TranscriptSegment.transcript_id)
        .join(Speaker, Speaker.id == TranscriptSegment.speaker_id, isouter=True)
        .where(Transcript.meeting_id == meeting_id)
    ).all()
    if not rows:
        raise HTTPException(status_code=404, detail="Segments not found")

    sorted_rows = sorted(rows, key=lambda row: row.start_sec)
    return SegmentsResponse(
        meeting_id=meeting_id,
        segments=[
            {
                "speaker": row.speaker_label or "UNKNOWN",
                "start_sec": row.start_sec,
                "end_sec": row.end_sec,
                "text": row.text,
            }
            for row in sorted_rows
        ],
    )


@router.get("/api/v1/meetings/{meeting_id}/tasks", response_model=TasksResponse)
def get_meeting_tasks(
    meeting_id: str,
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
) -> TasksResponse:
    _get_user_meeting_or_404(db, meeting_id, current_user.id)

    tasks = db.scalars(
        select(TaskItem)
        .where(TaskItem.meeting_id == meeting_id)
        .order_by(TaskItem.id)
    ).all()
    if not tasks:
        raise HTTPException(status_code=404, detail="Tasks not found")

    return TasksResponse(
        meeting_id=meeting_id,
        tasks=[
            {
                "id": task.id,
                "description": task.description,
                "assignee_speaker_label": task.assignee_speaker_label,
                "due_date": task.due_date,
                "priority": task.priority,
                "source_quote": task.source_quote,
                "confidence": task.confidence,
            }
            for task in tasks
        ],
    )
