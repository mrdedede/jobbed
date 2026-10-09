"""SQLAlchemy Core schema for the application's tables.

This module defines DDL only, as plain Table objects against one shared
MetaData — no ORM mapped classes, no Session. Alembic's autogenerate reads
`metadata` directly; all queries elsewhere in the app go through Core
(`select()`/`insert()`/`update()` against these Table objects), never through
an ORM session, to keep query cost equal to the SQL it actually runs.

Tables: users, user_keywords, user_blacklist, user_cvs, job_boards,
job_postings, ai_analysis, generated_cv.
"""

from sqlalchemy import (
    Boolean,
    Column,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func
from sqlalchemy.types import TIMESTAMP

metadata = MetaData()

UUID_PK = lambda: Column(
    "id", UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
)

users = Table(
    "users",
    metadata,
    UUID_PK(),
    Column("email", String, unique=True, nullable=False),
    Column("hashed_password", String(60), nullable=False),
    Column("created_at", TIMESTAMP(timezone=True), server_default=func.now()),
)

user_keywords = Table(
    "user_keywords",
    metadata,
    UUID_PK(),
    Column("user_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("term", String, nullable=False),
    Column("added_at", TIMESTAMP(timezone=True), server_default=func.now()),
    UniqueConstraint("user_id", "term", name="uq_user_keywords_user_term"),
)

user_blacklist = Table(
    "user_blacklist",
    metadata,
    UUID_PK(),
    Column("user_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("term", String, nullable=False),
    Column("added_at", TIMESTAMP(timezone=True), server_default=func.now()),
    UniqueConstraint("user_id", "term", name="uq_user_blacklist_user_term"),
)

user_cvs = Table(
    "user_cvs",
    metadata,
    UUID_PK(),
    Column("user_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("content_md", Text, nullable=False),
    Column("is_active", Boolean, nullable=False, server_default=text("true")),
    Column("added_at", TIMESTAMP(timezone=True), server_default=func.now()),
)

job_boards = Table(
    "job_boards",
    metadata,
    UUID_PK(),
    Column("company", String, nullable=False),
    Column("url", String, unique=True, nullable=False),
    Column("active", Boolean, nullable=False, server_default=text("true")),
    Column("added_at", TIMESTAMP(timezone=True), server_default=func.now()),
)

job_postings = Table(
    "job_postings",
    metadata,
    UUID_PK(),
    Column("company", String, nullable=False),
    Column("title", String, nullable=False),
    Column("description", Text),
    Column("url", String, unique=True, nullable=False),
    Column("place", String),
    Column("via", String),
    Column("ats", String),
    Column("scraped_at", TIMESTAMP(timezone=True), server_default=func.now()),
)

ai_analysis = Table(
    "ai_analysis",
    metadata,
    UUID_PK(),
    Column("user_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("job_id", UUID(as_uuid=True), ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False),
    Column("adequation_grade", Integer),
    Column("depth_analysis", Text),
    Column("ai_model", String),
    Column("created_at", TIMESTAMP(timezone=True), server_default=func.now()),
    UniqueConstraint("user_id", "job_id", name="uq_ai_analysis_user_job"),
)

generated_cv = Table(
    "generated_cv",
    metadata,
    UUID_PK(),
    Column("user_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("job_id", UUID(as_uuid=True), ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False),
    Column("ai_analysis_id", UUID(as_uuid=True), ForeignKey("ai_analysis.id", ondelete="CASCADE"), nullable=False),
    Column("locale", String),
    Column("cv", JSONB, nullable=False),
    Column("created_at", TIMESTAMP(timezone=True), server_default=func.now()),
)
