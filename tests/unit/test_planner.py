"""Tests for the planner. ORS responses mocked via respx."""

from __future__ import annotations

import httpx
import pytest
import respx

from warroute.clients.ors import (
    DIRECTIONS_PATH,
    OPTIMIZATION_PATH,
    ORS_API_BASE,
)
from warroute.coverage import cells as cells_dal
from warroute.coverage.grid import cells_in_radius
from warroute.db import run_migrations, transaction
from warroute.router.planner import (
    DEFAULT_AVG_SPEED_KMH,
    PlannerError,
    PlanRequest,
    plan,
)


def _seed_scored_grid(home_lat: float, home_lon: float, radius_km: float) -> list[str]:
    grid = cells_in_radius(home_lat, home_lon, radius_km)
    with transaction() as conn:
        cells_dal.upsert_grid(conn, grid)
        ids = [row["id"] for row in conn.execute("SELECT id FROM cells").fetchall()]
        for i, cid in enumerate(ids):
            cells_dal.update_density(conn, cid, estimated_total_aps=10 + i)
    return ids


def test_plan_request_reachable_radius_for_loop() -> None:
    req = PlanRequest(home_lat=44.94, home_lon=-72.21, duration_min=60, mode="loop")
    # 60 min @ 40 km/h, halved for loop = 20 km
    assert req.reachable_radius_km() == pytest.approx(DEFAULT_AVG_SPEED_KMH * 0.5)


def test_plan_request_reachable_radius_for_oneway() -> None:
    req = PlanRequest(
        home_lat=44.94,
        home_lon=-72.21,
        duration_min=60,
        mode="oneway",
        destination_lat=45.0,
        destination_lon=-72.0,
    )
    assert req.reachable_radius_km() == pytest.approx(DEFAULT_AVG_SPEED_KMH)


def test_plan_request_detour_budget_oneway_with_direct_min() -> None:
    """When direct_min is known, detour_budget is what's left for AP scanning."""
    req = PlanRequest(
        home_lat=44.94,
        home_lon=-72.21,
        duration_min=30,
        mode="oneway",
        destination_lat=44.95,
        destination_lon=-72.17,
        direct_min=6.0,
    )
    assert req.detour_budget_min() == pytest.approx(24.0)
    # Corridor: 24 min * 40 km/h / 60 / 8 = 2 km
    assert req.corridor_half_width_km() == pytest.approx(2.0, abs=0.05)


def test_plan_request_detour_budget_loop_uses_full_duration() -> None:
    req = PlanRequest(home_lat=44.94, home_lon=-72.21, duration_min=60, mode="loop")
    assert req.detour_budget_min() == pytest.approx(60.0)


def test_point_to_segment_km_helper() -> None:
    """Cell ON the segment -> 0. Cell perpendicular to segment -> haversine-equivalent."""
    from warroute.router.planner import _point_to_segment_km

    # Segment from (44.95, -72.13) to (44.95, -72.17): a horizontal line in lat=44.95.
    # A point right on the midpoint should be ~0 km away.
    d_on = _point_to_segment_km(44.95, -72.15, 44.95, -72.13, 44.95, -72.17)
    assert d_on < 0.05

    # A point 0.01 deg north of the midpoint (~1.11 km offset).
    d_off = _point_to_segment_km(44.96, -72.15, 44.95, -72.13, 44.95, -72.17)
    assert 1.0 < d_off < 1.2

    # A point BEYOND the segment endpoint should clamp to endpoint distance.
    # Point at (44.95, -72.10), segment ends at (44.95, -72.13) -> 0.03 deg lon -> ~2.4 km.
    d_beyond = _point_to_segment_km(44.95, -72.10, 44.95, -72.13, 44.95, -72.17)
    assert 2.2 < d_beyond < 2.7


def test_plan_request_oneway_requires_destination() -> None:
    req = PlanRequest(home_lat=44.94, home_lon=-72.21, duration_min=60, mode="oneway")
    with pytest.raises(PlannerError):
        req.end_waypoint()


@respx.mock
async def test_plan_auto_paints_grid_when_db_empty() -> None:
    """Empty DB + plan request -> paint grid for reachable radius, return spread plan.

    The user is in a virgin area we've never run `coverage refresh` against. Instead
    of failing, paint the grid (no WiGLE calls) and route through it with unprobed
    cells. They wardrive, upload, density populates for next time.
    """
    run_migrations()
    respx.post(ORS_API_BASE + OPTIMIZATION_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "routes": [
                    {
                        "vehicle": 1,
                        "duration": 1500,
                        "distance": 20000,
                        "steps": [{"type": "job", "job": 0}, {"type": "job", "job": 1}],
                    }
                ]
            },
        )
    )
    respx.post(ORS_API_BASE + DIRECTIONS_PATH).mock(
        return_value=httpx.Response(
            200,
            json={"routes": [{"summary": {"distance": 20000, "duration": 1500}, "geometry": None}]},
        )
    )

    req = PlanRequest(home_lat=44.94, home_lon=-72.21, duration_min=30, mode="loop")
    result = await plan(req)

    assert result.auto_painted_cells > 0  # grid was painted on-demand
    assert result.synthetic_density is True  # every chosen cell is unprobed
    assert result.planned_route_id is not None
    assert all(not c.probed for c in result.chosen_cells)


