from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    app_name: str = "AI Meeting Secretary"
    app_env: str = "local"
    app_host: str = "0.0.0.0"
    app_port: int = 8000

    database_url: str
    redis_url: str
    celery_broker_url: str
    celery_result_backend: str
    app_api_key: str
    max_upload_size_mb: int = Field(ge=1)
    storage_path: str = "storage"
    ffmpeg_binary: str = "ffmpeg"

    whisper_model_size: str = "small"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"

    pyannote_auth_token: str = ""
    pyannote_model_id: str = "pyannote/speaker-diarization-3.1"

    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


try:
    settings = Settings()
except Exception as exc:
    raise RuntimeError(
        "Settings validation failed. Ensure required env vars are set: "
        "DATABASE_URL, REDIS_URL, CELERY_BROKER_URL, CELERY_RESULT_BACKEND, "
        "APP_API_KEY, MAX_UPLOAD_SIZE_MB."
    ) from exc
