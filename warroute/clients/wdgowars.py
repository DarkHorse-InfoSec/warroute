"""WDGoWars API client.

Auth: Bearer token in Authorization header (assumed).
Known endpoints (from PLAN.md):
  - GET  /api/me           - player state, daily quota, owned territory
  - POST /api/upload-csv   - submit a WigleWifi-1.6 CSV

Other endpoints (territory enumeration, per-cell value) are undocumented;
use `WdgowarsClient.probe(path)` to inspect raw responses and grow the client.
See DECISIONS.md (2026-05-10 entry).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from warroute.config import get_settings

logger = logging.getLogger(__name__)

WDGOWARS_API_BASE = "https://wdgwars.pl"
ME_PATH = "/api/me"
UPLOAD_PATH = "/api/upload-csv"
DEFAULT_TIMEOUT = 60.0


class WdgowarsError(RuntimeError):
    """Raised on any non-2xx WDGoWars response or malformed body."""


class WdgowarsAuthError(WdgowarsError):
    """Auth failure (401/403)."""


class WdgowarsQuotaError(WdgowarsError):
    """The 20k new-AP-per-24h cap (or any other server-side throttle) was hit."""


@dataclass
class PlayerState:
    """Subset of /api/me we care about. Extra fields preserved in `raw`."""

    username: str
    points: int
    daily_quota_remaining: int | None
    owned_cell_ids: list[str]
    raw: dict[str, Any]


class WdgowarsClient:
    """Async client for WDGoWars."""

    def __init__(
        self,
        token: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        settings = get_settings()
        self._token = token or settings.wdgowars_token
        if not self._token:
            raise WdgowarsAuthError("WDGOWARS_TOKEN must be set in .env")
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> WdgowarsClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=WDGOWARS_API_BASE,
                timeout=DEFAULT_TIMEOUT,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Accept": "application/json",
                },
            )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if self._client is None:
            raise WdgowarsError("WdgowarsClient must be used as an async context manager")
        try:
            resp = await self._client.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            raise WdgowarsError(f"WDGoWars request to {path} failed: {exc}") from exc

        if resp.status_code in (401, 403):
            raise WdgowarsAuthError(f"WDGoWars rejected token at {path} ({resp.status_code})")
        if resp.status_code == 429:
            raise WdgowarsQuotaError(f"WDGoWars rate-limit/quota at {path}")
        if resp.status_code >= 400:
            raise WdgowarsError(
                f"WDGoWars HTTP {resp.status_code} at {path}: {resp.text[:200]}"
            )
        return resp

    async def me(self) -> PlayerState:
        """Fetch /api/me and project into PlayerState. Unknown fields preserved in .raw."""
        resp = await self._request("GET", ME_PATH)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise WdgowarsError(f"/api/me returned non-JSON: {resp.text[:200]}") from exc

        return PlayerState(
            username=str(payload.get("username") or payload.get("name") or ""),
            points=int(payload.get("points") or payload.get("score") or 0),
            daily_quota_remaining=_int_or_none(
                payload.get("daily_quota_remaining")
                or payload.get("daily_remaining")
                or payload.get("quota_remaining")
            ),
            owned_cell_ids=_strings_or_empty(
                payload.get("owned_cells")
                or payload.get("territory")
                or payload.get("cells")
            ),
            raw=payload,
        )

    async def probe(self, path: str) -> dict[str, Any]:
        """GET an arbitrary path and return the raw JSON. For endpoint discovery."""
        resp = await self._request("GET", path)
        try:
            data = resp.json()
        except ValueError:
            return {"_status": resp.status_code, "_body": resp.text}
        return dict(data) if isinstance(data, dict) else {"_data": data}

    async def upload_csv(self, csv_path: Path) -> dict[str, Any]:
        """POST a WigleWifi-1.6 CSV to /api/upload-csv. Stub: Phase 1 will harden."""
        with csv_path.open("rb") as fh:
            files = {"file": (csv_path.name, fh, "text/csv")}
            resp = await self._request("POST", UPLOAD_PATH, files=files)
        try:
            return dict(resp.json())
        except ValueError:
            return {"_status": resp.status_code, "_body": resp.text[:500]}


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, (float, str)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def _strings_or_empty(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if v is not None]
