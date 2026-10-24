"""Public routes: the only ones that work without a token."""

from fastapi import APIRouter, HTTPException, Request, status

from api.ratelimit import hit
from api.schemas import Credentials, TokenOut
from api.security import (burn_a_check, create_token, hash_password,
                          verify_password)
from db import users

router = APIRouter(prefix="/v1/auth", tags=["auth"])

TOO_MANY = {429: {"description": "Too many attempts; see Retry-After"}}

# Checked before bcrypt runs, so a rejected attempt costs nothing. Per IP
# stops one client spraying many accounts; per email stops many clients
# guessing one password.
SIGNUPS_PER_IP_PER_HOUR = 5
LOGINS_PER_IP_PER_15_MIN = 20
LOGINS_PER_EMAIL_PER_15_MIN = 10


@router.post("/signup", response_model=TokenOut,
             status_code=status.HTTP_201_CREATED,
             responses={409: {"description": "Email already registered"},
                        **TOO_MANY})
def signup(body: Credentials, request: Request) -> TokenOut:
    hit(f"signup:{request.client.host}", SIGNUPS_PER_IP_PER_HOUR, 3600)

    try:
        user_id = users.insert_user(body.email, hash_password(body.password))
    except users.EmailTaken:
        raise HTTPException(status.HTTP_409_CONFLICT, "email already used")

    return TokenOut(access_token=create_token(user_id))


@router.post("/login", response_model=TokenOut,
             responses={401: {"description": "Invalid credentials"},
                        **TOO_MANY})
def login(body: Credentials, request: Request) -> TokenOut:
    hit(f"login-ip:{request.client.host}", LOGINS_PER_IP_PER_15_MIN, 900)
    hit(f"login-email:{body.email}", LOGINS_PER_EMAIL_PER_15_MIN, 900)

    row = users.select_user_by_email(body.email)

    # Same 401 and same bcrypt cost for an unknown email and a wrong password.
    if row is None:
        burn_a_check(body.password)
    elif verify_password(body.password, row[2]):
        return TokenOut(access_token=create_token(row[0]))

    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED, "invalid credentials",
        headers={"WWW-Authenticate": "Bearer"})
