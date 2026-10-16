import asyncio
import logging
import time
from typing import List, Set
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status

from api.deps import CurrentUser
from api.security import hash_password, verify_password
from api.schemas import (AnalysisOut, AnalyzeOut, AnalyzeRequest, CvCreate,
                         CvOut, CvUpdate, DueOut, PasswordChange, TermIn,
                         TermOut, UserOut)
from ai.main_analysis import NoActiveCv, count_due, run_analysis
from db import analysis as db_analysis
from db import users

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/me", tags=["user"])

# CRUD handlers are plain `def` (FastAPI threads them): db.* is blocking.
# Only analyze is `async def`, awaiting run_analysis.

NOT_FOUND = {404: {"description": "No such row for this user"}}
LIMIT_REACHED = {409: {"description": "Per-user row limit reached"}}
DUPLICATE = {409: {"description": "The user already has this term"}}

# Users with an analysis running. One API worker only: the check-and-add has
# no await between them, so it is atomic on the single event loop.
# ponytail: process-local, a Postgres advisory lock if the API scales out.
# Double runs are already correct (upsert); this only prevents double spend.
_in_flight: Set[UUID] = set()

# Bound unbounded rows (100 kB x N CVs is the real concern). Check-then-insert
# can overshoot by a few under concurrency; this is abuse control, not a quota.
MAX_CVS = 10
MAX_TERMS = 200


def _found(matched: bool) -> None:
    """404 when a user-scoped update/delete matched nothing.

    Another user's row id lands here too, so existence is never leaked.
    """
    if not matched:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")


def _check_room(current: int, maximum: int) -> None:
    if current >= maximum:
        raise HTTPException(status.HTTP_409_CONFLICT, "limit reached")


def _cv(user_id: UUID, cv_id: UUID) -> CvOut:
    """The CV as stored now, 404 if it is not this user's."""
    for row_id, content_md, is_active in users.select_user_cvs(user_id):
        if row_id == cv_id:
            return CvOut(id=row_id, content_md=content_md,
                         is_active=is_active)

    raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")


# USER PROFILE

@router.get("", response_model=UserOut, responses=NOT_FOUND)
def get_user(user_id: CurrentUser) -> UserOut:
    row = users.select_user_by_id(user_id)

    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")

    return UserOut(id=row[0], email=row[1])


@router.put("/password", status_code=status.HTTP_204_NO_CONTENT,
            responses={401: {"description": "Current password is wrong"}})
def change_password(user_id: CurrentUser, body: PasswordChange) -> None:
    row = users.select_user_by_id(user_id)
    stored = users.select_user_by_email(row[1]) if row else None

    if stored is None or not verify_password(
            body.current_password, stored[2]):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "current password is wrong")

    users.update_user_password(user_id, hash_password(body.new_password))


@router.delete("", status_code=status.HTTP_204_NO_CONTENT,
               description="Deletes the account and everything under it. "
                           "Tokens stay valid until they expire.")
def delete_me(user_id: CurrentUser) -> None:
    users.delete_user(user_id)


# KEYWORDS

@router.get("/keywords", response_model=List[TermOut])
def list_keywords(user_id: CurrentUser) -> List[TermOut]:
    return [TermOut(id=i, term=t)
            for i, t in users.select_user_keywords(user_id)]


@router.post("/keywords", response_model=TermOut,
             status_code=status.HTTP_201_CREATED, responses=LIMIT_REACHED,
             description="Idempotent: an existing term returns its row.")
def create_keyword(user_id: CurrentUser, body: TermIn) -> TermOut:
    _check_room(len(users.select_user_keywords(user_id)), MAX_TERMS)

    return TermOut(id=users.insert_user_keyword(user_id, body.term),
                   term=body.term)


@router.put("/keywords/{keyword_id}", response_model=TermOut,
            responses={**NOT_FOUND, **DUPLICATE})
def change_keyword(user_id: CurrentUser, keyword_id: UUID,
                   body: TermIn) -> TermOut:
    try:
        _found(users.update_user_keyword(user_id, keyword_id, body.term))
    except users.DuplicateTerm:
        raise HTTPException(status.HTTP_409_CONFLICT, "term already exists")

    return TermOut(id=keyword_id, term=body.term)


@router.delete("/keywords/{keyword_id}",
               status_code=status.HTTP_204_NO_CONTENT, responses=NOT_FOUND)
def delete_keyword(user_id: CurrentUser, keyword_id: UUID) -> None:
    _found(users.delete_user_keyword(user_id, keyword_id))


# BLACKLIST

@router.get("/blacklist", response_model=List[TermOut])
def list_blacklist(user_id: CurrentUser) -> List[TermOut]:
    return [TermOut(id=i, term=t)
            for i, t in users.select_user_blacklist(user_id)]


@router.post("/blacklist", response_model=TermOut,
             status_code=status.HTTP_201_CREATED, responses=LIMIT_REACHED,
             description="Idempotent: an existing term returns its row.")
def create_blacklist(user_id: CurrentUser, body: TermIn) -> TermOut:
    _check_room(len(users.select_user_blacklist(user_id)), MAX_TERMS)

    return TermOut(id=users.insert_user_blacklist_term(user_id, body.term),
                   term=body.term)


