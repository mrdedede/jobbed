"""merge cover_letters and applications heads

Both were cut from a1c4e7d90b21 on separate branches, which left two heads
and broke every `alembic upgrade head` (the k8s migrate Job, the phone).
The two create independent tables, so the merge carries no schema change.
To roll back past it, name the target: `alembic downgrade c3e9b2f41a07`
(a plain `downgrade -1` is ambiguous at a merge point).

Revision ID: 4dbf62eae9c9
Revises: b7e2f4c81a35, c3e9b2f41a07
Create Date: 2026-10-24 14:19:28.506618

"""
from typing import Sequence, Union


# revision identifiers, used by Alembic.
revision: str = '4dbf62eae9c9'
down_revision: Union[str, Sequence[str], None] = ('b7e2f4c81a35', 'c3e9b2f41a07')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
