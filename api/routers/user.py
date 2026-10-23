import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, status

from api.deps import CurrentUser
from api.security import hash_password, verify_password
from api.schemas import (allowed_models, AnalysisOut, AnalyzeOut, AnalyzeRequest, CvCreate,
                         CandidateOut, CvOut, CvUpdate, JobOut, PasswordChange, TermIn,
                         TermOut, TermsIn, TermSuggestions, UserOut,
                         GenCvRequest, GenCvOut, GenCvStarted, GenCvStatus,
                         GenCvUpdate)
from ai import gateway
from ai.exceptions import NoActiveCv, NoAnalysisForJob, UnusableAnswer
from ai.main_analysis import list_due, run_analysis
from ai.cv_generation import generate_cv
from ai.term_suggestion import suggest_terms
from db import analysis as db_analysis
from db import generated_cv as db_cvs
from db import usage, users

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/me", tags=["user"])

# CRUD handlers are plain `def` (FastAPI threads them): db.* is blocking.
# Only handlers that await ai.* (analyze, candidates, cvs/generate) are `async def`.

NOT_FOUND = {404: {"description": "No such row for this user"}}
LIMIT_REACHED = {409: {"description": "Per-user row limit reached"}}
DUPLICATE = {409: {"description": "The user already has this term"}}

# Users with an analysis or cv generation running. One API worker only:
# the check-and-add has no await between them, so it is atomic on the single
# event loop.
# ponytail: process-local, a Postgres advisory lock if the API scales out.
# Double runs are already correct (upsert); this only prevents double spend.
_analysis_in_flight: Set[UUID] = set()
_cv_in_flight: Set[UUID] = set()

# Bound unbounded rows (100 kB x N CVs is the real concern). Check-then-insert
# can overshoot by a few under concurrency; the spend quota is db.usage.
MAX_CVS = 10
MAX_TERMS = 200

#: Postings graded per user per UTC day (the LLM spend cap).
DAILY_ANALYSIS_CAP = int(os.environ.get("DAILY_ANALYSIS_CAP", "10"))
#: CVs generated per user per UTC day (Sonnet, the expensive call).
DAILY_CV_CAP = int(os.environ.get("DAILY_CV_CAP", "2"))
#: Term suggestions per user per UTC day (one Haiku call each).
DAILY_TERMS_CAP = int(os.environ.get("DAILY_TERMS_CAP", "5"))


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


@router.post("/terms", status_code=status.HTTP_204_NO_CONTENT,
             responses=LIMIT_REACHED,
             description="Adds several keywords and blacklist terms at once "
                         "(accepted suggestions). Idempotent per term.")
def create_terms(user_id: CurrentUser, body: TermsIn) -> None:
    keywords = {t for _, t in users.select_user_keywords(user_id)}
    blacklist = {t for _, t in users.select_user_blacklist(user_id)}

    if (len(keywords | set(body.keywords)) > MAX_TERMS
            or len(blacklist | set(body.blacklist)) > MAX_TERMS):
        raise HTTPException(status.HTTP_409_CONFLICT, "limit reached")

    for term in body.keywords:
        users.insert_user_keyword(user_id, term)
    for term in body.blacklist:
        users.insert_user_blacklist_term(user_id, term)


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


@router.post("/cvs/{cv_id}/suggest-terms", response_model=TermSuggestions,
             responses={**NOT_FOUND,
                        429: {"description": "Daily suggestion cap reached"},
                        502: {"description": "The model call failed"}},
             description="Asks the model for keywords and blacklist terms "
                         "fitting this CV. Stores nothing: add the ones to "
                         "keep with POST /terms. Uses the first model in "
                         "ANALYSIS_MODELS. Takes 5 s to a few minutes.")
