"""Per-user daily LLM quota (table llm_usage; days are UTC dates).

`reserve` takes the units up front, atomically, and `refund` gives back what a
run did not use, so a crash mid-run can only under-count in the user's favour.
"""

from uuid import UUID

from sqlalchemy import text

from db.engine import engine

KIND_ANALYSIS = "analysis"
KIND_CV = "cv_generation"
KIND_TERMS = "term_suggestion"
KIND_LETTER = "cover_letter"


def reserve(user_id: UUID, kind: str, wanted: int, cap: int) -> int:
    """Take up to `wanted` of today's remaining `cap` units.

    Returns:
        Units granted: `wanted`, fewer when the cap is near, 0 when it is hit.
    """
    today = "(now() AT TIME ZONE 'utc')::date"
    args = dict(u=user_id, k=kind)

    with engine.begin() as conn:
        conn.execute(text(
            f"INSERT INTO llm_usage (user_id, day, kind) "
            f"VALUES (:u, {today}, :k) ON CONFLICT DO NOTHING"), args)
        # FOR UPDATE: concurrent reserves for this counter serialise here.
        used = conn.execute(text(
            f"SELECT count FROM llm_usage WHERE user_id = :u AND kind = :k "
            f"AND day = {today} FOR UPDATE"), args).scalar_one()
        granted = max(0, min(wanted, cap - used))
        conn.execute(text(
            f"UPDATE llm_usage SET count = count + :n WHERE user_id = :u "
            f"AND kind = :k AND day = {today}"), dict(args, n=granted))

    return granted


def refund(user_id: UUID, kind: str, units: int) -> None:
    """Give back unused units (never below zero)."""
    if units <= 0:
        return

    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE llm_usage SET count = GREATEST(0, count - :n) "
            "WHERE user_id = :u AND kind = :k "
            "AND day = (now() AT TIME ZONE 'utc')::date"),
            dict(u=user_id, k=kind, n=units))
