"""Sliding-window rate limits for the public auth routes."""

import time
from collections import defaultdict, deque
from typing import Deque, Dict

from fastapi import HTTPException, status

# ponytail: process-local, like _cv_in_flight (one API worker). A restart
# resets it; a second worker or replica needs a Postgres table or Redis.
# Idle keys are never evicted: a few bytes each, sweep if RSS ever shows it.
_hits: Dict[str, Deque[float]] = defaultdict(deque)


def hit(key: str, limit: int, window: float) -> None:
    """Count one attempt on key; 429 if it had `limit` in the last `window` s.

    Rejected attempts are not counted, so Retry-After is honest.
    """
    now = time.monotonic()
    q = _hits[key]
    while q and q[0] <= now - window:
        q.popleft()

    if len(q) >= limit:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS, "too many attempts",
            headers={"Retry-After": str(int(q[0] + window - now) + 1)})

    q.append(now)