async def suggest_cv_terms(user_id: CurrentUser, cv_id: UUID) -> TermSuggestions:
    cv = await asyncio.to_thread(_cv, user_id, cv_id)
    granted = await asyncio.to_thread(
        usage.reserve, user_id, usage.KIND_TERMS, 1, DAILY_TERMS_CAP)

    if granted == 0:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"daily limit of {DAILY_TERMS_CAP} term suggestions reached",
            headers={"Retry-After": str(_seconds_to_utc_midnight())})

    try:
        # The operator's first model: haiku by default, the local model on
        # an Ollama-only install (ANALYSIS_MODELS=ollama-...).
        suggested = await suggest_terms(cv.content_md, allowed_models()[0])
    except (gateway.GatewayError, UnusableAnswer) as exc:
        logger.warning("term suggestion failed for cv %s: %s", cv_id, exc)
        await asyncio.to_thread(usage.refund, user_id, usage.KIND_TERMS, 1)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            "the AI model call failed")
    except BaseException:
        await asyncio.to_thread(usage.refund, user_id, usage.KIND_TERMS, 1)
        raise

    keywords = {t for _, t in await asyncio.to_thread(
        users.select_user_keywords, user_id)}
    blacklist = {t for _, t in await asyncio.to_thread(
        users.select_user_blacklist, user_id)}

    return TermSuggestions(
        keywords=[t for t in suggested["keywords"] if t not in keywords],
        blacklist=[t for t in suggested["blacklist"] if t not in blacklist])


# ANALYSIS

def _analysis_out(row) -> AnalysisOut:
    return AnalysisOut(
        job_id=row[0], adequation_grade=row[1], company=row[2], title=row[3],
        place=row[4], url=row[5], ai_model=row[6], depth_analysis=row[7],
        analysed_at=row[8])


def _seconds_to_utc_midnight() -> int:
    now = datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)

    return int((tomorrow - now).total_seconds())


@router.post("/analyze", response_model=AnalyzeOut,
             responses={429: {"description": "Daily analysis cap reached"},
                        409: {"description": "No active CV, or a run is "
                                             "already in flight"},
                        502: {"description": "Every grading call failed"}},
             description="Grades the picked postings, in order, up to the "
                         "daily cap. Unknown, already graded and over-cap "
                         "ones come back as `skipped`.")
async def analyze_post(user_id: CurrentUser, body: AnalyzeRequest) -> AnalyzeOut:
    if user_id in _analysis_in_flight:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "analysis already running")

    _analysis_in_flight.add(user_id)
    started = time.perf_counter()
    granted = 0

    try:
        granted = await asyncio.to_thread(
            usage.reserve, user_id, usage.KIND_ANALYSIS, len(body.job_ids),
            DAILY_ANALYSIS_CAP)

        if granted == 0:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"daily limit of {DAILY_ANALYSIS_CAP} analyses reached",
                headers={"Retry-After": str(_seconds_to_utc_midnight())})

        stats = await run_analysis(
            user_id, body.job_ids[:granted], model=body.model)
    except NoActiveCv:
        await asyncio.to_thread(usage.refund, user_id, usage.KIND_ANALYSIS,
                                granted)
        raise HTTPException(status.HTTP_409_CONFLICT, "no active CV")
    except HTTPException:
        raise
    except BaseException:
        await asyncio.to_thread(usage.refund, user_id, usage.KIND_ANALYSIS,
                                granted)
        raise
    finally:
        _analysis_in_flight.discard(user_id)

    # Only stored verdicts count; failed gateway calls cost the user nothing.
    await asyncio.to_thread(usage.refund, user_id, usage.KIND_ANALYSIS,
                            granted - stats["analysed"])
    skipped = stats["skipped"] + len(body.job_ids) - granted

    logger.info(
        "analyze user=%s asked=%s granted=%s analysed=%s failed=%s "
        "skipped=%s seconds=%.1f", user_id, len(body.job_ids), granted,
        stats["analysed"], stats["failed"], skipped,
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
        analysed=stats["analysed"], failed=stats["failed"], skipped=skipped,
        results=[_analysis_out(r) for r in rows])


