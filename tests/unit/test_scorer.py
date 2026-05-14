"""Tests for the cell scorer."""

from __future__ import annotations

from warroute.coverage.cells import CellRow
from warroute.router.scorer import (
    FALLBACK_VALUE_RIVAL,
    FALLBACK_VALUE_SELF,
    FALLBACK_VALUE_UNCAPTURED,
    UNPROBED_DENSITY_PROXY,
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


def test_score_uses_proxy_density_when_unprobed() -> None:
    s = score_cell(_row(aps=None, capture_value=20))
    assert s.score == 20.0 * UNPROBED_DENSITY_PROXY
    assert s.probed is False
    assert s.estimated_aps == UNPROBED_DENSITY_PROXY


def test_score_marks_probed_when_density_present() -> None:
    s = score_cell(_row(aps=0, capture_value=20))
    assert s.probed is True
    assert s.score == 0.0  # legitimate "WiGLE says zero APs here" - still probed


def test_rank_cells_sorts_probed_before_unprobed() -> None:
    rows = [
        _row("low_probed", aps=5, capture_value=10),  # probed, score 50
        _row("high_unprobed", aps=None, capture_value=999),  # unprobed, would be 999
        _row("high_probed", aps=500, capture_value=10),  # probed, score 5000
        _row("mid_probed", aps=50, capture_value=10),  # probed, score 500
    ]
    ranked = rank_cells(rows)
    # All probed first (desc score), then unprobed - regardless of nominal score.
    assert [s.cell_id for s in ranked] == [
        "high_probed",
        "mid_probed",
        "low_probed",
        "high_unprobed",
    ]


def test_rank_cells_includes_zero_density_probed_cells() -> None:
    """A cell with estimated_total_aps=0 was queried; WiGLE knows nothing there.

    It should still rank (as score=0) so the planner can route through it as
    filler. Only None (never-queried) marks an unprobed cell.
    """
    rows = [
        _row("known_empty", aps=0, capture_value=100),  # probed, score 0
        _row("never_queried", aps=None, capture_value=100),  # unprobed, score 100
    ]
    ranked = rank_cells(rows)
    # Probed wins the tier-1 sort even at score 0.
    assert ranked[0].cell_id == "known_empty"
    assert ranked[1].cell_id == "never_queried"


def test_rank_cells_excludes_my_owned_cells_only_via_planner_not_scorer() -> None:
    # The scorer doesn't filter; the planner does. Scorer just labels.
    rows = [_row("mine", aps=100, owner="me"), _row("free", aps=100)]
    ranked = rank_cells(rows)
    assert {s.cell_id for s in ranked} == {"mine", "free"}
    mine = next(s for s in ranked if s.cell_id == "mine")
    assert mine.ownership == "me"
