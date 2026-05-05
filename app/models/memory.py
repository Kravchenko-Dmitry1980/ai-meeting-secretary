import uuid
from datetime import UTC
from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy import Float
from sqlalchemy import Boolean
from sqlalchemy import String
from sqlalchemy import Text
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column

from app.infrastructure.db.base import Base


def now_utc() -> datetime:
    return datetime.now(UTC)


class MemoryItem(Base):
    __tablename__ = "memory_items"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    user_id: Mapped[str] = mapped_column(String(255), index=True)
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(50), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_utc,
        index=True,
    )
    source: Mapped[str] = mapped_column(String(100), default="manual")
    meeting_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.5)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    priority: Mapped[str] = mapped_column(String(20), default="medium", index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    raw_extracted_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    validated: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    validation_source: Mapped[str | None] = mapped_column(String(50), nullable=True)
