"""Postgres connection management for generated CVs.

Covers generated_cv, joined to job_postings/ai_analysis for context -- what
the CV-generation path writes to and reads from. SQLAlchemy Core only, no
ORM Session, no mapped objects. Schema lives in db.tables, versioned by
Alembic, not created here.
"""

from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy import func, select

from db._sql_utils import text_interval
from db.engine import engine
from db.tables import ai_analysis, generated_cv, job_postings


# INSERTIONS

def insert_generated_cv(user_id: UUID, locale: str, cv: dict, job_id: UUID,
                         analysis_id: UUID) -> UUID:
    """Store one generated CV against the posting and user it was written for.

    Args:
        user_id: The user this CV was generated for.
        locale: Language the CV body is written in ("en", "es", "fr", "pt").
        cv: The CV object as returned by `ai.cv_generation.generate_cv`, stored
            as jsonb.
        job_id: The posting the CV targets.
        analysis_id: The ai_analysis row the generation was tailored against.

    Returns:
        The id of the new generated_cv row.

    Raises:
        RuntimeError: If the analysis is not this user's grade of this
            posting, or the database rejects the insertion.
    """
    # SQL: INSERT INTO generated_cv (user_id, locale, cv, job_id,
    #      ai_analysis_id) VALUES (?, ?, ?, ?, ?) RETURNING id
    stmt = generated_cv.insert().returning(generated_cv.c.id).values(
        user_id=user_id,
        locale=locale,
        cv=cv,
        job_id=job_id,
        ai_analysis_id=analysis_id,
    )
    # SQL: SELECT 1 FROM ai_analysis WHERE id = ? AND user_id = ? AND job_id = ?
    owned = select(ai_analysis.c.id).where(
        ai_analysis.c.id == analysis_id,
        ai_analysis.c.user_id == user_id,
        ai_analysis.c.job_id == job_id,
    )
    try:
        with engine.begin() as conn:
            if conn.execute(owned).first() is None:
                raise ValueError(
                    f"analysis {analysis_id} is not user {user_id}'s "
                    f"grade of job {job_id}")
            return conn.execute(stmt).scalar_one()
    except Exception as exc:
        raise RuntimeError(f"Database insert failed: {exc}") from exc


# UPDATES

def update_generated_cv(user_id: UUID, cv_id: UUID, cv: dict) -> bool:
    """Replace the body of one of a user's generated CVs (the user's edit).

    Args:
        user_id: The owner of the CV -- scopes the update.
        cv_id: The generated_cv row to change.
        cv: The edited CV, same shape as the generated one, stored as jsonb.

    Returns:
        Whether a row matched (False: no such CV for this user).

    Raises:
        RuntimeError: If the database rejects the update.
    """
    # SQL: UPDATE generated_cv SET cv = ? WHERE id = ? AND user_id = ?
    stmt = (
        generated_cv.update()
        .where(generated_cv.c.id == cv_id)
        .where(generated_cv.c.user_id == user_id)
        .values(cv=cv)
    )
    try:
        with engine.begin() as conn:
            return conn.execute(stmt).rowcount > 0
    except Exception as exc:
        raise RuntimeError(f"Database update failed: {exc}") from exc


# SELECTIONS

def select_job_for_generation(
        user_id: UUID, job_id: UUID) -> Optional[Tuple[str, UUID, str]]:
    """The posting text and this user's analysis of it, for the CV prompt.

    Args:
        user_id: The user the CV is for. Scopes the analysis: postings are
            shared, but a grade and its depth_analysis are one user's.
        job_id: The posting to generate a CV for.

    Returns:
        Tuple of (description, ai_analysis id, depth_analysis), or None when
        the posting does not exist or this user has not graded it yet.
    """
    # SQL: SELECT job_postings.description, ai_analysis.id,
    #      ai_analysis.depth_analysis FROM job_postings
    #      JOIN ai_analysis ON ai_analysis.job_id = job_postings.id
    #      WHERE job_postings.id = ? AND ai_analysis.user_id = ?
    stmt = (
        select(job_postings.c.description, ai_analysis.c.id,
               ai_analysis.c.depth_analysis)
        .join(ai_analysis, ai_analysis.c.job_id == job_postings.c.id)
        .where(job_postings.c.id == job_id)
        .where(ai_analysis.c.user_id == user_id)
    )
    with engine.connect() as conn:
        return conn.execute(stmt).first()


def _without_cv_where(user_id: UUID, min_grade: int,
                      since_days: Optional[int] = None) -> list:
    # SQL: SELECT job_id FROM generated_cv WHERE user_id = ?
    has_cv = select(generated_cv.c.job_id).where(
        generated_cv.c.user_id == user_id)
    where = [ai_analysis.c.user_id == user_id,
             ai_analysis.c.job_id.notin_(has_cv),
             ai_analysis.c.adequation_grade >= min_grade]

    if since_days is not None:
        # since_days is a validated int, never client text: text_interval
        # inlines it into SQL.
        where.append(ai_analysis.c.created_at
                     >= func.now() + text_interval(f"-{since_days} days"))

    return where


