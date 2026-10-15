from typing import List
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status

from api.schemas import (AnalysisOut, AnalyzeOut, AnalyzeRequest, CvCreate,
                         CvOut, CvUpdate, TermIn, TermOut, UserOut)

router = APIRouter(prefix="/v1/users", tags=["user"])

NOT_IMPLEMENTED = HTTPException(
    status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented")
NOT_FOUND = {404: {"description": "No such row for this user"}}

# USER PROFILE
@router.get("/{user_id}", response_model=UserOut)
async def get_user(user_id: UUID) -> UserOut:
    raise NOT_IMPLEMENTED

# KEYWORDS
@router.get("/{user_id}/keywords", response_model=List[TermOut])
async def list_keywords(user_id: UUID) -> List[TermOut]:
    raise NOT_IMPLEMENTED


@router.post("/{user_id}/keywords", response_model=TermOut,
             status_code=status.HTTP_201_CREATED)
async def create_keyword(user_id: UUID, body: TermIn) -> TermOut:
    raise NOT_IMPLEMENTED


@router.put("/{user_id}/keywords/{keyword_id}", response_model=TermOut,
            responses=NOT_FOUND)
async def change_keyword(user_id: UUID, keyword_id: UUID,
                         body: TermIn) -> TermOut:
    raise NOT_IMPLEMENTED


@router.delete("/{user_id}/keywords/{keyword_id}",
               status_code=status.HTTP_204_NO_CONTENT, responses=NOT_FOUND)
async def delete_keyword(user_id: UUID, keyword_id: UUID) -> None:
    raise NOT_IMPLEMENTED


# BLACKLIST
@router.get("/{user_id}/blacklist", response_model=List[TermOut])
async def list_blacklist(user_id: UUID) -> List[TermOut]:
    raise NOT_IMPLEMENTED


@router.post("/{user_id}/blacklist", response_model=TermOut,
             status_code=status.HTTP_201_CREATED)
async def create_blacklist(user_id: UUID, body: TermIn) -> TermOut:
    raise NOT_IMPLEMENTED


@router.put("/{user_id}/blacklist/{blacklist_id}", response_model=TermOut,
            responses=NOT_FOUND)
async def change_blacklist(user_id: UUID, blacklist_id: UUID,
                           body: TermIn) -> TermOut:
    raise NOT_IMPLEMENTED


@router.delete("/{user_id}/blacklist/{blacklist_id}",
               status_code=status.HTTP_204_NO_CONTENT, responses=NOT_FOUND)
async def delete_blacklist(user_id: UUID, blacklist_id: UUID) -> None:
    raise NOT_IMPLEMENTED


# CVs
@router.get("/{user_id}/cvs", response_model=List[CvOut])
async def list_cvs(user_id: UUID) -> List[CvOut]:
    raise NOT_IMPLEMENTED


@router.post("/{user_id}/cvs", response_model=CvOut,
             status_code=status.HTTP_201_CREATED)
async def create_cv(user_id: UUID, body: CvCreate) -> CvOut:
    raise NOT_IMPLEMENTED


@router.put("/{user_id}/cvs/{cv_id}", response_model=CvOut,
            responses=NOT_FOUND)
async def change_cv(user_id: UUID, cv_id: UUID, body: CvUpdate) -> CvOut:
    raise NOT_IMPLEMENTED


@router.put("/{user_id}/cvs/{cv_id}/activate", response_model=CvOut,
            responses=NOT_FOUND)
async def activate_cv(user_id: UUID, cv_id: UUID) -> CvOut:
    raise NOT_IMPLEMENTED


@router.delete("/{user_id}/cvs/{cv_id}",
               status_code=status.HTTP_204_NO_CONTENT, responses=NOT_FOUND)
async def delete_cv(user_id: UUID, cv_id: UUID) -> None:
    raise NOT_IMPLEMENTED


# ANALYSIS
@router.post("/{user_id}/analyze", response_model=AnalyzeOut,
             responses={409: {"description": "No active CV, or a run is "
                                             "already in flight"},
                        502: {"description": "Every grading call failed"}})
async def analyze_post(user_id: UUID, body: AnalyzeRequest) -> AnalyzeOut:
    raise NOT_IMPLEMENTED


@router.get("/{user_id}/analyses", response_model=List[AnalysisOut])
async def get_analyses(
        user_id: UUID,
        limit: int = Query(50, ge=1, le=200),
        min_grade: int = Query(0, ge=0, le=100)) -> List[AnalysisOut]:
    raise NOT_IMPLEMENTED
