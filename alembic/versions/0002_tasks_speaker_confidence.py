"""Add speaker label and confidence to tasks.

Revision ID: 0002_tasks_speaker_confidence
Revises: 0001_initial
Create Date: 2026-04-24 00:10:00
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0002_tasks_speaker_confidence"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("assignee_speaker_label", sa.String(length=100), nullable=True),
    )
    op.add_column("tasks", sa.Column("confidence", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "confidence")
    op.drop_column("tasks", "assignee_speaker_label")
