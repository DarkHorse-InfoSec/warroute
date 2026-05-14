"""Cell scoring using WDGoWars + WiGLE native numbers (no custom formula).

Per PLAN.md §6.4: WarRoute is a thin orchestration layer over the existing
services. We do NOT roll a tuned weighting; we project both signals onto
their native scales and multiply.

Inputs:
  - WDGoWars `wdgowars_capture_value` (points-if-captured) when available;
    otherwise derive from ownership status: uncaptured > rival > self.
  - WiGLE `estimated_total_aps` (raw density count from a bbox search).

Two-tier ranking (added 2026-05-14): cells that have actually been queried
against WiGLE (`estimated_total_aps is not None`) always outrank cells we
haven't probed yet, regardless of score. Unprobed cells get a unit density
proxy so they still produce a usable plan in virgin areas - sparse rural
coverage is the WarRoute use case, not a degenerate path.
"""

from __future__ import annotations

from dataclasses import dataclass

from warroute.coverage.cells import OWNER_ME, CellRow

# Ownership-derived fallback values, used only when WDGoWars hasn't surfaced
# a per-cell capture_value yet. Same scale as the game's own scoring would be:
# uncaptured cells are the prize, rival cells are worth taking, self-owned cells
# are worth a token revisit only.
FALLBACK_VALUE_UNCAPTURED = 100
FALLBACK_VALUE_RIVAL = 60
FALLBACK_VALUE_SELF = 5

# Stand-in density for cells we haven't queried WiGLE about yet. Picked so an
# unprobed uncaptured cell scores 100 - well below any probed cell with even
# a single AP (which would score 100+ at the same capture_value). Probed cells
# are always preferred via the two-tier sort in `rank_cells`; this value only
# matters for ordering *among* unprobed cells.
UNPROBED_DENSITY_PROXY = 1


@dataclass(frozen=True)
class CellScore:
    cell_id: str
    center_lat: float
    center_lon: float
    score: float
    capture_value: int
    estimated_aps: int
    ownership: str  # 'me' | 'rival' | 'uncaptured'
    probed: bool  # True if estimated_total_aps came from WiGLE; False if proxy


def ownership_label(cell: CellRow) -> str:
    if cell.wdgowars_owner == OWNER_ME:
        return "me"
    if cell.wdgowars_owner:
        return "rival"
    return "uncaptured"


def capture_value_for(cell: CellRow) -> int:
    """Use WDGoWars-supplied capture_value if present; else derive from ownership."""
    if cell.wdgowars_capture_value is not None:
        return cell.wdgowars_capture_value
    label = ownership_label(cell)
    if label == "me":
        return FALLBACK_VALUE_SELF
    if label == "rival":
        return FALLBACK_VALUE_RIVAL
    return FALLBACK_VALUE_UNCAPTURED


def score_cell(cell: CellRow) -> CellScore:
    """Score one cell. Multiplies capture value by AP density.

    Unprobed cells (no WiGLE reading yet) use `UNPROBED_DENSITY_PROXY` so they
    still rank above zero, and carry `probed=False` so the planner can sort
    them into the second tier.
    """
    value = capture_value_for(cell)
    if cell.estimated_total_aps is not None:
        density = cell.estimated_total_aps
        probed = True
    else:
        density = UNPROBED_DENSITY_PROXY
        probed = False
    score = float(value) * float(density)
    return CellScore(
        cell_id=cell.id,
        center_lat=cell.center_lat,
        center_lon=cell.center_lon,
        score=score,
        capture_value=value,
        estimated_aps=density,
        ownership=ownership_label(cell),
        probed=probed,
    )


def rank_cells(cells: list[CellRow]) -> list[CellScore]:
    """Score every cell and sort.

    Probed cells (real WiGLE density) always come before unprobed cells, then
    descending score within each tier. The intent: when we have data, use it;
    only fall back to unprobed cells when there's nothing better.
    """
    scored = [score_cell(c) for c in cells]
    scored.sort(key=lambda s: (not s.probed, -s.score))
    return scored
