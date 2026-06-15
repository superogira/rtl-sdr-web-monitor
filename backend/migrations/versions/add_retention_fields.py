"""
Migration: Add `audio_deleted` column to recordings table.

This flag indicates that the audio file has been deleted from disk by the
retention policy (keep_transcript mode), so the frontend can hide audio
playback controls while still showing the transcript.

Revision ID: add_retention_fields
Revises: add_stt_fields
Create Date: 2025-06-15
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'add_retention_fields'
down_revision = 'add_stt_fields'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'recordings',
        sa.Column('audio_deleted', sa.Boolean(), nullable=False, server_default='0'),
    )


def downgrade() -> None:
    op.drop_column('recordings', 'audio_deleted')