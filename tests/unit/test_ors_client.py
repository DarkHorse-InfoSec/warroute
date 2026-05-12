"""Tests for the OpenRouteService client. HTTP mocked via respx."""

from __future__ import annotations

import httpx
import pytest
import respx

from warroute.clients.ors import (
    DIRECTIONS_PATH,
    GEOCODE_PATH,
    MAX_OPTIMIZATION_JOBS,
    OPTIMIZATION_PATH,
    ORS_API_BASE,
    GeocodeResult,
    OrsAuthError,
    OrsClient,
    OrsError,
    OrsQuotaError,
    Waypoint,
)

_GEOCODE_SAMPLE = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-73.2127, 44.4759]},
            "properties": {
                "name": "Kohl's",
                "label": "Kohl's, 155 Dorset St, South Burlington, VT, USA",
                "layer": "venue",
                "confidence": 0.9,
            },
        },
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-72.5, 44.2]},
            "properties": {
                "name": "Kohl's",
                "label": "Kohl's, Williston, VT, USA",
                "layer": "venue",
                "confidence": 0.7,
            },
        },
    ],
}


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
    assert (
        '"coordinates": [[-72.21, 44.94], [-72.18, 44.96]]' in body
        or '"coordinates":[[-72.21,44.94],[-72.18,44.96]]' in body
    )


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


# ----- geocode (Pelias) ---------------------------------------------------


@respx.mock
async def test_geocode_parses_features() -> None:
    respx.get(ORS_API_BASE + GEOCODE_PATH).mock(
        return_value=httpx.Response(200, json=_GEOCODE_SAMPLE)
    )
    async with OrsClient() as ors:
        hits = await ors.geocode("Kohls VT", focus=_wp(44.94, -72.21))
    assert len(hits) == 2
    first = hits[0]
    assert isinstance(first, GeocodeResult)
    assert first.name == "Kohl's"
    assert "South Burlington" in first.label
    assert first.lat == pytest.approx(44.4759)
    assert first.lon == pytest.approx(-73.2127)
    assert first.layer == "venue"
    assert first.confidence == pytest.approx(0.9)


@respx.mock
async def test_geocode_empty_query_returns_empty() -> None:
    # No mock needed; we must not hit the network for blank input.
    async with OrsClient() as ors:
        assert await ors.geocode("") == []
        assert await ors.geocode("   ") == []


@respx.mock
async def test_geocode_sends_text_and_focus_params() -> None:
    route = respx.get(ORS_API_BASE + GEOCODE_PATH).mock(
        return_value=httpx.Response(200, json={"features": []})
    )
    async with OrsClient() as ors:
        await ors.geocode("Aunt Mary", focus=_wp(44.9367, -72.2051), country="US", size=3)
    params = dict(route.calls.last.request.url.params)
    assert params["text"] == "Aunt Mary"
    assert params["size"] == "3"
    assert params["boundary.country"] == "US"
    assert params["focus.point.lat"] == "44.936700"
    assert params["focus.point.lon"] == "-72.205100"


@respx.mock
async def test_geocode_raises_on_401() -> None:
    respx.get(ORS_API_BASE + GEOCODE_PATH).mock(return_value=httpx.Response(401))
    async with OrsClient() as ors:
        with pytest.raises(OrsAuthError):
            await ors.geocode("anywhere")


@respx.mock
async def test_geocode_raises_on_429() -> None:
    respx.get(ORS_API_BASE + GEOCODE_PATH).mock(return_value=httpx.Response(429))
    async with OrsClient() as ors:
        with pytest.raises(OrsQuotaError):
            await ors.geocode("anywhere")


@respx.mock
async def test_geocode_request_error_includes_exception_type() -> None:
    respx.get(ORS_API_BASE + GEOCODE_PATH).mock(side_effect=httpx.ReadTimeout(""))
    async with OrsClient() as ors:
        with pytest.raises(OrsError, match="ReadTimeout"):
            await ors.geocode("anywhere")


@respx.mock
async def test_geocode_skips_malformed_features() -> None:
    payload = {
        "features": [
            {"geometry": {"type": "Point", "coordinates": [1.0]}, "properties": {}},  # too few
            {"geometry": None, "properties": {}},  # bad geometry
            "not-a-dict",
            {
                "geometry": {"type": "Point", "coordinates": [-72.0, 44.0]},
                "properties": {"name": "Good", "label": "Good Place"},
            },
        ]
    }
    respx.get(ORS_API_BASE + GEOCODE_PATH).mock(return_value=httpx.Response(200, json=payload))
    async with OrsClient() as ors:
        hits = await ors.geocode("test")
    assert len(hits) == 1
    assert hits[0].name == "Good"


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
