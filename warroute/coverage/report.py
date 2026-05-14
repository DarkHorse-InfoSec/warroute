"""Text summary printer for `warroute coverage report`."""

from __future__ import annotations

from dataclasses import dataclass

from warroute.coverage.cells import OWNER_ME, all_cells
from warroute.db import transaction


@dataclass
class CoverageSummary:
    home_lat: float
    home_lon: float
    radius_km: float
    cells_total: int
    cells_owned_by_me: int
    cells_owned_by_rivals: int
    cells_uncaptured: int
    top_unexplored: list[tuple[str, int, float, float]]  # (id, est_aps, lat, lon)


def build_summary(
    home_lat: float,
    home_lon: float,
    radius_km: float,
    top_n: int = 5,
) -> CoverageSummary:
    with transaction() as conn:
        rows = all_cells(conn)

    owned_me = sum(1 for r in rows if r.wdgowars_owner == OWNER_ME)
    owned_rival = sum(1 for r in rows if r.wdgowars_owner and r.wdgowars_owner != OWNER_ME)
    uncaptured = sum(1 for r in rows if not r.wdgowars_owner)

    unexplored = [
        r for r in rows if r.wdgowars_owner != OWNER_ME and r.estimated_total_aps is not None
    ]
    unexplored.sort(key=lambda r: r.estimated_total_aps or 0, reverse=True)
    top = [
        (r.id, r.estimated_total_aps or 0, r.center_lat, r.center_lon) for r in unexplored[:top_n]
    ]

    return CoverageSummary(
        home_lat=home_lat,
        home_lon=home_lon,
        radius_km=radius_km,
        cells_total=len(rows),
        cells_owned_by_me=owned_me,
        cells_owned_by_rivals=owned_rival,
        cells_uncaptured=uncaptured,
        top_unexplored=top,
    )


def format_summary(summary: CoverageSummary) -> str:
    """Plaintext rendering. Mirrors the example in PLAN.md acceptance criteria."""
    lines: list[str] = []
    lines.append(f"Home: {summary.home_lat:.4f}, {summary.home_lon:.4f}")
    lines.append(f"Radius: {summary.radius_km:.0f} km")
    lines.append(f"Cells in radius: {summary.cells_total}")

    total = max(summary.cells_total, 1)
    pct_me = 100 * summary.cells_owned_by_me / total
    pct_rival = 100 * summary.cells_owned_by_rivals / total
    pct_unc = 100 * summary.cells_uncaptured / total
    lines.append(f"  Owned by you:        {summary.cells_owned_by_me:5d}  ({pct_me:.0f}%)")
    lines.append(f"  Owned by rivals:     {summary.cells_owned_by_rivals:5d}  ({pct_rival:.0f}%)")
    lines.append(f"  Uncaptured:          {summary.cells_uncaptured:5d}  ({pct_unc:.0f}%)")

    if summary.top_unexplored:
        lines.append("Top unexplored cells by estimated WiGLE density:")
        for i, (cell_id, est, lat, lon) in enumerate(summary.top_unexplored, start=1):
            lines.append(f"  {i}. {cell_id}  ({lat:+.4f}, {lon:+.4f})  ~{est} APs")
    else:
        lines.append("No density data yet. Run `warroute coverage refresh` first.")

    return "\n".join(lines)
