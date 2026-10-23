"""applications: candidature tracking board

Revision ID: c3e9b2f41a07
Revises: a1c4e7d90b21
Create Date: 2026-10-23 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c3e9b2f41a07'
down_revision: Union[str, Sequence[str], None] = 'a1c4e7d90b21'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('applications',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('job_id', sa.UUID(), nullable=False),
    sa.Column('generated_cv_id', sa.UUID(), nullable=True),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('rejected_from', sa.String(), nullable=True),
    sa.Column('rejection_reason', sa.String(), nullable=True),
    sa.Column('feedback', sa.Text(), nullable=True),
    sa.Column('created_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.Column('updated_at', sa.TIMESTAMP(timezone=True), server_default=sa.text('now()'), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['job_id'], ['job_postings.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['generated_cv_id'], ['generated_cv.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('user_id', 'job_id', name='uq_applications_user_job'),
    sa.CheckConstraint("status IN ('applied', 'interviews', 'jobbed', 'rejected')",
                       name='ck_applications_status'),
    sa.CheckConstraint("rejected_from IN ('cv_generated', 'applied', 'interviews')",
                       name='ck_applications_rejected_from'),
    sa.CheckConstraint("rejection_reason IN ('no_reply', 'rejected', 'withdrew', 'other')",
                       name='ck_applications_rejection_reason'),
    sa.CheckConstraint("(status = 'rejected') = (rejected_from IS NOT NULL)",
                       name='ck_applications_rejected_has_stage'),
    )


def downgrade() -> None:
    op.drop_table('applications')
