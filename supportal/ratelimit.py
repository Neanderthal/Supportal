"""In-memory sliding-window rate limiter for the browser widget send endpoint.

Flag-gated (``SUPPORT_RATE_LIMIT``, default off): limits one ``user_ref`` to N sends per
window. Keeps a short deque of hit timestamps per key and drops ones older than the window.

In-process only (fine for a single worker). For a multi-process / multi-replica deployment,
put a shared limiter (e.g. Redis) behind the same `allow()` call.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque


class SlidingWindowLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque] = defaultdict(deque)

    def allow(self, key: str, max_hits: int, window: float, now: float | None = None) -> bool:
        """True if `key` is under `max_hits` within the last `window` seconds (and records the hit)."""
        if max_hits <= 0:
            return True                      # misconfigured → don't lock everyone out
        now = time.time() if now is None else now
        hits = self._hits[key]
        cutoff = now - window
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(hits) >= max_hits:
            return False
        hits.append(now)
        return True

    def reset(self) -> None:
        self._hits.clear()


# Module-level singleton shared across requests in-process.
limiter = SlidingWindowLimiter()
