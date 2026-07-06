"""OpenRouteService (ORS) API client.

Worldwide routing backend per PLAN.md §6.2.
Auth: API key in `Authorization` header (raw key, no Bearer prefix).
Free-tier limits: 2000 directions/day, 500 optimization/day.

Endpoints used:
  - POST /v2/directions/driving-car  -- point-to-point with optional polyline
  - POST /optimization               -- VRP / TSP solver for multi-stop routes

Docs: https://openrouteservice.org/dev/#/api-docs
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

import httpx

from warroute.config import get_settings

logger = logging.getLogger(__name__)

ORS_API_BASE = "https://api.openrouteservice.org"
DIRECTIONS_PATH = "/v2/directions/driving-car"
OPTIMIZATION_PATH = "/optimization"
GEOCODE_PATH = "/geocode/search"  # ORS Pelias forward geocoding
DEFAULT_TIMEOUT = 60.0
MAX_OPTIMIZATION_JOBS = 25  # ORS-imposed practical ceiling per request


class OrsError(RuntimeError):
    """ORS returned a non-2xx response or malformed body."""


class OrsAuthError(OrsError):
    """401/403 from ORS."""


class OrsQuotaError(OrsError):
    """429 from ORS (daily quota or rate limit)."""


@dataclass
class Waypoint:
    """A point the route should visit. (lat, lon) order to match the rest of the codebase."""

    lat: float
    lon: float
    label: str | None = None

    def to_lon_lat(self) -> list[float]:
        """ORS uses [lon, lat] order in coordinates."""
        return [self.lon, self.lat]


@dataclass
class GeocodeResult:
    """A single geocoder hit. Returned by OrsClient.geocode().

    `name` is the short label (e.g. "Kohl's"); `label` is the full formatted
    address (e.g. "Kohl's, 155 Dorset St, South Burlington, VT 05403, USA").
    """

    name: str
    label: str
    lat: float
    lon: float
    layer: str | None = None  # venue, address, locality, region, etc.
    confidence: float | None = None  # 0.0-1.0 from Pelias
    distance_km: float | None = None  # great-circle from the geocode focus point


@dataclass
class RouteLeg:
    """A solved single-vehicle route through ordered waypoints."""

    distance_m: float
    duration_s: float
    geometry: Any  # GeoJSON LineString or encoded polyline; preserve as-is for the UI
    waypoint_order: list[int] = field(default_factory=list)  # original-index order
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def distance_km(self) -> float:
        return self.distance_m / 1000.0

    @property
    def duration_min(self) -> float:
        return self.duration_s / 60.0


class OrsClient:
    """Async ORS client. One-shot per call (no connection pool needed at single-user scale)."""

    def __init__(
        self,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        settings = get_settings()
        self._api_key = api_key or settings.ors_api_key
        if not self._api_key:
            raise OrsAuthError("ORS_API_KEY must be set in .env")
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> OrsClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=ORS_API_BASE,
                timeout=DEFAULT_TIMEOUT,
                headers={
                    "Authorization": self._api_key,
                    "Accept": "application/json, application/geo+json",
                    "Content-Type": "application/json",
                },
            )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _post(self, path: str, json_body: dict[str, Any]) -> dict[str, Any]:
        if self._client is None:
            raise OrsError("OrsClient must be used as an async context manager")
        try:
            resp = await self._client.post(path, json=json_body)
        except httpx.RequestError as exc:
            raise OrsError(f"ORS request to {path} failed ({type(exc).__name__}): {exc}") from exc

        if resp.status_code in (401, 403):
            raise OrsAuthError(f"ORS rejected key at {path} ({resp.status_code})")
        if resp.status_code == 429:
            raise OrsQuotaError(f"ORS quota or rate limit at {path}")
        if resp.status_code >= 400:
            raise OrsError(f"ORS HTTP {resp.status_code} at {path}: {resp.text[:300]}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise OrsError(f"ORS returned non-JSON at {path}: {resp.text[:200]}") from exc
        if not isinstance(data, dict):
            raise OrsError(f"ORS returned non-object body at {path}: {data!r}")
        return data

    async def geocode(
        self,
        query: str,
        focus: Waypoint | None = None,
        country: str = "US",
        size: int = 5,
    ) -> list[GeocodeResult]:
        """Geocode a free-text place name to one or more (lat, lon) hits.

        Uses ORS Pelias /geocode/search (free tier: ~1000 geocodes/day per key).
        `focus` biases results toward a region (e.g. user's home), which matters
        for short queries like "Kohl's" where there are many global matches.
        Returns an empty list when query is blank or no features come back.
        """
        if not query or not query.strip():
            return []
        if self._client is None:
            raise OrsError("OrsClient must be used as an async context manager")

        params: dict[str, str] = {"text": query.strip(), "size": str(size)}
        if country:
            params["boundary.country"] = country
        if focus is not None:
            params["focus.point.lat"] = f"{focus.lat:.6f}"
            params["focus.point.lon"] = f"{focus.lon:.6f}"

        try:
            resp = await self._client.get(GEOCODE_PATH, params=params)
        except httpx.RequestError as exc:
            raise OrsError(f"ORS geocode failed ({type(exc).__name__}): {exc}") from exc

        if resp.status_code in (401, 403):
            raise OrsAuthError(f"ORS rejected key at {GEOCODE_PATH} ({resp.status_code})")
        if resp.status_code == 429:
            raise OrsQuotaError(f"ORS geocode quota or rate limit at {GEOCODE_PATH}")
        if resp.status_code >= 400:
            raise OrsError(f"ORS geocode HTTP {resp.status_code}: {resp.text[:200]}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise OrsError(f"ORS geocode non-JSON: {resp.text[:200]}") from exc
        if not isinstance(data, dict):
            return []

        results: list[GeocodeResult] = []
        for feat in data.get("features", []):
            if not isinstance(feat, dict):
                continue
            geom = feat.get("geometry") or {}
            props = feat.get("properties") or {}
            if not isinstance(geom, dict) or not isinstance(props, dict):
                continue
            coords = geom.get("coordinates")
            if not isinstance(coords, list) or len(coords) < 2:
                continue
            try:
                lon = float(coords[0])
                lat = float(coords[1])
            except (TypeError, ValueError):
                continue
            confidence_raw = props.get("confidence")
            try:
                confidence = float(confidence_raw) if confidence_raw is not None else None
            except (TypeError, ValueError):
                confidence = None
            layer_raw = props.get("layer")
            distance_km = (
                haversine_km(focus.lat, focus.lon, lat, lon) if focus is not None else None
            )
            results.append(
                GeocodeResult(
                    name=str(props.get("name", "") or ""),
                    label=str(props.get("label", "") or ""),
                    lat=lat,
                    lon=lon,
                    layer=str(layer_raw) if layer_raw is not None else None,
                    confidence=confidence,
                    distance_km=distance_km,
                )
            )
        return results

    async def directions(
        self,
        coordinates: list[Waypoint],
        with_geometry: bool = True,
    ) -> RouteLeg:
        """Solve a directions route through the given coordinates in order."""
        if len(coordinates) < 2:
            raise OrsError("directions requires at least 2 coordinates")
        body: dict[str, Any] = {
            "coordinates": [w.to_lon_lat() for w in coordinates],
            "geometry": with_geometry,
        }
        data = await self._post(DIRECTIONS_PATH, body)
        try:
            route = data["routes"][0]
            summary = route["summary"]
        except (KeyError, IndexError, TypeError) as exc:
            raise OrsError(f"ORS directions response missing routes/summary: {data}") from exc
        return RouteLeg(
            distance_m=float(summary["distance"]),
            duration_s=float(summary["duration"]),
            geometry=route.get("geometry"),
            waypoint_order=list(range(len(coordinates))),
            raw=route,
        )

    async def optimize(
        self,
        start: Waypoint,
        jobs: list[Waypoint],
        end: Waypoint | None = None,
    ) -> RouteLeg:
        """Solve a VRP: visit all `jobs`, starting at `start`, ending at `end` (or back at start)."""
        if not jobs:
            raise OrsError("optimize requires at least one job waypoint")
        if len(jobs) > MAX_OPTIMIZATION_JOBS:
            raise OrsError(
                f"optimize: {len(jobs)} jobs exceeds MAX_OPTIMIZATION_JOBS={MAX_OPTIMIZATION_JOBS}"
            )
        end_wp = end or start
        body: dict[str, Any] = {
            "jobs": [{"id": idx, "location": w.to_lon_lat()} for idx, w in enumerate(jobs)],
            "vehicles": [
                {
                    "id": 1,
                    "profile": "driving-car",
                    "start": start.to_lon_lat(),
                    "end": end_wp.to_lon_lat(),
                }
            ],
        }
        data = await self._post(OPTIMIZATION_PATH, body)
        try:
            route = data["routes"][0]
            summary = route.get("summary") or data.get("summary") or {}
            duration_s = summary.get("duration") or route.get("duration")
            distance_m = summary.get("distance") or route.get("distance") or 0
        except (KeyError, IndexError, TypeError) as exc:
            raise OrsError(f"ORS optimize response shape unexpected: {data}") from exc
        if duration_s is None:
            raise OrsError(f"ORS optimize did not return duration: {data}")

        order = [
            int(step["job"])
            for step in route.get("steps", [])
            if step.get("type") == "job" and step.get("job") is not None
        ]
        return RouteLeg(
            distance_m=float(distance_m),
            duration_s=float(duration_s),
            geometry=None,  # /optimization doesn't return geometry; call directions afterward
            waypoint_order=order,
            raw=route,
        )


_EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km between two WGS84 points."""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))
