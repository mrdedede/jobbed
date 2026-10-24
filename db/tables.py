"""SQLAlchemy Core schema for the application's tables.

This module defines DDL only, as plain Table objects against one shared
MetaData — no ORM mapped classes, no Session. Alembic's autogenerate reads
`metadata` directly; all queries elsewhere in the app go through Core
(`select()`/`insert()`/`update()` against these Table objects), never through
an ORM session, to keep query cost equal to the SQL it actually runs.

Tables: users, user_keywords, user_blacklist, user_cvs, job_boards,
job_postings, ai_analysis, generated_cv, cover_letters, llm_usage, applications.
"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
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
    Column("email", String, nullable=False),
    Column("hashed_password", String(60), nullable=False),
    Column("created_at", TIMESTAMP(timezone=True), server_default=func.now()),
    # Emails are stored lowercased by db.users, but the index is the guarantee:
    # a writer that forgets to normalize cannot create "A@x.com" next to "a@x.com".
    Index("uq_users_email_lower", text("lower(email)"), unique=True),
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
    # At most one active CV per user, enforced here rather than only by
    # db.users deactivating the old one first.
    Index(
        "uq_user_cvs_one_active", "user_id", unique=True,
        postgresql_where=text("is_active"),
    ),
)

job_boards = Table(
    "job_boards",
    metadata,
    UUID_PK(),
    Column("company", String, nullable=False),
    Column("url", String, nullable=False),
    Column("active", Boolean, nullable=False, server_default=text("true")),
    Column("added_at", TIMESTAMP(timezone=True), server_default=func.now()),
    # Unique on md5(url), not url: a btree entry tops out around 2.7 kB and
    # real board/posting URLs with search-filter query strings exceed it (one
    # in job_boards.csv is 4096 chars). Conflict targets must repeat the
    # expression -- see db.boards / db.jobs.
    Index("uq_job_boards_url_md5", text("md5(url)"), unique=True),
)

job_postings = Table(
    "job_postings",
    metadata,
    UUID_PK(),
    Column("company", String, nullable=False),
    Column("title", String, nullable=False),
    Column("description", Text),
    Column("url", String, nullable=False),
    Column("place", String),
    Column("via", String),
    Column("ats", String),
    Column("scraped_at", TIMESTAMP(timezone=True), server_default=func.now()),
    Index("uq_job_postings_url_md5", text("md5(url)"), unique=True),
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
    Index("ix_generated_cv_user_created", "user_id", text("created_at DESC")),
)

# Several letters per generated CV: regenerating adds a row. `answers` keeps
# what the user told the model, so the editor can show it next to the letter.
cover_letters = Table(
    "cover_letters",
    metadata,
    UUID_PK(),
    Column("user_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("generated_cv_id", UUID(as_uuid=True), ForeignKey("generated_cv.id", ondelete="CASCADE"), nullable=False),
    Column("job_id", UUID(as_uuid=True), ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False),
    Column("locale", String),
    Column("answers", JSONB, nullable=False),
    Column("letter", Text, nullable=False),
    Column("created_at", TIMESTAMP(timezone=True), server_default=func.now()),
    Index("ix_cover_letters_user_created", "user_id", text("created_at DESC")),
)

# Per-user daily LLM spend, one counter per (day, kind). Days are UTC dates.
llm_usage = Table(
    "llm_usage",
    metadata,
    Column("user_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("day", Date, nullable=False),
    Column("kind", String, nullable=False),
    Column("count", Integer, nullable=False, server_default=text("0")),
    PrimaryKeyConstraint("user_id", "day", "kind"),
)


# A user's application to a posting they generated a CV for. No row means the
# card is still in "CV generated": the board is generated_cv LEFT JOIN this.
applications = Table(
    "applications",
    metadata,
    UUID_PK(),
    Column("user_id", UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("job_id", UUID(as_uuid=True), ForeignKey("job_postings.id", ondelete="CASCADE"), nullable=False),
    # The CV sent; kept null rather than losing the application if it goes.
    Column("generated_cv_id", UUID(as_uuid=True), ForeignKey("generated_cv.id", ondelete="SET NULL")),
    Column("status", String, nullable=False),
    # The stage a rejected application died at, for stats.
    Column("rejected_from", String),
    Column("rejection_reason", String),
    Column("feedback", Text),
    Column("created_at", TIMESTAMP(timezone=True), server_default=func.now()),
    Column("updated_at", TIMESTAMP(timezone=True), server_default=func.now()),
    UniqueConstraint("user_id", "job_id", name="uq_applications_user_job"),
    CheckConstraint("status IN ('applied', 'interviews', 'jobbed', 'rejected')",
                    name="ck_applications_status"),
    CheckConstraint("rejected_from IN ('cv_generated', 'applied', 'interviews')",
                    name="ck_applications_rejected_from"),
    CheckConstraint("rejection_reason IN ('no_reply', 'rejected', 'withdrew', 'other')",
                    name="ck_applications_rejection_reason"),
    CheckConstraint("(status = 'rejected') = (rejected_from IS NOT NULL)",
                    name="ck_applications_rejected_has_stage"),
)
