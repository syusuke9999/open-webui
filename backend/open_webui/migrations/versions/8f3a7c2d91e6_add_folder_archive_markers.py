"""Add markers for reversible folder archiving.

Revision ID: 8f3a7c2d91e6
Revises: d4c1a8e37b62
Create Date: 2026-09-30
"""

import sqlalchemy as sa
from alembic import op

revision = '8f3a7c2d91e6'
down_revision = 'd4c1a8e37b62'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('folder', sa.Column('archive_root_id', sa.Text(), nullable=True))
    op.create_index('ix_folder_archive_root_id', 'folder', ['archive_root_id'])
    op.add_column('chat', sa.Column('archived_by_folder_id', sa.Text(), nullable=True))
    op.create_index('ix_chat_archived_by_folder_id', 'chat', ['archived_by_folder_id'])


def downgrade() -> None:
    # Removing folder archiving restores only chats that this feature archived.
    chat = sa.table('chat', sa.column('archived', sa.Boolean), sa.column('archived_by_folder_id', sa.Text))
    op.execute(chat.update().where(chat.c.archived_by_folder_id.is_not(None)).values(archived=False))
    op.drop_index('ix_chat_archived_by_folder_id', table_name='chat')
    op.drop_column('chat', 'archived_by_folder_id')
    op.drop_index('ix_folder_archive_root_id', table_name='folder')
    op.drop_column('folder', 'archive_root_id')
