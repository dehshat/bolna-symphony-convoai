"""Per-client rate limits for the endpoints that cost money: map searches (Google Places), real phone calls
(Bolna credits) and chat (Gemini quota). In-memory and per process, which suits the one-worker deployment."""

from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

_hits: dict[str, deque] = defaultdict(deque)


def client_ip(request: Request) -> str:
    # uvicorn runs with --proxy-headers, so behind Render/Railway this is the real client address
    return request.client.host if request.client else "unknown"


def check(request: Request, bucket: str, per_hour: int) -> None:
    """Raise 429 once this client used `per_hour` requests of `bucket` in the last hour. 0 disables the limit."""
    if per_hour <= 0:
        return
    now = time.monotonic()
    hits = _hits[f"{bucket}:{client_ip(request)}"]
    while hits and now - hits[0] > 3600:
        hits.popleft()
    if len(hits) >= per_hour:
        wait = int(3600 - (now - hits[0])) // 60 + 1
        raise HTTPException(429, f"Too many {bucket} requests from this device. Try again in about {wait} min.")
    hits.append(now)


def reset() -> None:
    _hits.clear()
