"""Postgres connection management for AI grading of job postings.

Covers ai_analysis, joined to job_postings for context -- what the on-demand
per-user analysis path writes to and reads from. SQLAlchemy Core only, no
ORM Session, no mapped objects. Schema lives in db.tables, versioned by
Alembic, not created here.
"""

from typing import Iterator, List, Optional, Sequence, Tuple
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


def select_ungraded_jobs(
        user_id: UUID,
        job_ids: Sequence[UUID]) -> List[Tuple[UUID, str, str, str]]:
    """Of these postings, the ones that exist and this user has not graded.

    Args:
        user_id: The user asking for postings to grade.
        job_ids: The postings the user picked.

    Returns:
        List of (id, company, title, description), in no particular order.
    """
    # SQL: SELECT job_id FROM ai_analysis WHERE user_id = ?
    graded = select(ai_analysis.c.job_id).where(ai_analysis.c.user_id == user_id)
    # SQL: SELECT id, company, title, description FROM job_postings
    #      WHERE id IN (...) AND id NOT IN (<graded subquery above>)
    stmt = (
        select(job_postings.c.id, job_postings.c.company,
               job_postings.c.title, job_postings.c.description)
        .where(job_postings.c.id.in_(job_ids))
        .where(job_postings.c.id.notin_(graded))
    )
    with engine.connect() as conn:
        return conn.execute(stmt).all()


def select_job(job_id: UUID) -> Optional[Tuple]:
    """One posting, description included, for a user deciding to grade it.

    Args:
        job_id: The posting to read.

    Returns:
        (id, company, title, place, url, scraped_at, description), or None.
    """
    # SQL: SELECT id, company, title, place, url, scraped_at, description
    #      FROM job_postings WHERE id = ?
    stmt = select(
        job_postings.c.id, job_postings.c.company, job_postings.c.title,
        job_postings.c.place, job_postings.c.url, job_postings.c.scraped_at,
        job_postings.c.description,
    ).where(job_postings.c.id == job_id)
    with engine.connect() as conn:
        return conn.execute(stmt).first()


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
    stmt = _candidates(user_id, window)
    if limit:
        stmt = stmt.limit(limit)

    with engine.connect() as conn:
        return conn.execute(stmt).all()


def iter_candidates(user_id: UUID, window: str = "-24 hours",
                    chunk: int = 2000) -> Iterator[List[Tuple]]:
    """`select_candidates` in chunks, streamed from a server-side cursor.

    For callers that filter as they go: the full candidate set with its
    descriptions is hundreds of MB once the table holds a backlog.

    Yields:
        Lists of up to `chunk` rows, shaped like `select_candidates`'.
    """
    with engine.connect() as conn:
        result = conn.execution_options(yield_per=chunk).execute(
            _candidates(user_id, window))

        for part in result.partitions():
            yield part


def candidates_state(window: str = "-24 hours") -> Tuple:
    """(posting count, newest scraped_at, window start), in one round trip.

    The first two change whenever postings are added or removed, so a cache
    of filtered candidates can key on them; the third is `now() + window`.
    """
    # SQL: SELECT count(*), max(scraped_at), now() + INTERVAL '<window>'
    #      FROM job_postings
    stmt = select(func.count(), func.max(job_postings.c.scraped_at),
                  func.now() + text_interval(window))

    with engine.connect() as conn:
        return tuple(conn.execute(stmt).one())


def select_graded_ids(user_id: UUID) -> set:
    """Ids of the postings this user has graded."""
    # SQL: SELECT job_id FROM ai_analysis WHERE user_id = ?
    stmt = select(ai_analysis.c.job_id).where(ai_analysis.c.user_id == user_id)

    with engine.connect() as conn:
        return set(conn.execute(stmt).scalars())


def _candidates(user_id: UUID, window: str):
    # SQL: SELECT job_id FROM ai_analysis WHERE user_id = ?
    graded = select(ai_analysis.c.job_id).where(ai_analysis.c.user_id == user_id)
    # SQL: SELECT id, company, title, url, place, description, scraped_at
    #      FROM job_postings
    #      WHERE id NOT IN (<graded subquery above>)
    #      AND scraped_at >= now() + INTERVAL '<window>'
    #      ORDER BY id
    return (
        select(job_postings.c.id, job_postings.c.company,
               job_postings.c.title, job_postings.c.url,
               job_postings.c.place, job_postings.c.description,
               job_postings.c.scraped_at)
        .where(job_postings.c.id.notin_(graded))
        .where(job_postings.c.scraped_at >= func.now() + text_interval(window))
        .order_by(job_postings.c.id)
    )


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


def _analysis_where(user_id: UUID, min_grade: int = 0,
                    job_ids: Optional[Sequence[UUID]] = None,
                    since_days: Optional[int] = None) -> list:
    """The WHERE conditions shared by the analysis page, its count and stats."""
    where = [ai_analysis.c.user_id == user_id]
    if job_ids is not None:
        where.append(ai_analysis.c.job_id.in_(job_ids))
    if min_grade > 0:
        where.append(ai_analysis.c.adequation_grade >= min_grade)
    if since_days is not None:
        # since_days is a validated int, never client text: text_interval
        # inlines it into SQL.
        where.append(ai_analysis.c.created_at
                     >= func.now() + text_interval(f"-{since_days} days"))

    return where


