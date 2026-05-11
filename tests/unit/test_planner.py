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


def test_plan_request_oneway_requires_destination() -> None:
    req = PlanRequest(home_lat=44.94, home_lon=-72.21, duration_min=60, mode="oneway")
    with pytest.raises(PlannerError):
        req.end_waypoint()


@respx.mock
async def test_plan_raises_when_no_candidates() -> None:
    run_migrations()
    req = PlanRequest(home_lat=44.94, home_lon=-72.21, duration_min=90, mode="loop")
    with pytest.raises(PlannerError, match="No scored cells"):
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
    run_migrations()
    ids = _seed_scored_grid(44.9367, -72.2051, radius_km=4)
    # Mark all cells as owned by me - should leave zero candidates.
    with transaction() as conn:
        cells_dal.mark_owned_by_me(conn, ids)

    req = PlanRequest(home_lat=44.9367, home_lon=-72.2051, duration_min=90, mode="loop")
    with pytest.raises(PlannerError, match="No scored cells"):
        await plan(req)
