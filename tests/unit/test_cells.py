"""Tests for the cells DAL."""

from __future__ import annotations

from datetime import timedelta

from warroute.coverage import cells as cells_dal
from warroute.coverage.grid import cells_in_radius
from warroute.db import run_migrations, transaction


def _seed_small_grid() -> None:
    grid = cells_in_radius(44.9367, -72.2051, radius_km=5)
    with transaction() as conn:
        cells_dal.upsert_grid(conn, grid)


def test_upsert_grid_inserts_then_dedups() -> None:
    run_migrations()
    grid = cells_in_radius(44.9367, -72.2051, radius_km=5)
    with transaction() as conn:
        inserted1 = cells_dal.upsert_grid(conn, grid)
    with transaction() as conn:
        inserted2 = cells_dal.upsert_grid(conn, grid)
    assert inserted1 == len(grid)
    assert inserted2 == 0


def test_update_density_records_estimate_and_timestamp() -> None:
    run_migrations()
    _seed_small_grid()
    with transaction() as conn:
        any_id = conn.execute("SELECT id FROM cells LIMIT 1").fetchone()["id"]
        cells_dal.update_density(conn, any_id, estimated_total_aps=42)
    with transaction() as conn:
        row = conn.execute(
            "SELECT estimated_total_aps, last_refreshed FROM cells WHERE id = ?",
            (any_id,),
        ).fetchone()
    assert row["estimated_total_aps"] == 42
    assert row["last_refreshed"] is not None


def test_mark_owned_by_me_updates_only_provided_ids() -> None:
    run_migrations()
    _seed_small_grid()
    with transaction() as conn:
        ids = [row["id"] for row in conn.execute("SELECT id FROM cells LIMIT 3").fetchall()]
    with transaction() as conn:
        updated = cells_dal.mark_owned_by_me(conn, ids)
    assert updated == 3
    with transaction() as conn:
        owned = conn.execute(
            "SELECT COUNT(*) AS n FROM cells WHERE wdgowars_owner = 'me'"
        ).fetchone()["n"]
        unowned = conn.execute(
            "SELECT COUNT(*) AS n FROM cells WHERE wdgowars_owner IS NULL"
        ).fetchone()["n"]
    assert owned == 3
    assert unowned > 0


def test_update_ownership_sets_rival_and_value() -> None:
    run_migrations()
    _seed_small_grid()
    with transaction() as conn:
        any_id = conn.execute("SELECT id FROM cells LIMIT 1").fetchone()["id"]
        cells_dal.update_ownership(conn, any_id, owner="rival_wd", capture_value=500)
    with transaction() as conn:
        row = conn.execute(
            "SELECT wdgowars_owner, wdgowars_capture_value FROM cells WHERE id = ?",
            (any_id,),
        ).fetchone()
    assert row["wdgowars_owner"] == "rival_wd"
    assert row["wdgowars_capture_value"] == 500


def test_stale_density_cells_returns_unrefreshed_initially() -> None:
    run_migrations()
    _seed_small_grid()
    with transaction() as conn:
        stale = cells_dal.stale_density_cells(conn, older_than=timedelta(hours=24))
    with transaction() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM cells").fetchone()["n"]
    assert len(stale) == count


def test_stale_density_cells_excludes_recently_refreshed() -> None:
    run_migrations()
    _seed_small_grid()
    with transaction() as conn:
        any_id = conn.execute("SELECT id FROM cells LIMIT 1").fetchone()["id"]
        cells_dal.update_density(conn, any_id, estimated_total_aps=100)
    with transaction() as conn:
        stale = cells_dal.stale_density_cells(conn, older_than=timedelta(hours=24))
    assert any_id not in stale


def test_all_cells_returns_dataclasses() -> None:
    run_migrations()
    _seed_small_grid()
    with transaction() as conn:
        rows = cells_dal.all_cells(conn)
    assert rows
    sample = rows[0]
    assert sample.id
    assert isinstance(sample.center_lat, float)
    assert sample.bbox_geojson.startswith("{")
