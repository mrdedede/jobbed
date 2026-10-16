"""Request dependencies: who is calling."""

from typing import Annotated, Optional
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from api.security import decode_token

_bearer = HTTPBearer(auto_error=False)


def current_user_id(
        credentials: Annotated[Optional[HTTPAuthorizationCredentials], Depends(_bearer)]) -> UUID:
    """The token's user; 401 for a missing, forged or expired token."""
    user_id = decode_token(credentials.credentials) if credentials else None

    if user_id is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "invalid or missing token",
            headers={"WWW-Authenticate": "Bearer"})

    return user_id


CurrentUser = Annotated[UUID, Depends(current_user_id)]
