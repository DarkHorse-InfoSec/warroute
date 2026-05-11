"""Tests for the cell scorer."""

from __future__ import annotations

from warroute.coverage.cells import CellRow
from warroute.router.scorer import (
    FALLBACK_VALUE_RIVAL,
    FALLBACK_VALUE_SELF,
    FALLBACK_VALUE_UNCAPTURED,
    capture_value_for,
    ownership_label,
    rank_cells,
    score_cell,
)


def _row(
    cell_id: str = "x",
    aps: int | None = 100,
    owner: str | None = None,
    capture_value: int | None = None,
) -> CellRow:
    return CellRow(
        id=cell_id,
        center_lat=44.94,
        center_lon=-72.20,
        bbox_geojson="{}",
        estimated_total_aps=aps,
        wdgowars_owner=owner,
        wdgowars_capture_value=capture_value,
    )


def test_ownership_label_classifies_three_states() -> None:
    assert ownership_label(_row(owner=None)) == "uncaptured"
    assert ownership_label(_row(owner="me")) == "me"
    assert ownership_label(_row(owner="rival_x")) == "rival"


def test_capture_value_uses_wdgowars_when_present() -> None:
    assert capture_value_for(_row(owner=None, capture_value=777)) == 777


def test_capture_value_falls_back_to_ownership_when_unknown() -> None:
    assert capture_value_for(_row(owner=None)) == FALLBACK_VALUE_UNCAPTURED
    assert capture_value_for(_row(owner="rival_x")) == FALLBACK_VALUE_RIVAL
    assert capture_value_for(_row(owner="me")) == FALLBACK_VALUE_SELF


def test_score_multiplies_value_by_density() -> None:
    s = score_cell(_row(aps=10, capture_value=20))
    assert s.score == 200.0
    assert s.capture_value == 20
    assert s.estimated_aps == 10


def test_score_treats_missing_density_as_zero() -> None:
    s = score_cell(_row(aps=None, capture_value=20))
    assert s.score == 0.0


def test_rank_cells_sorts_descending_and_skips_no_density() -> None:
    rows = [
        _row("low", aps=5, capture_value=10),  # score 50
        _row("none", aps=None, capture_value=999),  # excluded
        _row("high", aps=500, capture_value=10),  # score 5000
        _row("mid", aps=50, capture_value=10),  # score 500
    ]
    ranked = rank_cells(rows)
    assert [s.cell_id for s in ranked] == ["high", "mid", "low"]


def test_rank_cells_excludes_my_owned_cells_only_via_planner_not_scorer() -> None:
    # The scorer doesn't filter; the planner does. Scorer just labels.
    rows = [_row("mine", aps=100, owner="me"), _row("free", aps=100)]
    ranked = rank_cells(rows)
    assert {s.cell_id for s in ranked} == {"mine", "free"}
    mine = next(s for s in ranked if s.cell_id == "mine")
    assert mine.ownership == "me"
