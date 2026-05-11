"""2x3 km cell grid covering a home + radius.

Cells are aligned to a fixed-degree grid so the same lat/lon always lands in the
same cell across runs. Cell IDs are the SW-corner lat/lon, formatted to a
canonical string. We do NOT attempt to match the WDGoWars cell scheme - that
mapping comes later once /api/me responses tell us their grid (see DECISIONS.md).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass

from warroute.clients.wigle import BBox

# Grid step in degrees. Fixed so cells are stable across runs and machines.
# 0.018 deg lat ~= 2.0 km. 0.036 deg lon ~= 2.85 km at lat ~45 (cos(45 deg) ~= 0.71).
GRID_LAT_STEP = 0.018
GRID_LON_STEP = 0.036
EARTH_KM_PER_DEG_LAT = 111.32


@dataclass(frozen=True)
class Cell:
    """A single grid cell aligned to (GRID_LAT_STEP, GRID_LON_STEP)."""

    id: str
    sw_lat: float
    sw_lon: float
    ne_lat: float
    ne_lon: float

    @property
    def center_lat(self) -> float:
        return (self.sw_lat + self.ne_lat) / 2

    @property
    def center_lon(self) -> float:
        return (self.sw_lon + self.ne_lon) / 2

    def bbox(self) -> BBox:
        return BBox(south=self.sw_lat, north=self.ne_lat, west=self.sw_lon, east=self.ne_lon)

    def bbox_geojson(self) -> str:
        polygon = [
            [self.sw_lon, self.sw_lat],
            [self.ne_lon, self.sw_lat],
            [self.ne_lon, self.ne_lat],
            [self.sw_lon, self.ne_lat],
            [self.sw_lon, self.sw_lat],
        ]
        return json.dumps(
            {"type": "Polygon", "coordinates": [polygon]},
            separators=(",", ":"),
        )


def cell_id_for(lat: float, lon: float) -> str:
    """Return the canonical id of the cell containing (lat, lon)."""
    sw_lat = math.floor(lat / GRID_LAT_STEP) * GRID_LAT_STEP
    sw_lon = math.floor(lon / GRID_LON_STEP) * GRID_LON_STEP
    return f"{sw_lat:.5f}_{sw_lon:.5f}"


def cell_for(lat: float, lon: float) -> Cell:
    """Materialize the cell containing (lat, lon)."""
    sw_lat = math.floor(lat / GRID_LAT_STEP) * GRID_LAT_STEP
    sw_lon = math.floor(lon / GRID_LON_STEP) * GRID_LON_STEP
    return Cell(
        id=f"{sw_lat:.5f}_{sw_lon:.5f}",
        sw_lat=sw_lat,
        sw_lon=sw_lon,
        ne_lat=sw_lat + GRID_LAT_STEP,
        ne_lon=sw_lon + GRID_LON_STEP,
    )


def cells_in_radius(home_lat: float, home_lon: float, radius_km: float) -> list[Cell]:
    """All cells whose center is within `radius_km` of (home_lat, home_lon).

    Uses a flat-earth approximation since v1 only ever cares about a few hundred
    cells around a single home. The 1-degree-longitude length is computed at
    home_lat (acceptable because radius is small relative to Earth).
    """
    if radius_km <= 0:
        raise ValueError(f"radius_km must be positive, got {radius_km}")

    km_per_deg_lon = EARTH_KM_PER_DEG_LAT * math.cos(math.radians(home_lat))
    if km_per_deg_lon <= 0:
        raise ValueError(f"home_lat={home_lat} too close to a pole for flat-earth grid")

    d_lat = radius_km / EARTH_KM_PER_DEG_LAT
    d_lon = radius_km / km_per_deg_lon

    south = home_lat - d_lat
    north = home_lat + d_lat
    west = home_lon - d_lon
    east = home_lon + d_lon

    sw_lat_start = math.floor(south / GRID_LAT_STEP) * GRID_LAT_STEP
    sw_lon_start = math.floor(west / GRID_LON_STEP) * GRID_LON_STEP

    cells: list[Cell] = []
    sw_lat = sw_lat_start
    while sw_lat < north:
        sw_lon = sw_lon_start
        while sw_lon < east:
            candidate = Cell(
                id=f"{sw_lat:.5f}_{sw_lon:.5f}",
                sw_lat=sw_lat,
                sw_lon=sw_lon,
                ne_lat=sw_lat + GRID_LAT_STEP,
                ne_lon=sw_lon + GRID_LON_STEP,
            )
            if _km_between(home_lat, home_lon, candidate.center_lat, candidate.center_lon) <= radius_km:
                cells.append(candidate)
            sw_lon += GRID_LON_STEP
        sw_lat += GRID_LAT_STEP
    return cells


def _km_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in km. Accurate enough for small radii."""
    radius = 6371.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))
