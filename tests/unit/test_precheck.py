"""Tests for the pre-drive sanity harness."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from warroute.clients.ors import DIRECTIONS_PATH, ORS_API_BASE
from warroute.clients.wdgowars import ME_PATH, WDGOWARS_API_BASE
from warroute.clients.wigle import SEARCH_PATH, WIGLE_API_BASE
from warroute.config import get_settings
from warroute.precheck import (
    QUOTA_WARN_THRESHOLD,
    Status,
    check_filesystem,
    check_ors,
    check_wdgowars,
    check_wigle,
    run_all,
    verdict,
)


@pytest.fixture(autouse=True)
def _wigle_no_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("warroute.clients.wigle.MIN_INTERVAL_SEC", 0.0)


@pytest.fixture(autouse=True)
def _writable_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point SPOOL_DIR + GPX_OUT_DIR at writable tmp dirs by default."""
    spool = tmp_path / "spool"
    gpx = tmp_path / "gpx"
    spool.mkdir()
    gpx.mkdir()
    monkeypatch.setenv("SPOOL_DIR", str(spool))
    monkeypatch.setenv("GPX_OUT_DIR", str(gpx))
    get_settings.cache_clear()


# ----- verdict --------------------------------------------------------------


def test_verdict_all_ok() -> None:
    from warroute.precheck import CheckResult

    results = [
        CheckResult("A", Status.OK, ""),
        CheckResult("B", Status.OK, ""),
    ]
    assert verdict(results) == Status.OK


def test_verdict_any_warn() -> None:
    from warroute.precheck import CheckResult

    results = [
        CheckResult("A", Status.OK, ""),
        CheckResult("B", Status.WARN, ""),
    ]
    assert verdict(results) == Status.WARN


def test_verdict_fail_dominates_warn() -> None:
    from warroute.precheck import CheckResult

    results = [
        CheckResult("A", Status.WARN, ""),
        CheckResult("B", Status.FAIL, ""),
        CheckResult("C", Status.OK, ""),
    ]
    assert verdict(results) == Status.FAIL


# ----- WiGLE ---------------------------------------------------------------


@respx.mock
async def test_check_wigle_ok() -> None:
    respx.get(WIGLE_API_BASE + SEARCH_PATH).mock(
        return_value=httpx.Response(
            200, json={"success": True, "totalResults": 42, "results": []}
        )
    )
    result = await check_wigle()
    assert result.status == Status.OK
    assert "42" in result.detail


@respx.mock
async def test_check_wigle_auth_fail() -> None:
    respx.get(WIGLE_API_BASE + SEARCH_PATH).mock(return_value=httpx.Response(401))
    result = await check_wigle()
    assert result.status == Status.FAIL
    assert result.hint is not None
    assert "WIGLE_NAME" in result.hint


@respx.mock
async def test_check_wigle_rate_limit_is_warn() -> None:
    respx.get(WIGLE_API_BASE + SEARCH_PATH).mock(return_value=httpx.Response(429))
    result = await check_wigle()
    assert result.status == Status.WARN
    assert "req/sec" in (result.hint or "")


@respx.mock
async def test_check_wigle_transport_failure() -> None:
    respx.get(WIGLE_API_BASE + SEARCH_PATH).mock(
        side_effect=httpx.ConnectError("boom")
    )
    result = await check_wigle()
    assert result.status == Status.FAIL
    assert "cert chain" in (result.hint or "")


# ----- WDGoWars ------------------------------------------------------------


@respx.mock
async def test_check_wdgowars_ok_high_headroom() -> None:
    respx.get(WDGOWARS_API_BASE + ME_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "username": "Domenic",
                "total": 1000,
                "wifi": 800,
                "recent_today": 100,  # headroom = 20000 - 100 = 19900
            },
        )
    )
    result = await check_wdgowars()
    assert result.status == Status.OK
    assert "Domenic" in result.detail
    assert "headroom=19900" in result.detail


@respx.mock
async def test_check_wdgowars_warn_when_quota_low() -> None:
    respx.get(WDGOWARS_API_BASE + ME_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "username": "x",
                "wifi": 0,
                "recent_today": 20000 - (QUOTA_WARN_THRESHOLD - 1),
            },
        )
    )
    result = await check_wdgowars()
    assert result.status == Status.WARN
    assert "headroom" in (result.hint or "")


