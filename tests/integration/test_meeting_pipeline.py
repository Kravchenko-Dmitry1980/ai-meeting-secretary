import os
from pathlib import Path

os.environ["DATABASE_URL"] = "sqlite+pysqlite:///./test_integration.db"
os.environ["REDIS_URL"] = "redis://localhost:6379/0"
os.environ["CELERY_BROKER_URL"] = "redis://localhost:6379/0"
os.environ["CELERY_RESULT_BACKEND"] = "redis://localhost:6379/1"

from fastapi.testclient import TestClient
from sqlalchemy import select
import pytest

from app.infrastructure.config.settings import settings
from app.infrastructure.db.base import Base
from app.infrastructure.db.models import Meeting
from app.infrastructure.db.models import MeetingSummary
from app.infrastructure.db.models import ProcessingJob
from app.infrastructure.db.models import TaskItem
from app.infrastructure.db.models import Transcript
from app.infrastructure.db.session import SessionLocal
from app.infrastructure.db.session import engine
from app.main import app
from app.workers.tasks import process_meeting_pipeline


def _cleanup_sqlite_db() -> None:
    sqlite_path = Path("test_integration.db")
    if sqlite_path.exists():
        sqlite_path.unlink()


def setup_module() -> None:
    _cleanup_sqlite_db()
    Base.metadata.create_all(bind=engine)


def teardown_module() -> None:
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def reset_db_and_storage(monkeypatch):
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    settings.storage_path = "test_storage"
    Path(settings.storage_path).mkdir(parents=True, exist_ok=True)

    def fake_send_task(name: str, args: list[str], queue: str) -> None:
        _ = (name, args, queue)

    monkeypatch.setattr("app.api.routes.celery_app.send_task", fake_send_task)
    yield


def test_upload_file() -> None:
    client = TestClient(app)
    response = client.post(
        "/api/v1/meetings/upload",
        files={"file": ("meeting.mp3", b"fake-audio-bytes", "audio/mpeg")},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["meeting_id"]
    assert payload["processing_job_id"]
    assert payload["status"] == "queued"


def test_meeting_created_after_upload() -> None:
    client = TestClient(app)
    response = client.post(
        "/api/v1/meetings/upload",
        files={"file": ("meeting.wav", b"fake-wav-bytes", "audio/wav")},
    )
    meeting_id = response.json()["meeting_id"]

    db = SessionLocal()
    try:
        meeting = db.scalar(select(Meeting).where(Meeting.id == meeting_id))
        assert meeting is not None
        assert meeting.status == "uploaded"
        assert meeting.source_type == "upload"
    finally:
        db.close()


def test_celery_pipeline_started(monkeypatch) -> None:
    called = {"value": False}

    def fake_prepare_audio_file(media_file_path: str) -> str:
        return media_file_path

    def fake_transcribe_audio_file(_: str) -> tuple[str, list]:
        return "Тестовая расшифровка встречи.", [
            type(
                "SttSeg",
                (),
                {"start_sec": 0.0, "end_sec": 2.0, "text": "Привет команда"},
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
        ]

    def fake_summarize_transcript_text(_: str) -> str:
        return "Короткое тестовое саммари."

    def fake_extract_tasks_from_transcript(_: str) -> list:
        return [
            type(
                "ExtractedTask",
                (),
                {
                    "description": "Подготовить КП",
                    "assignee_speaker_label": "SPEAKER_01",
                    "due_date": "2026-05-01",
                    "priority": "high",
                    "source_quote": "Подготовить КП к пятнице",
                    "confidence": 0.9,
                },
            )(),
        ]

    def fake_send_task(name: str, args: list[str], queue: str) -> None:
        called["value"] = True
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
    response = client.post(
        "/api/v1/meetings/upload",
        files={"file": ("meeting.mp4", b"fake-video-bytes", "video/mp4")},
    )
    meeting_id = response.json()["meeting_id"]

    assert called["value"] is True

    db = SessionLocal()
    try:
        job = db.scalar(
            select(ProcessingJob).where(ProcessingJob.meeting_id == meeting_id)
        )
        transcript = db.scalar(
            select(Transcript).where(Transcript.meeting_id == meeting_id)
        )
        summary = db.scalar(
            select(MeetingSummary).where(MeetingSummary.meeting_id == meeting_id)
        )
        task = db.scalar(select(TaskItem).where(TaskItem.meeting_id == meeting_id))
        assert job is not None
        assert job.stage == "done"
        assert job.status == "done"
        assert transcript is not None
        assert summary is not None
        assert task is not None
    finally:
        db.close()


def test_get_processing_status(monkeypatch) -> None:
    def fake_prepare_audio_file(media_file_path: str) -> str:
        return media_file_path

    def fake_transcribe_audio_file(_: str) -> tuple[str, list]:
        return "Еще один тестовый транскрипт.", [
            type(
                "SttSeg",
                (),
                {"start_sec": 0.0, "end_sec": 2.0, "text": "Следующий шаг"},
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
        ]

    def fake_summarize_transcript_text(_: str) -> str:
        return "Еще одно тестовое summary."

    def fake_extract_tasks_from_transcript(_: str) -> list:
        return []

    def fake_send_task(name: str, args: list[str], queue: str) -> None:
        _ = (name, queue)
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
        files={"file": ("meeting_status.mp3", b"audio", "audio/mpeg")},
    )
    meeting_id = upload_response.json()["meeting_id"]

    status_response = client.get(f"/api/v1/meetings/{meeting_id}")
    assert status_response.status_code == 200

    payload = status_response.json()
    assert payload["meeting_id"] == meeting_id
    assert payload["meeting_status"] == "done"
    assert payload["job_status"] == "done"
    assert payload["stage"] == "done"