def select_analysis_page(
        user_id: UUID, limit: int, min_grade: int = 0,
        job_ids: Optional[Sequence[UUID]] = None,
        since_days: Optional[int] = None, offset: int = 0) -> List[Tuple]:
    """A user's graded postings, best fit first, without the description.

    The description is large and the url has it, so unlike `select_analyses`
    this is safe to put in a response.

    Args:
        user_id: The user whose analyses to return.
        limit: Cap on rows returned.
        min_grade: Only grades at or above this; 0 keeps ungraded (NULL) rows.
        job_ids: Restrict to these postings (e.g. one run's results).
        since_days: Only analyses made within this many days.
        offset: Rows to skip, for paging.

    Returns:
        List of (job_id, adequation_grade, company, title, place, url,
        ai_model, depth_analysis, analysed_at).
    """
    # SQL: SELECT job_postings.id, ai_analysis.adequation_grade,
    #      job_postings.company, job_postings.title, job_postings.place,
    #      job_postings.url, ai_analysis.ai_model, ai_analysis.depth_analysis,
    #      ai_analysis.created_at
    #      FROM ai_analysis JOIN job_postings ON job_postings.id = ai_analysis.job_id
    #      WHERE <_analysis_where>
    #      ORDER BY adequation_grade DESC NULLS LAST, created_at DESC, id
    #      LIMIT ? OFFSET ?
    stmt = (
        select(
            job_postings.c.id, ai_analysis.c.adequation_grade,
            job_postings.c.company, job_postings.c.title,
            job_postings.c.place, job_postings.c.url, ai_analysis.c.ai_model,
            ai_analysis.c.depth_analysis, ai_analysis.c.created_at,
        )
        .select_from(ai_analysis)
        .join(job_postings, job_postings.c.id == ai_analysis.c.job_id)
        .where(*_analysis_where(user_id, min_grade, job_ids, since_days))
        # The id makes the order total, so pages neither repeat nor skip rows.
        .order_by(ai_analysis.c.adequation_grade.desc().nulls_last(),
                  ai_analysis.c.created_at.desc(), ai_analysis.c.id)
        .limit(limit)
        .offset(offset)
    )
    with engine.connect() as conn:
        return conn.execute(stmt).all()


def count_analyses(user_id: UUID, min_grade: int = 0,
                   since_days: Optional[int] = None) -> int:
    """How many rows `select_analysis_page` pages through with these filters."""
    # SQL: SELECT count(*) FROM ai_analysis WHERE <_analysis_where>
    stmt = (select(func.count()).select_from(ai_analysis)
            .where(*_analysis_where(user_id, min_grade,
                                    since_days=since_days)))
    with engine.connect() as conn:
        return conn.execute(stmt).scalar_one()


def analysis_stats(user_id: UUID, min_grade: int = 0,
                   since_days: Optional[int] = None) -> dict:
    """Count, average grade, grade histogram and average per day, in SQL.

    Ungraded (NULL) rows count towards `count` but not the averages or
    the histogram.

    Returns:
        {"count": int, "average": Optional[int],
         "buckets": {bucket 0-4: count}, "per_day": [(date, average)]},
        a bucket being the grade // 20, 100 falling in bucket 4.
    """
    where = _analysis_where(user_id, min_grade, since_days=since_days)
    grade = ai_analysis.c.adequation_grade
    bucket = func.least(grade // 20, 4)
    day = func.date(func.timezone("UTC", ai_analysis.c.created_at))

    # SQL: SELECT count(*), round(avg(adequation_grade)) FROM ai_analysis
    #      WHERE <_analysis_where>
    totals = select(func.count(), func.round(func.avg(grade))).where(*where)
    # SQL: SELECT least(adequation_grade / 20, 4), count(*) FROM ai_analysis
    #      WHERE <_analysis_where> AND adequation_grade IS NOT NULL GROUP BY 1
    histogram = (select(bucket, func.count()).where(*where, grade.isnot(None))
                 .group_by(bucket))
    # SQL: SELECT date(created_at AT TIME ZONE 'UTC'),
    #      round(avg(adequation_grade)) FROM ai_analysis
    #      WHERE <_analysis_where> AND adequation_grade IS NOT NULL
    #      GROUP BY 1 ORDER BY 1
    per_day = (select(day, func.round(func.avg(grade)))
               .where(*where, grade.isnot(None))
               .group_by(day).order_by(day))

    with engine.connect() as conn:
        count, average = conn.execute(totals).one()
        buckets = dict(conn.execute(histogram).all())
        days = conn.execute(per_day).all()

    return {
        "count": count,
        "average": None if average is None else int(average),
        "buckets": buckets,
        "per_day": [(d, int(a)) for d, a in days],
    }


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
