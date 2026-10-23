"""Postgres connection management for the application-tracking board.

Covers applications, read through generated_cv: every posting the user wrote
a CV for is on the board, and one with no applications row is still in the
"CV generated" column. SQLAlchemy Core only, no ORM Session. Schema lives in
db.tables, versioned by Alembic, not created here.
"""

from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import distinct_on, insert

from db.engine import engine
from db.tables import ai_analysis, applications, generated_cv, job_postings

#: The status of a board card that has no applications row.
CV_GENERATED = "cv_generated"


# SQL: SELECT DISTINCT ON (generated_cv.job_id) generated_cv.id,
#      generated_cv.job_id, job_postings.title, job_postings.company,
#      job_postings.place, job_postings.url, ai_analysis.adequation_grade,
#      generated_cv.created_at, coalesce(applications.status, 'cv_generated'),
#      applications.rejected_from, applications.rejection_reason,
#      applications.feedback, applications.updated_at
#      FROM generated_cv
#      JOIN job_postings ON job_postings.id = generated_cv.job_id
#      JOIN ai_analysis ON ai_analysis.id = generated_cv.ai_analysis_id
#      LEFT JOIN applications ON applications.user_id = generated_cv.user_id
#                            AND applications.job_id = generated_cv.job_id
_BOARD_COLUMNS = (
    select(
        generated_cv.c.id, generated_cv.c.job_id, job_postings.c.title,
        job_postings.c.company, job_postings.c.place, job_postings.c.url,
        ai_analysis.c.adequation_grade, generated_cv.c.created_at,
        func.coalesce(applications.c.status, CV_GENERATED),
        applications.c.rejected_from, applications.c.rejection_reason,
        applications.c.feedback, applications.c.updated_at,
    )
    .ext(distinct_on(generated_cv.c.job_id))
    .select_from(generated_cv)
    .join(job_postings, job_postings.c.id == generated_cv.c.job_id)
    .join(ai_analysis, ai_analysis.c.id == generated_cv.c.ai_analysis_id)
    .outerjoin(applications,
               (applications.c.user_id == generated_cv.c.user_id)
               & (applications.c.job_id == generated_cv.c.job_id))
    # DISTINCT ON keeps the first row per job: the newest CV.
    .order_by(generated_cv.c.job_id, generated_cv.c.created_at.desc())
)


# SELECTIONS

def select_board(user_id: UUID) -> List[Tuple]:
    """Every posting this user generated a CV for, with its application state.

    Args:
        user_id: The user whose board to return.

    Returns:
        List of (cv_id, job_id, title, company, place, url, adequation_grade,
        cv_created_at, status, rejected_from, rejection_reason, feedback,
        updated_at), one per posting (its newest CV), rejected ones included.
        `status` is "cv_generated" when the user has not moved the card yet.
    """
    stmt = _BOARD_COLUMNS.where(generated_cv.c.user_id == user_id)
    with engine.connect() as conn:
        return conn.execute(stmt).all()


def select_card(user_id: UUID, job_id: UUID) -> Optional[Tuple]:
    """One card of `select_board`, or None if the user has no CV for the job."""
    stmt = _BOARD_COLUMNS.where(generated_cv.c.user_id == user_id,
                                generated_cv.c.job_id == job_id)
    with engine.connect() as conn:
        return conn.execute(stmt).first()


# UPDATES

def upsert_status(user_id: UUID, job_id: UUID, status: str,
                  reason: Optional[str] = None,
                  feedback: Optional[str] = None) -> bool:
    """Move one of the user's cards to `status` (not "cv_generated").

    Rejecting records the stage the card was at in `rejected_from`; rejecting
    an already rejected card only changes the reason and feedback. Any other
    status clears the rejection fields (a restore).

    Args:
        user_id: The owner -- scopes everything.
        job_id: The posting the card is for.
        status: "applied", "interviews", "jobbed" or "rejected".
        reason: Why it was rejected, rejected only.
        feedback: The user's free text, rejected only.

    Returns:
        Whether the card exists (False: no CV of this user for this job).

    Raises:
        RuntimeError: If the database rejects the write.
    """
    # SQL: SELECT id FROM generated_cv WHERE user_id = ? AND job_id = ?
    #      ORDER BY created_at DESC LIMIT 1
    newest_cv = (
        select(generated_cv.c.id)
        .where(generated_cv.c.user_id == user_id,
               generated_cv.c.job_id == job_id)
        .order_by(generated_cv.c.created_at.desc())
        .limit(1)
    )
    # SQL: SELECT status, rejected_from FROM applications
    #      WHERE user_id = ? AND job_id = ? FOR UPDATE
    current = (
        select(applications.c.status, applications.c.rejected_from)
        .where(applications.c.user_id == user_id,
               applications.c.job_id == job_id)
        .with_for_update()
    )
    try:
        with engine.begin() as conn:
            cv_id = conn.execute(newest_cv).scalar()
            if cv_id is None:
                return False

            row = conn.execute(current).first()
            values = dict(status=status, generated_cv_id=cv_id,
                          rejected_from=None, rejection_reason=None,
                          feedback=None, updated_at=func.now())
            if status == "rejected":
                was, was_from = row if row else (CV_GENERATED, None)
                values.update(rejected_from=was_from if was == "rejected" else was,
                              rejection_reason=reason, feedback=feedback)

            # SQL: INSERT INTO applications (user_id, job_id, ...) VALUES (...)
            #      ON CONFLICT (user_id, job_id) DO UPDATE SET ...
            stmt = insert(applications).values(
                user_id=user_id, job_id=job_id, **values)
            conn.execute(stmt.on_conflict_do_update(
                constraint="uq_applications_user_job", set_=values))
            return True
    except Exception as exc:
        raise RuntimeError(f"Database upsert failed: {exc}") from exc


# DELETIONS

def delete_application(user_id: UUID, job_id: UUID) -> None:
    """Move a card back to "CV generated" (drops its row, if any)."""
    # SQL: DELETE FROM applications WHERE user_id = ? AND job_id = ?
    stmt = applications.delete().where(applications.c.user_id == user_id,
                                       applications.c.job_id == job_id)
    try:
        with engine.begin() as conn:
            conn.execute(stmt)
    except Exception as exc:
        raise RuntimeError(f"Database delete failed: {exc}") from exc
