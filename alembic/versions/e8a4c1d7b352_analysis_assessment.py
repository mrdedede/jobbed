"""analysis_assessment: what each grade was computed from

Revision ID: e8a4c1d7b352
Revises: d5f1a8c3e902
Create Date: 2026-11-04 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'e8a4c1d7b352'
down_revision: Union[str, Sequence[str], None] = 'd5f1a8c3e902'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('ai_analysis',
                  sa.Column('assessment', postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column('ai_analysis', 'assessment')
