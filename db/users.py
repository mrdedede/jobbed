"""Postgres connection management for user accounts and per-user lists.

Covers users, user_keywords, user_blacklist, user_cvs -- the account and
personalization tables, as opposed to db.db_connection's job/analysis/CV
tables. Same approach as db.db_connection: SQLAlchemy Core only, no ORM
Session, no mapped objects.

Password hashing itself (bcrypt) happens in the API/auth layer, not here --
these functions take an already-hashed password in and never see the
plaintext.
"""

from typing import List, Optional, Tuple
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from db.engine import engine
from db.tables import user_blacklist, user_cvs, user_keywords, users


# USERS

def insert_user(email: str, hashed_password: str) -> UUID:
    """Register a new user.

    Args:
        email: The user's email, unique across all accounts.
        hashed_password: The bcrypt hash of their password -- never the
            plaintext, which this function never sees.

    Returns:
        The new user's id.

    Raises:
        RuntimeError: If the email is already registered, or the database
            rejects the insertion.
    """
    # SQL: INSERT INTO users (email, hashed_password)
    #      VALUES (?, ?) RETURNING id
    stmt = (
        users.insert()
        .values(email=email, hashed_password=hashed_password)
        .returning(users.c.id)
    )
    try:
        with engine.begin() as conn:
            return conn.execute(stmt).scalar_one()
    except Exception as exc:
        raise RuntimeError(f"Database insert failed: {exc}") from exc


def select_user_by_email(
        email: str) -> Optional[Tuple[UUID, str, str]]:
    """Look a user up by email, for login.

    Args:
        email: The email to look up.

    Returns:
        Tuple of (id, email, hashed_password), or None if no account
        matches. The caller verifies the password with bcrypt.checkpw
        against the hash this returns -- never compare hashes with `==`.
    """
    # SQL: SELECT id, email, hashed_password FROM users WHERE email = ?
    stmt = select(users.c.id, users.c.email, users.c.hashed_password).where(
        users.c.email == email)
    with engine.connect() as conn:
        return conn.execute(stmt).first()


def select_user_by_id(user_id: UUID) -> Optional[Tuple[UUID, str]]:
    """Look a user up by id.

    Args:
        user_id: The user to look up.

    Returns:
        Tuple of (id, email), or None if no account matches.
    """
    # SQL: SELECT id, email FROM users WHERE id = ?
    stmt = select(users.c.id, users.c.email).where(users.c.id == user_id)
    with engine.connect() as conn:
        return conn.execute(stmt).first()


def update_user_password(user_id: UUID, hashed_password: str) -> None:
    """Replace a user's password hash.

    Args:
        user_id: The user changing their password.
        hashed_password: The new bcrypt hash -- never the plaintext.

    Raises:
        RuntimeError: If the database rejects the update.
    """
    # SQL: UPDATE users SET hashed_password = ? WHERE id = ?
    stmt = (
        users.update()
        .where(users.c.id == user_id)
        .values(hashed_password=hashed_password)
    )
    try:
        with engine.begin() as conn:
            conn.execute(stmt)
    except Exception as exc:
        raise RuntimeError(f"Database update failed: {exc}") from exc


def delete_user(user_id: UUID) -> None:
    """Delete a user's account and everything scoped to it.

    Every table carrying a user_id foreign key (user_keywords,
    user_blacklist, user_cvs, ai_analysis, generated_cv) declares
    ON DELETE CASCADE, so this one statement is enough -- Postgres removes
    the dependent rows itself. See db.tables.

    Args:
        user_id: The account to delete.

    Raises:
        RuntimeError: If the database rejects the deletion.
    """
    # SQL: DELETE FROM users WHERE id = ?
    #      (cascades to user_keywords, user_blacklist, user_cvs,
    #       ai_analysis, generated_cv via ON DELETE CASCADE)
    stmt = users.delete().where(users.c.id == user_id)
    try:
        with engine.begin() as conn:
            conn.execute(stmt)
    except Exception as exc:
        raise RuntimeError(f"Database delete failed: {exc}") from exc


# USER KEYWORDS

def insert_user_keyword(user_id: UUID, term: str) -> None:
    """Add one keyword to a user's list, no-op if it's already there.

    Args:
        user_id: The user this keyword belongs to.
        term: The keyword, stored as given -- callers normalize case/
            whitespace before calling, same as job_scraper.filters expects.

    Raises:
        RuntimeError: If the database rejects the insertion.
    """
    # SQL: INSERT INTO user_keywords (user_id, term)
    #      VALUES (?, ?) ON CONFLICT (user_id, term) DO NOTHING
    stmt = (
        pg_insert(user_keywords)
        .values(user_id=user_id, term=term)
        .on_conflict_do_nothing(
            index_elements=[user_keywords.c.user_id, user_keywords.c.term])
    )
    try:
        with engine.begin() as conn:
            conn.execute(stmt)
    except Exception as exc:
        raise RuntimeError(f"Database insert failed: {exc}") from exc


