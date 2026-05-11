"""Cell scoring using WDGoWars + WiGLE native numbers (no custom formula).

Per PLAN.md §6.4: WarRoute is a thin orchestration layer over the existing
services. We do NOT roll a tuned weighting; we project both signals onto
their native scales and multiply.

Inputs:
  - WDGoWars `wdgowars_capture_value` (points-if-captured) when available;
    otherwise derive from ownership status: uncaptured > rival > self.
  - WiGLE `estimated_total_aps` (raw density count from a bbox search).
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


@dataclass(frozen=True)
class CellScore:
    cell_id: str
    center_lat: float
    center_lon: float
    score: float
    capture_value: int
    estimated_aps: int
    ownership: str  # 'me' | 'rival' | 'uncaptured'


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
    """Score one cell. Multiplies capture value by AP density (WiGLE native count)."""
    value = capture_value_for(cell)
    density = cell.estimated_total_aps if cell.estimated_total_aps is not None else 0
    score = float(value) * float(density)
    return CellScore(
        cell_id=cell.id,
        center_lat=cell.center_lat,
        center_lon=cell.center_lon,
        score=score,
        capture_value=value,
        estimated_aps=density,
        ownership=ownership_label(cell),
    )


def rank_cells(cells: list[CellRow]) -> list[CellScore]:
    """Score every cell and sort by descending score. Skips cells with no density data."""
    scored = [score_cell(c) for c in cells if c.estimated_total_aps is not None]
    scored.sort(key=lambda s: s.score, reverse=True)
    return scored
