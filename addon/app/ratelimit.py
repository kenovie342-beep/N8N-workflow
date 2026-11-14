"""Small in-process sliding-window rate limiter (per client IP + bucket)."""
import threading
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

_lock = threading.Lock()
_hits: dict[tuple[str, str], deque] = defaultdict(deque)


def reset() -> None:
    with _lock:
        _hits.clear()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def limit(request: Request, bucket: str, max_hits: int, window: int = 60) -> None:
    key = (client_ip(request), bucket)
    now = time.monotonic()
    with _lock:
        q = _hits[key]
        while q and q[0] < now - window:
            q.popleft()
        if len(q) >= max_hits:
            raise HTTPException(429, "Rate limit exceeded", headers={"Retry-After": str(window)})
        q.append(now)
