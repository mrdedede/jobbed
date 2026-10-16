"""Tests for db/users.py."""

import uuid

import pytest

from db import users


# USERS

def test_insert_user_returns_a_usable_id(db):
    user_id = users.insert_user("a@example.com", "hashed-pw")

    assert users.select_user_by_id(user_id) == (user_id, "a@example.com")


def test_insert_user_rejects_a_duplicate_email(db):
    from sqlalchemy.exc import IntegrityError

    users.insert_user("a@example.com", "hashed-pw")

    with pytest.raises((RuntimeError, IntegrityError)):
        users.insert_user("a@example.com", "another-hash")


def test_select_user_by_email_returns_the_hash_for_login(db):
    users.insert_user("a@example.com", "hashed-pw")

    user_id, email, hashed_password = users.select_user_by_email(
        "a@example.com")

    assert email == "a@example.com"
    assert hashed_password == "hashed-pw"


def test_select_user_by_email_none_when_unregistered(db):
    assert users.select_user_by_email("nobody@example.com") is None


def test_update_user_password_replaces_the_hash(db):
    user_id = users.insert_user("a@example.com", "old-hash")

    users.update_user_password(user_id, "new-hash")

    _, _, hashed_password = users.select_user_by_email("a@example.com")
    assert hashed_password == "new-hash"


def test_delete_user_removes_the_account(db):
    user_id = users.insert_user("a@example.com", "hashed-pw")

    users.delete_user(user_id)

    assert users.select_user_by_id(user_id) is None


def test_delete_user_cascades_to_every_per_user_table(db):
    """The whole point of ON DELETE CASCADE on every user_id FK: deleting a
    user must not leave orphaned keywords/blacklist/CVs/analyses behind, and
    must not raise an FK violation either."""
    user_id = users.insert_user("a@example.com", "hashed-pw")
    users.insert_user_keyword(user_id, "python")
    users.insert_user_blacklist_term(user_id, "unpaid")
    users.insert_user_cv(user_id, "# My CV")

    users.delete_user(user_id)

    assert _terms(users.select_user_keywords(user_id)) == []
    assert _terms(users.select_user_blacklist(user_id)) == []
    assert users.select_user_cvs(user_id) == []


# USER KEYWORDS

def test_insert_user_keyword_is_idempotent(db):
    user_id = users.insert_user("a@example.com", "hashed-pw")

    users.insert_user_keyword(user_id, "python")
    users.insert_user_keyword(user_id, "python")

    assert _terms(users.select_user_keywords(user_id)) == ["python"]


def test_update_user_keyword_changes_the_term(db):
    user_id = users.insert_user("a@example.com", "hashed-pw")
    users.insert_user_keyword(user_id, "python")
    (keyword_id,) = _keyword_ids(user_id)

    users.update_user_keyword(user_id, keyword_id, "golang")

    assert _terms(users.select_user_keywords(user_id)) == ["golang"]


def test_update_user_keyword_is_scoped_to_its_owner(db):
    """Another user's id must not let you edit someone else's keyword."""
    owner = users.insert_user("owner@example.com", "hashed-pw")
    other = users.insert_user("other@example.com", "hashed-pw")
    users.insert_user_keyword(owner, "python")
    (keyword_id,) = _keyword_ids(owner)

    users.update_user_keyword(other, keyword_id, "golang")

    assert _terms(users.select_user_keywords(owner)) == ["python"]


def test_delete_user_keyword_removes_it(db):
    user_id = users.insert_user("a@example.com", "hashed-pw")
    users.insert_user_keyword(user_id, "python")
    (keyword_id,) = _keyword_ids(user_id)

    users.delete_user_keyword(user_id, keyword_id)

    assert _terms(users.select_user_keywords(user_id)) == []


