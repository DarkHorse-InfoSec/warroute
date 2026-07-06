"""US Census Bureau geocoder (free, no API key).

The Census "onelineaddress" service is built on TIGER/Line data: every US street
segment with its house-number ranges, including rural roads. So it can pin
"1414 Mead Hill Road, Holland VT" to an actual point by interpolating along the
segment, where OpenStreetMap-based ORS geocoding usually only has the road itself.

US-only: for non-US or no-match queries it returns [], and the geocode endpoint
falls back to ORS (worldwide). See DECISIONS.md 2026-07-05 (census).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from warroute.clients.ors import GeocodeResult, Waypoint, haversine_km

logger = logging.getLogger(__name__)

CENSUS_API_BASE = "https://geocoding.geo.census.gov"
ONELINE_PATH = "/geocoder/locations/onelineaddress"
BENCHMARK = "Public_AR_Current"  # current address-range benchmark
DEFAULT_TIMEOUT = 6.0  # government API; keep type-ahead responsive, fall back to ORS on timeout


class CensusError(RuntimeError):
    """Raised on any Census transport/HTTP/parse failure (caller falls back to ORS)."""


class CensusClient:
    """Async client for the US Census onelineaddress geocoder. No auth."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> CensusClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=CENSUS_API_BASE,
                timeout=DEFAULT_TIMEOUT,
                headers={"Accept": "application/json"},
            )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def geocode(
        self, address: str, *, focus: Waypoint | None = None, size: int = 5
    ) -> list[GeocodeResult]:
        """Geocode a one-line US address. Returns [] for no match (not an error)."""
        if self._client is None:
            raise CensusError("CensusClient must be used as an async context manager")
        params = {"address": address, "benchmark": BENCHMARK, "format": "json"}
        try:
            resp = await self._client.get(ONELINE_PATH, params=params)
        except httpx.RequestError as exc:
            raise CensusError(f"Census request failed ({type(exc).__name__}): {exc}") from exc
        if resp.status_code >= 400:
            raise CensusError(f"Census HTTP {resp.status_code}")
        try:
            payload: Any = resp.json()
            matches = payload["result"]["addressMatches"]
        except (ValueError, KeyError, TypeError) as exc:
            raise CensusError(f"Census returned unexpected body: {resp.text[:150]}") from exc
        if not isinstance(matches, list):
            return []
        out: list[GeocodeResult] = []
        for m in matches[:size]:
            if not isinstance(m, dict):
                continue
            coords = m.get("coordinates") or {}
            try:
                lat = float(coords["y"])
                lon = float(coords["x"])
            except (KeyError, TypeError, ValueError):
                continue
            label = str(m.get("matchedAddress") or "").strip()
            if not label:
                continue
            name = label.split(",")[0].strip() or label
            distance = haversine_km(focus.lat, focus.lon, lat, lon) if focus is not None else None
            out.append(
                GeocodeResult(
                    name=name,
                    label=label,
                    lat=lat,
                    lon=lon,
                    layer="address",
                    confidence=None,
                    distance_km=distance,
                )
            )
        return out
