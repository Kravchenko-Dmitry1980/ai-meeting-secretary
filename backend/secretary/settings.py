from __future__ import annotations

from pathlib import Path
from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_DIR / ".env", env_file_encoding="utf-8", env_ignore_empty=True, extra="ignore", populate_by_name=True)
    project_dir: Path = PROJECT_DIR
    data_dir: Path = PROJECT_DIR / "data"
    polza_api_key: SecretStr = Field(default=SecretStr(""), validation_alias="POLZA_API_KEY")
    polza_base_url: str = Field(default="https://polza.ai/api/v1", validation_alias="POLZA_BASE_URL")
    stt_model: str = Field(default="openai/whisper-large-v3-turbo", validation_alias=AliasChoices("POLZA_STT_MODEL", "stt_model"))
    summary_model: str = Field(default="qwen/qwen3-30b-a3b-instruct-2507", validation_alias=AliasChoices("POLZA_SUMMARY_MODEL", "summary_model"))
    chunk_seconds: int = Field(default=120, ge=15, le=240)
    request_timeout_seconds: int = Field(default=180, ge=10, le=1800)
    meeting_budget_rub: float = Field(default=100, ge=0, le=100000)
    local_cost_limits_enabled: bool = True
    allow_unknown_price: bool = False
    unknown_request_reservation_rub: float | None = Field(default=None, gt=0, le=100000)
    cloud_enabled: bool = True
    # Owner deployment policy, deliberately excluded from editable team/local config.
    approved_monthly_external_costs_rub: float = Field(default=0, ge=0, le=3000, allow_inf_nan=False)
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    stt_price_rub_per_minute: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    summary_input_rub_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    summary_output_rub_per_million: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    summary_max_output_tokens: int = Field(default=8192, ge=512, le=16384)
    summary_batch_chars: int = Field(default=12000, ge=5000, le=100000)
    max_upload_bytes: int = Field(default=2 * 1024**3, ge=1024)

    @property
    def key_configured(self) -> bool:
        return bool(self.polza_api_key.get_secret_value().strip())

    def public(self) -> dict:
        return {"key_configured": self.key_configured, **{k: getattr(self, k) for k in EDITABLE_CONFIG}}


EDITABLE_CONFIG = {"stt_model", "summary_model", "chunk_seconds", "request_timeout_seconds", "meeting_budget_rub", "local_cost_limits_enabled", "allow_unknown_price", "unknown_request_reservation_rub", "cloud_enabled", "stt_price_rub_per_minute", "summary_input_rub_per_million", "summary_output_rub_per_million", "summary_max_output_tokens", "summary_batch_chars"}