def test_select_user_keywords_is_per_user(db):
    user_a = users.insert_user("a@example.com", "hashed-pw")
    user_b = users.insert_user("b@example.com", "hashed-pw")
    users.insert_user_keyword(user_a, "python")
    users.insert_user_keyword(user_b, "golang")

    assert _terms(users.select_user_keywords(user_a)) == ["python"]
    assert _terms(users.select_user_keywords(user_b)) == ["golang"]


# USER BLACKLIST

def test_insert_user_blacklist_term_is_idempotent(db):
    user_id = users.insert_user("a@example.com", "hashed-pw")

    users.insert_user_blacklist_term(user_id, "unpaid")
    users.insert_user_blacklist_term(user_id, "unpaid")

    assert _terms(users.select_user_blacklist(user_id)) == ["unpaid"]


def test_update_user_blacklist_term_changes_the_term(db):
    user_id = users.insert_user("a@example.com", "hashed-pw")
    users.insert_user_blacklist_term(user_id, "unpaid")
    (blacklist_id,) = _blacklist_ids(user_id)

    users.update_user_blacklist_term(user_id, blacklist_id, "internship")

    assert _terms(users.select_user_blacklist(user_id)) == ["internship"]


def test_delete_user_blacklist_term_removes_it(db):
    user_id = users.insert_user("a@example.com", "hashed-pw")
    users.insert_user_blacklist_term(user_id, "unpaid")
    (blacklist_id,) = _blacklist_ids(user_id)

    users.delete_user_blacklist_term(user_id, blacklist_id)

    assert _terms(users.select_user_blacklist(user_id)) == []


# USER CVS

def test_insert_user_cv_defaults_to_active(db):
    user_id = users.insert_user("a@example.com", "hashed-pw")

    cv_id = users.insert_user_cv(user_id, "# My CV")

    assert users.select_active_user_cv(user_id) == (cv_id, "# My CV")


def test_insert_user_cv_can_skip_activation(db):
    user_id = users.insert_user("a@example.com", "hashed-pw")
    first_id = users.insert_user_cv(user_id, "# First")

    users.insert_user_cv(user_id, "# Second", make_active=False)

    assert users.select_active_user_cv(user_id) == (first_id, "# First")


def test_insert_user_cv_deactivates_the_previous_active_one(db):
    user_id = users.insert_user("a@example.com", "hashed-pw")
    users.insert_user_cv(user_id, "# First")

    second_id = users.insert_user_cv(user_id, "# Second")

    assert users.select_active_user_cv(user_id) == (second_id, "# Second")
    cvs = {cv_id: is_active for cv_id, _, is_active in
           users.select_user_cvs(user_id)}
    assert sum(cvs.values()) == 1


def test_set_active_user_cv_switches_which_one_is_active(db):
    user_id = users.insert_user("a@example.com", "hashed-pw")
    first_id = users.insert_user_cv(user_id, "# First")
    users.insert_user_cv(user_id, "# Second")

    users.set_active_user_cv(user_id, first_id)

    assert users.select_active_user_cv(user_id) == (first_id, "# First")


def test_update_user_cv_changes_the_content(db):
    user_id = users.insert_user("a@example.com", "hashed-pw")
    cv_id = users.insert_user_cv(user_id, "# Draft")

    users.update_user_cv(user_id, cv_id, "# Final")

    assert users.select_active_user_cv(user_id) == (cv_id, "# Final")


def test_delete_user_cv_removes_it(db):
    user_id = users.insert_user("a@example.com", "hashed-pw")
    cv_id = users.insert_user_cv(user_id, "# My CV")

    users.delete_user_cv(user_id, cv_id)

    assert users.select_user_cvs(user_id) == []


def test_select_active_user_cv_none_when_no_cv_stored(db):
    user_id = users.insert_user("a@example.com", "hashed-pw")

    assert users.select_active_user_cv(user_id) is None


# RETURN VALUES THE API RELIES ON

