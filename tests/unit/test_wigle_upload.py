"""Tests for the WiGLE.net file upload client."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from warroute.clients.wigle import WIGLE_API_BASE, WigleAuthError, WigleError, WigleRateLimitError
from warroute.uploader.wigle_upload import UPLOAD_PATH, upload_to_wigle

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "sample_wiglewifi.csv"


@respx.mock
async def test_upload_success_returns_transid() -> None:
    respx.post(WIGLE_API_BASE + UPLOAD_PATH).mock(
        return_value=httpx.Response(
            200,
            json={"success": True, "results": [{"transid": "abc-123"}]},
        )
    )
    result = await upload_to_wigle(FIXTURE)
    assert result.success is True
    assert result.transid == "abc-123"


@respx.mock
async def test_upload_uses_basic_auth() -> None:
    route = respx.post(WIGLE_API_BASE + UPLOAD_PATH).mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    await upload_to_wigle(FIXTURE)
    auth_header = route.calls.last.request.headers["authorization"]
    assert auth_header.startswith("Basic ")


@respx.mock
async def test_upload_raises_on_401() -> None:
    respx.post(WIGLE_API_BASE + UPLOAD_PATH).mock(return_value=httpx.Response(401))
    with pytest.raises(WigleAuthError):
        await upload_to_wigle(FIXTURE)


@respx.mock
async def test_upload_retries_on_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("warroute.uploader.wigle_upload.INITIAL_BACKOFF_SEC", 0.0)
    respx.post(WIGLE_API_BASE + UPLOAD_PATH).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(429),
            httpx.Response(200, json={"success": True, "transid": "ok"}),
        ]
    )
    result = await upload_to_wigle(FIXTURE)
    assert result.success is True
    assert result.transid == "ok"


@respx.mock
async def test_upload_gives_up_on_persistent_429(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("warroute.uploader.wigle_upload.INITIAL_BACKOFF_SEC", 0.0)
    monkeypatch.setattr("warroute.uploader.wigle_upload.MAX_RETRIES", 3)
    respx.post(WIGLE_API_BASE + UPLOAD_PATH).mock(return_value=httpx.Response(429))
    with pytest.raises(WigleRateLimitError):
        await upload_to_wigle(FIXTURE)


@respx.mock
async def test_upload_raises_on_success_false() -> None:
    respx.post(WIGLE_API_BASE + UPLOAD_PATH).mock(
        return_value=httpx.Response(200, json={"success": False, "message": "bad payload"})
    )
    with pytest.raises(WigleError, match="success=false"):
        await upload_to_wigle(FIXTURE)


def test_upload_requires_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    from warroute.config import get_settings

    monkeypatch.setenv("WIGLE_NAME", "")
    monkeypatch.setenv("WIGLE_TOKEN", "")
    get_settings.cache_clear()

    import asyncio

    with pytest.raises(WigleAuthError):
        asyncio.run(upload_to_wigle(FIXTURE))
