"""Tests for the WDGoWars client. HTTP mocked via respx."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from warroute.clients.wdgowars import (
    ME_PATH,
    STATS_PATH,
    TERRITORIES_PATH,
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


# ----------------------------------------------------------------------------
# Richer /api/me fields + gang territories + server version (post-v1)
# ----------------------------------------------------------------------------


@respx.mock
async def test_me_surfaces_richer_fields() -> None:
    respx.get(WDGOWARS_API_BASE + ME_PATH).mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "username": "darkhorse",
                "country": "US",
                "gang": "Biscuits",
                "gang_id": 16,
                "gang_role": "member",
                "wifi": 34870,
                "total": 61819,
                "mesh": 3,
                "cracked": 12,
                "aircraft": 1,
                "recent_today": 40,
                "recent_7d": 11682,
                "reinforce_total": 512,
                "credits": {"balance": 250, "bounties_completed": 4, "lifetime_earned": 900},
                "badges": ["wigle_user", "first_blood"],
                "trusted": True,
                "is_superuser": False,
            },
        )
    )
    async with WdgowarsClient() as wdg:
        player = await wdg.me()
    assert player.gang == "Biscuits"
    assert player.gang_id == 16
    assert player.gang_role == "member"
    assert player.recent_7d == 11682
    assert player.reinforce_total == 512
    assert player.credits_balance == 250
    assert player.badges == ["wigle_user", "first_blood"]
    assert player.badge_count == 2
    assert player.trusted is True
    assert player.mesh == 3 and player.cracked == 12 and player.aircraft == 1


@respx.mock
async def test_me_richer_fields_default_when_absent() -> None:
    respx.get(WDGOWARS_API_BASE + ME_PATH).mock(
        return_value=httpx.Response(200, json={"ok": True, "username": "sparse", "wifi": 5})
    )
    async with WdgowarsClient() as wdg:
        player = await wdg.me()
    assert player.gang is None
    assert player.gang_id is None
    assert player.recent_7d == 0
    assert player.credits_balance is None
    assert player.badges == []
    assert player.badge_count == 0


@respx.mock
async def test_gang_territories_parses_list() -> None:
    respx.get(WDGOWARS_API_BASE + TERRITORIES_PATH).mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "name": "Biscuits",
                    "gang_id": 16,
                    "color": "#22d3ee",
                    "members": 8,
                    "points": 9001,
                    "rank": 3,
                    "hull": [[44.9, -72.2], [45.0, -72.1], [44.8, -72.0]],
                },
                {"name": "Rivals", "gang_id": 7, "hull": []},
            ],
        )
    )
    async with WdgowarsClient() as wdg:
        gangs = await wdg.gang_territories()
    assert len(gangs) == 2
    first = gangs[0]
    assert first.name == "Biscuits"
    assert first.gang_id == 16
    assert first.rank == 3
    # Hull points preserved in API order (no lat/lon swap in the client).
    assert first.hull[0] == [44.9, -72.2]


@respx.mock
async def test_gang_territories_tolerates_dict_wrapper_and_bad_rows() -> None:
    respx.get(WDGOWARS_API_BASE + TERRITORIES_PATH).mock(
        return_value=httpx.Response(
            200,
            json={"territories": [{"name": "A", "hull": [[1, 2], "junk", [3, 4]]}, "notadict"]},
        )
    )
    async with WdgowarsClient() as wdg:
        gangs = await wdg.gang_territories()
    assert len(gangs) == 1
    assert gangs[0].hull == [[1.0, 2.0], [3.0, 4.0]]  # malformed point skipped


@respx.mock
async def test_server_version_reads_stats() -> None:
    respx.get(WDGOWARS_API_BASE + STATS_PATH).mock(
        return_value=httpx.Response(200, json={"version": "1.3.0", "uptime": 12345})
    )
    async with WdgowarsClient() as wdg:
        assert await wdg.server_version() == "1.3.0"


@respx.mock
async def test_server_version_none_when_field_missing() -> None:
    respx.get(WDGOWARS_API_BASE + STATS_PATH).mock(
        return_value=httpx.Response(200, json={"uptime": 12345})
    )
    async with WdgowarsClient() as wdg:
        assert await wdg.server_version() is None
