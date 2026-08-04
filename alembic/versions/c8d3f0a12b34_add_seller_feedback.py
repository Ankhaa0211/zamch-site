"""add seller feedback table

Revision ID: c8d3f0a12b34
Revises: b7c2e91a4f10
Create Date: 2026-08-04 19:40:00.000000
"""

from typing import Optional, Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = "c8d3f0a12b34"
down_revision: Optional[str] = "b7c2e91a4f10"
branch_labels: Optional[Union[str, Sequence[str]]] = None
depends_on: Optional[Union[str, Sequence[str]]] = None


def upgrade() -> None:
    op.create_table(
        "sellerfeedback",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("store_id", sa.Integer(), nullable=True),
        sa.Column("message", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("status", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_sellerfeedback_user_id"), "sellerfeedback", ["user_id"], unique=False)
    op.create_index(op.f("ix_sellerfeedback_store_id"), "sellerfeedback", ["store_id"], unique=False)
    op.create_index(op.f("ix_sellerfeedback_status"), "sellerfeedback", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_sellerfeedback_status"), table_name="sellerfeedback")
    op.drop_index(op.f("ix_sellerfeedback_store_id"), table_name="sellerfeedback")
    op.drop_index(op.f("ix_sellerfeedback_user_id"), table_name="sellerfeedback")
    op.drop_table("sellerfeedback")
