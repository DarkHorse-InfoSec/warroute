"""US Census geocoder client. HTTP mocked via respx."""

from __future__ import annotations

import httpx
import pytest
import respx

from warroute.clients.census import (
    CENSUS_API_BASE,
    ONELINE_PATH,
    CensusClient,
    CensusError,
)
from warroute.clients.ors import Waypoint

_MATCH = {
    "matchedAddress": "1414 MEAD HILL RD, DERBY, VT, 05829",
    "coordinates": {"x": -72.00880, "y": 44.94324},
}


@respx.mock
async def test_census_geocode_parses_matches() -> None:
    respx.get(CENSUS_API_BASE + ONELINE_PATH).mock(
        return_value=httpx.Response(200, json={"result": {"addressMatches": [_MATCH]}})
    )
    async with CensusClient() as c:
        hits = await c.geocode("1414 Mead Hill Rd Derby VT", focus=Waypoint(44.9, -72.2))
    assert len(hits) == 1
    assert hits[0].lat == pytest.approx(44.94324)
    assert hits[0].lon == pytest.approx(-72.00880)
    assert hits[0].name == "1414 MEAD HILL RD"
    assert "DERBY" in hits[0].label
    assert hits[0].distance_km is not None  # computed from focus


@respx.mock
async def test_census_geocode_no_match_returns_empty() -> None:
    respx.get(CENSUS_API_BASE + ONELINE_PATH).mock(
        return_value=httpx.Response(200, json={"result": {"addressMatches": []}})
    )
    async with CensusClient() as c:
        assert await c.geocode("nowhere at all") == []


@respx.mock
async def test_census_geocode_http_error_raises() -> None:
    respx.get(CENSUS_API_BASE + ONELINE_PATH).mock(return_value=httpx.Response(500))
    async with CensusClient() as c:
        with pytest.raises(CensusError):
            await c.geocode("x")


@respx.mock
async def test_census_geocode_bad_body_raises() -> None:
    respx.get(CENSUS_API_BASE + ONELINE_PATH).mock(
        return_value=httpx.Response(200, text="<html>not json</html>")
    )
    async with CensusClient() as c:
        with pytest.raises(CensusError):
            await c.geocode("x")


@respx.mock
async def test_census_skips_malformed_matches() -> None:
    respx.get(CENSUS_API_BASE + ONELINE_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "result": {
                    "addressMatches": [
                        {"matchedAddress": "no coords here"},
                        {"coordinates": {"x": "bad", "y": "bad"}, "matchedAddress": "X"},
                        _MATCH,
                    ]
                }
            },
        )
    )
    async with CensusClient() as c:
        hits = await c.geocode("1414 Mead Hill Rd Derby VT")
    assert len(hits) == 1  # only the well-formed match survived
