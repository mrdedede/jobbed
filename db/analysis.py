"""Postgres connection management for AI grading of job postings.

Covers ai_analysis, joined to job_postings for context -- what the on-demand
per-user analysis path writes to and reads from. SQLAlchemy Core only, no
ORM Session, no mapped objects. Schema lives in db.tables, versioned by
Alembic, not created here.
"""

from typing import List, Sequence, Tuple
from uuid import UUID

from sqlalchemy import func, select

from db._sql_utils import text_interval
from db.engine import engine
from db.tables import ai_analysis, job_postings


# INSERTIONS

def insert_analysis(analysis: Sequence) -> None:
    """Insert one analysis into the ai_analysis table.

    Args:
        analysis: (user_id, adequation_grade, depth_analysis, ai_model,
            job_id) -- which postings are worth analysing is decided by
            `select_jobs_to_analyse`, not here.

    Raises:
        RuntimeError: If the database rejects the insertion.
    """
    user_id, adequation_grade, depth_analysis, ai_model, job_id = analysis
    # SQL: INSERT INTO ai_analysis (user_id, job_id, adequation_grade,
    #      depth_analysis, ai_model) VALUES (?, ?, ?, ?, ?)
    stmt = ai_analysis.insert().values(
        user_id=user_id,
        job_id=job_id,
        adequation_grade=adequation_grade,
        depth_analysis=depth_analysis,
        ai_model=ai_model,
    )
    try:
        with engine.begin() as conn:
            conn.execute(stmt)
    except Exception as exc:
        raise RuntimeError(f"Database insert failed: {exc}") from exc


# SELECTIONS

def select_jobs_to_analyse(
        user_id: UUID, limit: int = 0,
        window: str = "-24 hours") -> List[Tuple[UUID, str, str, str]]:
    """Select the recent postings this user has not graded yet.

    Args:
        user_id: The user asking for postings to grade.
        limit: Cap on postings returned; 0 means no cap.
        window: Postgres interval string applied to `now() - interval`.
            Widen it ("-7 days") to pick up postings whose analysis failed
            earlier.

    Returns:
        List of (id, company, title, description). The company and title go
        into the prompt header, so grading is not done on a bare description.
    """
    # SQL: SELECT job_id FROM ai_analysis WHERE user_id = ?
    graded = select(ai_analysis.c.job_id).where(ai_analysis.c.user_id == user_id)
    # SQL: SELECT id, company, title, description FROM job_postings
    #      WHERE id NOT IN (<graded subquery above>)
    #      AND scraped_at >= now() + INTERVAL '<window>'
    #      ORDER BY id
    stmt = (
        select(job_postings.c.id, job_postings.c.company,
               job_postings.c.title, job_postings.c.description)
        .where(job_postings.c.id.notin_(graded))
        .where(job_postings.c.scraped_at >= func.now() + text_interval(window))
        .order_by(job_postings.c.id)
    )
    with engine.connect() as conn:
        jobs = conn.execute(stmt).all()

    return jobs[:limit] if limit else jobs


def count_jobs_to_analyse(user_id: UUID, window: str = "-24 hours") -> int:
    """How many recent postings this user has not graded yet.

    Args:
        user_id: The user asking for the backlog size.
        window: Postgres interval string, as in `select_jobs_to_analyse`.

    Returns:
        The number of postings a run over this window would grade.
    """
    # SQL: SELECT job_id FROM ai_analysis WHERE user_id = ?
    graded = select(ai_analysis.c.job_id).where(ai_analysis.c.user_id == user_id)
    # SQL: SELECT COUNT(*) FROM job_postings
    #      WHERE id NOT IN (<graded subquery above>)
    #      AND scraped_at >= now() + INTERVAL '<window>'
    stmt = (
        select(func.count())
        .select_from(job_postings)
        .where(job_postings.c.id.notin_(graded))
        .where(job_postings.c.scraped_at >= func.now() + text_interval(window))
    )
    with engine.connect() as conn:
        return conn.execute(stmt).scalar_one()


def select_analyses(user_id: UUID) -> List[Tuple]:
    """Every posting this user has graded, joined to its own row, best fit first.

    Args:
        user_id: The user whose analyses to return.

    Returns:
        List of (job_id, adequation_grade, company, title, place, url,
        ai_model, depth_analysis, description, analysed_at).
    """
    # SQL: SELECT job_postings.id, ai_analysis.adequation_grade,
    #      job_postings.company, job_postings.title, job_postings.place,
    #      job_postings.url, ai_analysis.ai_model, ai_analysis.depth_analysis,
    #      job_postings.description, ai_analysis.created_at
    #      FROM ai_analysis JOIN job_postings ON job_postings.id = ai_analysis.job_id
    #      WHERE ai_analysis.user_id = ?
    #      ORDER BY ai_analysis.adequation_grade DESC
    stmt = (
        select(
            job_postings.c.id, ai_analysis.c.adequation_grade,
            job_postings.c.company, job_postings.c.title,
            job_postings.c.place, job_postings.c.url, ai_analysis.c.ai_model,
            ai_analysis.c.depth_analysis, job_postings.c.description,
            ai_analysis.c.created_at,
        )
        .select_from(ai_analysis)
        .join(job_postings, job_postings.c.id == ai_analysis.c.job_id)
        .where(ai_analysis.c.user_id == user_id)
        .order_by(ai_analysis.c.adequation_grade.desc())
    )
    with engine.connect() as conn:
        return conn.execute(stmt).all()


def select_job_state(user_id: UUID, job_id: UUID) -> Tuple[bool, bool]:
    """Report whether a posting exists and whether this user already graded it.

    Args:
        user_id: The user asking.
        job_id: The posting to look up.

    Returns:
        Tuple of (exists in job_postings, has a row in ai_analysis for this user).
    """
    # SQL: SELECT EXISTS(SELECT id FROM job_postings WHERE id = ?)
    exists_stmt = select(
        select(job_postings.c.id).where(job_postings.c.id == job_id).exists()
    )
    # SQL: SELECT EXISTS(SELECT id FROM ai_analysis
    #      WHERE job_id = ? AND user_id = ?)
    analysed_stmt = select(
        select(ai_analysis.c.id)
        .where(ai_analysis.c.job_id == job_id)
        .where(ai_analysis.c.user_id == user_id)
        .exists()
    )
    with engine.connect() as conn:
        exists = conn.execute(exists_stmt).scalar_one()
        analysed = conn.execute(analysed_stmt).scalar_one()

    return bool(exists), bool(analysed)
