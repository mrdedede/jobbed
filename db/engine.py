"""Postgres engine for the application.

One pooled Engine, read from DATABASE_URL -- same env var Alembic reads
(see alembic/env.py), so migrations and the app always point at the same
database.
"""

import os

from sqlalchemy import create_engine

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg://joblister:joblister@localhost:5432/joblister",
)

engine = create_engine(DATABASE_URL)
