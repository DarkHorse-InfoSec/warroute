"""Tests for the WDGoWars upload wrapper (with quota pre-flight)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from warroute.clients.wdgowars import ME_PATH, UPLOAD_PATH, WDGOWARS_API_BASE
from warroute.uploader.wdgowars_upload import WdgowarsQuotaSkip, upload_to_wdgowars

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "sample_wiglewifi.csv"


@respx.mock
async def test_upload_succeeds_when_under_quota() -> None:
    respx.get(WDGOWARS_API_BASE + ME_PATH).mock(
        return_value=httpx.Response(
            200, json={"username": "x", "points": 0, "daily_quota_remaining": 5000}
        )
    )
    respx.post(WDGOWARS_API_BASE + UPLOAD_PATH).mock(
        return_value=httpx.Response(200, json={"run_id": "r-1", "new_aps": 50})
    )
    result = await upload_to_wdgowars(FIXTURE, new_aps_in_csv=50)
    assert result.success is True
    assert result.skipped is False
    assert result.headroom_remaining == 4950


@respx.mock
async def test_upload_skips_when_over_quota() -> None:
    respx.get(WDGOWARS_API_BASE + ME_PATH).mock(
        return_value=httpx.Response(
            200, json={"username": "x", "points": 0, "daily_quota_remaining": 100}
        )
    )
    with pytest.raises(WdgowarsQuotaSkip):
        await upload_to_wdgowars(FIXTURE, new_aps_in_csv=500)


@respx.mock
async def test_upload_uses_default_quota_when_me_returns_no_remaining() -> None:
    respx.get(WDGOWARS_API_BASE + ME_PATH).mock(
        return_value=httpx.Response(200, json={"username": "x", "points": 0})
    )
    respx.post(WDGOWARS_API_BASE + UPLOAD_PATH).mock(
        return_value=httpx.Response(200, json={"run_id": "r-2", "new_aps": 10})
    )
    result = await upload_to_wdgowars(FIXTURE, new_aps_in_csv=10)
    assert result.success is True
    # Default 20000 - 10 = 19990 expected
    assert result.headroom_remaining == 19990


@respx.mock
async def test_upload_proceeds_even_if_me_unreachable() -> None:
    respx.get(WDGOWARS_API_BASE + ME_PATH).mock(return_value=httpx.Response(500))
    respx.post(WDGOWARS_API_BASE + UPLOAD_PATH).mock(
        return_value=httpx.Response(200, json={"run_id": "r-3", "new_aps": 5})
    )
    result = await upload_to_wdgowars(FIXTURE, new_aps_in_csv=5)
    assert result.success is True
