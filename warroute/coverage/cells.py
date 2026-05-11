"""Data access for the `cells` table."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from warroute.coverage.grid import Cell

OWNER_ME = "me"


@dataclass
class CellRow:
    """One row in the cells table, as we read or write it."""

    id: str
    center_lat: float
    center_lon: float
    bbox_geojson: str
    your_ap_count: int = 0
    estimated_total_aps: int | None = None
    wdgowars_owner: str | None = None
    wdgowars_capture_value: int | None = None
    last_refreshed: datetime | None = None


def upsert_grid(conn: sqlite3.Connection, cells: Iterable[Cell]) -> int:
    """Insert any cells from `cells` not already present. Returns inserted count."""
    rows = [
        (cell.id, cell.center_lat, cell.center_lon, cell.bbox_geojson())
        for cell in cells
    ]
    if not rows:
        return 0
    cursor = conn.executemany(
        """
        INSERT INTO cells (id, center_lat, center_lon, bbox_geojson)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(id) DO NOTHING
        """,
        rows,
    )
    return cursor.rowcount or 0


def update_density(
    conn: sqlite3.Connection,
    cell_id: str,
    estimated_total_aps: int,
) -> None:
    """Record a fresh WiGLE density reading for a cell."""
    conn.execute(
        """
        UPDATE cells
        SET estimated_total_aps = ?,
            last_refreshed = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (estimated_total_aps, cell_id),
    )


def update_ownership(
    conn: sqlite3.Connection,
    cell_id: str,
    owner: str | None,
    capture_value: int | None = None,
) -> None:
    """Record cell ownership pulled from WDGoWars."""
    conn.execute(
        """
        UPDATE cells
        SET wdgowars_owner = ?,
            wdgowars_capture_value = ?,
            last_refreshed = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (owner, capture_value, cell_id),
    )


def mark_owned_by_me(conn: sqlite3.Connection, cell_ids: Iterable[str]) -> int:
    """Bulk-mark a set of cell IDs as owned by the current player."""
    rows = [(cell_id,) for cell_id in cell_ids]
    if not rows:
        return 0
    cursor = conn.executemany(
        f"UPDATE cells SET wdgowars_owner = '{OWNER_ME}', last_refreshed = CURRENT_TIMESTAMP WHERE id = ?",
        rows,
    )
    return cursor.rowcount or 0


def stale_density_cells(
    conn: sqlite3.Connection,
    older_than: timedelta = timedelta(hours=24),
) -> list[str]:
    """Cell IDs whose density reading is missing or older than `older_than`."""
    cutoff = datetime.now(UTC) - older_than
    rows = conn.execute(
        """
        SELECT id FROM cells
        WHERE estimated_total_aps IS NULL
           OR last_refreshed IS NULL
           OR last_refreshed < ?
        """,
        (cutoff.isoformat(),),
    ).fetchall()
    return [row["id"] for row in rows]


def all_cells(conn: sqlite3.Connection) -> list[CellRow]:
    rows = conn.execute(
        """
        SELECT id, center_lat, center_lon, bbox_geojson,
               your_ap_count, estimated_total_aps,
               wdgowars_owner, wdgowars_capture_value, last_refreshed
        FROM cells
        """
    ).fetchall()
    return [_row_to_cellrow(row) for row in rows]


def _row_to_cellrow(row: sqlite3.Row) -> CellRow:
    refreshed_raw = row["last_refreshed"]
    refreshed: datetime | None
    if refreshed_raw is None:
        refreshed = None
    elif isinstance(refreshed_raw, datetime):
        refreshed = refreshed_raw
    else:
        refreshed = _parse_sqlite_ts(str(refreshed_raw))
    return CellRow(
        id=row["id"],
        center_lat=row["center_lat"],
        center_lon=row["center_lon"],
        bbox_geojson=row["bbox_geojson"],
        your_ap_count=row["your_ap_count"] or 0,
        estimated_total_aps=row["estimated_total_aps"],
        wdgowars_owner=row["wdgowars_owner"],
        wdgowars_capture_value=row["wdgowars_capture_value"],
        last_refreshed=refreshed,
    )


def _parse_sqlite_ts(value: str) -> datetime | None:
    """SQLite returns CURRENT_TIMESTAMP as 'YYYY-MM-DD HH:MM:SS' (UTC, naive)."""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None
