"""Tests for the OpenRouteService client. HTTP mocked via respx."""

from __future__ import annotations

import httpx
import pytest
import respx

from warroute.clients.ors import (
    DIRECTIONS_PATH,
    MAX_OPTIMIZATION_JOBS,
    OPTIMIZATION_PATH,
    ORS_API_BASE,
    OrsAuthError,
    OrsClient,
    OrsError,
    OrsQuotaError,
    Waypoint,
)


def _wp(lat: float, lon: float) -> Waypoint:
    return Waypoint(lat=lat, lon=lon)


@respx.mock
async def test_directions_parses_summary() -> None:
    respx.post(ORS_API_BASE + DIRECTIONS_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "routes": [
                    {
                        "summary": {"distance": 12500.5, "duration": 1830.2},
                        "geometry": "encoded_polyline_str",
                    }
                ]
            },
        )
    )
    async with OrsClient() as ors:
        leg = await ors.directions([_wp(44.94, -72.21), _wp(44.96, -72.18)])
    assert leg.distance_m == pytest.approx(12500.5)
    assert leg.duration_s == pytest.approx(1830.2)
    assert leg.geometry == "encoded_polyline_str"
    assert leg.distance_km == pytest.approx(12.5005)
    assert leg.duration_min == pytest.approx(30.503, abs=0.01)


@respx.mock
async def test_directions_sends_lon_lat_order() -> None:
    route = respx.post(ORS_API_BASE + DIRECTIONS_PATH).mock(
        return_value=httpx.Response(
            200,
            json={"routes": [{"summary": {"distance": 0, "duration": 0}}]},
        )
    )
    async with OrsClient() as ors:
        await ors.directions([_wp(44.94, -72.21), _wp(44.96, -72.18)])

    body = route.calls.last.request.read().decode()
    # ORS expects [lon, lat] per coordinate; ensure we're not flipping.
    assert '"coordinates": [[-72.21, 44.94], [-72.18, 44.96]]' in body or \
           '"coordinates":[[-72.21,44.94],[-72.18,44.96]]' in body


@respx.mock
async def test_directions_requires_at_least_two_points() -> None:
    async with OrsClient() as ors:
        with pytest.raises(OrsError):
            await ors.directions([_wp(44.94, -72.21)])


@respx.mock
async def test_optimize_returns_job_order() -> None:
    respx.post(ORS_API_BASE + OPTIMIZATION_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "routes": [
                    {
                        "vehicle": 1,
                        "duration": 4500,
                        "distance": 50000,
                        "steps": [
                            {"type": "start", "location": [-72.20, 44.94]},
                            {"type": "job", "job": 2, "location": [-72.18, 44.96]},
                            {"type": "job", "job": 0, "location": [-72.22, 44.95]},
                            {"type": "job", "job": 1, "location": [-72.16, 44.93]},
                            {"type": "end", "location": [-72.20, 44.94]},
                        ],
                    }
                ]
            },
        )
    )
    async with OrsClient() as ors:
        leg = await ors.optimize(
            start=_wp(44.94, -72.20),
            jobs=[_wp(44.95, -72.22), _wp(44.93, -72.16), _wp(44.96, -72.18)],
        )
    assert leg.duration_s == 4500
    assert leg.distance_m == 50000
    assert leg.waypoint_order == [2, 0, 1]


@respx.mock
async def test_optimize_supports_oneway_with_end() -> None:
    route = respx.post(ORS_API_BASE + OPTIMIZATION_PATH).mock(
        return_value=httpx.Response(
            200,
            json={"routes": [{"vehicle": 1, "duration": 1, "distance": 1, "steps": []}]},
        )
    )
    async with OrsClient() as ors:
        await ors.optimize(
            start=_wp(44.94, -72.20),
            jobs=[_wp(44.95, -72.22)],
            end=_wp(45.00, -72.00),
        )
    body = route.calls.last.request.read().decode()
    # vehicle.start should differ from vehicle.end
    assert '"start": [-72.2, 44.94]' in body or '"start":[-72.2,44.94]' in body
    assert '"end": [-72.0, 45.0]' in body or '"end":[-72.0,45.0]' in body


@respx.mock
async def test_optimize_requires_at_least_one_job() -> None:
    async with OrsClient() as ors:
        with pytest.raises(OrsError):
            await ors.optimize(start=_wp(44.94, -72.20), jobs=[])


@respx.mock
async def test_optimize_rejects_too_many_jobs() -> None:
    too_many = [_wp(44.94 + i * 0.001, -72.20) for i in range(MAX_OPTIMIZATION_JOBS + 1)]
    async with OrsClient() as ors:
        with pytest.raises(OrsError, match="exceeds"):
            await ors.optimize(start=_wp(44.94, -72.20), jobs=too_many)


@respx.mock
async def test_directions_raises_on_403() -> None:
    respx.post(ORS_API_BASE + DIRECTIONS_PATH).mock(return_value=httpx.Response(403))
    async with OrsClient() as ors:
        with pytest.raises(OrsAuthError):
            await ors.directions([_wp(44.94, -72.21), _wp(44.96, -72.18)])


@respx.mock
async def test_directions_raises_on_429() -> None:
    respx.post(ORS_API_BASE + DIRECTIONS_PATH).mock(return_value=httpx.Response(429))
    async with OrsClient() as ors:
        with pytest.raises(OrsQuotaError):
            await ors.directions([_wp(44.94, -72.21), _wp(44.96, -72.18)])


@respx.mock
async def test_directions_raises_on_missing_routes_key() -> None:
    respx.post(ORS_API_BASE + DIRECTIONS_PATH).mock(
        return_value=httpx.Response(200, json={"error": "no route"})
    )
    async with OrsClient() as ors:
        with pytest.raises(OrsError):
            await ors.directions([_wp(44.94, -72.21), _wp(44.96, -72.18)])


def test_missing_api_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from warroute.config import get_settings

    monkeypatch.setenv("ORS_API_KEY", "")
    get_settings.cache_clear()
    with pytest.raises(OrsAuthError):
        OrsClient()


@respx.mock
async def test_authorization_header_is_raw_key() -> None:
    route = respx.post(ORS_API_BASE + DIRECTIONS_PATH).mock(
        return_value=httpx.Response(
            200, json={"routes": [{"summary": {"distance": 0, "duration": 0}}]}
        )
    )
    async with OrsClient() as ors:
        await ors.directions([_wp(44.94, -72.21), _wp(44.96, -72.18)])
    # ORS uses the bare key, NOT "Bearer <key>"
    assert route.calls.last.request.headers["authorization"] == "test-key"