def update_user_keyword(user_id: UUID, keyword_id: UUID, term: str) -> None:
    """Change the text of one of a user's keywords.

    Args:
        user_id: The owner of the keyword -- scopes the update so one user
            can't edit another's row by guessing an id.
        keyword_id: The keyword row to change.
        term: The new text.

    Raises:
        RuntimeError: If the database rejects the update.
    """
    # SQL: UPDATE user_keywords SET term = ?
    #      WHERE id = ? AND user_id = ?
    stmt = (
        user_keywords.update()
        .where(user_keywords.c.id == keyword_id)
        .where(user_keywords.c.user_id == user_id)
        .values(term=term)
    )
    try:
        with engine.begin() as conn:
            conn.execute(stmt)
    except Exception as exc:
        raise RuntimeError(f"Database update failed: {exc}") from exc


def delete_user_keyword(user_id: UUID, keyword_id: UUID) -> None:
    """Remove one keyword from a user's list.

    Args:
        user_id: The owner of the keyword -- scopes the delete so one user
            can't delete another's row by guessing an id.
        keyword_id: The keyword row to remove.

    Raises:
        RuntimeError: If the database rejects the deletion.
    """
    # SQL: DELETE FROM user_keywords WHERE id = ? AND user_id = ?
    stmt = (
        user_keywords.delete()
        .where(user_keywords.c.id == keyword_id)
        .where(user_keywords.c.user_id == user_id)
    )
    try:
        with engine.begin() as conn:
            conn.execute(stmt)
    except Exception as exc:
        raise RuntimeError(f"Database delete failed: {exc}") from exc


def select_user_keywords(user_id: UUID) -> List[str]:
    """A user's keyword list, for job_scraper.filters.first_filter/second_filter.

    Args:
        user_id: The user whose keywords to fetch.

    Returns:
        List of keyword terms.
    """
    # SQL: SELECT term FROM user_keywords WHERE user_id = ?
    stmt = select(user_keywords.c.term).where(
        user_keywords.c.user_id == user_id)
    with engine.connect() as conn:
        return [term for (term,) in conn.execute(stmt)]


# USER BLACKLIST

def insert_user_blacklist_term(user_id: UUID, term: str) -> None:
    """Add one term to a user's blacklist, no-op if it's already there.

    Args:
        user_id: The user this term belongs to.
        term: The blacklisted term, stored as given.

    Raises:
        RuntimeError: If the database rejects the insertion.
    """
    # SQL: INSERT INTO user_blacklist (user_id, term)
    #      VALUES (?, ?) ON CONFLICT (user_id, term) DO NOTHING
    stmt = (
        pg_insert(user_blacklist)
        .values(user_id=user_id, term=term)
        .on_conflict_do_nothing(
            index_elements=[user_blacklist.c.user_id, user_blacklist.c.term])
    )
    try:
        with engine.begin() as conn:
            conn.execute(stmt)
    except Exception as exc:
        raise RuntimeError(f"Database insert failed: {exc}") from exc


def update_user_blacklist_term(
        user_id: UUID, blacklist_id: UUID, term: str) -> None:
    """Change the text of one of a user's blacklisted terms.

    Args:
        user_id: The owner of the term -- scopes the update.
        blacklist_id: The blacklist row to change.
        term: The new text.

    Raises:
        RuntimeError: If the database rejects the update.
    """
    # SQL: UPDATE user_blacklist SET term = ?
    #      WHERE id = ? AND user_id = ?
    stmt = (
        user_blacklist.update()
        .where(user_blacklist.c.id == blacklist_id)
        .where(user_blacklist.c.user_id == user_id)
        .values(term=term)
    )
    try:
        with engine.begin() as conn:
            conn.execute(stmt)
    except Exception as exc:
        raise RuntimeError(f"Database update failed: {exc}") from exc


def delete_user_blacklist_term(user_id: UUID, blacklist_id: UUID) -> None:
    """Remove one term from a user's blacklist.

    Args:
        user_id: The owner of the term -- scopes the delete.
        blacklist_id: The blacklist row to remove.

    Raises:
        RuntimeError: If the database rejects the deletion.
    """
    # SQL: DELETE FROM user_blacklist WHERE id = ? AND user_id = ?
    stmt = (
        user_blacklist.delete()
        .where(user_blacklist.c.id == blacklist_id)
        .where(user_blacklist.c.user_id == user_id)
    )
    try:
        with engine.begin() as conn:
            conn.execute(stmt)
    except Exception as exc:
        raise RuntimeError(f"Database delete failed: {exc}") from exc


def select_user_blacklist(user_id: UUID) -> List[str]:
    """A user's blacklist, for job_scraper.filters.first_filter/second_filter.

    Args:
        user_id: The user whose blacklist to fetch.

    Returns:
        List of blacklisted terms.
    """
    # SQL: SELECT term FROM user_blacklist WHERE user_id = ?
    stmt = select(user_blacklist.c.term).where(
        user_blacklist.c.user_id == user_id)
    with engine.connect() as conn:
        return [term for (term,) in conn.execute(stmt)]


# USER CVS

