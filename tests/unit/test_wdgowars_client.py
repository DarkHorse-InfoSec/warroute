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
async def test_me_projects_known_fields() -> None:
    respx.get(WDGOWARS_API_BASE + ME_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "username": "Darkhorse",
                "points": 12345,
                "daily_quota_remaining": 18000,
                "owned_cells": ["44.93000_-72.21600", "44.94800_-72.21600"],
                "extra_field": "ignored-but-preserved",
            },
        )
    )
    async with WdgowarsClient() as wdg:
        player = await wdg.me()
    assert player.username == "Darkhorse"
    assert player.points == 12345
    assert player.daily_quota_remaining == 18000
    assert player.owned_cell_ids == ["44.93000_-72.21600", "44.94800_-72.21600"]
    assert player.raw["extra_field"] == "ignored-but-preserved"


@respx.mock
async def test_me_handles_alternative_field_names() -> None:
    """Be lenient: WDGoWars docs are unknown so we tolerate name variants."""
    respx.get(WDGOWARS_API_BASE + ME_PATH).mock(
        return_value=httpx.Response(
            200,
            json={"name": "alt", "score": 7, "quota_remaining": 5, "territory": ["c1"]},
        )
    )
    async with WdgowarsClient() as wdg:
        player = await wdg.me()
    assert player.username == "alt"
    assert player.points == 7
    assert player.daily_quota_remaining == 5
    assert player.owned_cell_ids == ["c1"]


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
async def test_authorization_header_is_bearer() -> None:
    route = respx.get(WDGOWARS_API_BASE + ME_PATH).mock(
        return_value=httpx.Response(200, json={"username": "x"})
    )
    async with WdgowarsClient() as wdg:
        await wdg.me()
    assert route.calls.last.request.headers["authorization"].startswith("Bearer ")