@router.get("/analyses", response_model=List[AnalysisOut])
def get_analyses(
        user_id: CurrentUser,
        limit: int = Query(50, ge=1, le=200),
        min_grade: int = Query(0, ge=0, le=100),
        since_days: Optional[int] = Query(None, ge=1, le=365)
) -> List[AnalysisOut]:
    rows = db_analysis.select_analysis_page(
        user_id, limit, min_grade, since_days=since_days)

    return [_analysis_out(r) for r in rows]


@router.get("/analyses/candidates", response_model=List[CandidateOut],
            description="Ungraded postings matching the user's keywords and "
                        "blacklist, newest first: what /analyze picks from.")
async def get_candidates(user_id: CurrentUser,
                         window_days: int = Query(1, ge=1, le=30)
                         ) -> List[CandidateOut]:
    # The window is built from the validated int, never client text:
    # text_interval inlines it into SQL.
    rows = await list_due(user_id, f"-{window_days} days")

    return [CandidateOut(job_id=r[0], company=r[1], title=r[2], place=r[3],
                         url=r[4], scraped_at=r[5]) for r in rows]


@router.get("/jobs/{job_id}", response_model=JobOut,
            responses={404: {"description": "No such posting"}})
def get_job(user_id: CurrentUser, job_id: UUID) -> JobOut:
    row = db_analysis.select_job(job_id)
    _found(row is not None)

    return JobOut(job_id=row[0], company=row[1], title=row[2], place=row[3],
                  url=row[4], scraped_at=row[5], description=row[6])


@dataclass
class _Generation:
    user_id: UUID
    status: str = "running"
    cv: Optional[GenCvOut] = None
    error: Optional[dict] = None
    finished: float = 0.0
    task: Optional[asyncio.Task] = field(default=None, repr=False)


# Generation runs in a background task; the client polls this registry.
# ponytail: process-local like _cv_in_flight (one API worker). A restart
# mid-run makes the poll 404 -- the client treats that as "interrupted".
# Move to a table if the API scales out.
_generations: Dict[UUID, _Generation] = {}
_GENERATION_TTL_S = 3600


def _prune_generations() -> None:
    cutoff = time.monotonic() - _GENERATION_TTL_S
    for key in [k for k, g in _generations.items() if g.finished and g.finished < cutoff]:
        del _generations[key]


async def _refund_cv(user_id: UUID) -> None:
    try:
        await asyncio.to_thread(usage.refund, user_id, usage.KIND_CV, 1)
    except Exception:
        logger.exception("cv generation refund failed for %s", user_id)


async def _run_generation(gen: _Generation, job_id: UUID) -> None:
    """Generate one CV, record the outcome, refund the reserved unit on failure.

    Once the row is stored the unit is spent: a failure reading it back still
    reports done (cv=None, the client lists /generated-cvs instead).
    """
    cv_id = None
    try:
        cv_id = await generate_cv(gen.user_id, job_id)
        row = await asyncio.to_thread(db_cvs.select_generated_cv, gen.user_id, cv_id)
        gen.cv = _gen_cv_out(row)
        gen.status = "done"
    except asyncio.CancelledError:
        # Shutdown mid-run: give the unit back before the loop goes away.
        gen.status, gen.error = "failed", {"status": 503, "detail": "interrupted"}
        if cv_id is None:
            await _refund_cv(gen.user_id)
        raise
    except Exception as exc:
        if cv_id is not None:
            logger.exception("generated cv %s stored but not readable", cv_id)
            gen.status = "done"
            return

        known = {
            NoAnalysisForJob: (status.HTTP_409_CONFLICT, "no analysis for job"),
            NoActiveCv: (status.HTTP_409_CONFLICT, "no active CV"),
            UnusableAnswer: (status.HTTP_502_BAD_GATEWAY, "the model's answer was unusable"),
            gateway.GatewayError: (status.HTTP_502_BAD_GATEWAY, "the AI model call failed"),
        }
        code, detail = next((v for k, v in known.items() if isinstance(exc, k)),
                            (status.HTTP_500_INTERNAL_SERVER_ERROR, "generation failed"))
        if code == status.HTTP_500_INTERNAL_SERVER_ERROR:
            logger.exception("cv generation crashed")
        else:
            logger.warning("cv generation failed for job %s: %s (%s)",
                           job_id, detail, exc)
        gen.status, gen.error = "failed", {"status": code, "detail": detail}
        await _refund_cv(gen.user_id)
    finally:
        gen.finished = time.monotonic()
        _cv_in_flight.discard(gen.user_id)


