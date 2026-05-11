"""Integration-style tests for coverage.sync.refresh, all dependencies mocked."""

from __future__ import annotations

import httpx
import pytest
import respx

from warroute.clients.wdgowars import ME_PATH, WDGOWARS_API_BASE
from warroute.clients.wigle import SEARCH_PATH, WIGLE_API_BASE
from warroute.coverage import cells as cells_dal
from warroute.coverage.sync import refresh
from warroute.db import run_migrations, transaction


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("warroute.clients.wigle.MIN_INTERVAL_SEC", 0.0)


@respx.mock
async def test_refresh_paints_grid_and_records_density() -> None:
    respx.get(WDGOWARS_API_BASE + ME_PATH).mock(
        return_value=httpx.Response(
            200,
            json={"username": "Darkhorse", "points": 0, "owned_cells": []},
        )
    )
    respx.get(WIGLE_API_BASE + SEARCH_PATH).mock(
        return_value=httpx.Response(
            200, json={"success": True, "totalResults": 23, "results": []}
        )
    )

    run_migrations()
    summary = await refresh(home_lat=44.9367, home_lon=-72.2051, radius_km=4)

    assert summary.cells_total > 0
    assert summary.cells_inserted == summary.cells_total
    assert summary.wdgowars_synced is True
    assert summary.cells_density_refreshed == summary.cells_total
    assert summary.cells_density_failed == 0

    with transaction() as conn:
        rows = cells_dal.all_cells(conn)
    assert all(r.estimated_total_aps == 23 for r in rows)


@respx.mock
async def test_refresh_marks_owned_cells() -> None:
    run_migrations()

    # First seed the grid so we know what IDs exist.
    summary = await _seed_no_external(4)
    assert summary.cells_total > 0

    with transaction() as conn:
        seeded_ids = [
            row["id"]
            for row in conn.execute("SELECT id FROM cells LIMIT 2").fetchall()
        ]

    respx.get(WDGOWARS_API_BASE + ME_PATH).mock(
        return_value=httpx.Response(
            200, json={"username": "me", "points": 0, "owned_cells": seeded_ids}
        )
    )
    respx.get(WIGLE_API_BASE + SEARCH_PATH).mock(
        return_value=httpx.Response(
            200, json={"success": True, "totalResults": 0, "results": []}
        )
    )

    summary = await refresh(home_lat=44.9367, home_lon=-72.2051, radius_km=4)
    assert summary.cells_owned_by_me == 2


@respx.mock
async def test_refresh_continues_when_wdgowars_fails() -> None:
    respx.get(WDGOWARS_API_BASE + ME_PATH).mock(return_value=httpx.Response(500))
    respx.get(WIGLE_API_BASE + SEARCH_PATH).mock(
        return_value=httpx.Response(
            200, json={"success": True, "totalResults": 5, "results": []}
        )
    )

    run_migrations()
    summary = await refresh(home_lat=44.9367, home_lon=-72.2051, radius_km=4)
    assert summary.wdgowars_synced is False
    assert summary.wdgowars_error is not None
    assert summary.cells_density_refreshed == summary.cells_total


@respx.mock
async def test_refresh_counts_wigle_failures() -> None:
    respx.get(WDGOWARS_API_BASE + ME_PATH).mock(
        return_value=httpx.Response(200, json={"username": "x", "points": 0, "owned_cells": []})
    )
    respx.get(WIGLE_API_BASE + SEARCH_PATH).mock(return_value=httpx.Response(500))

    run_migrations()
    summary = await refresh(home_lat=44.9367, home_lon=-72.2051, radius_km=4)
    assert summary.cells_density_refreshed == 0
    assert summary.cells_density_failed == summary.cells_total


@respx.mock
async def _seed_no_external(radius: float):  # type: ignore[no-untyped-def]
    """Helper: paints grid with stub external responses; returns summary."""
    respx.get(WDGOWARS_API_BASE + ME_PATH).mock(
        return_value=httpx.Response(200, json={"username": "x", "points": 0, "owned_cells": []})
    )
    respx.get(WIGLE_API_BASE + SEARCH_PATH).mock(
        return_value=httpx.Response(
            200, json={"success": True, "totalResults": 0, "results": []}
        )
    )
    return await refresh(home_lat=44.9367, home_lon=-72.2051, radius_km=radius)