@router.put("/blacklist/{blacklist_id}", response_model=TermOut,
            responses={**NOT_FOUND, **DUPLICATE})
def change_blacklist(user_id: CurrentUser, blacklist_id: UUID,
                     body: TermIn) -> TermOut:
    try:
        _found(users.update_user_blacklist_term(
            user_id, blacklist_id, body.term))
    except users.DuplicateTerm:
        raise HTTPException(status.HTTP_409_CONFLICT, "term already exists")

    return TermOut(id=blacklist_id, term=body.term)


@router.delete("/blacklist/{blacklist_id}",
               status_code=status.HTTP_204_NO_CONTENT, responses=NOT_FOUND)
def delete_blacklist(user_id: CurrentUser, blacklist_id: UUID) -> None:
    _found(users.delete_user_blacklist_term(user_id, blacklist_id))


# CVs

@router.get("/cvs", response_model=List[CvOut])
def list_cvs(user_id: CurrentUser) -> List[CvOut]:
    return [CvOut(id=i, content_md=c, is_active=a)
            for i, c, a in users.select_user_cvs(user_id)]


@router.post("/cvs", response_model=CvOut,
             status_code=status.HTTP_201_CREATED, responses=LIMIT_REACHED)
def create_cv(user_id: CurrentUser, body: CvCreate) -> CvOut:
    _check_room(len(users.select_user_cvs(user_id)), MAX_CVS)
    cv_id = users.insert_user_cv(user_id, body.content_md, body.make_active)

    return _cv(user_id, cv_id)


@router.put("/cvs/{cv_id}", response_model=CvOut,
            responses=NOT_FOUND)
def change_cv(user_id: CurrentUser, cv_id: UUID, body: CvUpdate) -> CvOut:
    _found(users.update_user_cv(user_id, cv_id, body.content_md))

    return _cv(user_id, cv_id)


@router.put("/cvs/{cv_id}/activate", response_model=CvOut,
            responses=NOT_FOUND)
def activate_cv(user_id: CurrentUser, cv_id: UUID) -> CvOut:
    _found(users.set_active_user_cv(user_id, cv_id))

    return _cv(user_id, cv_id)


@router.delete("/cvs/{cv_id}",
               status_code=status.HTTP_204_NO_CONTENT, responses=NOT_FOUND,
               description="Deleting the active CV leaves none active; "
                           "activate another before analysing.")
def delete_cv(user_id: CurrentUser, cv_id: UUID) -> None:
    _found(users.delete_user_cv(user_id, cv_id))


# ANALYSIS

def _analysis_out(row) -> AnalysisOut:
    return AnalysisOut(
        job_id=row[0], adequation_grade=row[1], company=row[2], title=row[3],
        place=row[4], url=row[5], ai_model=row[6], depth_analysis=row[7],
        analysed_at=row[8])


@router.post("/analyze", response_model=AnalyzeOut,
             responses={409: {"description": "No active CV, or a run is "
                                             "already in flight"},
                        502: {"description": "Every grading call failed"}},
             description="Grades the next `limit` matching postings, newest "
                         "first. Call again while `remaining` > 0.")
async def analyze_post(user_id: CurrentUser, body: AnalyzeRequest) -> AnalyzeOut:
    if user_id in _in_flight:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "analysis already running")

    _in_flight.add(user_id)
    started = time.perf_counter()

    try:
        # The window is built from the validated int, never client text:
        # text_interval inlines it into SQL.
        stats = await run_analysis(
            user_id, limit=body.limit, window=f"-{body.window_days} days",
            model=body.model)
    except NoActiveCv:
        raise HTTPException(status.HTTP_409_CONFLICT, "no active CV")
    finally:
        _in_flight.discard(user_id)

    logger.info(
        "analyze user=%s limit=%s window_days=%s due=%s analysed=%s "
        "failed=%s seconds=%.1f", user_id, body.limit, body.window_days,
        stats["due"], stats["analysed"], stats["failed"],
        time.perf_counter() - started)

    if stats["analysed"] == 0 and stats["failed"] > 0:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            "every grading call failed")

    rows = []

    if stats["job_ids"]:
        rows = await asyncio.to_thread(
            db_analysis.select_analysis_page, user_id,
            len(stats["job_ids"]), 0, stats["job_ids"])

    return AnalyzeOut(
        analysed=stats["analysed"], failed=stats["failed"],
        due=stats["due"], filtered_out=stats["filtered_out"],
        remaining=stats["remaining"],
        results=[_analysis_out(r) for r in rows])


@router.get("/analyses", response_model=List[AnalysisOut])
def get_analyses(
        user_id: CurrentUser,
        limit: int = Query(50, ge=1, le=200),
        min_grade: int = Query(0, ge=0, le=100)) -> List[AnalysisOut]:
    rows = db_analysis.select_analysis_page(user_id, limit, min_grade)

    return [_analysis_out(r) for r in rows]


@router.get("/analyses/due", response_model=DueOut)
async def get_due(user_id: CurrentUser,
                  window_days: int = Query(1, ge=1, le=30)) -> DueOut:
    return DueOut(due=await count_due(user_id, f"-{window_days} days"))
