"""Add memory_items table for task memory.

Revision ID: 0004_memory_items
Revises: 0003_ts_segments_float
Create Date: 2026-05-05 11:20:00
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0004_memory_items"
down_revision = "0003_ts_segments_float"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_items",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False, server_default="pending"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("source", sa.String(length=100), nullable=False, server_default="manual"),
        sa.Column("meeting_id", sa.String(length=36), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_memory_items_user_id", "memory_items", ["user_id"], unique=False)
    op.create_index("ix_memory_items_status", "memory_items", ["status"], unique=False)
    op.create_index(
        "ix_memory_items_created_at",
        "memory_items",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_memory_items_created_at", table_name="memory_items")
    op.drop_index("ix_memory_items_status", table_name="memory_items")
    op.drop_index("ix_memory_items_user_id", table_name="memory_items")
    op.drop_table("memory_items")
