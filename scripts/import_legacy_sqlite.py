"""One-off: import the old single-user SQLite history into Postgres.

    python scripts/import_legacy_sqlite.py --sqlite db/joblister.db \
        --email you@example.com [--user-info user_info/]

Creates (or reuses) the user, seeds their keywords / blacklist / active CV
from the old user_info files, then copies postings, grades and generated CVs.
Safe to re-run: every insert skips what is already there. The SQLite file is
opened read-only. Postgres comes from DATABASE_URL, as everywhere else.
"""

import argparse
import getpass
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.schemas import CvMarkdown, Password, Term  # noqa: E402
from api.security import hash_password  # noqa: E402
from db import users  # noqa: E402
from db.engine import engine  # noqa: E402
from db.tables import ai_analysis, generated_cv, job_postings  # noqa: E402

CHUNK = 500


def _utc(stamp):
    """SQLite CURRENT_TIMESTAMP text ("YYYY-MM-DD HH:MM:SS", UTC) -> aware."""
    if not stamp:
        return None

    return datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc)


def _clean(text):
    """Postgres text cannot hold NUL."""
    return text.replace("\x00", "") if text else text


def _chunks(rows):
    for i in range(0, len(rows), CHUNK):
        yield rows[i:i + CHUNK]


def _md5(url):
    return hashlib.md5(url.encode()).hexdigest()


def seed_user(email, password, user_info):
    """Create or reuse the user; seed terms and CV from the old files.

    Returns:
        (user_id, created) -- seeding only happens for what the user lacks.
    """
    found = users.select_user_by_email(email)

    if found:
        user_id, created = found[0], False
    else:
        user_id, created = users.insert_user(
            email, hash_password(TypeAdapter(Password).validate_python(
                password))), True

    for name, insert in (("keywords.txt", users.insert_user_keyword),
                         ("blacklist.txt", users.insert_user_blacklist_term)):
        path = Path(user_info) / name

        if not path.exists():
            continue

        for line in path.read_text().splitlines():
            try:
                insert(user_id, TypeAdapter(Term).validate_python(line))
            except ValidationError:
                if line.strip():
                    print(f"skipped invalid term: {line[:40]!r}")

    cv_path = Path(user_info) / "my_cv.md"

    if cv_path.exists() and users.select_active_user_cv(user_id) is None:
        users.insert_user_cv(user_id, TypeAdapter(CvMarkdown).validate_python(
            cv_path.read_text()))

    return user_id, created


def import_postings(src):
    """Copy job_data. Returns ({old_id: uuid}, total, inserted)."""
    rows = src.execute(
        "SELECT id, company, title, description, url, place, timestamp "
        "FROM job_data WHERE url IS NOT NULL").fetchall()
    values = [dict(company=r[1] or "", title=r[2] or "",
                   description=_clean(r[3]), url=r[4], place=r[5],
                   scraped_at=_utc(r[6])) for r in rows]
    inserted = 0

    with engine.begin() as conn:
        for chunk in _chunks(values):
            # RETURNING: rowcount is -1 for multi-row INSERTs.
            inserted += len(conn.execute(
                pg_insert(job_postings).values(chunk).on_conflict_do_nothing(
                    index_elements=[func.md5(job_postings.c.url)])
                .returning(job_postings.c.id)).all())

        by_md5 = {}

        for chunk in _chunks([v["url"] for v in values]):
            by_md5.update(conn.execute(
                select(func.md5(job_postings.c.url), job_postings.c.id)
                .where(func.md5(job_postings.c.url).in_(
                    [_md5(u) for u in chunk]))).all())

    return ({r[0]: by_md5[_md5(r[4])] for r in rows}, len(rows), inserted)


def import_analyses(src, user_id, job_map):
    """Copy ai_analysis, latest grade per job. Returns (kept, total, inserted)."""
    rows = src.execute(
        "SELECT id, adequation_grade, depth_analysis, ai_model, job_id, "
        "timestamp FROM ai_analysis ORDER BY timestamp, id").fetchall()
    latest = {}  # old job id -> row; later rows overwrite earlier ones

    for r in rows:
        if r[4] in job_map:
            latest[r[4]] = r

    values = [dict(user_id=user_id, job_id=job_map[r[4]],
                   adequation_grade=r[1], depth_analysis=_clean(r[2]),
                   ai_model=r[3], created_at=_utc(r[5]))
              for r in latest.values()]
    inserted = 0

    with engine.begin() as conn:
        for chunk in _chunks(values):
            inserted += len(conn.execute(
                pg_insert(ai_analysis).values(chunk).on_conflict_do_nothing(
                    constraint="uq_ai_analysis_user_job")
                .returning(ai_analysis.c.id)).all())

    return {r[0]: r[4] for r in latest.values()}, len(rows), inserted


def import_cvs(src, user_id, job_map, kept_analyses):
    """Copy generated_cv. Returns (total, inserted, skipped)."""
    rows = src.execute(
        "SELECT locale, cv, job_id, ai_analysis_id, timestamp "
        "FROM generated_cv").fetchall()
    inserted = skipped = 0

    with engine.begin() as conn:
        analysis_ids = dict(conn.execute(
            select(ai_analysis.c.job_id, ai_analysis.c.id)
            .where(ai_analysis.c.user_id == user_id)).all())
        have = set(conn.execute(
            select(generated_cv.c.job_id, generated_cv.c.locale,
                   generated_cv.c.created_at)
            .where(generated_cv.c.user_id == user_id)).all())

        for locale, cv, old_job, old_ai, stamp in rows:
            job_id = job_map.get(old_job)

            if (job_id is None or old_ai not in kept_analyses
                    or job_id not in analysis_ids):
                skipped += 1
                continue

            if (job_id, locale, _utc(stamp)) in have:
                continue

            conn.execute(generated_cv.insert().values(
                user_id=user_id, job_id=job_id,
                ai_analysis_id=analysis_ids[job_id], locale=locale,
                cv=json.loads(cv), created_at=_utc(stamp)))
            inserted += 1

    return len(rows), inserted, skipped


def run(sqlite_path, email, password, user_info):
    src = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    user_id, created = seed_user(email, password, user_info)
    print(f"user {email}: {'created' if created else 'reused'} ({user_id})")

    job_map, total, inserted = import_postings(src)
    print(f"job_postings: {total} legacy, {inserted} new, "
          f"{total - inserted} already present")

    kept, total, inserted = import_analyses(src, user_id, job_map)
    print(f"ai_analysis: {total} legacy, {len(kept)} after keeping the "
          f"latest grade per job, {inserted} new")

    total, inserted, skipped = import_cvs(src, user_id, job_map, kept)
    print(f"generated_cv: {total} legacy, {inserted} new, {skipped} skipped")
    src.close()

    return user_id


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--sqlite", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--user-info", default="user_info")
    args = parser.parse_args()
    # Never argv/env: either would land in shell history or process listings.
    # Only asked for when the account has to be created.
    pw = ("" if users.select_user_by_email(args.email)
          else getpass.getpass("Password for the new account: "))
    run(args.sqlite, args.email, pw, args.user_info)