def select_jobs_for_cv(user_id: UUID, min_grade: int = 0,
                       limit: Optional[int] = None,
                       offset: int = 0,
                       since_days: Optional[int] = None) -> List[Tuple]:
    """This user's graded postings still missing a CV, best fit first.

    Args:
        user_id: The user whose graded postings to consider.
        min_grade: Lowest adequation grade worth writing a CV for.
        limit: Cap on rows returned; None means no cap.
        offset: Rows to skip, for paging.
        since_days: Only postings graded within this many days.

    Returns:
        List of (job_id, adequation_grade, company, title, place, url,
        ai_model, depth_analysis, analysed_at), shaped like
        db.analysis.select_analysis_page.
    """
    # SQL: SELECT job_postings.id, ai_analysis.adequation_grade,
    #      job_postings.company, job_postings.title, job_postings.place,
    #      job_postings.url, ai_analysis.ai_model, ai_analysis.depth_analysis,
    #      ai_analysis.created_at
    #      FROM ai_analysis JOIN job_postings ON job_postings.id = ai_analysis.job_id
    #      WHERE ai_analysis.user_id = ?
    #      AND ai_analysis.job_id NOT IN (<has_cv subquery>)
    #      AND ai_analysis.adequation_grade >= ?
    #      ORDER BY adequation_grade DESC, created_at DESC, id LIMIT ? OFFSET ?
    stmt = (
        select(
            job_postings.c.id, ai_analysis.c.adequation_grade,
            job_postings.c.company, job_postings.c.title,
            job_postings.c.place, job_postings.c.url, ai_analysis.c.ai_model,
            ai_analysis.c.depth_analysis, ai_analysis.c.created_at,
        )
        .select_from(ai_analysis)
        .join(job_postings, job_postings.c.id == ai_analysis.c.job_id)
        .where(*_without_cv_where(user_id, min_grade, since_days))
        .order_by(ai_analysis.c.adequation_grade.desc(),
                  ai_analysis.c.created_at.desc(), ai_analysis.c.id)
        .limit(limit)
        .offset(offset)
    )
    with engine.connect() as conn:
        return conn.execute(stmt).all()


def count_jobs_for_cv(user_id: UUID, min_grade: int = 0,
                      since_days: Optional[int] = None) -> int:
    """How many rows `select_jobs_for_cv` pages through."""
    stmt = (select(func.count()).select_from(ai_analysis)
            .where(*_without_cv_where(user_id, min_grade, since_days)))
    with engine.connect() as conn:
        return conn.execute(stmt).scalar_one()


# SQL: SELECT generated_cv.id, job_postings.title, job_postings.company,
#      ai_analysis.adequation_grade, generated_cv.locale, generated_cv.cv,
#      generated_cv.job_id, ai_analysis.depth_analysis, generated_cv.created_at, job_postings.url
#      FROM generated_cv
#      JOIN job_postings ON job_postings.id = generated_cv.job_id
#      JOIN ai_analysis ON ai_analysis.id = generated_cv.ai_analysis_id
_CV_COLUMNS = (
    select(
        generated_cv.c.id, job_postings.c.title, job_postings.c.company,
        ai_analysis.c.adequation_grade, generated_cv.c.locale,
        generated_cv.c.cv, generated_cv.c.job_id, ai_analysis.c.depth_analysis, generated_cv.c.created_at,
        job_postings.c.url,
    )
    .select_from(generated_cv)
    .join(job_postings, job_postings.c.id == generated_cv.c.job_id)
    .join(ai_analysis, ai_analysis.c.id == generated_cv.c.ai_analysis_id)
)


def select_generated_cvs(user_id: UUID, limit: Optional[int] = None,
                         offset: int = 0) -> List[Tuple]:
    """The CVs stored for this user, newest first, then best fit.

    Args:
        user_id: The user whose generated CVs to return.
        limit: Cap on rows returned; None means no cap.
        offset: Rows to skip, for paging.

    Returns:
        List of (cv_id, title, company, adequation_grade, locale, cv, job_id,
        depth_analysis, created_at, url),
        `cv` being the CV as a dict (decoded from jsonb).
    """
    # SQL: SELECT generated_cv.id, job_postings.title, job_postings.company,
    #      ai_analysis.adequation_grade, generated_cv.locale, generated_cv.cv,
    #      generated_cv.job_id, ai_analysis.depth_analysis, generated_cv.created_at, job_postings.url
    #      FROM generated_cv
    #      JOIN job_postings ON job_postings.id = generated_cv.job_id
    #      JOIN ai_analysis ON ai_analysis.id = generated_cv.ai_analysis_id
    #      WHERE generated_cv.user_id = ?
    #      ORDER BY generated_cv.created_at DESC,
    #      ai_analysis.adequation_grade DESC, generated_cv.id LIMIT ? OFFSET ?
    stmt = (
        _CV_COLUMNS
        .where(generated_cv.c.user_id == user_id)
        .order_by(generated_cv.c.created_at.desc(),
                  ai_analysis.c.adequation_grade.desc(), generated_cv.c.id)
        .limit(limit)
        .offset(offset)
    )
    with engine.connect() as conn:
        return conn.execute(stmt).all()


def count_generated_cvs(user_id: UUID) -> int:
    """How many CVs are stored for this user."""
    # SQL: SELECT count(*) FROM generated_cv WHERE user_id = ?
    stmt = (select(func.count()).select_from(generated_cv)
            .where(generated_cv.c.user_id == user_id))
    with engine.connect() as conn:
        return conn.execute(stmt).scalar_one()


def select_generated_cv(user_id: UUID, cv_id: UUID) -> Optional[Tuple]:
    """One of this user's generated CVs.

    Args:
        user_id: The owner. Another user's id yields None, never their row.
        cv_id: The generated_cv id.

    Returns:
        The same tuple `select_generated_cvs` yields, or None.
    """
    stmt = _CV_COLUMNS.where(generated_cv.c.user_id == user_id,
                             generated_cv.c.id == cv_id)
    with engine.connect() as conn:
        return conn.execute(stmt).first()
