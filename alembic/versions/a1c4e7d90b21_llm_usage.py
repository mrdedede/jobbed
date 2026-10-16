"""llm_usage: per-user daily LLM quota

Revision ID: a1c4e7d90b21
Revises: 0ffa6d87016f
Create Date: 2026-10-16 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a1c4e7d90b21'
down_revision: Union[str, Sequence[str], None] = '0ffa6d87016f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('llm_usage',
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('day', sa.Date(), nullable=False),
    sa.Column('kind', sa.String(), nullable=False),
    sa.Column('count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('user_id', 'day', 'kind')
    )


def downgrade() -> None:
    op.drop_table('llm_usage')