def test_insert_user_keyword_returns_the_id_even_when_already_there(db):
    user_id = users.insert_user("a@example.com", "hashed-pw")

    first = users.insert_user_keyword(user_id, "python")
    second = users.insert_user_keyword(user_id, "python")

    assert first == second
    assert users.select_user_keywords(user_id) == [(first, "python")]


def test_insert_user_blacklist_term_returns_the_id_even_when_already_there(db):
    user_id = users.insert_user("a@example.com", "hashed-pw")

    first = users.insert_user_blacklist_term(user_id, "unpaid")
    second = users.insert_user_blacklist_term(user_id, "unpaid")

    assert first == second
    assert users.select_user_blacklist(user_id) == [(first, "unpaid")]


def test_update_and_delete_report_whether_a_row_matched(db):
    owner = users.insert_user("owner@example.com", "hashed-pw")
    other = users.insert_user("other@example.com", "hashed-pw")
    keyword_id = users.insert_user_keyword(owner, "python")
    cv_id = users.insert_user_cv(owner, "# CV")

    assert users.update_user_keyword(other, keyword_id, "go") is False
    assert users.delete_user_keyword(other, keyword_id) is False
    assert users.update_user_cv(other, cv_id, "# x") is False
    assert users.delete_user_cv(other, cv_id) is False
    assert users.update_user_keyword(owner, keyword_id, "go") is True
    assert users.delete_user_keyword(owner, keyword_id) is True


def test_set_active_user_cv_unknown_id_keeps_the_current_active_cv(db):
    user_id = users.insert_user("a@example.com", "hashed-pw")
    other = users.insert_user("b@example.com", "hashed-pw")
    current = users.insert_user_cv(user_id, "# Mine")
    foreign = users.insert_user_cv(other, "# Theirs")

    assert users.set_active_user_cv(user_id, foreign) is False
    assert users.select_active_user_cv(user_id) == (current, "# Mine")


# HELPERS

def _terms(rows):
    return [term for _, term in rows]


def _keyword_ids(user_id):
    from sqlalchemy import select
    from db.engine import engine
    from db.tables import user_keywords

    with engine.connect() as conn:
        return [row[0] for row in conn.execute(
            select(user_keywords.c.id)
            .where(user_keywords.c.user_id == user_id))]


def _blacklist_ids(user_id):
    from sqlalchemy import select
    from db.engine import engine
    from db.tables import user_blacklist

    with engine.connect() as conn:
        return [row[0] for row in conn.execute(
            select(user_blacklist.c.id)
            .where(user_blacklist.c.user_id == user_id))]


def test_email_is_case_insensitive(db):
    from sqlalchemy.exc import IntegrityError

    user_id = users.insert_user("  A@Example.com ", "hashed-pw")

    assert users.select_user_by_email("a@EXAMPLE.com")[0] == user_id
    assert users.select_user_by_id(user_id)[1] == "a@example.com"

    with pytest.raises((RuntimeError, IntegrityError)):
        users.insert_user("a@example.com", "other")


def test_only_one_cv_can_be_active_per_user(db):
    from sqlalchemy.exc import IntegrityError
    from db.engine import engine
    from db.tables import user_cvs

    user_id = users.insert_user("a@example.com", "hashed-pw")
    users.insert_user_cv(user_id, "one")

    with pytest.raises(IntegrityError):
        with engine.begin() as conn:
            conn.execute(user_cvs.insert().values(
                user_id=user_id, content_md="two", is_active=True))


@pytest.mark.parametrize("insert,update", [
    (users.insert_user_keyword, users.update_user_keyword),
    (users.insert_user_blacklist_term, users.update_user_blacklist_term),
])
def test_renaming_onto_an_existing_term_raises_duplicate_term(
        db, insert, update):
    user_id = users.insert_user("dup@example.com", "hashed")
    insert(user_id, "python")
    other = insert(user_id, "golang")

    with pytest.raises(users.DuplicateTerm):
        update(user_id, other, "python")
