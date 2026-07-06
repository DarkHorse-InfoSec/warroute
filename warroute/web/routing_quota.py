"""Shared-ORS carve-out guard for the stateless web tier.

WiGLE/WDGoWars keys are strictly client-supplied (warroute.web.creds). ORS is the
one service most wardrivers lack, so ORS operations may fall back to the operator's
shared key, but only behind a per-IP rate limit and a per-day usage cap, so
anonymous web users can plan without draining the operator's free tier. See
DECISIONS.md 2026-07-04 (design), routing-key resolution.

The per-day cap is a SOFT pre-emptive limit (each counted action may be 1-3 real
ORS ops); the real ORS 429 remains the hard backstop, handled by the ORS client.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict, deque
from dataclasses import dataclass
from enum import StrEnum

from warroute.config import get_settings
from warroute.db import transaction

logger = logging.getLogger(__name__)


class OrsSource(StrEnum):
    USER = "user"  # the user's own key (unlimited, their quota)
    SHARED = "shared"  # operator shared key (counted against the daily cap)
    NONE = "none"  # no key available: user has none and no shared key configured
    RATE_LIMITED = "rate_limited"  # shared key throttled for this IP
    QUOTA_EXHAUSTED = "quota_exhausted"  # shared daily cap reached


@dataclass(frozen=True)
class OrsResolution:
    key: str | None
    source: OrsSource

    @property
    def usable(self) -> bool:
        return self.key is not None


# In-memory per-IP sliding window for the shared key. The deploy is single-process
# uvicorn, so module state is adequate; a multi-worker deploy would need shared
# storage (e.g. Redis). Lock-guarded for safety under the async threadpool.
_rate_lock = threading.Lock()
# Routing (plan optimize/directions) and geocode (address search) get SEPARATE
# per-IP windows. Geocode is per-keystroke type-ahead, so it needs a much higher
# limit; sharing one window would let typing an address starve a plan submit (and
# vice-versa). See DECISIONS.md 2026-07-05 (geocode rate-limit fix).
_rate_hits: dict[str, deque[float]] = defaultdict(deque)
_geo_rate_hits: dict[str, deque[float]] = defaultdict(deque)
_RATE_WINDOW_SEC = 60.0


def _rate_ok(
    hits_map: dict[str, deque[float]], client_ip: str, now: float, max_per_min: int
) -> bool:
    with _rate_lock:
        hits = hits_map[client_ip]
        cutoff = now - _RATE_WINDOW_SEC
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(hits) >= max_per_min:
            return False
        hits.append(now)
        return True


def _shared_used(day: str) -> int:
    with transaction() as conn:
        row = conn.execute(
            "SELECT count FROM shared_routing_usage WHERE day = ?", (day,)
        ).fetchone()
    return int(row["count"]) if row else 0


def _increment_shared(day: str) -> None:
    with transaction() as conn:
        conn.execute(
            "INSERT INTO shared_routing_usage (day, count) VALUES (?, 1)"
            " ON CONFLICT(day) DO UPDATE SET count = count + 1",
            (day,),
        )


def _shared_geocode_used(day: str) -> int:
    with transaction() as conn:
        row = conn.execute(
            "SELECT count FROM shared_geocode_usage WHERE day = ?", (day,)
        ).fetchone()
    return int(row["count"]) if row else 0


def _increment_shared_geocode(day: str) -> None:
    with transaction() as conn:
        conn.execute(
            "INSERT INTO shared_geocode_usage (day, count) VALUES (?, 1)"
            " ON CONFLICT(day) DO UPDATE SET count = count + 1",
            (day,),
        )


def resolve_ors_key(user_key: str | None, client_ip: str, *, day: str, now: float) -> OrsResolution:
    """Decide which ORS key an ORS operation should use, if any.

    - user_key present -> use it (their quota, no counting).
    - else shared key, if configured AND under the per-IP rate limit AND under the
      daily cap; each grant increments the daily counter.
    - else a non-usable resolution whose `source` tells the caller why (so the UI
      can say "add your own ORS key" vs "shared routing is busy, try again").

    `day` is a 'YYYY-MM-DD' UTC string; `now` is a monotonic seconds float. Both are
    injected so callers control the clock and tests stay deterministic.
    """
    if user_key:
        return OrsResolution(user_key, OrsSource.USER)
    settings = get_settings()
    shared = settings.ors_api_key or None
    if not shared:
        return OrsResolution(None, OrsSource.NONE)
    if not _rate_ok(_rate_hits, client_ip, now, settings.ors_shared_rate_per_min):
        logger.info("Shared ORS rate-limited for ip=%s", client_ip)
        return OrsResolution(None, OrsSource.RATE_LIMITED)
    if _shared_used(day) >= settings.ors_shared_daily_cap:
        logger.info("Shared ORS daily cap reached (day=%s)", day)
        return OrsResolution(None, OrsSource.QUOTA_EXHAUSTED)
    _increment_shared(day)
    return OrsResolution(shared, OrsSource.SHARED)


def resolve_geocode_ors_key(
    user_key: str | None, client_ip: str, *, day: str, now: float
) -> OrsResolution:
    """ORS key for GEOCODE (address search type-ahead). Distinct from routing:
    a separate, generous per-IP rate limit and its own daily cap (ORS geocoding is
    a separate quota from directions, and type-ahead is high-frequency). User key
    wins; else the shared key under the geocode per-IP rate limit AND the geocode
    daily cap, which bounds the operator's exposure even under distributed abuse."""
    if user_key:
        return OrsResolution(user_key, OrsSource.USER)
    settings = get_settings()
    shared = settings.ors_api_key or None
    if not shared:
        return OrsResolution(None, OrsSource.NONE)
    if not _rate_ok(_geo_rate_hits, client_ip, now, settings.ors_shared_geocode_rate_per_min):
        logger.info("Shared ORS geocode rate-limited for ip=%s", client_ip)
        return OrsResolution(None, OrsSource.RATE_LIMITED)
    if _shared_geocode_used(day) >= settings.ors_shared_geocode_daily_cap:
        logger.info("Shared ORS geocode daily cap reached (day=%s)", day)
        return OrsResolution(None, OrsSource.QUOTA_EXHAUSTED)
    _increment_shared_geocode(day)
    return OrsResolution(shared, OrsSource.SHARED)


def reset_rate_state() -> None:
    """Test helper: clear the in-memory per-IP rate windows."""
    with _rate_lock:
        _rate_hits.clear()
        _geo_rate_hits.clear()
