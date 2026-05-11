"""Tests for the coverage report formatter."""

from __future__ import annotations

from warroute.coverage import cells as cells_dal
from warroute.coverage.grid import cells_in_radius
from warroute.coverage.report import build_summary, format_summary
from warroute.db import run_migrations, transaction


def test_build_summary_with_no_data() -> None:
    run_migrations()
    summary = build_summary(44.9367, -72.2051, radius_km=10)
    assert summary.cells_total == 0
    assert summary.cells_owned_by_me == 0
    assert summary.cells_uncaptured == 0
    text = format_summary(summary)
    assert "Newport" not in text  # we don't reverse-geocode
    assert "Home: 44.9367, -72.2051" in text


def test_build_summary_partitions_owners_correctly() -> None:
    run_migrations()
    grid = cells_in_radius(44.9367, -72.2051, radius_km=4)
    with transaction() as conn:
        cells_dal.upsert_grid(conn, grid)
        ids = [row["id"] for row in conn.execute("SELECT id FROM cells").fetchall()]
        # 2 cells owned by me
        cells_dal.mark_owned_by_me(conn, ids[:2])
        # 1 cell owned by a rival
        cells_dal.update_ownership(conn, ids[2], owner="rival_x", capture_value=100)
        # 3 cells get density
        for cid in ids[3:6]:
            cells_dal.update_density(conn, cid, estimated_total_aps=50 + ids.index(cid))

    summary = build_summary(44.9367, -72.2051, radius_km=4, top_n=3)
    assert summary.cells_owned_by_me == 2
    assert summary.cells_owned_by_rivals == 1
    assert summary.cells_uncaptured == len(ids) - 3
    assert len(summary.top_unexplored) <= 3
    text = format_summary(summary)
    assert "Owned by you:" in text
    assert "Top unexplored cells" in text


def test_build_summary_top_unexplored_excludes_self_owned() -> None:
    run_migrations()
    grid = cells_in_radius(44.9367, -72.2051, radius_km=4)
    with transaction() as conn:
        cells_dal.upsert_grid(conn, grid)
        ids = [row["id"] for row in conn.execute("SELECT id FROM cells").fetchall()]
        cells_dal.mark_owned_by_me(conn, [ids[0]])
        cells_dal.update_density(conn, ids[0], estimated_total_aps=9999)
        cells_dal.update_density(conn, ids[1], estimated_total_aps=10)

    summary = build_summary(44.9367, -72.2051, radius_km=4)
    top_ids = [t[0] for t in summary.top_unexplored]
    assert ids[0] not in top_ids
    assert ids[1] in top_ids
