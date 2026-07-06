"""Client-supplied credentials for the stateless web tier.

In the public/stateless access model (DECISIONS.md 2026-07-04 design) the browser
holds each user's WiGLE / WDGoWars / ORS keys in localStorage and sends them per
request as headers; the server stores nothing. WiGLE and WDGoWars have NO
system-key fallback here - a missing key means the operation is unavailable, never
the operator's key. ORS is the single carve-out and is resolved separately by
`warroute.web.routing_quota.resolve_ors_key` (shared key behind a quota guard).

This intentionally replaces the server-side `get_credentials(username)` path for
web-facing operations. That server-side path (user_prefs, DECISIONS 2026-05-14)
stays only for the operator's own single-tenant/admin use and background daemons.
"""

from __future__ import annotations

from fastapi import Request

from warroute.web.user_prefs import UserCredentials

# Header names the browser attaches from localStorage. Lowercased because
# Starlette's Headers lookup is case-insensitive but we compare against lowercase.
HDR_WIGLE_NAME = "x-wigle-name"
HDR_WIGLE_TOKEN = "x-wigle-token"
HDR_WDGOWARS_NAME = "x-wdgowars-name"
HDR_WDGOWARS_TOKEN = "x-wdgowars-token"
HDR_ORS_KEY = "x-ors-key"
HDR_MAPBOX_KEY = "x-mapbox-key"
HDR_NTFY_TOPIC = "x-ntfy-topic"


def _header(request: Request, name: str) -> str | None:
    """Return a trimmed non-empty header value, or None."""
    value = (request.headers.get(name) or "").strip()
    return value or None


def web_credentials(request: Request) -> UserCredentials:
    """Build UserCredentials from request headers only. No DB, no env fallback.

    Every field is whatever the browser sent (or None). Callers must treat a None
    WiGLE/WDGoWars credential as "unavailable, ask the user to add it" - they must
    NOT fall back to the system key (that would let anonymous users drain the
    operator's quota). ORS is handled by resolve_ors_key, not here.
    """
    return UserCredentials(
        wigle_name=_header(request, HDR_WIGLE_NAME),
        wigle_token=_header(request, HDR_WIGLE_TOKEN),
        wdgowars_name=_header(request, HDR_WDGOWARS_NAME),
        wdgowars_token=_header(request, HDR_WDGOWARS_TOKEN),
        ors_api_key=_header(request, HDR_ORS_KEY),
        mapbox_api_key=_header(request, HDR_MAPBOX_KEY),
        ntfy_topic=_header(request, HDR_NTFY_TOPIC),
    )