@respx.mock
async def test_plan_radius_too_huge_to_auto_paint() -> None:
    """Runaway radius (e.g. 8-hour loop) should refuse to auto-paint silently.

    Capped at MAX_AUTO_PAINT_CELLS so we don't insert 50k+ rows on a single plan
    request. The user should run `warroute coverage refresh` deliberately for an
    area that size.
    """
    run_migrations()
    # 8-hour loop in rural terrain at 40km/h = 160km radius = ~22k cells, well over cap.
    req = PlanRequest(home_lat=44.94, home_lon=-72.21, duration_min=480, mode="loop")
    with pytest.raises(PlannerError, match="Could not generate any candidate"):
        await plan(req)


@respx.mock
async def test_plan_returns_route_and_persists() -> None:
    run_migrations()
    _seed_scored_grid(44.9367, -72.2051, radius_km=4)

    respx.post(ORS_API_BASE + OPTIMIZATION_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "routes": [
                    {
                        "vehicle": 1,
                        "duration": 1800,  # 30 min, well under 90 min budget
                        "distance": 25000,
                        "steps": [
                            {"type": "start"},
                            {"type": "job", "job": 0},
                            {"type": "job", "job": 2},
                            {"type": "job", "job": 1},
                            {"type": "end"},
                        ],
                    }
                ]
            },
        )
    )
    respx.post(ORS_API_BASE + DIRECTIONS_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "routes": [
                    {
                        "summary": {"distance": 25500.0, "duration": 1850.0},
                        "geometry": "encoded_polyline",
                    }
                ]
            },
        )
    )

    req = PlanRequest(home_lat=44.9367, home_lon=-72.2051, duration_min=90, mode="loop")
    result = await plan(req)

    assert result.planned_route_id is not None
    assert result.planned_route_id > 0
    assert result.geometry == "encoded_polyline"
    assert result.estimated_drive_min == pytest.approx(1850.0 / 60.0)
    assert len(result.ordered_waypoints) >= 4  # home + at least 2 cells + end
    assert result.estimated_new_aps > 0

    with transaction() as conn:
        row = conn.execute(
            "SELECT mode, duration_min, estimated_new_aps FROM planned_routes WHERE id = ?",
            (result.planned_route_id,),
        ).fetchone()
    assert row["mode"] == "loop"
    assert row["duration_min"] == 90


@respx.mock
async def test_plan_backs_off_when_over_budget() -> None:
    run_migrations()
    _seed_scored_grid(44.9367, -72.2051, radius_km=4)

    # First call: way over budget. Second: under budget.
    respx.post(ORS_API_BASE + OPTIMIZATION_PATH).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "routes": [
                        {
                            "vehicle": 1,
                            "duration": 99999,  # way over 90 min
                            "distance": 99999,
                            "steps": [{"type": "job", "job": 0}],
                        }
                    ]
                },
            ),
            httpx.Response(
                200,
                json={
                    "routes": [
                        {
                            "vehicle": 1,
                            "duration": 1500,
                            "distance": 20000,
                            "steps": [{"type": "job", "job": 0}],
                        }
                    ]
                },
            ),
        ]
    )
    respx.post(ORS_API_BASE + DIRECTIONS_PATH).mock(
        return_value=httpx.Response(
            200,
            json={"routes": [{"summary": {"distance": 0, "duration": 0}, "geometry": None}]},
        )
    )

    req = PlanRequest(home_lat=44.9367, home_lon=-72.2051, duration_min=90, mode="loop")
    result = await plan(req)
    assert len(result.drops_for_slack) >= 1


@respx.mock
async def test_plan_raises_when_back_off_exhausts_candidates() -> None:
    run_migrations()
    grid = cells_in_radius(44.9367, -72.2051, radius_km=2)
    with transaction() as conn:
        cells_dal.upsert_grid(conn, grid)
        ids = [row["id"] for row in conn.execute("SELECT id FROM cells").fetchall()]
        # Only seed 2 cells with density - matches MIN_WAYPOINTS lower bound.
        for cid in ids[:2]:
            cells_dal.update_density(conn, cid, estimated_total_aps=5)

    # Always returns over-budget; the planner should drop until it gives up.
    respx.post(ORS_API_BASE + OPTIMIZATION_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "routes": [
                    {
                        "vehicle": 1,
                        "duration": 99999,
                        "distance": 99999,
                        "steps": [{"type": "job", "job": 0}],
                    }
                ]
            },
        )
    )

    req = PlanRequest(home_lat=44.9367, home_lon=-72.2051, duration_min=90, mode="loop")
    with pytest.raises(PlannerError, match="Could not fit"):
        await plan(req)


@respx.mock
async def test_plan_skips_my_owned_cells() -> None:
    """Cells the player already owns are excluded from candidates.

    With the auto-paint fallback, an all-mine seed no longer raises (the planner
    paints additional unprobed cells outside the seed area). What we verify is
    that no me-owned cell ends up in chosen_cells.
    """
    run_migrations()
    ids = _seed_scored_grid(44.9367, -72.2051, radius_km=4)
    with transaction() as conn:
        cells_dal.mark_owned_by_me(conn, ids)

    respx.post(ORS_API_BASE + OPTIMIZATION_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "routes": [
                    {
                        "vehicle": 1,
                        "duration": 1500,
                        "distance": 20000,
                        "steps": [{"type": "job", "job": 0}, {"type": "job", "job": 1}],
                    }
                ]
            },
        )
    )
    respx.post(ORS_API_BASE + DIRECTIONS_PATH).mock(
        return_value=httpx.Response(
            200,
            json={"routes": [{"summary": {"distance": 20000, "duration": 1500}, "geometry": None}]},
        )
    )

    req = PlanRequest(home_lat=44.9367, home_lon=-72.2051, duration_min=90, mode="loop")
    result = await plan(req)
    assert not any(c.ownership == "me" for c in result.chosen_cells)
