"""WiGLE.net API client.

Auth: HTTP Basic (username = WIGLE_NAME, password = WIGLE_TOKEN).
Rate limit: 1 req/sec on the free tier; we throttle to avoid 429s.
Docs: https://api.wigle.net/swagger
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from warroute.config import get_settings

logger = logging.getLogger(__name__)

WIGLE_API_BASE = "https://api.wigle.net"
SEARCH_PATH = "/api/v2/network/search"
PROFILE_PATH = "/api/v2/profile/user"  # cheap auth check; search hits the slow network index
DEFAULT_TIMEOUT = 60.0  # WiGLE free-tier search is slow; 30s hit ReadTimeouts
MIN_INTERVAL_SEC = 1.05  # nudge above 1 req/sec to stay under the throttle


class WigleError(RuntimeError):
    """Raised when the WiGLE API returns a non-success status or malformed body."""


class WigleAuthError(WigleError):
    """Auth failure: missing or rejected credentials."""


class WigleRateLimitError(WigleError):
    """429 from WiGLE, or our local throttle would be violated."""


@dataclass
class BBox:
    """Geographic bounding box in WGS84 degrees."""

    south: float
    north: float
    west: float
    east: float

    def as_query(self) -> dict[str, str]:
        return {
            "latrange1": f"{self.south:.6f}",
            "latrange2": f"{self.north:.6f}",
            "longrange1": f"{self.west:.6f}",
            "longrange2": f"{self.east:.6f}",
        }


@dataclass
class WigleSearchResult:
    total_results: int
    networks: list[dict[str, Any]] = field(default_factory=list)


class WigleClient:
    """Async client for WiGLE search. Throttled to <=1 req/sec process-wide."""

    _last_call_at: float = 0.0
    _throttle_lock = asyncio.Lock()

    def __init__(
        self,
        name: str | None = None,
        token: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        settings = get_settings()
        self._name = name or settings.wigle_name
        self._token = token or settings.wigle_token
        if not self._name or not self._token:
            raise WigleAuthError("WIGLE_NAME and WIGLE_TOKEN must be set in .env")
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> WigleClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=WIGLE_API_BASE,
                timeout=DEFAULT_TIMEOUT,
                auth=(self._name, self._token),
                headers={"Accept": "application/json"},
            )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _throttle(self) -> None:
        async with WigleClient._throttle_lock:
            wait = MIN_INTERVAL_SEC - (time.monotonic() - WigleClient._last_call_at)
            if wait > 0:
                await asyncio.sleep(wait)
            WigleClient._last_call_at = time.monotonic()

    async def profile(self) -> dict[str, Any]:
        """GET /api/v2/profile/user - cheap auth check (sub-second).

        Returns the raw profile dict. Use for liveness/auth probes instead of
        search_bbox, which hits the slow network index.
        """
        if self._client is None:
            raise WigleError("WigleClient must be used as an async context manager")
        await self._throttle()
        try:
            resp = await self._client.get(PROFILE_PATH)
        except httpx.RequestError as exc:
            raise WigleError(f"WiGLE request failed ({type(exc).__name__}): {exc}") from exc

        if resp.status_code == 401:
            raise WigleAuthError("WiGLE rejected credentials (401)")
        if resp.status_code == 429:
            raise WigleRateLimitError("WiGLE rate limit hit (429)")
        if resp.status_code >= 400:
            raise WigleError(f"WiGLE returned HTTP {resp.status_code}: {resp.text[:200]}")

        try:
            return dict(resp.json())
        except (ValueError, TypeError) as exc:
            raise WigleError(f"WiGLE profile non-JSON: {resp.text[:200]}") from exc

    async def search_bbox(
        self,
        bbox: BBox,
        result_per_page: int = 100,
        only_mine: bool = False,
    ) -> WigleSearchResult:
        """Search networks within a bounding box. Returns counts + first page."""
        if self._client is None:
            raise WigleError("WigleClient must be used as an async context manager")

        params = bbox.as_query()
        params["resultsPerPage"] = str(result_per_page)
        if only_mine:
            params["onlymine"] = "true"

        await self._throttle()
        try:
            resp = await self._client.get(SEARCH_PATH, params=params)
        except httpx.RequestError as exc:
            raise WigleError(f"WiGLE request failed ({type(exc).__name__}): {exc}") from exc

        if resp.status_code == 401:
            raise WigleAuthError("WiGLE rejected credentials (401)")
        if resp.status_code == 429:
            raise WigleRateLimitError("WiGLE rate limit hit (429)")
        if resp.status_code >= 400:
            raise WigleError(f"WiGLE returned HTTP {resp.status_code}: {resp.text[:200]}")

        try:
            payload = resp.json()
        except ValueError as exc:
            raise WigleError(f"WiGLE returned non-JSON body: {resp.text[:200]}") from exc

        if not payload.get("success"):
            raise WigleError(f"WiGLE returned success=false: {payload.get('message')}")

        return WigleSearchResult(
            total_results=int(payload.get("totalResults", 0)),
            networks=list(payload.get("results", [])),
        )
