"""add store verify columns and phoneotp

Revision ID: b7c2e91a4f10
Revises: 84754ebe15e8
Create Date: 2026-08-03 02:42:00.000000
"""

from typing import Optional, Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


revision: str = "b7c2e91a4f10"
down_revision: Optional[str] = "84754ebe15e8"
branch_labels: Optional[Union[str, Sequence[str]]] = None
depends_on: Optional[Union[str, Sequence[str]]] = None


def upgrade() -> None:
    op.add_column("store", sa.Column("latitude", sa.Float(), nullable=True))
    op.add_column("store", sa.Column("longitude", sa.Float(), nullable=True))
    op.add_column(
        "store",
        sa.Column("phone_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "phoneotp",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("phone", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("code_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("purpose", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("expires_at", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_phoneotp_phone"), "phoneotp", ["phone"], unique=False)
    op.create_index(op.f("ix_phoneotp_purpose"), "phoneotp", ["purpose"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_phoneotp_purpose"), table_name="phoneotp")
    op.drop_index(op.f("ix_phoneotp_phone"), table_name="phoneotp")
    op.drop_table("phoneotp")
    op.drop_column("store", "phone_verified")
    op.drop_column("store", "longitude")
    op.drop_column("store", "latitude")
