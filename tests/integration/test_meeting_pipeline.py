import os
from time import perf_counter
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

os.environ["DATABASE_URL"] = "sqlite+pysqlite:///./test_integration.db"
os.environ["REDIS_URL"] = "redis://localhost:6379/0"
os.environ["CELERY_BROKER_URL"] = "redis://localhost:6379/0"
os.environ["CELERY_RESULT_BACKEND"] = "redis://localhost:6379/1"
os.environ["APP_API_KEY"] = "test-api-key"
os.environ["MAX_UPLOAD_SIZE_MB"] = "1"

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
from app.infrastructure.db.models import User
from app.infrastructure.db.session import SessionLocal
from app.infrastructure.db.session import engine
from app.main import app
from app.models.memory import MemoryItem
from app.services.memory_service import apply_auto_validation
from app.services.memory_service import build_active_tasks_context
from app.services.memory_service import calculate_attention_score
from app.services.memory_service import parse_extracted_tasks_from_llm_output
from app.workers.tasks import process_meeting_pipeline


def _auth_headers(user_email: str) -> dict[str, str]:
    return {"X-User-Email": user_email, "X-API-Key": settings.app_api_key}


def _cleanup_sqlite_db() -> None:
    sqlite_path = Path("test_integration.db")
    if sqlite_path.exists():
        sqlite_path.unlink()


