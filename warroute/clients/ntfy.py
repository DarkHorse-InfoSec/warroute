"""ntfy.sh push-notification client. Best-effort; failures never propagate.

POSTs to `{base_url}/{topic}` with the message as the request body. Headers
carry optional title, priority, tags, and click-through URL. Auth via Bearer
token if `auth_token` is set (for self-hosted/private ntfy servers); public
ntfy.sh requires no auth.

Design contract: this is a best-effort notification channel. Transport errors,
HTTP failures, or an empty `topic` all return False without raising. Callers
must not let notification failures break their primary flow.
"""

from __future__ import annotations

import logging

import httpx

from warroute.config import get_settings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0


class NtfyClient:
    """Async client for ntfy.sh-compatible servers."""

    def __init__(
        self,
        topic: str | None = None,
        base_url: str | None = None,
        auth_token: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        settings = get_settings()
        self._topic = topic if topic is not None else settings.ntfy_topic
        self._base_url = (base_url or settings.ntfy_base_url).rstrip("/")
        self._auth_token = auth_token if auth_token is not None else settings.ntfy_auth_token
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> NtfyClient:
        if self._client is None:
            headers: dict[str, str] = {}
            if self._auth_token:
                headers["Authorization"] = f"Bearer {self._auth_token}"
            self._client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, headers=headers)
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def enabled(self) -> bool:
        """True if a topic is configured. Callers can short-circuit on this."""
        return bool(self._topic)

    async def notify(
        self,
        message: str,
        *,
        title: str | None = None,
        priority: int | None = None,
        tags: list[str] | None = None,
        click_url: str | None = None,
    ) -> bool:
        """POST a notification. Returns True on 2xx, False on any failure or empty topic."""
        if not self._topic:
            return False
        if self._client is None:
            raise RuntimeError("NtfyClient must be used as an async context manager")

        url = f"{self._base_url}/{self._topic}"
        headers: dict[str, str] = {}
        if title is not None:
            headers["Title"] = title
        if priority is not None:
            headers["Priority"] = str(priority)
        if tags:
            headers["Tags"] = ",".join(tags)
        if click_url:
            headers["Click"] = click_url

        try:
            resp = await self._client.post(url, content=message, headers=headers)
        except httpx.RequestError as exc:
            logger.warning("ntfy.sh transport error: %s", exc)
            return False

        if resp.status_code >= 400:
            logger.warning(
                "ntfy.sh rejected notification: HTTP %d body=%s",
                resp.status_code,
                resp.text[:200],
            )
            return False
        return True
