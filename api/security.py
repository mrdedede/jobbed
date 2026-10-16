"""Password hashing (bcrypt) and access tokens (PyJWT, HS256).

The token is the only credential the API accepts: `sub` is the user id, and
every route acts on the token's user, so there is no id in a path to forge.
"""

import functools
import os
import time
from typing import Optional
from uuid import UUID

import bcrypt
import jwt

BCRYPT_ROUNDS = 12
TOKEN_TTL_SECONDS = 60 * 60
ALGORITHM = "HS256"
MIN_SECRET_LENGTH = 32
MAX_PASSWORD_BYTES = 72  # bcrypt's limit; longer input is an error in 5.x


def jwt_secret() -> str:
    """The signing key, read per call so tests and startup see the env.

    Raises:
        RuntimeError: JWT_SECRET is unset or too short to sign with.
    """
    secret = os.environ.get("JWT_SECRET", "")

    if len(secret) < MIN_SECRET_LENGTH:
        raise RuntimeError(
            f"JWT_SECRET must be set to at least {MIN_SECRET_LENGTH} chars")

    return secret


def hash_password(password: str) -> str:
    """bcrypt hash, 60 chars (fits users.hashed_password)."""
    return bcrypt.hashpw(
        password.encode(), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode()


def verify_password(password: str, hashed: str) -> bool:
    """Whether the password matches; False, not an error, when it cannot."""
    raw = password.encode()

    if len(raw) > MAX_PASSWORD_BYTES:
        return False

    return bcrypt.checkpw(raw, hashed.encode())


@functools.cache
def _dummy_hash() -> str:
    return hash_password("not-a-real-password")


def burn_a_check(password: str) -> None:
    """Spend one bcrypt compare, so an unknown email costs what a wrong
    password does and login timing does not reveal which accounts exist."""
    verify_password(password, _dummy_hash())


def create_token(user_id: UUID) -> str:
    now = int(time.time())

    return jwt.encode(
        {"sub": str(user_id), "iat": now, "exp": now + TOKEN_TTL_SECONDS},
        jwt_secret(), algorithm=ALGORITHM)


def decode_token(token: str) -> Optional[UUID]:
    """The user id a valid, unexpired token names, else None."""
    try:
        claims = jwt.decode(
            token, jwt_secret(), algorithms=[ALGORITHM],
            options={"require": ["exp", "sub"]})

        return UUID(claims["sub"])
    except (jwt.PyJWTError, ValueError):
        return None
