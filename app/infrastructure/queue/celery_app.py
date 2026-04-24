from celery import Celery

from app.infrastructure.config.settings import settings

celery_app = Celery(
    "ai_meeting_secretary",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery_app.conf.task_routes = {
    "app.workers.tasks.process_meeting_pipeline": {"queue": "meetings"},
}

celery_app.autodiscover_tasks(["app.workers"])
