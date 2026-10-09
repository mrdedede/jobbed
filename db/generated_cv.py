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
                         analysis_id: UUID) -> None:
    """Store one generated CV against the posting and user it was written for.

    Args:
        user_id: The user this CV was generated for.
        locale: Language the CV body is written in ("en", "es", "fr", "pt").
        cv: The CV object as returned by `ai.cv_generation.generate`, stored
            as jsonb.
        job_id: The posting the CV targets.
        analysis_id: The ai_analysis row the generation was tailored against.

    Raises:
        RuntimeError: If the database rejects the insertion.
    """
    # SQL: INSERT INTO generated_cv (user_id, locale, cv, job_id,
    #      ai_analysis_id) VALUES (?, ?, ?, ?, ?)
    stmt = generated_cv.insert().values(
        user_id=user_id,
        locale=locale,
        cv=cv,
        job_id=job_id,
        ai_analysis_id=analysis_id,
    )
    try:
        with engine.begin() as conn:
            conn.execute(stmt)
    except Exception as exc:
        raise RuntimeError(f"Database insert failed: {exc}") from exc


# SELECTIONS

def select_job_for_generation(
        job_id: UUID) -> Optional[Tuple[str, UUID, str]]:
    """The posting text and its stored analysis, for the CV generation prompt.

    Args:
        job_id: The posting to generate a CV for.

    Returns:
        Tuple of (description, ai_analysis id, depth_analysis), or None when
        the posting does not exist or has not been graded yet.
    """
    # SQL: SELECT job_postings.description, ai_analysis.id,
    #      ai_analysis.depth_analysis FROM job_postings
    #      JOIN ai_analysis ON ai_analysis.job_id = job_postings.id
    #      WHERE job_postings.id = ?
    stmt = (
        select(job_postings.c.description, ai_analysis.c.id,
               ai_analysis.c.depth_analysis)
        .join(ai_analysis, ai_analysis.c.job_id == job_postings.c.id)
        .where(job_postings.c.id == job_id)
    )
    with engine.connect() as conn:
        return conn.execute(stmt).first()


def select_jobs_for_cv(
        user_id: UUID, min_grade: int = 0,
        window: str = "-100 years") -> List[Tuple]:
    """This user's graded postings still missing a CV, best fit first.

    Args:
        user_id: The user whose graded postings to consider.
        min_grade: Lowest adequation grade worth writing a CV for.
        window: Postgres interval string, as in db.analysis.select_jobs_to_analyse.
            Defaults to effectively no floor.

    Returns:
        List of (job_id, adequation_grade, company, title, place, url,
        analysed_at).
    """
    # SQL: SELECT job_id FROM generated_cv WHERE user_id = ?
    has_cv = select(generated_cv.c.job_id).where(
        generated_cv.c.user_id == user_id)
    # SQL: SELECT job_postings.id, ai_analysis.adequation_grade,
    #      job_postings.company, job_postings.title, job_postings.place,
    #      job_postings.url, ai_analysis.created_at
    #      FROM ai_analysis JOIN job_postings ON job_postings.id = ai_analysis.job_id
    #      WHERE ai_analysis.user_id = ?
    #      AND job_postings.id NOT IN (<has_cv subquery above>)
    #      AND ai_analysis.adequation_grade >= ?
    #      AND ai_analysis.created_at >= now() + INTERVAL '<window>'
    #      ORDER BY ai_analysis.adequation_grade DESC
    stmt = (
        select(
            job_postings.c.id, ai_analysis.c.adequation_grade,
            job_postings.c.company, job_postings.c.title,
            job_postings.c.place, job_postings.c.url,
            ai_analysis.c.created_at,
        )
        .select_from(ai_analysis)
        .join(job_postings, job_postings.c.id == ai_analysis.c.job_id)
        .where(ai_analysis.c.user_id == user_id)
        .where(job_postings.c.id.notin_(has_cv))
        .where(ai_analysis.c.adequation_grade >= min_grade)
        .where(ai_analysis.c.created_at >= func.now() + text_interval(window))
        .order_by(ai_analysis.c.adequation_grade.desc())
    )
    with engine.connect() as conn:
        return conn.execute(stmt).all()


def select_generated_cvs(user_id: UUID) -> List[Tuple]:
    """Every CV stored for this user, newest first, then best fit.

    Args:
        user_id: The user whose generated CVs to return.

    Returns:
        List of (cv_id, title, company, adequation_grade, locale, cv), the
        last being the CV as a dict (decoded from jsonb).
    """
    # SQL: SELECT generated_cv.id, job_postings.title, job_postings.company,
    #      ai_analysis.adequation_grade, generated_cv.locale, generated_cv.cv,
    #      job_postings.url, generated_cv.created_at
    #      FROM generated_cv
    #      JOIN job_postings ON job_postings.id = generated_cv.job_id
    #      JOIN ai_analysis ON ai_analysis.id = generated_cv.ai_analysis_id
    #      WHERE generated_cv.user_id = ?
    #      ORDER BY generated_cv.created_at DESC, ai_analysis.adequation_grade DESC
    stmt = (
        select(
            generated_cv.c.id, job_postings.c.title, job_postings.c.company,
            ai_analysis.c.adequation_grade, generated_cv.c.locale,
            generated_cv.c.cv, job_postings.c.url, generated_cv.c.created_at,
        )
        .select_from(generated_cv)
        .join(job_postings, job_postings.c.id == generated_cv.c.job_id)
        .join(ai_analysis, ai_analysis.c.id == generated_cv.c.ai_analysis_id)
        .where(generated_cv.c.user_id == user_id)
        .order_by(generated_cv.c.created_at.desc(),
                  ai_analysis.c.adequation_grade.desc())
    )
    with engine.connect() as conn:
        return conn.execute(stmt).all()
