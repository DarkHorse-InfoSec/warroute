"""/settings: read-only display of config + API key fingerprints (NEVER full keys)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from warroute.config import get_settings
from warroute.web.templating import render

router = APIRouter()


def _fingerprint(secret: str) -> str:
    """Return a redacted preview of a secret value: 'set | last4=abcd' or 'unset'."""
    if not secret:
        return "unset"
    last4 = secret[-4:] if len(secret) >= 8 else "...."
    return f"set (last4={last4})"


@router.get("")
async def get_settings_page(request: Request):  # type: ignore[no-untyped-def]
    s = get_settings()
    rows = [
        ("Home lat", f"{s.home_lat:.4f}"),
        ("Home lon", f"{s.home_lon:.4f}"),
        ("Home radius (km)", f"{s.home_radius_km:.0f}"),
        ("Default duration (min)", str(s.default_duration_min)),
        ("Database", str(s.sqlite_path)),
        ("Spool dir", str(s.spool_dir)),
        ("GPX out dir", str(s.gpx_out_dir)),
        ("Hetzner IP", s.hetzner_ip_addr or "unset"),
        ("WIGLE_NAME", s.wigle_name or "unset"),
        ("WIGLE_TOKEN", _fingerprint(s.wigle_token)),
        ("WDGOWARS_NAME", s.wdgowars_name or "unset"),
        ("WDGOWARS_TOKEN", _fingerprint(s.wdgowars_token)),
        ("ORS_API_KEY", _fingerprint(s.ors_api_key)),
        ("MAPBOX_API_KEY", _fingerprint(s.mapbox_api_key)),
        ("NTFY_TOPIC", s.ntfy_topic or "unset"),
    ]
    return render(request, "settings.html", rows=rows)
