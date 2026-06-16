"""
Migration: Add `recorded_by_user_id` column to recordings table.

This tracks which user created a manual (per-listener) recording, so the
recordings page and dashboard feed can show who recorded each entry.

Also creates an index on the column for efficient per-user queries.

Revision ID: add_recorded_by_user
Revises: add_retention_fields
Create Date: 2025-06-16
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'add_recorded_by_user'
down_revision = 'add_retention_fields'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'recordings',
        sa.Column(
            'recorded_by_user_id',
            sa.String(36),
            sa.ForeignKey('users.id'),
            nullable=True,
            comment="User who recorded this (manual recordings)",
        ),
    )
    op.create_index(
        'ix_recordings_recorded_by_user_id',
        'recordings',
        ['recorded_by_user_id'],
    )


def downgrade() -> None:
    op.drop_index('ix_recordings_recorded_by_user_id', table_name='recordings')
    op.drop_column('recordings', 'recorded_by_user_id')