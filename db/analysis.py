"""Postgres connection management for AI grading of job postings.

Covers ai_analysis, joined to job_postings for context -- what the on-demand
per-user analysis path writes to and reads from. SQLAlchemy Core only, no
ORM Session, no mapped objects. Schema lives in db.tables, versioned by
Alembic, not created here.
"""

from typing import List, Optional, Sequence, Tuple
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db._sql_utils import text_interval
from db.engine import engine
from db.tables import ai_analysis, job_postings


# INSERTIONS

def insert_analysis(analysis: Sequence) -> None:
    """Store one analysis, replacing this user's earlier grade of the posting.

    Re-analysis updates the row in place, so its id -- and any generated_cv
    that points at it -- survives.

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
    #      ON CONFLICT (user_id, job_id) DO UPDATE SET adequation_grade = ...,
    #      depth_analysis = ..., ai_model = ..., created_at = now()
    values = dict(
        adequation_grade=adequation_grade,
        depth_analysis=depth_analysis,
        ai_model=ai_model,
    )
    stmt = pg_insert(ai_analysis).values(
        user_id=user_id, job_id=job_id, **values
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[ai_analysis.c.user_id, ai_analysis.c.job_id],
        set_={**values, "created_at": func.now()},
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
    if limit:
        stmt = stmt.limit(limit)

    with engine.connect() as conn:
        return conn.execute(stmt).all()


def select_candidates(user_id: UUID, limit: int = 0,
                      window: str = "-24 hours") -> List[Tuple]:
    """Select ungraded recent postings with every column the filter needs.

    Same selection as `select_jobs_to_analyse`, plus `url` and `place`.
    `limit` caps rows fetched, so a caller that filters afterwards should
    pass 0 and apply its own limit to the filtered result.

    Args:
        user_id: The user asking for postings to grade.
        limit: Cap on postings returned; 0 means no cap.
        window: Postgres interval string applied to `now() - interval`.

    Returns:
        List of (id, company, title, url, place, description, scraped_at).
    """
    # SQL: SELECT job_id FROM ai_analysis WHERE user_id = ?
    graded = select(ai_analysis.c.job_id).where(ai_analysis.c.user_id == user_id)
    # SQL: SELECT id, company, title, url, place, description, scraped_at
    #      FROM job_postings
    #      WHERE id NOT IN (<graded subquery above>)
    #      AND scraped_at >= now() + INTERVAL '<window>'
    #      ORDER BY id
    stmt = (
        select(job_postings.c.id, job_postings.c.company,
               job_postings.c.title, job_postings.c.url,
               job_postings.c.place, job_postings.c.description,
               job_postings.c.scraped_at)
        .where(job_postings.c.id.notin_(graded))
        .where(job_postings.c.scraped_at >= func.now() + text_interval(window))
        .order_by(job_postings.c.id)
    )
    if limit:
        stmt = stmt.limit(limit)

    with engine.connect() as conn:
        return conn.execute(stmt).all()


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


def select_analysis_page(
        user_id: UUID, limit: int, min_grade: int = 0,
        job_ids: Optional[Sequence[UUID]] = None) -> List[Tuple]:
    """A user's graded postings, best fit first, without the description.

    The description is large and the url has it, so unlike `select_analyses`
    this is safe to put in a response.

    Args:
        user_id: The user whose analyses to return.
        limit: Cap on rows returned.
        min_grade: Only grades at or above this; 0 keeps ungraded (NULL) rows.
        job_ids: Restrict to these postings (e.g. one run's results).

    Returns:
        List of (job_id, adequation_grade, company, title, place, url,
        ai_model, depth_analysis, analysed_at).
    """
    # SQL: SELECT job_postings.id, ai_analysis.adequation_grade,
    #      job_postings.company, job_postings.title, job_postings.place,
    #      job_postings.url, ai_analysis.ai_model, ai_analysis.depth_analysis,
    #      ai_analysis.created_at
    #      FROM ai_analysis JOIN job_postings ON job_postings.id = ai_analysis.job_id
    #      WHERE ai_analysis.user_id = ? [AND job_id IN (...)]
    #      [AND adequation_grade >= ?]
    #      ORDER BY adequation_grade DESC NULLS LAST, created_at DESC LIMIT ?
    stmt = (
        select(
            job_postings.c.id, ai_analysis.c.adequation_grade,
            job_postings.c.company, job_postings.c.title,
            job_postings.c.place, job_postings.c.url, ai_analysis.c.ai_model,
            ai_analysis.c.depth_analysis, ai_analysis.c.created_at,
        )
        .select_from(ai_analysis)
        .join(job_postings, job_postings.c.id == ai_analysis.c.job_id)
        .where(ai_analysis.c.user_id == user_id)
        .order_by(ai_analysis.c.adequation_grade.desc().nulls_last(),
                  ai_analysis.c.created_at.desc())
        .limit(limit)
    )
    if job_ids is not None:
        stmt = stmt.where(ai_analysis.c.job_id.in_(job_ids))
    if min_grade > 0:
        stmt = stmt.where(ai_analysis.c.adequation_grade >= min_grade)

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
