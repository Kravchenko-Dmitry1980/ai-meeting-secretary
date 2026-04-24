"""Initial schema for foundation v1.

Revision ID: 0001_initial
Revises:
Create Date: 2026-04-24 00:00:00
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("email", sa.String(length=255), nullable=False, unique=True),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("hashed_password", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "meetings",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("owner_id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("source_type", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("media_file_path", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
    )
    op.create_index("ix_meetings_owner_id", "meetings", ["owner_id"])

    op.create_table(
        "participants",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("meeting_id", sa.String(length=36), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("role", sa.String(length=100), nullable=True),
        sa.ForeignKeyConstraint(["meeting_id"], ["meetings.id"]),
    )
    op.create_index("ix_participants_meeting_id", "participants", ["meeting_id"])

    op.create_table(
        "speakers",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("meeting_id", sa.String(length=36), nullable=False),
        sa.Column("speaker_label", sa.String(length=100), nullable=False),
        sa.Column("participant_id", sa.String(length=36), nullable=True),
        sa.Column("confidence", sa.String(length=50), nullable=True),
        sa.ForeignKeyConstraint(["meeting_id"], ["meetings.id"]),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.id"]),
    )
    op.create_index("ix_speakers_meeting_id", "speakers", ["meeting_id"])

    op.create_table(
        "transcripts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("meeting_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("language", sa.String(length=20), nullable=False),
        sa.Column("full_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["meeting_id"], ["meetings.id"]),
    )
    op.create_index("ix_transcripts_meeting_id", "transcripts", ["meeting_id"])

    op.create_table(
        "transcript_segments",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("transcript_id", sa.String(length=36), nullable=False),
        sa.Column("speaker_id", sa.String(length=36), nullable=True),
        sa.Column("start_sec", sa.String(length=30), nullable=False),
        sa.Column("end_sec", sa.String(length=30), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["transcript_id"], ["transcripts.id"]),
        sa.ForeignKeyConstraint(["speaker_id"], ["speakers.id"]),
    )
    op.create_index(
        "ix_transcript_segments_transcript_id",
        "transcript_segments",
        ["transcript_id"],
    )

    op.create_table(
        "meeting_summaries",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("meeting_id", sa.String(length=36), nullable=False),
        sa.Column("summary_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["meeting_id"], ["meetings.id"]),
    )
    op.create_index(
        "ix_meeting_summaries_meeting_id",
        "meeting_summaries",
        ["meeting_id"],
    )

    op.create_table(
        "tasks",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("meeting_id", sa.String(length=36), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("assignee_participant_id", sa.String(length=36), nullable=True),
        sa.Column("due_date", sa.String(length=50), nullable=True),
        sa.Column("priority", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("source_quote", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["meeting_id"], ["meetings.id"]),
        sa.ForeignKeyConstraint(
            ["assignee_participant_id"],
            ["participants.id"],
        ),
    )
    op.create_index("ix_tasks_meeting_id", "tasks", ["meeting_id"])

    op.create_table(
        "processing_jobs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("meeting_id", sa.String(length=36), nullable=False),
        sa.Column("stage", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["meeting_id"], ["meetings.id"]),
    )
    op.create_index(
        "ix_processing_jobs_meeting_id",
        "processing_jobs",
        ["meeting_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_processing_jobs_meeting_id", table_name="processing_jobs")
    op.drop_table("processing_jobs")
    op.drop_index("ix_tasks_meeting_id", table_name="tasks")
    op.drop_table("tasks")
    op.drop_index("ix_meeting_summaries_meeting_id", table_name="meeting_summaries")
    op.drop_table("meeting_summaries")
    op.drop_index(
        "ix_transcript_segments_transcript_id",
        table_name="transcript_segments",
    )
    op.drop_table("transcript_segments")
    op.drop_index("ix_transcripts_meeting_id", table_name="transcripts")
    op.drop_table("transcripts")
    op.drop_index("ix_speakers_meeting_id", table_name="speakers")
    op.drop_table("speakers")
    op.drop_index("ix_participants_meeting_id", table_name="participants")
    op.drop_table("participants")
    op.drop_index("ix_meetings_owner_id", table_name="meetings")
    op.drop_table("meetings")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