def _create_test_user(email: str = "user@example.com") -> str:
    db = SessionLocal()
    try:
        user = User(
            email=email,
            full_name="Test User",
            hashed_password="hashed-password",
            is_active=True,
        )
        db.add(user)
        db.commit()
        return email
    finally:
        db.close()


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
    user_email = _create_test_user()
    client = TestClient(app)
    response = client.post(
        "/api/v1/meetings/upload",
        files={"file": ("meeting.mp3", b"fake-audio-bytes", "audio/mpeg")},
        headers=_auth_headers(user_email),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["meeting_id"]
    assert payload["processing_job_id"]
    assert payload["status"] == "queued"


def test_meeting_created_after_upload() -> None:
    user_email = _create_test_user()
    client = TestClient(app)
    response = client.post(
        "/api/v1/meetings/upload",
        files={"file": ("meeting.wav", b"fake-wav-bytes", "audio/wav")},
        headers=_auth_headers(user_email),
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

    user_email = _create_test_user()
    monkeypatch.setattr("app.api.routes.celery_app.send_task", fake_send_task)
    client = TestClient(app)
    response = client.post(
        "/api/v1/meetings/upload",
        files={"file": ("meeting.mp4", b"fake-video-bytes", "video/mp4")},
        headers=_auth_headers(user_email),
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

    user_email = _create_test_user()
    monkeypatch.setattr("app.api.routes.celery_app.send_task", fake_send_task)
    client = TestClient(app)
    upload_response = client.post(
        "/api/v1/meetings/upload",
        files={"file": ("meeting_status.mp3", b"audio", "audio/mpeg")},
        headers=_auth_headers(user_email),
    )
    meeting_id = upload_response.json()["meeting_id"]

    status_response = client.get(
        f"/api/v1/meetings/{meeting_id}",
        headers=_auth_headers(user_email),
    )
    assert status_response.status_code == 200

    payload = status_response.json()
    assert payload["meeting_id"] == meeting_id
    assert payload["meeting_status"] == "done"
    assert payload["job_status"] == "done"
    assert payload["stage"] == "done"


def test_upload_unsupported_extension_returns_415() -> None:
    user_email = _create_test_user()
    client = TestClient(app)

    response = client.post(
        "/api/v1/meetings/upload",
        files={"file": ("meeting.txt", b"not-supported", "text/plain")},
        headers=_auth_headers(user_email),
    )

    assert response.status_code == 415


def test_upload_oversize_returns_413() -> None:
    user_email = _create_test_user()
    client = TestClient(app)
    oversized_payload = b"a" * (settings.max_upload_size_mb * 1024 * 1024 + 1)

    response = client.post(
        "/api/v1/meetings/upload",
        files={"file": ("big.mp3", oversized_payload, "audio/mpeg")},
        headers=_auth_headers(user_email),
    )

    assert response.status_code == 413


def test_missing_api_key_returns_401() -> None:
    user_email = _create_test_user()
    client = TestClient(app)

    response = client.post(
        "/api/v1/meetings/upload",
        files={"file": ("meeting.mp3", b"fake-audio-bytes", "audio/mpeg")},
        headers={"X-User-Email": user_email},
    )

    assert response.status_code == 401


def test_wrong_api_key_returns_403() -> None:
    user_email = _create_test_user()
    client = TestClient(app)

    response = client.post(
        "/api/v1/meetings/upload",
        files={"file": ("meeting.mp3", b"fake-audio-bytes", "audio/mpeg")},
        headers={"X-User-Email": user_email, "X-API-Key": "wrong-key"},
    )

    assert response.status_code == 403


def test_memory_extract_endpoint() -> None:
    user_email = _create_test_user()
    client = TestClient(app)

    def fake_extract_tasks(_: str) -> list[str]:
        return ["Иван подготовит отчет к пятнице", "Мария отправит клиенту договор"]

    import app.api.memory as memory_api

    original = memory_api.extract_tasks
    memory_api.extract_tasks = fake_extract_tasks
    try:
        response = client.post(
            "/memory/extract",
            json={
                "text": (
                    "На встрече решили: Иван подготовит отчет к пятнице, "
                    "Мария отправит клиенту договор."
                )
            },
            headers=_auth_headers(user_email),
        )
    finally:
        memory_api.extract_tasks = original

    assert response.status_code == 200
    assert response.json() == {
        "tasks": ["Иван подготовит отчет к пятнице", "Мария отправит клиенту договор"]
    }


def test_memory_tasks_endpoint_returns_pending_only() -> None:
    user_email = _create_test_user()
    db = SessionLocal()
    try:
        owner = db.scalar(select(User).where(User.email == user_email))
        assert owner is not None
        db.add(
            MemoryItem(
                user_id=owner.id,
                content="Подготовить демо к понедельнику",
                status="pending",
                source="manual",
                validated=True,
            )
        )
        db.add(
            MemoryItem(
                user_id=owner.id,
                content="Закрыть старую задачу",
                status="done",
                source="manual",
            )
        )
        db.commit()
    finally:
        db.close()

    client = TestClient(app)
    response = client.get("/memory/tasks", headers=_auth_headers(user_email))

    assert response.status_code == 200
    payload = response.json()
    assert "tasks" in payload
    assert len(payload["tasks"]) == 1
    assert payload["tasks"][0]["content"] == "Подготовить демо к понедельнику"
    assert payload["tasks"][0]["status"] == "pending"
    assert "confidence" in payload["tasks"][0]
    assert "priority" in payload["tasks"][0]
    assert "deadline_at" in payload["tasks"][0]


def test_memory_done_endpoint_marks_task_done() -> None:
    user_email = _create_test_user()
    db = SessionLocal()
    try:
        owner = db.scalar(select(User).where(User.email == user_email))
        assert owner is not None
        task = MemoryItem(
            user_id=owner.id,
            content="Позвонить клиенту",
            status="pending",
            source="manual",
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        task_id = task.id
    finally:
        db.close()

    client = TestClient(app)
    response = client.post(f"/memory/tasks/{task_id}/done", headers=_auth_headers(user_email))
    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == task_id
    assert payload["status"] == "done"
    assert payload["completed_at"] is not None


def test_memory_approve_endpoint_sets_validated_true() -> None:
    user_email = _create_test_user()
    db = SessionLocal()
    try:
        owner = db.scalar(select(User).where(User.email == user_email))
        assert owner is not None
        task = MemoryItem(
            user_id=owner.id,
            content="Проверить презентацию",
            status="pending",
            confidence=0.4,
            validated=False,
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        task_id = task.id
    finally:
        db.close()

    client = TestClient(app)
    response = client.post(
        f"/memory/tasks/{task_id}/approve",
        headers=_auth_headers(user_email),
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["id"] == task_id
    assert payload["validated"] is True
    assert payload["validated_at"] is not None


def test_memory_report_endpoint_returns_counts() -> None:
    user_email = _create_test_user()
    now = datetime.now(UTC)
    db = SessionLocal()
    try:
        owner = db.scalar(select(User).where(User.email == user_email))
        assert owner is not None
        db.add_all(
            [
                MemoryItem(
                    user_id=owner.id,
                    content="Срочная задача",
                    status="pending",
                    priority="urgent",
                    confidence=0.7,
                    validated=True,
                    deadline_at=now + timedelta(days=2),
                ),
                MemoryItem(
                    user_id=owner.id,
                    content="Просроченная high",
                    status="pending",
                    priority="high",
                    confidence=0.5,
                    validated=False,
                    deadline_at=now - timedelta(days=1),
                ),
                MemoryItem(
                    user_id=owner.id,
                    content="Сделанная задача",
                    status="done",
                    priority="medium",
                    confidence=0.9,
                ),
            ]
        )
        db.commit()
    finally:
        db.close()

    client = TestClient(app)
    response = client.get("/memory/report", headers=_auth_headers(user_email))
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 3
    assert payload["pending"] == 2
    assert payload["done"] == 1
    assert payload["overdue"] == 1
    assert payload["high_priority"] == 1
    assert payload["urgent"] == 1
    assert payload["upcoming_deadlines"] == 1
    assert payload["low_confidence_pending"] == 1
    assert payload["validated_pending"] == 1
    assert payload["unvalidated_pending"] == 1
    assert payload["generated_at"]


def test_auto_validation_sets_validated_true() -> None:
    item = MemoryItem(
        user_id="u1",
        content="Сделать релиз",
        status="pending",
        confidence=0.9,
    )
    apply_auto_validation(item)
    assert item.validated is True
    assert item.validation_source == "auto"
    assert item.validated_at is not None


def test_auto_validation_sets_validated_false() -> None:
    item = MemoryItem(
        user_id="u1",
        content="Черновая задача",
        status="pending",
        confidence=0.5,
    )
    apply_auto_validation(item)
    assert item.validated is False
    assert item.validation_source is None
    assert item.validated_at is None


def test_context_uses_only_validated_tasks() -> None:
    user_email = _create_test_user()
    db = SessionLocal()
    try:
        owner = db.scalar(select(User).where(User.email == user_email))
        assert owner is not None
        db.add(
            MemoryItem(
                user_id=owner.id,
                content="Валидированная задача",
                status="pending",
                confidence=0.9,
                validated=True,
                priority="high",
            )
        )
        db.add(
            MemoryItem(
                user_id=owner.id,
                content="Невалидированная задача",
                status="pending",
                confidence=0.9,
                validated=False,
                priority="urgent",
            )
        )
        db.commit()
        context_text = build_active_tasks_context(owner.id, db, limit=10)
    finally:
        db.close()

    assert "Валидированная задача" in context_text
    assert "Невалидированная задача" not in context_text


def test_overdue_detection() -> None:
    user_email = _create_test_user()
    now = datetime.now(UTC)
    db = SessionLocal()
    try:
        owner = db.scalar(select(User).where(User.email == user_email))
        assert owner is not None
        db.add(
            MemoryItem(
                user_id=owner.id,
                content="Просроченная задача",
                status="pending",
                confidence=0.9,
                validated=True,
                deadline_at=now - timedelta(hours=2),
            )
        )
        db.commit()
    finally:
        db.close()

    client = TestClient(app)
    report_response = client.get("/memory/report", headers=_auth_headers(user_email))
    assert report_response.status_code == 200
    assert report_response.json()["overdue"] == 1


def test_memory_parser_handles_json_objects() -> None:
    payload = (
        '[{"content":"Подготовить договор","confidence":0.9,"priority":"high",'
        '"deadline":"2026-05-10T10:00:00"}]'
    )
    parsed = parse_extracted_tasks_from_llm_output(payload)
    assert len(parsed) == 1
    assert parsed[0]["content"] == "подготовить договор"
    assert parsed[0]["priority"] == "high"
    assert parsed[0]["confidence"] == 0.9
    assert parsed[0]["deadline_at"] is not None


def test_memory_parser_handles_legacy_string_array() -> None:
    parsed = parse_extracted_tasks_from_llm_output('["Согласовать ТЗ"]')
    assert len(parsed) == 1
    assert parsed[0]["content"] == "согласовать тз"
    assert parsed[0]["priority"] == "medium"
    assert parsed[0]["confidence"] == 0.5
    assert parsed[0]["deadline_at"] is None


def test_memory_parser_handles_invalid_json() -> None:
    parsed = parse_extracted_tasks_from_llm_output("not-json")
    assert parsed == []


def test_memory_parser_normalizes_invalid_priority_and_confidence() -> None:
    payload = '[{"content":"Сделать задачу","confidence":2.7,"priority":"critical","deadline":"invalid"}]'
    parsed = parse_extracted_tasks_from_llm_output(payload)
    assert len(parsed) == 1
    assert parsed[0]["priority"] == "medium"
    assert parsed[0]["confidence"] == 1.0
    assert parsed[0]["deadline_at"] is None


def test_issues_endpoint_returns_categories() -> None:
    user_email = _create_test_user()
    now = datetime.now(UTC)
    db = SessionLocal()
    try:
        owner = db.scalar(select(User).where(User.email == user_email))
        assert owner is not None
        db.add_all(
            [
                MemoryItem(
                    user_id=owner.id,
                    content="Невалидированная задача",
                    status="pending",
                    confidence=0.9,
                    validated=False,
                ),
                MemoryItem(
                    user_id=owner.id,
                    content="Низкая уверенность",
                    status="pending",
                    confidence=0.5,
                    validated=True,
                ),
                MemoryItem(
                    user_id=owner.id,
                    content="Просроченная задача",
                    status="pending",
                    confidence=0.9,
                    validated=True,
                    deadline_at=now - timedelta(days=1),
                ),
            ]
        )
        db.commit()
    finally:
        db.close()

    client = TestClient(app)
    response = client.get("/memory/issues", headers=_auth_headers(user_email))
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["unvalidated_tasks"]) == 1
    assert len(payload["low_confidence_tasks"]) == 1
    assert len(payload["overdue_tasks"]) == 1
    assert payload["needs_attention"] == 3


def test_issues_needs_attention_sum_of_unique_classified_tasks() -> None:
    user_email = _create_test_user()
    now = datetime.now(UTC)
    db = SessionLocal()
    try:
        owner = db.scalar(select(User).where(User.email == user_email))
        assert owner is not None
        db.add_all(
            [
                MemoryItem(
                    user_id=owner.id,
                    content="overdue 1",
                    status="pending",
                    confidence=0.9,
                    validated=True,
                    deadline_at=now - timedelta(days=1),
                ),
                MemoryItem(
                    user_id=owner.id,
                    content="overdue 2",
                    status="pending",
                    confidence=0.8,
                    validated=True,
                    deadline_at=now - timedelta(hours=3),
                ),
                MemoryItem(
                    user_id=owner.id,
                    content="unvalidated 1",
                    status="pending",
                    confidence=0.9,
                    validated=False,
                ),
                MemoryItem(
                    user_id=owner.id,
                    content="unvalidated 2",
                    status="pending",
                    confidence=0.9,
                    validated=False,
                ),
                MemoryItem(
                    user_id=owner.id,
                    content="unvalidated 3",
                    status="pending",
                    confidence=0.7,
                    validated=False,
                ),
                MemoryItem(
                    user_id=owner.id,
                    content="low confidence 1",
                    status="pending",
                    confidence=0.4,
                    validated=True,
                ),
            ]
        )
        db.commit()
    finally:
        db.close()

    client = TestClient(app)
    response = client.get("/memory/issues", headers=_auth_headers(user_email))
    assert response.status_code == 200
    payload = response.json()
    assert payload["overdue_total"] == 2
    assert payload["unvalidated_total"] == 3
    assert payload["low_confidence_total"] == 1
    assert payload["needs_attention"] == 6
    assert payload["needs_attention"] == (
        payload["overdue_total"]
        + payload["unvalidated_total"]
        + payload["low_confidence_total"]
    )


def test_validation_rules_endpoint() -> None:
    client = TestClient(app)
    user_email = _create_test_user()
    response = client.get("/memory/validation-rules", headers=_auth_headers(user_email))
    assert response.status_code == 200
    payload = response.json()
    assert payload["auto_validate_threshold"] == 0.75
    assert payload["context_threshold"] == 0.55
    assert len(payload["rules"]) == 3


def test_report_contains_action_required() -> None:
    user_email = _create_test_user()
    now = datetime.now(UTC)
    db = SessionLocal()
    try:
        owner = db.scalar(select(User).where(User.email == user_email))
        assert owner is not None
        db.add(
            MemoryItem(
                user_id=owner.id,
                content="Просроченная",
                status="pending",
                confidence=0.9,
                validated=True,
                deadline_at=now - timedelta(hours=1),
            )
        )
        db.commit()
    finally:
        db.close()

    client = TestClient(app)
    response = client.get("/memory/report", headers=_auth_headers(user_email))
    assert response.status_code == 200
    payload = response.json()
    assert payload["action_required"] is True
    assert payload["main_problem"] == "1 overdue tasks"


def test_hidden_tasks_count() -> None:
    user_email = _create_test_user()
    db = SessionLocal()
    try:
        owner = db.scalar(select(User).where(User.email == user_email))
        assert owner is not None
        db.add_all(
            [
                MemoryItem(
                    user_id=owner.id,
                    content="Hidden 1",
                    status="pending",
                    validated=False,
                ),
                MemoryItem(
                    user_id=owner.id,
                    content="Hidden 2",
                    status="pending",
                    validated=False,
                ),
                MemoryItem(
                    user_id=owner.id,
                    content="Visible",
                    status="pending",
                    validated=True,
                ),
            ]
        )
        db.commit()
    finally:
        db.close()

    client = TestClient(app)
    response = client.get("/memory/report", headers=_auth_headers(user_email))
    assert response.status_code == 200
    assert response.json()["hidden_tasks"] == 2


def test_main_problem_logic() -> None:
    user_email = _create_test_user()
    db = SessionLocal()
    try:
        owner = db.scalar(select(User).where(User.email == user_email))
        assert owner is not None
        for idx in range(4):
            db.add(
                MemoryItem(
                    user_id=owner.id,
                    content=f"Need validation {idx}",
                    status="pending",
                    confidence=0.8,
                    validated=False,
                )
            )
        db.commit()
    finally:
        db.close()

    client = TestClient(app)
    response = client.get("/memory/report", headers=_auth_headers(user_email))
    assert response.status_code == 200
    payload = response.json()
    assert payload["action_required"] is True
    assert payload["main_problem"] == "too many unvalidated tasks"


def test_issues_debug_mode_returns_reason() -> None:
    user_email = _create_test_user()
    now = datetime.now(UTC)
    db = SessionLocal()
    try:
        owner = db.scalar(select(User).where(User.email == user_email))
        assert owner is not None
        db.add(
            MemoryItem(
                user_id=owner.id,
                content="Debug overdue",
                status="pending",
                confidence=0.9,
                validated=True,
                deadline_at=now - timedelta(days=1),
            )
        )
        db.commit()
    finally:
        db.close()

    client = TestClient(app)
    response = client.get("/memory/issues?debug=true", headers=_auth_headers(user_email))
    assert response.status_code == 200
    payload = response.json()
    assert payload["overdue_tasks"]
    assert payload["overdue_tasks"][0]["reason"] == "overdue"


def test_attention_score_calculation() -> None:
    score = calculate_attention_score(
        {
            "overdue": 2,
            "unvalidated_pending": 3,
            "low_confidence_pending": 4,
        }
    )
    assert score == 43


def test_today_focus_endpoint() -> None:
    user_email = _create_test_user()
    now = datetime.now(UTC)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    db = SessionLocal()
    try:
        owner = db.scalar(select(User).where(User.email == user_email))
        assert owner is not None
        db.add_all(
            [
                MemoryItem(
                    user_id=owner.id,
                    content="today task",
                    status="pending",
                    validated=True,
                    confidence=0.9,
                    deadline_at=today_start + timedelta(hours=8),
                    priority="medium",
                ),
                MemoryItem(
                    user_id=owner.id,
                    content="overdue task",
                    status="pending",
                    validated=True,
                    confidence=0.9,
                    deadline_at=today_start - timedelta(days=1),
                    priority="high",
                ),
                MemoryItem(
                    user_id=owner.id,
                    content="urgent task",
                    status="pending",
                    validated=True,
                    confidence=0.9,
                    priority="urgent",
                ),
            ]
        )
        db.commit()
    finally:
        db.close()

    client = TestClient(app)
    response = client.get("/memory/today-focus", headers=_auth_headers(user_email))
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["today_tasks"]) >= 2
    assert len(payload["overdue"]) >= 1
    assert len(payload["urgent"]) >= 2


def test_nudge_endpoint_messages() -> None:
    user_email = _create_test_user()
    now = datetime.now(UTC)
    db = SessionLocal()
    try:
        owner = db.scalar(select(User).where(User.email == user_email))
        assert owner is not None
        db.add(
            MemoryItem(
                user_id=owner.id,
                content="nudge overdue",
                status="pending",
                validated=True,
                confidence=0.9,
                deadline_at=now - timedelta(hours=1),
            )
        )
        db.commit()
    finally:
        db.close()

    client = TestClient(app)
    response = client.get("/memory/nudge", headers=_auth_headers(user_email))
    assert response.status_code == 200
    assert "просроченных задач" in response.json()["message"]


def test_nudge_includes_up_to_three_task_titles() -> None:
    user_email = _create_test_user()
    now = datetime.now(UTC)
    db = SessionLocal()
    try:
        owner = db.scalar(select(User).where(User.email == user_email))
        assert owner is not None
        db.add_all(
            [
                MemoryItem(
                    user_id=owner.id,
                    content="alpha overdue task for nudge",
                    status="pending",
                    validated=True,
                    confidence=0.9,
                    deadline_at=now - timedelta(hours=1),
                ),
                MemoryItem(
                    user_id=owner.id,
                    content="beta overdue task for nudge",
                    status="pending",
                    validated=True,
                    confidence=0.9,
                    deadline_at=now - timedelta(hours=2),
                ),
                MemoryItem(
                    user_id=owner.id,
                    content="gamma overdue task for nudge",
                    status="pending",
                    validated=True,
                    confidence=0.9,
                    deadline_at=now - timedelta(hours=3),
                ),
                MemoryItem(
                    user_id=owner.id,
                    content="delta overdue task for nudge",
                    status="pending",
                    validated=True,
                    confidence=0.9,
                    deadline_at=now - timedelta(hours=4),
                ),
            ]
        )
        db.commit()
    finally:
        db.close()

    client = TestClient(app)
    response = client.get("/memory/nudge", headers=_auth_headers(user_email))
    assert response.status_code == 200
    message = response.json()["message"]
    assert "Примеры:" in message
    examples_text = message.split("Примеры:", maxsplit=1)[1]
    examples = [item.strip().strip(".") for item in examples_text.split(",") if item.strip()]
    assert 1 <= len(examples) <= 3
    assert len(message) <= 220


def test_report_contains_problems_list() -> None:
    user_email = _create_test_user()
    now = datetime.now(UTC)
    db = SessionLocal()
    try:
        owner = db.scalar(select(User).where(User.email == user_email))
        assert owner is not None
        db.add_all(
            [
                MemoryItem(
                    user_id=owner.id,
                    content="problem overdue",
                    status="pending",
                    validated=True,
                    confidence=0.9,
                    deadline_at=now - timedelta(days=1),
                ),
                MemoryItem(
                    user_id=owner.id,
                    content="problem unvalidated",
                    status="pending",
                    validated=False,
                    confidence=0.9,
                ),
                MemoryItem(
                    user_id=owner.id,
                    content="problem low conf",
                    status="pending",
                    validated=True,
                    confidence=0.4,
                ),
            ]
        )
        db.commit()
    finally:
        db.close()

    client = TestClient(app)
    response = client.get("/memory/report", headers=_auth_headers(user_email))
    assert response.status_code == 200
    payload = response.json()
    assert "attention_score" in payload
    assert isinstance(payload["problems"], list)
    problem_types = {item["type"] for item in payload["problems"]}
    assert "overdue" in problem_types
    assert "unvalidated" in problem_types


def test_memory_endpoints_performance_with_10k_tasks() -> None:
    user_email = _create_test_user()
    now = datetime.now(UTC).replace(tzinfo=None)
    db = SessionLocal()
    try:
        owner = db.scalar(select(User).where(User.email == user_email))
        assert owner is not None
        items: list[MemoryItem] = []
        for idx in range(10_000):
            priority = "medium"
            if idx % 10 == 0:
                priority = "urgent"
            elif idx % 3 == 0:
                priority = "high"
            deadline_at = None
            if idx % 4 == 0:
                deadline_at = now + timedelta(days=2)
            elif idx % 7 == 0:
                deadline_at = now - timedelta(days=1)
            items.append(
                MemoryItem(
                    user_id=owner.id,
                    content=f"perf-task-{idx}",
                    status="pending",
                    validated=(idx % 5 != 0),
                    confidence=0.9 if idx % 6 else 0.4,
                    priority=priority,
                    deadline_at=deadline_at,
                    created_at=now - timedelta(minutes=idx % 1440),
                )
            )
        db.add_all(items)
        db.commit()
    finally:
        db.close()

    client = TestClient(app)
    headers = _auth_headers(user_email)
    max_latency_ms = 1000.0
    endpoints = ["/memory/report", "/memory/issues", "/memory/today-focus"]

    for endpoint in endpoints:
        started_at = perf_counter()
        response = client.get(endpoint, headers=headers)
        elapsed_ms = (perf_counter() - started_at) * 1000
        assert response.status_code == 200
        assert elapsed_ms < max_latency_ms, (
            f"{endpoint} took {elapsed_ms:.2f}ms, expected < {max_latency_ms:.0f}ms"
        )
