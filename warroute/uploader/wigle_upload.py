"""Upload a WigleWifi-1.6 CSV to WiGLE.net.

Endpoint: POST https://api.wigle.net/api/v2/file/upload
Auth: HTTP Basic (WIGLE_NAME, WIGLE_TOKEN).
Returns: JSON with `success` and a `transid` per accepted file.
Rate-limited; 429 -> exponential backoff (max 5 attempts).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from warroute.clients.wigle import WIGLE_API_BASE, WigleAuthError, WigleError, WigleRateLimitError
from warroute.config import get_settings

logger = logging.getLogger(__name__)

UPLOAD_PATH = "/api/v2/file/upload"
DEFAULT_TIMEOUT = 120.0
MAX_RETRIES = 5
INITIAL_BACKOFF_SEC = 2.0


@dataclass
class WigleUploadResult:
    success: bool
    transid: str | None
    raw: dict[str, object]


async def upload_to_wigle(csv_path: Path) -> WigleUploadResult:
    """POST a CSV to WiGLE.net file upload. Retries on 429 with exponential backoff."""
    settings = get_settings()
    if not settings.wigle_name or not settings.wigle_token:
        raise WigleAuthError("WIGLE_NAME and WIGLE_TOKEN must be set in .env")

    backoff = INITIAL_BACKOFF_SEC
    async with httpx.AsyncClient(
        base_url=WIGLE_API_BASE,
        timeout=DEFAULT_TIMEOUT,
        auth=(settings.wigle_name, settings.wigle_token),
        headers={"Accept": "application/json"},
    ) as client:
        for attempt in range(1, MAX_RETRIES + 1):
            with csv_path.open("rb") as fh:
                files = {"file": (csv_path.name, fh, "text/csv")}
                try:
                    resp = await client.post(UPLOAD_PATH, files=files)
                except httpx.RequestError as exc:
                    raise WigleError(f"WiGLE upload network error: {exc}") from exc

            if resp.status_code == 401:
                raise WigleAuthError("WiGLE rejected credentials (401)")
            if resp.status_code == 429:
                if attempt == MAX_RETRIES:
                    raise WigleRateLimitError(f"WiGLE 429 after {MAX_RETRIES} attempts")
                logger.info("WiGLE 429; retry %d after %.1fs", attempt, backoff)
                await asyncio.sleep(backoff)
                backoff *= 2
                continue
            if resp.status_code >= 400:
                raise WigleError(f"WiGLE upload HTTP {resp.status_code}: {resp.text[:300]}")

            try:
                payload = resp.json()
            except ValueError as exc:
                raise WigleError(f"WiGLE returned non-JSON: {resp.text[:200]}") from exc
            if not payload.get("success"):
                raise WigleError(f"WiGLE upload success=false: {payload}")
            transid = _extract_transid(payload)
            return WigleUploadResult(success=True, transid=transid, raw=payload)

    raise WigleRateLimitError("WiGLE upload exhausted retries")


def _extract_transid(payload: dict[str, object]) -> str | None:
    """The API has wrapped the transid differently across versions; grab the first hit."""
    for key in ("transid", "transID", "transactionId"):
        value = payload.get(key)
        if value:
            return str(value)
    results = payload.get("results")
    if isinstance(results, list) and results:
        first = results[0]
        if isinstance(first, dict):
            for key in ("transid", "transID"):
                value = first.get(key)
                if value:
                    return str(value)
    return None
