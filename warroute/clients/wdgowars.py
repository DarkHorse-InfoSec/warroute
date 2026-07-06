"""WDGoWars API client.

Auth: `X-API-Key: <token>` header. Confirmed empirically 2026-05-11 against
the operator's account; Authorization-header variants (Bearer, raw, Token) all 401.

Known endpoints:
  - GET  /api/me           - player state (ok, username, country, wifi count, ...)
  - POST /api/upload-csv   - submit a WigleWifi-1.6 CSV

Response convention: top-level `ok: true|false`. Errors include `error: <msg>`.

Other endpoints (territory enumeration, per-cell value) remain undocumented;
use `WdgowarsClient.probe(path)` to inspect raw responses and grow the client.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from warroute.config import get_settings

logger = logging.getLogger(__name__)

WDGOWARS_API_BASE = "https://wdgwars.pl"
ME_PATH = "/api/me"
UPLOAD_PATH = "/api/upload-csv"
TERRITORIES_PATH = "/api/territories"
STATS_PATH = "/api/stats"
DEFAULT_TIMEOUT = 60.0


class WdgowarsError(RuntimeError):
    """Raised on any non-2xx WDGoWars response or malformed body."""


class WdgowarsAuthError(WdgowarsError):
    """Auth failure (401/403)."""


class WdgowarsQuotaError(WdgowarsError):
    """The 20k new-AP-per-24h cap (or any other server-side throttle) was hit."""


DAILY_QUOTA_CAP = 20000  # WDGoWars per-account 24h cap on new APs (from PLAN.md)


@dataclass
class PlayerState:
    """Subset of /api/me. Extra fields preserved in `raw`.

    Real /api/me does NOT expose owned-cell IDs (only `reinforce` counts per
    zoom level); owned_cell_ids stays empty until we discover the right
    endpoint. `daily_quota_remaining` is derived from recent_today vs the
    documented 20k/24h cap.
    """

    username: str
    total: int  # total entities discovered (was previously called `points`)
    wifi: int  # WiFi APs only
    ble: int  # BLE devices
    recent_today: int
    daily_quota_remaining: int | None
    owned_cell_ids: list[str]
    raw: dict[str, Any]
    # Richer /api/me fields (see memory reference_wdgowars_api.md). Optional with
    # safe defaults so sparse payloads and older constructor call sites still work.
    country: str | None = None
    gang: str | None = None
    gang_id: int | None = None
    gang_role: str | None = None
    mesh: int = 0
    cracked: int = 0
    aircraft: int = 0
    recent_7d: int = 0
    reinforce_total: int = 0
    credits_balance: int | None = None
    badges: list[str] = field(default_factory=list)
    trusted: bool = False
    is_superuser: bool = False

    @property
    def points(self) -> int:
        """Backwards-compatible alias for callers that still expect `points`."""
        return self.total

    @property
    def badge_count(self) -> int:
        return len(self.badges)


@dataclass
class GangTerritory:
    """One gang's territory from /api/territories (187 gangs, server v1.3.0).

    `hull` is the polygon as returned by the API. The coordinate ORDER of each
    hull point (lat,lon vs lon,lat) is NOT documented and was not captured in
    the 2026-05-11 probe; see WDGOWARS_HULL_IS_LATLON in
    warroute/web/routes/coverage.py for the assumption used when rendering and
    how to flip it after one live check.
    """

    name: str
    gang_id: int | None
    color: str | None
    members: int | None
    points: int | None
    rank: int | None
    hull: list[list[float]]
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
                    "X-API-Key": self._token,
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
            raise WdgowarsError(
                f"WDGoWars request to {path} failed ({type(exc).__name__}): {exc}"
            ) from exc

        if resp.status_code in (401, 403):
            raise WdgowarsAuthError(f"WDGoWars rejected token at {path} ({resp.status_code})")
        if resp.status_code == 429:
            raise WdgowarsQuotaError(f"WDGoWars rate-limit/quota at {path}")
        if resp.status_code >= 400:
            raise WdgowarsError(f"WDGoWars HTTP {resp.status_code} at {path}: {resp.text[:200]}")
        return resp

    async def me(self) -> PlayerState:
        """Fetch /api/me and project into PlayerState. Unknown fields preserved in .raw."""
        resp = await self._request("GET", ME_PATH)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise WdgowarsError(f"/api/me returned non-JSON: {resp.text[:200]}") from exc

        total = _int_or_none(_first_present(payload, "total", "points", "score")) or 0
        wifi = _int_or_none(_first_present(payload, "wifi")) or 0
        ble = _int_or_none(_first_present(payload, "ble")) or 0
        recent_today = _int_or_none(_first_present(payload, "recent_today")) or 0
        explicit_remaining = _int_or_none(
            _first_present(payload, "daily_quota_remaining", "daily_remaining", "quota_remaining")
        )
        derived_remaining = max(DAILY_QUOTA_CAP - recent_today, 0)
        credits = _first_present(payload, "credits")
        credits_balance = (
            _int_or_none(credits.get("balance")) if isinstance(credits, dict) else None
        )
        return PlayerState(
            username=str(_first_present(payload, "username", "name") or ""),
            total=total,
            wifi=wifi,
            ble=ble,
            recent_today=recent_today,
            daily_quota_remaining=(
                explicit_remaining if explicit_remaining is not None else derived_remaining
            ),
            owned_cell_ids=_strings_or_empty(
                _first_present(payload, "owned_cells", "territory", "cells")
            ),
            raw=payload,
            country=_str_or_none(_first_present(payload, "country")),
            gang=_str_or_none(_first_present(payload, "gang")),
            gang_id=_int_or_none(_first_present(payload, "gang_id")),
            gang_role=_str_or_none(_first_present(payload, "gang_role")),
            mesh=_int_or_none(_first_present(payload, "mesh")) or 0,
            cracked=_int_or_none(_first_present(payload, "cracked")) or 0,
            aircraft=_int_or_none(_first_present(payload, "aircraft")) or 0,
            recent_7d=_int_or_none(_first_present(payload, "recent_7d")) or 0,
            reinforce_total=_int_or_none(_first_present(payload, "reinforce_total")) or 0,
            credits_balance=credits_balance,
            badges=_strings_or_empty(_first_present(payload, "badges")),
            trusted=bool(_first_present(payload, "trusted")),
            is_superuser=bool(_first_present(payload, "is_superuser")),
        )

    async def gang_territories(self) -> list[GangTerritory]:
        """Fetch /api/territories: gang polygons for the coverage overlay.

        Returns one GangTerritory per gang. Coordinate order of each hull point
        is preserved as-returned; the coverage route decides how to project it
        into GeoJSON (see WDGOWARS_HULL_IS_LATLON there).
        """
        resp = await self._request("GET", TERRITORIES_PATH)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise WdgowarsError(f"/api/territories returned non-JSON: {resp.text[:200]}") from exc
        # Documented shape is a bare list; tolerate a {"territories": [...]} wrap.
        rows = payload
        if isinstance(payload, dict):
            rows = _first_present(payload, "territories", "gangs", "data") or []
        if not isinstance(rows, list):
            return []
        out: list[GangTerritory] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            out.append(
                GangTerritory(
                    name=str(_first_present(row, "name") or "?"),
                    gang_id=_int_or_none(_first_present(row, "gang_id", "id")),
                    color=_str_or_none(_first_present(row, "color")),
                    members=_int_or_none(_first_present(row, "members")),
                    points=_int_or_none(_first_present(row, "points")),
                    rank=_int_or_none(_first_present(row, "rank")),
                    hull=_coord_pairs(_first_present(row, "hull")),
                    raw=row,
                )
            )
        return out

    async def server_version(self) -> str | None:
        """Return the WDGoWars server version from /api/stats, or None.

        Cheap monitoring hook: a version bump may expose new endpoints worth
        re-probing (per DECISIONS.md 2026-05-11). Never raises for a missing
        field; returns None so callers can degrade gracefully.
        """
        resp = await self._request("GET", STATS_PATH)
        try:
            payload = resp.json()
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        version = _first_present(payload, "version")
        return str(version) if version is not None else None

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


def _str_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _strings_or_empty(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if v is not None]


def _coord_pairs(value: object) -> list[list[float]]:
    """Coerce a hull payload into a list of [a, b] float pairs.

    Preserves the API's coordinate order (does NOT swap lat/lon); the caller
    decides projection. Skips malformed points rather than raising, so one bad
    gang row can't break the whole overlay.
    """
    if not isinstance(value, list):
        return []
    pairs: list[list[float]] = []
    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        try:
            pairs.append([float(point[0]), float(point[1])])
        except (TypeError, ValueError):
            continue
    return pairs


def _first_present(payload: dict[str, Any], *keys: str) -> Any:
    """Return the first value in payload whose key is present (even if value is 0/False/'')."""
    for key in keys:
        if key in payload:
            return payload[key]
    return None
