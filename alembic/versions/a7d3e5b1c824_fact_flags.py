"""fact_flags: facts in a generated document its sources do not contain

Revision ID: a7d3e5b1c824
Revises: f1c7a2d94e10
Create Date: 2026-11-04 18:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'a7d3e5b1c824'
down_revision: Union[str, Sequence[str], None] = 'f1c7a2d94e10'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for table in ('generated_cv', 'cover_letters'):
        op.add_column(table, sa.Column('fact_flags', postgresql.JSONB(), nullable=False,
                                       server_default=sa.text("'[]'::jsonb")))


def downgrade() -> None:
    for table in ('generated_cv', 'cover_letters'):
        op.drop_column(table, 'fact_flags')