async def cancel_generations() -> None:
    """Shutdown hook: cancel running generations so they refund their unit.

    Only this loop's: a task can be awaited only on the loop that runs it, and
    tests run one loop per TestClient against the same module state.
    """
    loop = asyncio.get_running_loop()
    tasks = [g.task for g in _generations.values()
             if g.task and not g.task.done() and g.task.get_loop() is loop]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _gen_cv_out(row) -> GenCvOut:
    return GenCvOut(cv_id=row[0], title=row[1], company=row[2], adequation_grade=row[3],
                    locale=row[4], cv=row[5], job_id=row[6], created_at=row[7],
                    url=row[8])


@router.post("/cvs/generate", response_model=GenCvStarted,
             status_code=status.HTTP_202_ACCEPTED,
             responses={429: {"description": "Daily generation cap reached"},
                        409: {"description": "A generation is already running"}},
             description="Starts writing a CV tailored to a graded posting. "
                         "Poll GET /cvs/generate/{generation_id} for the result; "
                         "failures inside the run (no analysis, no active CV, "
                         "model errors) arrive in the poll body.")
async def start_cv_generation(user_id: CurrentUser, body: GenCvRequest) -> GenCvStarted:
    if user_id in _cv_in_flight:
        raise HTTPException(status.HTTP_409_CONFLICT, "generation already running")

    # No await between the check above and this add: atomic on the event loop.
    _cv_in_flight.add(user_id)
    try:
        granted = await asyncio.to_thread(
            usage.reserve, user_id, usage.KIND_CV, 1, DAILY_CV_CAP)

        if granted == 0:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"daily limit of {DAILY_CV_CAP} CV generations reached",
                headers={"Retry-After": str(_seconds_to_utc_midnight())})
    except BaseException:
        _cv_in_flight.discard(user_id)
        raise

    _prune_generations()
    generation_id = uuid4()
    gen = _generations[generation_id] = _Generation(user_id)
    # Held so the loop cannot garbage-collect the task mid-run.
    gen.task = asyncio.create_task(_run_generation(gen, body.job_id))

    return GenCvStarted(generation_id=generation_id)


@router.get("/cvs/generate/{generation_id}", response_model=GenCvStatus,
            responses=NOT_FOUND)
def get_cv_generation(user_id: CurrentUser, generation_id: UUID) -> GenCvStatus:
    gen = _generations.get(generation_id)

    if gen is None or gen.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such generation")

    return GenCvStatus(status=gen.status, cv=gen.cv, error=gen.error)


@router.get("/generated-cvs", response_model=List[GenCvOut])
def list_gen_cvs(user_id: CurrentUser) -> List[GenCvOut]:
    return [_gen_cv_out(r) for r in db_cvs.select_generated_cvs(user_id)]


@router.put("/generated-cvs/{cv_id}", response_model=GenCvOut,
            responses=NOT_FOUND)
def change_gen_cv(user_id: CurrentUser, cv_id: UUID,
                  body: GenCvUpdate) -> GenCvOut:
    _found(db_cvs.update_generated_cv(user_id, cv_id, body.cv.model_dump()))

    return _gen_cv_out(db_cvs.select_generated_cv(user_id, cv_id))
