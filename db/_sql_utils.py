"""Shared SQL-building helpers used across the db.* query modules."""

from sqlalchemy import text


def text_interval(window: str):
    """Build a Postgres INTERVAL literal from a window string like "-24 hours".

    A window string can't be bound as a normal query parameter inside an
    INTERVAL '...' literal -- Postgres only accepts a constant string there,
    not a placeholder -- so it's inlined via f-string. Every caller passes an
    internal default or a caller-trusted constant, never raw end-user input;
    if that stops being true, this needs a safe allowlist before it's inlined.
    """
    return text(f"INTERVAL '{window}'")