def insert_user_cv(user_id: UUID, content_md: str,
                    make_active: bool = True) -> UUID:
    """Store a new CV for a user.

    Args:
        user_id: The user this CV belongs to.
        content_md: The CV body, as Markdown.
        make_active: Whether this becomes the user's active CV (the one
            grading/CV-generation reads). Defaults to True since a user who
            uploads a CV almost always means to use it right away; pass
            False to add a variant without switching away from the current
            active one.

    Returns:
        The new CV's id.

    Raises:
        RuntimeError: If the database rejects the insertion.
    """
    try:
        with engine.begin() as conn:
            if make_active:
                # SQL: UPDATE user_cvs SET is_active = false
                #      WHERE user_id = ?
                conn.execute(
                    user_cvs.update()
                    .where(user_cvs.c.user_id == user_id)
                    .values(is_active=False)
                )
            # SQL: INSERT INTO user_cvs (user_id, content_md, is_active)
            #      VALUES (?, ?, ?) RETURNING id
            return conn.execute(
                user_cvs.insert()
                .values(user_id=user_id, content_md=content_md,
                        is_active=make_active)
                .returning(user_cvs.c.id)
            ).scalar_one()
    except Exception as exc:
        raise RuntimeError(f"Database insert failed: {exc}") from exc


def update_user_cv(user_id: UUID, cv_id: UUID, content_md: str) -> None:
    """Change the text of one of a user's stored CVs.

    Args:
        user_id: The owner of the CV -- scopes the update.
        cv_id: The CV row to change.
        content_md: The new CV body, as Markdown.

    Raises:
        RuntimeError: If the database rejects the update.
    """
    # SQL: UPDATE user_cvs SET content_md = ?
    #      WHERE id = ? AND user_id = ?
    stmt = (
        user_cvs.update()
        .where(user_cvs.c.id == cv_id)
        .where(user_cvs.c.user_id == user_id)
        .values(content_md=content_md)
    )
    try:
        with engine.begin() as conn:
            conn.execute(stmt)
    except Exception as exc:
        raise RuntimeError(f"Database update failed: {exc}") from exc


def set_active_user_cv(user_id: UUID, cv_id: UUID) -> None:
    """Make one of a user's existing CVs the active one, deactivating the rest.

    Args:
        user_id: The owner of the CV -- scopes both updates.
        cv_id: The CV to activate.

    Raises:
        RuntimeError: If the database rejects either update.
    """
    try:
        with engine.begin() as conn:
            # SQL: UPDATE user_cvs SET is_active = false WHERE user_id = ?
            conn.execute(
                user_cvs.update()
                .where(user_cvs.c.user_id == user_id)
                .values(is_active=False)
            )
            # SQL: UPDATE user_cvs SET is_active = true
            #      WHERE id = ? AND user_id = ?
            conn.execute(
                user_cvs.update()
                .where(user_cvs.c.id == cv_id)
                .where(user_cvs.c.user_id == user_id)
                .values(is_active=True)
            )
    except Exception as exc:
        raise RuntimeError(f"Database update failed: {exc}") from exc


def delete_user_cv(user_id: UUID, cv_id: UUID) -> None:
    """Remove one of a user's stored CVs.

    Args:
        user_id: The owner of the CV -- scopes the delete.
        cv_id: The CV row to remove.

    Raises:
        RuntimeError: If the database rejects the deletion.
    """
    # SQL: DELETE FROM user_cvs WHERE id = ? AND user_id = ?
    stmt = (
        user_cvs.delete()
        .where(user_cvs.c.id == cv_id)
        .where(user_cvs.c.user_id == user_id)
    )
    try:
        with engine.begin() as conn:
            conn.execute(stmt)
    except Exception as exc:
        raise RuntimeError(f"Database delete failed: {exc}") from exc


def select_active_user_cv(user_id: UUID) -> Optional[Tuple[UUID, str]]:
    """The CV grading/CV-generation should read for this user.

    Args:
        user_id: The user whose active CV to fetch.

    Returns:
        Tuple of (cv_id, content_md), or None if the user has no CV yet.
    """
    # SQL: SELECT id, content_md FROM user_cvs
    #      WHERE user_id = ? AND is_active = true
    stmt = (
        select(user_cvs.c.id, user_cvs.c.content_md)
        .where(user_cvs.c.user_id == user_id)
        .where(user_cvs.c.is_active.is_(True))
    )
    with engine.connect() as conn:
        return conn.execute(stmt).first()


def select_user_cvs(user_id: UUID) -> List[Tuple[UUID, str, bool]]:
    """Every CV a user has stored, for a "manage your CVs" list view.

    Args:
        user_id: The user whose CVs to fetch.

    Returns:
        List of (id, content_md, is_active).
    """
    # SQL: SELECT id, content_md, is_active FROM user_cvs
    #      WHERE user_id = ?
    stmt = select(
        user_cvs.c.id, user_cvs.c.content_md, user_cvs.c.is_active
    ).where(user_cvs.c.user_id == user_id)
    with engine.connect() as conn:
        return conn.execute(stmt).all()
