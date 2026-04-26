"""Convert transcript segment timestamps from string to float.

Revision ID: 0003_transcript_segment_timestamps_float
Revises: 0002_tasks_speaker_confidence
Create Date: 2026-04-26 20:45:00
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0003_transcript_segment_timestamps_float"
down_revision = "0002_tasks_speaker_confidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "transcript_segments",
        "start_sec",
        existing_type=sa.String(length=30),
        type_=sa.Float(),
        existing_nullable=False,
        postgresql_using="start_sec::double precision",
    )
    op.alter_column(
        "transcript_segments",
        "end_sec",
        existing_type=sa.String(length=30),
        type_=sa.Float(),
        existing_nullable=False,
        postgresql_using="end_sec::double precision",
    )


def downgrade() -> None:
    op.alter_column(
        "transcript_segments",
        "end_sec",
        existing_type=sa.Float(),
        type_=sa.String(length=30),
        existing_nullable=False,
        postgresql_using="end_sec::text",
    )
    op.alter_column(
        "transcript_segments",
        "start_sec",
        existing_type=sa.Float(),
        type_=sa.String(length=30),
        existing_nullable=False,
        postgresql_using="start_sec::text",
    )
