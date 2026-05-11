"""Pre-drive sanity harness.

Verifies that the four external dependencies (WiGLE, WDGoWars, ORS, filesystem)
are healthy *before* Domenic turns the key on a planned wardrive. Cheap by design:
one auth-check call per service, no full planner run. For a real end-to-end test,
run `warroute plan --duration 15m --out /tmp/test.gpx` after precheck is green.

Each check returns a `CheckResult` with status (ok | warn | fail), a one-line
detail, and an optional actionable hint. `run_all()` orchestrates them and
returns a list; the CLI renders + sets an exit code from the overall verdict.

NOTE: These functions make REAL HTTP calls when run live. Tests stub them via
respx mocks. Do not run live from a network with TLS interception (see
DECISIONS.md 2026-05-11 Fortinet entry); tokens would transit the inspection
device in plaintext. Cert-chain check first.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from warroute.clients.ors import OrsAuthError, OrsClient, OrsError, OrsQuotaError, Waypoint
from warroute.clients.wdgowars import WdgowarsAuthError, WdgowarsClient, WdgowarsError
from warroute.clients.wigle import (
    WigleAuthError,
    WigleClient,
    WigleError,
    WigleRateLimitError,
)
from warroute.config import get_settings

logger = logging.getLogger(__name__)

QUOTA_WARN_THRESHOLD = 1000  # WDGoWars headroom below this -> WARN


class Status(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass
class CheckResult:
    name: str
    status: Status
    detail: str
    hint: str | None = None


def verdict(results: list[CheckResult]) -> Status:
    """Overall verdict: FAIL if any fail, else WARN if any warn, else OK."""
    if any(r.status == Status.FAIL for r in results):
        return Status.FAIL
    if any(r.status == Status.WARN for r in results):
        return Status.WARN
    return Status.OK


async def check_wigle() -> CheckResult:
    """Cheap WIGLE auth check via /api/v2/profile/user.

    Sub-second response, no network-index lookup. The previous bbox-search
    variant was prone to 60s+ ReadTimeouts on the free tier even for 100m
    bboxes, masking real auth/network issues as transport timeouts.
    """
    try:
        async with WigleClient() as wigle:
            profile = await wigle.profile()
        userid = profile.get("userid")
        detail = f"auth ok; userid={userid}" if userid else "auth ok"
        return CheckResult(name="WiGLE", status=Status.OK, detail=detail)
    except WigleAuthError as exc:
        return CheckResult(
            name="WiGLE",
            status=Status.FAIL,
            detail=str(exc),
            hint="Check WIGLE_NAME (the AID-prefixed name) and WIGLE_TOKEN at https://wigle.net/account",
        )
    except WigleRateLimitError as exc:
        return CheckResult(
            name="WiGLE",
            status=Status.WARN,
            detail=str(exc),
            hint="Wait ~60s; WiGLE free tier is 1 req/sec",
        )
    except WigleError as exc:
        return CheckResult(
            name="WiGLE",
            status=Status.FAIL,
            detail=str(exc),
            hint="Network reachability problem; verify TLS cert chain if on unfamiliar network",
        )


async def check_wdgowars() -> CheckResult:
    """WDGoWars /api/me + quota headroom."""
    try:
        async with WdgowarsClient() as wdg:
            player = await wdg.me()
        headroom = player.daily_quota_remaining
        detail = (
            f"user={player.username or '?'} "
            f"wifi={player.wifi} "
            f"recent_today={player.recent_today} "
            f"headroom={headroom}"
        )
        if headroom is None:
            return CheckResult(name="WDGoWars", status=Status.OK, detail=detail)
        if headroom <= 0:
            return CheckResult(
                name="WDGoWars",
                status=Status.WARN,
                detail=detail,
                hint="Daily 20k cap exhausted; uploads will be skipped until UTC midnight",
            )
        if headroom < QUOTA_WARN_THRESHOLD:
            return CheckResult(
                name="WDGoWars",
                status=Status.WARN,
                detail=detail,
                hint=f"Only {headroom} new-AP headroom today; large CSVs will be skipped",
            )
        return CheckResult(name="WDGoWars", status=Status.OK, detail=detail)
    except WdgowarsAuthError as exc:
        return CheckResult(
            name="WDGoWars",
            status=Status.FAIL,
            detail=str(exc),
            hint="Token rejected. Auth uses X-API-Key header (NOT Bearer). Regenerate at https://wdgwars.pl/profile",
        )
    except WdgowarsError as exc:
        return CheckResult(
            name="WDGoWars",
            status=Status.FAIL,
            detail=str(exc),
            hint="Network or server problem; verify TLS cert chain if on unfamiliar network",
        )


async def check_ors() -> CheckResult:
    """ORS auth + reachability via a tiny 2-point directions call."""
    settings = get_settings()
    home = Waypoint(lat=settings.home_lat, lon=settings.home_lon, label="home")
    nudged = Waypoint(
        lat=settings.home_lat + 0.001,
        lon=settings.home_lon + 0.001,
        label="nudge",
    )
    try:
        async with OrsClient() as ors:
            leg = await ors.directions([home, nudged], with_geometry=False)
        return CheckResult(
            name="ORS",
            status=Status.OK,
            detail=f"auth ok; tiny route: {leg.distance_m:.0f} m, {leg.duration_s:.1f} s",
        )
    except OrsAuthError as exc:
        return CheckResult(
            name="ORS",
            status=Status.FAIL,
            detail=str(exc),
            hint="Verify ORS_API_KEY at https://openrouteservice.org/dev (free tier is 2000 directions/day)",
        )
    except OrsQuotaError as exc:
        return CheckResult(
            name="ORS",
            status=Status.WARN,
            detail=str(exc),
            hint="Daily quota exhausted; plans will fail until UTC midnight or wire up MAPBOX_API_KEY fallback",
        )
    except OrsError as exc:
        return CheckResult(
            name="ORS",
            status=Status.FAIL,
            detail=str(exc),
            hint="Network or server problem; verify TLS cert chain if on unfamiliar network",
        )


def check_filesystem() -> list[CheckResult]:
    """Verify SPOOL_DIR and GPX_OUT_DIR exist and are writable. Synchronous."""
    settings = get_settings()
    return [
        _check_writable_dir("SPOOL_DIR", settings.spool_dir),
        _check_writable_dir("GPX_OUT_DIR", settings.gpx_out_dir),
    ]


def _check_writable_dir(name: str, path: Path) -> CheckResult:
    if not path.exists():
        return CheckResult(
            name=name,
            status=Status.FAIL,
            detail=f"does not exist: {path}",
            hint=f"mkdir -p '{path}' (or update {name} in .env)",
        )
    if not path.is_dir():
        return CheckResult(
            name=name,
            status=Status.FAIL,
            detail=f"not a directory: {path}",
            hint=f"Remove the file and create as a directory: rm '{path}' && mkdir -p '{path}'",
        )
    try:
        with tempfile.NamedTemporaryFile(
            dir=str(path), prefix=".warroute-precheck-", delete=False
        ) as fh:
            marker = Path(fh.name)
        os.unlink(marker)
        return CheckResult(name=name, status=Status.OK, detail=f"writable: {path}")
    except OSError as exc:
        return CheckResult(
            name=name,
            status=Status.FAIL,
            detail=f"not writable: {path} ({exc})",
            hint="chown / chmod the directory so the running user can write",
        )


async def run_all() -> list[CheckResult]:
    """Run all checks. The three external auth checks run concurrently; filesystem checks are synchronous."""
    wigle_task = asyncio.create_task(check_wigle())
    wdg_task = asyncio.create_task(check_wdgowars())
    ors_task = asyncio.create_task(check_ors())
    api_results = await asyncio.gather(wigle_task, wdg_task, ors_task)
    fs_results = check_filesystem()
    return list(api_results) + fs_results
