"""Tests for the WDGoWars client. HTTP mocked via respx."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from warroute.clients.wdgowars import (
    ME_PATH,
    UPLOAD_PATH,
    WDGOWARS_API_BASE,
    WdgowarsAuthError,
    WdgowarsClient,
    WdgowarsError,
    WdgowarsQuotaError,
)


@respx.mock
async def test_me_projects_real_response_shape() -> None:
    """Mirrors the actual /api/me response (probed 2026-05-11)."""
    respx.get(WDGOWARS_API_BASE + ME_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "username": "darkhorse",
                "country": "US",
                "joined": "2026-04-12",
                "is_superuser": False,
                "trusted": True,
                "gang": "Biscuits",
                "wifi": 34870,
                "ble": 26949,
                "total": 61819,
                "recent_today": 0,
                "recent_7d": 11682,
                "badges": ["wigle_user", "first_blood"],
            },
        )
    )
    async with WdgowarsClient() as wdg:
        player = await wdg.me()
    assert player.username == "darkhorse"
    assert player.total == 61819
    assert player.points == 61819  # property alias
    assert player.wifi == 34870
    assert player.ble == 26949
    assert player.recent_today == 0
    assert player.daily_quota_remaining == 20000  # 20000 - recent_today
    assert player.owned_cell_ids == []
    assert player.raw["gang"] == "Biscuits"


@respx.mock
async def test_me_handles_alternative_field_names() -> None:
    """Tolerate older or alternative shapes too (e.g. `points` instead of `total`)."""
    respx.get(WDGOWARS_API_BASE + ME_PATH).mock(
        return_value=httpx.Response(
            200,
            json={"name": "alt", "points": 7, "quota_remaining": 5, "territory": ["c1"]},
        )
    )
    async with WdgowarsClient() as wdg:
        player = await wdg.me()
    assert player.username == "alt"
    assert player.total == 7
    assert player.points == 7
    assert player.daily_quota_remaining == 5
    assert player.owned_cell_ids == ["c1"]


@respx.mock
async def test_me_derives_quota_when_recent_today_present() -> None:
    """When /api/me only reports recent_today, derive remaining = 20000 - recent_today."""
    respx.get(WDGOWARS_API_BASE + ME_PATH).mock(
        return_value=httpx.Response(
            200, json={"username": "x", "total": 0, "wifi": 0, "ble": 0, "recent_today": 1500}
        )
    )
    async with WdgowarsClient() as wdg:
        player = await wdg.me()
    assert player.daily_quota_remaining == 18500


@respx.mock
async def test_me_raises_on_401() -> None:
    respx.get(WDGOWARS_API_BASE + ME_PATH).mock(return_value=httpx.Response(401))
    async with WdgowarsClient() as wdg:
        with pytest.raises(WdgowarsAuthError):
            await wdg.me()


@respx.mock
async def test_me_raises_on_429() -> None:
    respx.get(WDGOWARS_API_BASE + ME_PATH).mock(return_value=httpx.Response(429))
    async with WdgowarsClient() as wdg:
        with pytest.raises(WdgowarsQuotaError):
            await wdg.me()


@respx.mock
async def test_me_raises_on_non_json_body() -> None:
    respx.get(WDGOWARS_API_BASE + ME_PATH).mock(
        return_value=httpx.Response(200, text="<html>plain</html>")
    )
    async with WdgowarsClient() as wdg:
        with pytest.raises(WdgowarsError):
            await wdg.me()


@respx.mock
async def test_probe_returns_raw_dict() -> None:
    respx.get(WDGOWARS_API_BASE + "/api/whatever").mock(
        return_value=httpx.Response(200, json={"hello": "world"})
    )
    async with WdgowarsClient() as wdg:
        body = await wdg.probe("/api/whatever")
    assert body == {"hello": "world"}


@respx.mock
async def test_probe_wraps_non_dict_responses() -> None:
    respx.get(WDGOWARS_API_BASE + "/api/list").mock(
        return_value=httpx.Response(200, json=[1, 2, 3])
    )
    async with WdgowarsClient() as wdg:
        body = await wdg.probe("/api/list")
    assert body == {"_data": [1, 2, 3]}


@respx.mock
async def test_upload_csv_posts_file(tmp_path: Path) -> None:
    csv = tmp_path / "test.csv"
    csv.write_text("WigleWifi-1.6,header,line\n", encoding="utf-8")
    route = respx.post(WDGOWARS_API_BASE + UPLOAD_PATH).mock(
        return_value=httpx.Response(200, json={"run_id": "abc123", "new_aps": 47})
    )
    async with WdgowarsClient() as wdg:
        result = await wdg.upload_csv(csv)
    assert route.called
    assert result["run_id"] == "abc123"
    assert result["new_aps"] == 47


def test_missing_token_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from warroute.config import get_settings

    monkeypatch.setenv("WDGOWARS_TOKEN", "")
    get_settings.cache_clear()
    with pytest.raises(WdgowarsAuthError):
        WdgowarsClient()


@respx.mock
async def test_auth_header_is_x_api_key() -> None:
    route = respx.get(WDGOWARS_API_BASE + ME_PATH).mock(
        return_value=httpx.Response(200, json={"username": "x"})
    )
    async with WdgowarsClient() as wdg:
        await wdg.me()
    headers = route.calls.last.request.headers
    assert "x-api-key" in headers
    assert "authorization" not in headers
