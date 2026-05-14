"""Upload a WigleWifi-1.6 CSV to WDGoWars.

Wraps the lower-level `clients/wdgowars.py` upload_csv with:
  - Pre-flight quota check via /api/me (skips upload if today's headroom < new APs)
  - Returns a typed result (success/skipped/queued)

Note: WDGoWars caps per-account at ~20k new APs/24h. If the file would
overflow, we skip and surface the headroom so the orchestrator can decide
whether to split (future work) or queue for the next day.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from warroute.clients.wdgowars import (
    WdgowarsAuthError,
    WdgowarsClient,
    WdgowarsError,
)

logger = logging.getLogger(__name__)

# Conservative per-day headroom assumption when /api/me doesn't surface a value.
DEFAULT_DAILY_QUOTA = 20000


class WdgowarsQuotaSkip(WdgowarsError):
    """The CSV would exceed today's WDGoWars quota; skipped without uploading."""


@dataclass
class WdgowarsUploadResult:
    success: bool
    skipped: bool
    headroom_remaining: int | None
    new_aps_in_csv: int
    raw: dict[str, object]


async def upload_to_wdgowars(
    csv_path: Path,
    new_aps_in_csv: int,
) -> WdgowarsUploadResult:
    """Pre-flight quota check, then POST. Raises WdgowarsQuotaSkip if it would overflow."""
    async with WdgowarsClient() as wdg:
        try:
            player = await wdg.me()
        except WdgowarsAuthError:
            raise
        except WdgowarsError as exc:
            logger.warning("WDGoWars /api/me unreachable; uploading anyway: %s", exc)
            player = None

        headroom = (
            player.daily_quota_remaining
            if player and player.daily_quota_remaining is not None
            else DEFAULT_DAILY_QUOTA
        )

        if new_aps_in_csv > headroom:
            raise WdgowarsQuotaSkip(
                f"CSV has {new_aps_in_csv} new APs; only {headroom} headroom today. "
                "Skipping; will retry tomorrow."
            )

        body = await wdg.upload_csv(csv_path)
        return WdgowarsUploadResult(
            success=True,
            skipped=False,
            headroom_remaining=headroom - new_aps_in_csv,
            new_aps_in_csv=new_aps_in_csv,
            raw=body,
        )
