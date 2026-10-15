from uuid import UUID

from fastapi import APIRouter, HTTPException, status

from api.schemas import Credentials, PasswordChange, TokenOut

router = APIRouter(prefix="/v1/auth", tags=["auth"])

NOT_IMPLEMENTED = HTTPException(
    status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="not implemented")


@router.post("/signup", response_model=TokenOut,
             status_code=status.HTTP_201_CREATED,
             responses={409: {"description": "Email already registered"}})
async def signup(body: Credentials) -> TokenOut:
    raise NOT_IMPLEMENTED


@router.post("/login", response_model=TokenOut,
             responses={401: {"description": "Invalid credentials"}})
async def login(body: Credentials) -> TokenOut:
    raise NOT_IMPLEMENTED

@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(user_id: UUID) -> None:
    raise NOT_IMPLEMENTED


@router.put("/users/{user_id}/password",
            status_code=status.HTTP_204_NO_CONTENT,
            responses={401: {"description": "Current password is wrong"}})
async def update_user_password(user_id: UUID, body: PasswordChange) -> None:
    raise NOT_IMPLEMENTED
