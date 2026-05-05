"""Add validation fields for memory_items v3.

Revision ID: 0006_memory_items_v3_validation
Revises: 0005_memory_items_v2_fields
Create Date: 2026-05-05 11:50:00
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0006_memory_items_v3_validation"
down_revision = "0005_memory_items_v2_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "memory_items",
        sa.Column("validated", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "memory_items",
        sa.Column("validated_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "memory_items",
        sa.Column("validation_source", sa.String(length=50), nullable=True),
    )
    op.create_index(
        "ix_memory_items_validated",
        "memory_items",
        ["validated"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_memory_items_validated", table_name="memory_items")
    op.drop_column("memory_items", "validation_source")
    op.drop_column("memory_items", "validated_at")
    op.drop_column("memory_items", "validated")
