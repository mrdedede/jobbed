"""user_list_indexes: paged per-user listings of generated CVs and letters

Revision ID: d5f1a8c3e902
Revises: 4dbf62eae9c9
Create Date: 2026-10-24 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd5f1a8c3e902'
down_revision: Union[str, Sequence[str], None] = '4dbf62eae9c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index('ix_generated_cv_user_created', 'generated_cv',
                    ['user_id', sa.text('created_at DESC')])
    op.create_index('ix_cover_letters_user_created', 'cover_letters',
                    ['user_id', sa.text('created_at DESC')])


def downgrade() -> None:
    op.drop_index('ix_cover_letters_user_created', table_name='cover_letters')
    op.drop_index('ix_generated_cv_user_created', table_name='generated_cv')
