"""Postgres connection management for cover letters.

Covers cover_letters, joined to generated_cv/job_postings/ai_analysis for
context -- what the cover-letter path writes to and reads from. SQLAlchemy
Core only, no ORM Session, no mapped objects. Schema lives in db.tables,
versioned by Alembic, not created here.
"""

from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy import select

from db.engine import engine
from db.tables import ai_analysis, cover_letters, generated_cv, job_postings


# INSERTIONS

def insert_cover_letter(user_id: UUID, cv_id: UUID, job_id: UUID,
                        locale: str, answers: dict, letter: str) -> UUID:
    """Store one cover letter against the generated CV it was written from.

    Args:
        user_id: The user this letter was written for.
        cv_id: The generated_cv row the letter builds on.
        job_id: The posting that CV targets.
        locale: Language the letter is written in ("en", "es", "fr", "pt").
        answers: The user's own inputs to the generation, stored as jsonb.
        letter: The letter text, paragraphs separated by blank lines.

    Returns:
        The id of the new cover_letters row.

    Raises:
        RuntimeError: If the CV is not this user's CV for this posting, or
            the database rejects the insertion.
    """
    # SQL: INSERT INTO cover_letters (user_id, generated_cv_id, job_id,
    #      locale, answers, letter) VALUES (?, ?, ?, ?, ?, ?) RETURNING id
    stmt = cover_letters.insert().returning(cover_letters.c.id).values(
        user_id=user_id,
        generated_cv_id=cv_id,
        job_id=job_id,
        locale=locale,
        answers=answers,
        letter=letter,
    )
    # SQL: SELECT 1 FROM generated_cv WHERE id = ? AND user_id = ? AND job_id = ?
    owned = select(generated_cv.c.id).where(
        generated_cv.c.id == cv_id,
        generated_cv.c.user_id == user_id,
        generated_cv.c.job_id == job_id,
    )
    try:
        with engine.begin() as conn:
            if conn.execute(owned).first() is None:
                raise ValueError(
                    f"cv {cv_id} is not user {user_id}'s CV for job {job_id}")
            return conn.execute(stmt).scalar_one()
    except Exception as exc:
        raise RuntimeError(f"Database insert failed: {exc}") from exc


# UPDATES

def update_cover_letter(user_id: UUID, letter_id: UUID, letter: str) -> bool:
    """Replace the text of one of a user's cover letters (the user's edit).

    Args:
        user_id: The owner of the letter -- scopes the update.
        letter_id: The cover_letters row to change.
        letter: The edited text.

    Returns:
        Whether a row matched (False: no such letter for this user).

    Raises:
        RuntimeError: If the database rejects the update.
    """
    # SQL: UPDATE cover_letters SET letter = ? WHERE id = ? AND user_id = ?
    stmt = (
        cover_letters.update()
        .where(cover_letters.c.id == letter_id)
        .where(cover_letters.c.user_id == user_id)
        .values(letter=letter)
    )
    try:
        with engine.begin() as conn:
            return conn.execute(stmt).rowcount > 0
    except Exception as exc:
        raise RuntimeError(f"Database update failed: {exc}") from exc


# SELECTIONS

def select_cv_for_letter(
        user_id: UUID, cv_id: UUID) -> Optional[Tuple[UUID, dict, str, str]]:
    """A generated CV with its posting and analysis, for the letter prompt.

    Args:
        user_id: The owner. Another user's CV yields None, never their row.
        cv_id: The generated_cv id.

    Returns:
        Tuple of (job_id, cv, description, depth_analysis), or None.
    """
    # SQL: SELECT generated_cv.job_id, generated_cv.cv,
    #      job_postings.description, ai_analysis.depth_analysis
    #      FROM generated_cv
    #      JOIN job_postings ON job_postings.id = generated_cv.job_id
    #      JOIN ai_analysis ON ai_analysis.id = generated_cv.ai_analysis_id
    #      WHERE generated_cv.id = ? AND generated_cv.user_id = ?
    stmt = (
        select(generated_cv.c.job_id, generated_cv.c.cv,
               job_postings.c.description, ai_analysis.c.depth_analysis)
        .select_from(generated_cv)
        .join(job_postings, job_postings.c.id == generated_cv.c.job_id)
        .join(ai_analysis, ai_analysis.c.id == generated_cv.c.ai_analysis_id)
        .where(generated_cv.c.id == cv_id)
        .where(generated_cv.c.user_id == user_id)
    )
    with engine.connect() as conn:
        return conn.execute(stmt).first()


# SQL: SELECT cover_letters.id, cover_letters.generated_cv_id,
#      cover_letters.job_id, job_postings.title, job_postings.company,
#      ai_analysis.adequation_grade, ai_analysis.depth_analysis,
#      cover_letters.locale, cover_letters.answers, cover_letters.letter,
#      cover_letters.created_at
#      FROM cover_letters
#      JOIN generated_cv ON generated_cv.id = cover_letters.generated_cv_id
#      JOIN job_postings ON job_postings.id = cover_letters.job_id
#      JOIN ai_analysis ON ai_analysis.id = generated_cv.ai_analysis_id
_LETTER_COLUMNS = (
    select(
        cover_letters.c.id, cover_letters.c.generated_cv_id,
        cover_letters.c.job_id, job_postings.c.title, job_postings.c.company,
        ai_analysis.c.adequation_grade, ai_analysis.c.depth_analysis,
        cover_letters.c.locale, cover_letters.c.answers,
        cover_letters.c.letter, cover_letters.c.created_at,
    )
    .select_from(cover_letters)
    .join(generated_cv, generated_cv.c.id == cover_letters.c.generated_cv_id)
    .join(job_postings, job_postings.c.id == cover_letters.c.job_id)
    .join(ai_analysis, ai_analysis.c.id == generated_cv.c.ai_analysis_id)
)


def select_cover_letters(user_id: UUID) -> List[Tuple]:
    """Every cover letter stored for this user, newest first.

    Returns:
        List of (letter_id, cv_id, job_id, title, company, adequation_grade,
        depth_analysis, locale, answers, letter, created_at).
    """
    stmt = (_LETTER_COLUMNS
            .where(cover_letters.c.user_id == user_id)
            .order_by(cover_letters.c.created_at.desc()))
    with engine.connect() as conn:
        return conn.execute(stmt).all()


def select_cover_letter(user_id: UUID, letter_id: UUID) -> Optional[Tuple]:
    """One of this user's cover letters, same tuple as `select_cover_letters`."""
    stmt = _LETTER_COLUMNS.where(cover_letters.c.user_id == user_id,
                                 cover_letters.c.id == letter_id)
    with engine.connect() as conn:
        return conn.execute(stmt).first()
