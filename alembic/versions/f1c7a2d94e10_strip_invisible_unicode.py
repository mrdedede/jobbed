"""strip_invisible_unicode: clean text stored before the scrubber existed

Known URLs are never re-inserted, so a re-scrape would not fix these rows.
Uses `job_scraper.text`'s always-strip set; the characters it only removes next
to a Latin letter stay for the scrubber, since they can be legitimate here.

Revision ID: f1c7a2d94e10
Revises: e8a4c1d7b352
Create Date: 2026-11-04 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

revision: str = 'f1c7a2d94e10'
down_revision: Union[str, Sequence[str], None] = 'e8a4c1d7b352'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PATTERN = (r"[­͏؜᠎​‎‏‪-‮"
           r"⁠-⁤⁦-⁩⁪-⁯﻿\U000E0000-\U000E007F]")


def upgrade() -> None:
    for table, column in (("job_postings", "description"),
                          ("job_postings", "title"),
                          ("job_postings", "company"),
                          ("cover_letters", "letter")):
        op.execute(
            f"UPDATE {table} SET {column} = regexp_replace({column}, '{PATTERN}', '', 'g') "
            f"WHERE {column} ~ '{PATTERN}'")

    # None of these characters is JSON syntax, so the text round trip is safe.
    op.execute(
        f"UPDATE generated_cv SET cv = CAST(regexp_replace(CAST(cv AS text), "
        f"'{PATTERN}', '', 'g') AS jsonb) WHERE CAST(cv AS text) ~ '{PATTERN}'")


def downgrade() -> None:
    """Nothing to restore: the removed characters carried no content."""
