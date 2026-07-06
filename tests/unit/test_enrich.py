"""Live per-user cell enrichment: WiGLE density + WDGoWars territory ownership.

See DECISIONS.md 2026-07-05 (enrich). External calls mocked via respx.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from warroute.clients.wdgowars import ME_PATH, TERRITORIES_PATH, WDGOWARS_API_BASE
from warroute.clients.wigle import SEARCH_PATH, WIGLE_API_BASE
from warroute.coverage.cells import OWNER_ME, CellRow
from warroute.db import run_migrations, transaction
from warroute.router.enrich import (
    _bbox_from_geojson,
    enrich_wigle_density,
    point_in_ring,
    wdgowars_ownership_map,
)


@pytest.fixture(autouse=True)
def _no_wigle_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("warroute.clients.wigle.MIN_INTERVAL_SEC", 0.0)


def _bbox_geojson(sw_lat: float, sw_lon: float, ne_lat: float, ne_lon: float) -> str:
    poly = [
        [sw_lon, sw_lat],
        [ne_lon, sw_lat],
        [ne_lon, ne_lat],
        [sw_lon, ne_lat],
        [sw_lon, sw_lat],
    ]
    return json.dumps({"type": "Polygon", "coordinates": [poly]})


def _cell(cid: str, lat: float, lon: float, aps: int | None = None) -> CellRow:
    return CellRow(
        id=cid,
        center_lat=lat,
        center_lon=lon,
        bbox_geojson=_bbox_geojson(lat - 0.01, lon - 0.01, lat + 0.01, lon + 0.01),
        estimated_total_aps=aps,
    )


# --- geometry helpers -------------------------------------------------------


def test_point_in_ring_inside_and_outside() -> None:
    # Square in [lat, lon] order: lat 44..45, lon -72..-71.
    ring = [[44.0, -72.0], [45.0, -72.0], [45.0, -71.0], [44.0, -71.0]]
    assert point_in_ring(44.5, -71.5, ring) is True
    assert point_in_ring(46.0, -71.5, ring) is False
    assert point_in_ring(44.5, -70.0, ring) is False


def test_point_in_ring_degenerate() -> None:
    assert point_in_ring(1.0, 1.0, [[0.0, 0.0], [1.0, 1.0]]) is False


def test_bbox_from_geojson() -> None:
    b = _bbox_from_geojson(_bbox_geojson(44.0, -72.0, 45.0, -71.0))
    assert b is not None
    assert b.south == 44.0 and b.north == 45.0
    assert b.west == -72.0 and b.east == -71.0


def test_bbox_from_geojson_bad_input() -> None:
    assert _bbox_from_geojson("not json") is None


# --- WiGLE density enrichment ----------------------------------------------


@respx.mock
async def test_enrich_wigle_density_persists_and_caps() -> None:
    run_migrations()
    # Seed 3 unprobed cells in the DB so update_density has rows to write.
    rows = [_cell(f"c{i}", 44.9 + i * 0.02, -72.2) for i in range(3)]
    with transaction() as conn:
        for r in rows:
            conn.execute(
                "INSERT INTO cells (id, center_lat, center_lon, bbox_geojson) VALUES (?, ?, ?, ?)",
                (r.id, r.center_lat, r.center_lon, r.bbox_geojson),
            )
    route = respx.get(WIGLE_API_BASE + SEARCH_PATH).mock(
        return_value=httpx.Response(200, json={"success": True, "totalResults": 42, "results": []})
    )
    # cap=2 -> only the 2 nearest unprobed cells get queried.
    n = await enrich_wigle_density(
        rows, name="x", token="y", home_lat=44.9, home_lon=-72.2, cap=2, budget_s=30
    )
    assert n == 2
    assert route.call_count == 2
    # persisted to DB
    with transaction() as conn:
        got = conn.execute(
            "SELECT COUNT(*) AS n FROM cells WHERE estimated_total_aps = 42"
        ).fetchone()["n"]
    assert got == 2
    # and set on the in-memory rows
    assert sum(1 for r in rows if r.estimated_total_aps == 42) == 2


@respx.mock
async def test_enrich_wigle_density_skips_probed() -> None:
    run_migrations()
    rows = [_cell("probed", 44.9, -72.2, aps=99)]  # already probed
    route = respx.get(WIGLE_API_BASE + SEARCH_PATH).mock(
        return_value=httpx.Response(200, json={"totalResults": 5})
    )
    n = await enrich_wigle_density(
        rows, name="x", token="y", home_lat=44.9, home_lon=-72.2, cap=8, budget_s=30
    )
    assert n == 0
    assert not route.called  # probed cells are never re-queried


# --- WDGoWars territory ownership ------------------------------------------


@respx.mock
async def test_wdgowars_ownership_map_tags_me_rival_uncaptured() -> None:
    # my gang 16 owns the lat 44..45 / lon -72..-71 square; rival gang 7 owns
    # lat 40..41 / lon -75..-74.
    respx.get(WDGOWARS_API_BASE + ME_PATH).mock(
        return_value=httpx.Response(200, json={"username": "me", "gang_id": 16})
    )
    respx.get(WDGOWARS_API_BASE + TERRITORIES_PATH).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "name": "Biscuits",
                    "gang_id": 16,
                    "hull": [[44.0, -72.0], [45.0, -72.0], [45.0, -71.0], [44.0, -71.0]],
                },
                {
                    "name": "Rivals",
                    "gang_id": 7,
                    "hull": [[40.0, -75.0], [41.0, -75.0], [41.0, -74.0], [40.0, -74.0]],
                },
            ],
        )
    )
    rows = [
        _cell("mine", 44.5, -71.5),  # in my gang's hull
        _cell("rival", 40.5, -74.5),  # in rival's hull
        _cell("wild", 10.0, 10.0),  # nowhere -> uncaptured (omitted)
    ]
    owned = await wdgowars_ownership_map(rows, token="tok")
    assert owned["mine"] == OWNER_ME
    assert owned["rival"] == "Rivals"
    assert "wild" not in owned  # uncaptured cells are omitted


@respx.mock
async def test_wdgowars_ownership_map_best_effort_on_error() -> None:
    respx.get(WDGOWARS_API_BASE + ME_PATH).mock(return_value=httpx.Response(500))
    owned = await wdgowars_ownership_map([_cell("c", 44.5, -71.5)], token="tok")
    assert owned == {}
