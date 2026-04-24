import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

os.environ["DATABASE_URL"] = "sqlite+pysqlite:///./test_integration.db"
os.environ["REDIS_URL"] = "redis://localhost:6379/0"
os.environ["CELERY_BROKER_URL"] = "redis://localhost:6379/0"
os.environ["CELERY_RESULT_BACKEND"] = "redis://localhost:6379/1"

from app.infrastructure.config.settings import settings
from app.infrastructure.db.base import Base
from app.infrastructure.db.models import ProcessingJob
from app.infrastructure.db.models import TaskItem
from app.infrastructure.db.models import Transcript
from app.infrastructure.db.models import TranscriptSegment
from app.infrastructure.db.session import SessionLocal
from app.infrastructure.db.session import engine
from app.main import app
from app.workers.tasks import process_meeting_pipeline


@pytest.fixture(autouse=True)
def reset_db(monkeypatch):
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    settings.storage_path = "test_storage"
    Path(settings.storage_path).mkdir(parents=True, exist_ok=True)

    def fake_send_task(name: str, args: list[str], queue: str) -> None:
        _ = (name, args, queue)

    monkeypatch.setattr("app.api.routes.celery_app.send_task", fake_send_task)
    yield


def test_video_upload_pipeline_and_transcript_summary_endpoints(monkeypatch) -> None:
    calls = {"audio_extracted": False}

    def fake_prepare_audio_file(media_file_path: str) -> str:
        assert media_file_path.endswith(".mp4")
        calls["audio_extracted"] = True
        return str(Path(media_file_path).with_suffix(".wav"))

    def fake_transcribe_audio_file(audio_file_path: str) -> tuple[str, list]:
        assert audio_file_path.endswith(".wav")
        return "Обсудили запуск кампании и следующие шаги.", [
            type(
                "SttSeg",
                (),
                {
                    "start_sec": 0.0,
                    "end_sec": 4.0,
                    "text": "Запускаем кампанию в мае",
                },
            )(),
            type(
                "SttSeg",
                (),
                {
                    "start_sec": 4.0,
                    "end_sec": 8.0,
                    "text": "Подготовить медиаплан до понедельника",
                },
            )(),
        ]

    def fake_diarize_audio_file(_: str) -> list:
        return [
            type(
                "Diar",
                (),
                {
                    "speaker_label": "SPEAKER_01",
                    "start_sec": 0.0,
                    "end_sec": 5.0,
                },
            )(),
            type(
                "Diar",
                (),
                {
                    "speaker_label": "SPEAKER_02",
                    "start_sec": 5.0,
                    "end_sec": 10.0,
                },
            )(),
        ]

    def fake_summarize_transcript_text(transcript_text: str) -> str:
        assert "SPEAKER_01" in transcript_text
        return "Команда согласовала запуск кампании и сроки."

    def fake_extract_tasks_from_transcript(transcript_text: str) -> list:
        assert "SPEAKER_02" in transcript_text
        return [
            type(
                "ExtractedTask",
                (),
                {
                    "description": "Подготовить медиаплан",
                    "assignee_speaker_label": "SPEAKER_02",
                    "due_date": "2026-05-10",
                    "priority": "high",
                    "source_quote": "Подготовить медиаплан до понедельника",
                    "confidence": 0.88,
                },
            )(),
        ]

    def fake_send_task(name: str, args: list[str], queue: str) -> None:
        assert name == "app.workers.tasks.process_meeting_pipeline"
        assert queue == "meetings"
        monkeypatch.setattr(
            "app.workers.tasks.prepare_audio_file",
            fake_prepare_audio_file,
        )
        monkeypatch.setattr(
            "app.workers.tasks.transcribe_audio_file",
            fake_transcribe_audio_file,
        )
        monkeypatch.setattr(
            "app.workers.tasks.diarize_audio_file",
            fake_diarize_audio_file,
        )
        monkeypatch.setattr(
            "app.workers.tasks.summarize_transcript_text",
            fake_summarize_transcript_text,
        )
        monkeypatch.setattr(
            "app.workers.tasks.extract_tasks_from_transcript",
            fake_extract_tasks_from_transcript,
        )
        process_meeting_pipeline(args[0])

    monkeypatch.setattr("app.api.routes.celery_app.send_task", fake_send_task)
    client = TestClient(app)

    upload_response = client.post(
        "/api/v1/meetings/upload",
        files={"file": ("meeting.mp4", b"video-binary", "video/mp4")},
    )
    assert upload_response.status_code == 200
    meeting_id = upload_response.json()["meeting_id"]

    status_response = client.get(f"/api/v1/meetings/{meeting_id}")
    assert status_response.status_code == 200
    status_payload = status_response.json()
    assert status_payload["stage"] == "done"
    assert status_payload["job_status"] == "done"
    assert calls["audio_extracted"] is True

    transcript_response = client.get(f"/api/v1/meetings/{meeting_id}/transcript")
    assert transcript_response.status_code == 200
    transcript_payload = transcript_response.json()
    assert transcript_payload["meeting_id"] == meeting_id
    assert "кампании" in transcript_payload["transcript"]
    assert transcript_payload["provider"] == "faster-whisper"

    summary_response = client.get(f"/api/v1/meetings/{meeting_id}/summary")
    assert summary_response.status_code == 200
    summary_payload = summary_response.json()
    assert summary_payload["meeting_id"] == meeting_id
    assert "кампании" in summary_payload["summary"]

    segments_response = client.get(f"/api/v1/meetings/{meeting_id}/segments")
    assert segments_response.status_code == 200
    segments_payload = segments_response.json()
    assert len(segments_payload["segments"]) == 2
    assert segments_payload["segments"][0]["speaker"] == "SPEAKER_01"
    assert segments_payload["segments"][1]["speaker"] == "SPEAKER_02"

    tasks_response = client.get(f"/api/v1/meetings/{meeting_id}/tasks")
    assert tasks_response.status_code == 200
    tasks_payload = tasks_response.json()
    assert len(tasks_payload["tasks"]) == 1
    assert tasks_payload["tasks"][0]["assignee_speaker_label"] == "SPEAKER_02"

    db = SessionLocal()
    try:
        transcript = db.scalar(
            select(Transcript).where(Transcript.meeting_id == meeting_id)
        )
        transcript_segments = db.scalars(
            select(TranscriptSegment).join(
                Transcript,
                Transcript.id == TranscriptSegment.transcript_id,
            )
            .where(Transcript.meeting_id == meeting_id)
            .order_by(TranscriptSegment.start_sec)
        ).all()
        task = db.scalar(select(TaskItem).where(TaskItem.meeting_id == meeting_id))
        job = db.scalar(
            select(ProcessingJob).where(ProcessingJob.meeting_id == meeting_id)
        )
        assert transcript is not None
        assert len(transcript_segments) == 2
        assert task is not None
        assert job is not None
        assert job.stage == "done"
    finally:
        db.close()
