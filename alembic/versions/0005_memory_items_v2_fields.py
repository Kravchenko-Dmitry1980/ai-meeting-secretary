"""Extend memory_items with v2 control fields.

Revision ID: 0005_memory_items_v2_fields
Revises: 0004_memory_items
Create Date: 2026-05-05 11:35:00
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0005_memory_items_v2_fields"
down_revision = "0004_memory_items"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "memory_items",
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.5"),
    )
    op.add_column(
        "memory_items",
        sa.Column("deadline_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "memory_items",
        sa.Column("priority", sa.String(length=20), nullable=False, server_default="medium"),
    )
    op.add_column(
        "memory_items",
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "memory_items",
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "memory_items",
        sa.Column("raw_extracted_payload", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_memory_items_deadline_at",
        "memory_items",
        ["deadline_at"],
        unique=False,
    )
    op.create_index(
        "ix_memory_items_priority",
        "memory_items",
        ["priority"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_memory_items_priority", table_name="memory_items")
    op.drop_index("ix_memory_items_deadline_at", table_name="memory_items")
    op.drop_column("memory_items", "raw_extracted_payload")
    op.drop_column("memory_items", "updated_at")
    op.drop_column("memory_items", "completed_at")
    op.drop_column("memory_items", "priority")
    op.drop_column("memory_items", "deadline_at")
    op.drop_column("memory_items", "confidence")