@respx.mock
async def test_check_wdgowars_warn_when_quota_zero() -> None:
    respx.get(WDGOWARS_API_BASE + ME_PATH).mock(
        return_value=httpx.Response(200, json={"username": "x", "recent_today": 20000})
    )
    result = await check_wdgowars()
    assert result.status == Status.WARN
    assert "exhausted" in (result.hint or "")


@respx.mock
async def test_check_wdgowars_auth_fail() -> None:
    respx.get(WDGOWARS_API_BASE + ME_PATH).mock(return_value=httpx.Response(401))
    result = await check_wdgowars()
    assert result.status == Status.FAIL
    assert "X-API-Key" in (result.hint or "")


# ----- ORS ----------------------------------------------------------------


@respx.mock
async def test_check_ors_ok() -> None:
    respx.post(ORS_API_BASE + DIRECTIONS_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "routes": [
                    {
                        "summary": {"distance": 142.0, "duration": 18.0},
                        "geometry": None,
                    }
                ]
            },
        )
    )
    result = await check_ors()
    assert result.status == Status.OK
    assert "142 m" in result.detail


@respx.mock
async def test_check_ors_auth_fail() -> None:
    respx.post(ORS_API_BASE + DIRECTIONS_PATH).mock(return_value=httpx.Response(403))
    result = await check_ors()
    assert result.status == Status.FAIL
    assert "ORS_API_KEY" in (result.hint or "")


@respx.mock
async def test_check_ors_quota_is_warn() -> None:
    respx.post(ORS_API_BASE + DIRECTIONS_PATH).mock(return_value=httpx.Response(429))
    result = await check_ors()
    assert result.status == Status.WARN
    assert "quota" in (result.hint or "").lower()


# ----- filesystem ---------------------------------------------------------


def test_check_filesystem_ok(tmp_path: Path) -> None:
    results = check_filesystem()
    assert len(results) == 2
    for r in results:
        assert r.status == Status.OK


def test_check_filesystem_missing_spool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPOOL_DIR", str(tmp_path / "does-not-exist"))
    get_settings.cache_clear()
    results = check_filesystem()
    spool = next(r for r in results if r.name == "SPOOL_DIR")
    assert spool.status == Status.FAIL
    assert "mkdir" in (spool.hint or "")


def test_check_filesystem_spool_is_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool_as_file = tmp_path / "spool-as-file"
    spool_as_file.write_text("oops")
    monkeypatch.setenv("SPOOL_DIR", str(spool_as_file))
    get_settings.cache_clear()
    results = check_filesystem()
    spool = next(r for r in results if r.name == "SPOOL_DIR")
    assert spool.status == Status.FAIL
    assert "not a directory" in spool.detail


# ----- run_all (orchestration) --------------------------------------------


@respx.mock
async def test_run_all_happy_path() -> None:
    respx.get(WIGLE_API_BASE + SEARCH_PATH).mock(
        return_value=httpx.Response(
            200, json={"success": True, "totalResults": 5, "results": []}
        )
    )
    respx.get(WDGOWARS_API_BASE + ME_PATH).mock(
        return_value=httpx.Response(
            200, json={"username": "x", "wifi": 0, "recent_today": 0}
        )
    )
    respx.post(ORS_API_BASE + DIRECTIONS_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "routes": [
                    {"summary": {"distance": 100.0, "duration": 10.0}, "geometry": None}
                ]
            },
        )
    )

    results = await run_all()
    names = [r.name for r in results]
    assert names == ["WiGLE", "WDGoWars", "ORS", "SPOOL_DIR", "GPX_OUT_DIR"]
    assert verdict(results) == Status.OK


@respx.mock
async def test_run_all_mixed_verdict_fail_dominates() -> None:
    respx.get(WIGLE_API_BASE + SEARCH_PATH).mock(return_value=httpx.Response(401))
    respx.get(WDGOWARS_API_BASE + ME_PATH).mock(
        return_value=httpx.Response(
            200, json={"username": "x", "wifi": 0, "recent_today": 0}
        )
    )
    respx.post(ORS_API_BASE + DIRECTIONS_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "routes": [
                    {"summary": {"distance": 100.0, "duration": 10.0}, "geometry": None}
                ]
            },
        )
    )

    results = await run_all()
    assert verdict(results) == Status.FAIL
