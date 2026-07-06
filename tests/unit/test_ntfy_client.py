"""Tests for the ntfy.sh client. HTTP mocked via respx."""

from __future__ import annotations

import httpx
import pytest
import respx

from warroute.clients.ntfy import NtfyClient
from warroute.config import get_settings


@pytest.fixture(autouse=True)
def _ntfy_topic_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with a known topic; individual tests override as needed."""
    monkeypatch.setenv("NTFY_TOPIC", "warroute-test")
    monkeypatch.setenv("NTFY_BASE_URL", "https://ntfy.sh")
    monkeypatch.setenv("NTFY_AUTH_TOKEN", "")
    get_settings.cache_clear()


@respx.mock
async def test_notify_posts_message_to_topic_url() -> None:
    route = respx.post("https://ntfy.sh/warroute-test").mock(
        return_value=httpx.Response(200, json={"id": "abc"})
    )
    async with NtfyClient() as ntfy:
        ok = await ntfy.notify("hello")
    assert ok is True
    assert route.called
    sent = route.calls.last.request
    assert sent.content == b"hello"


@respx.mock
async def test_notify_sends_optional_headers() -> None:
    route = respx.post("https://ntfy.sh/warroute-test").mock(return_value=httpx.Response(200))
    async with NtfyClient() as ntfy:
        await ntfy.notify(
            "body",
            title="A title",
            priority=4,
            tags=["car", "white_check_mark"],
            click_url="https://example.com/runs/42",
        )
    sent = route.calls.last.request
    assert sent.headers["Title"] == "A title"
    assert sent.headers["Priority"] == "4"
    assert sent.headers["Tags"] == "car,white_check_mark"
    assert sent.headers["Click"] == "https://example.com/runs/42"


@respx.mock
async def test_notify_omits_optional_headers_when_unset() -> None:
    route = respx.post("https://ntfy.sh/warroute-test").mock(return_value=httpx.Response(200))
    async with NtfyClient() as ntfy:
        await ntfy.notify("body")
    sent = route.calls.last.request
    assert "Title" not in sent.headers
    assert "Priority" not in sent.headers
    assert "Tags" not in sent.headers
    assert "Click" not in sent.headers


async def test_notify_skips_silently_when_topic_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NTFY_TOPIC", "")
    get_settings.cache_clear()
    async with NtfyClient() as ntfy:
        assert ntfy.enabled is False
        ok = await ntfy.notify("hello")
    assert ok is False


@respx.mock
async def test_notify_returns_false_on_5xx() -> None:
    respx.post("https://ntfy.sh/warroute-test").mock(
        return_value=httpx.Response(503, text="service unavailable")
    )
    async with NtfyClient() as ntfy:
        ok = await ntfy.notify("hello")
    assert ok is False


@respx.mock
async def test_notify_returns_false_on_transport_error() -> None:
    respx.post("https://ntfy.sh/warroute-test").mock(
        side_effect=httpx.ConnectError("connection refused")
    )
    async with NtfyClient() as ntfy:
        ok = await ntfy.notify("hello")
    assert ok is False


@respx.mock
async def test_notify_sends_bearer_auth_when_token_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NTFY_AUTH_TOKEN", "tk_private_xyz")
    get_settings.cache_clear()
    route = respx.post("https://ntfy.sh/warroute-test").mock(return_value=httpx.Response(200))
    async with NtfyClient() as ntfy:
        await ntfy.notify("body")
    assert route.calls.last.request.headers["Authorization"] == "Bearer tk_private_xyz"


@respx.mock
async def test_notify_respects_custom_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NTFY_BASE_URL", "https://ntfy.darkhorseinfosec.com")
    get_settings.cache_clear()
    route = respx.post("https://ntfy.darkhorseinfosec.com/warroute-test").mock(
        return_value=httpx.Response(200)
    )
    async with NtfyClient() as ntfy:
        ok = await ntfy.notify("body")
    assert ok is True
    assert route.called


def test_constructor_overrides_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NTFY_TOPIC", "default")
    get_settings.cache_clear()
    ntfy = NtfyClient(topic="override", base_url="https://example.com/", auth_token="t")
    assert ntfy._topic == "override"
    assert ntfy._base_url == "https://example.com"  # trailing slash stripped
    assert ntfy._auth_token == "t"
