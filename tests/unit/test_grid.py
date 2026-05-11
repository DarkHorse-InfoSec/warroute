"""Tests for the cell grid generator."""

from __future__ import annotations

import json
import math

from warroute.coverage.grid import (
    GRID_LAT_STEP,
    GRID_LON_STEP,
    Cell,
    cell_for,
    cell_id_for,
    cells_in_radius,
)


def test_cell_id_is_stable_for_same_input() -> None:
    a = cell_id_for(44.9367, -72.2051)
    b = cell_id_for(44.9367, -72.2051)
    assert a == b


def test_neighboring_points_share_cell_when_within_step() -> None:
    base_lat, base_lon = 44.9367, -72.2051
    nudged_lat = base_lat + GRID_LAT_STEP / 4
    nudged_lon = base_lon + GRID_LON_STEP / 4
    assert cell_id_for(base_lat, base_lon) == cell_id_for(nudged_lat, nudged_lon)


def test_distant_points_get_different_cells() -> None:
    a = cell_id_for(44.9367, -72.2051)
    b = cell_id_for(44.9367 + 5 * GRID_LAT_STEP, -72.2051 + 5 * GRID_LON_STEP)
    assert a != b


def test_cell_for_bbox_covers_input() -> None:
    lat, lon = 44.9367, -72.2051
    cell = cell_for(lat, lon)
    assert cell.sw_lat <= lat < cell.ne_lat
    assert cell.sw_lon <= lon < cell.ne_lon


def test_cell_geojson_is_valid_polygon() -> None:
    cell = cell_for(44.9367, -72.2051)
    geom = json.loads(cell.bbox_geojson())
    assert geom["type"] == "Polygon"
    assert len(geom["coordinates"][0]) == 5  # closed polygon
    assert geom["coordinates"][0][0] == geom["coordinates"][0][-1]


def test_cells_in_radius_centered_on_home_includes_home_cell() -> None:
    cells = cells_in_radius(44.9367, -72.2051, radius_km=10)
    home_id = cell_id_for(44.9367, -72.2051)
    assert any(c.id == home_id for c in cells)


def test_cells_in_radius_count_scales_with_radius() -> None:
    small = cells_in_radius(44.9367, -72.2051, radius_km=5)
    big = cells_in_radius(44.9367, -72.2051, radius_km=25)
    assert len(big) > len(small) > 0


def test_cells_in_radius_50km_for_newport_vt_in_expected_range() -> None:
    """Sanity check the example in PLAN.md (~287 cells at 50 km from Newport)."""
    cells = cells_in_radius(44.9367, -72.2051, radius_km=50)
    # Cell area ~ 2 km * 3 km = 6 km^2, area of 50km radius = pi*50^2 ~ 7854 km^2
    # Expect ~1300 cells, give a wide range to tolerate rounding + clipping.
    assert 800 < len(cells) < 1800, f"got {len(cells)}"


def test_cells_in_radius_no_duplicate_ids() -> None:
    cells = cells_in_radius(44.9367, -72.2051, radius_km=20)
    ids = [c.id for c in cells]
    assert len(ids) == len(set(ids))


def test_cells_in_radius_rejects_zero_radius() -> None:
    import pytest

    with pytest.raises(ValueError):
        cells_in_radius(44.9367, -72.2051, 0)


def test_cell_dataclass_geometry_self_consistent() -> None:
    cell = Cell(id="x", sw_lat=44.0, sw_lon=-72.0, ne_lat=44.018, ne_lon=-71.964)
    assert math.isclose(cell.center_lat, 44.009)
    assert math.isclose(cell.center_lon, -71.982)
    bbox = cell.bbox()
    assert bbox.south == 44.0 and bbox.north == 44.018
    assert bbox.west == -72.0 and bbox.east == -71.964
