"""cover_letters: AI-written cover letters, one per generation

Revision ID: b7e2f4c81a35
Revises: a1c4e7d90b21
Create Date: 2026-10-24 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'b7e2f4c81a35'
down_revision: Union[str, Sequence[str], None] = 'a1c4e7d90b21'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('cover_letters',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('generated_cv_id', sa.UUID(), nullable=False),
    sa.Column('job_id', sa.UUID(), nullable=False),
    sa.Column('locale', sa.String(), nullable=True),
    sa.Column('answers', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('letter', sa.Text(), nullable=False),
    sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['generated_cv_id'], ['generated_cv.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['job_id'], ['job_postings.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )


def downgrade() -> None:
    op.drop_table('cover_letters')
