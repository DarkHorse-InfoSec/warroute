"""Per-user preferences (home location) keyed by Caddy basic_auth username.

The app reads `X-Forwarded-User` (injected by the Caddy reverse_proxy after
basic_auth succeeds) and uses the username as the primary key into the
`user_prefs` table. Local dev without Caddy has no header -> functions return
None and callers fall back to .env defaults.

See DECISIONS.md 2026-05-14 (late evening) for the scope + threat model.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Request

from warroute.config import get_settings
from warroute.db import transaction

logger = logging.getLogger(__name__)

# Allowlist for the X-Forwarded-User identity. Accepts both a basic_auth
# username AND a Cloudflare Access email (Cf-Access-Authenticated-User-Email),
# since the app can be deployed behind either edge and keys prefs off whichever
# it gets. Permitted: lowercase ASCII letters, digits, and `_ . - + @`, up to
# the 254-char email maximum. Still rejects spaces, quotes, semicolons, slashes,
# and other SQL/header-injection characters.
_USERNAME_RE = re.compile(r"^[a-z0-9_.+@\-]{1,254}$")

# Navigation apps the plan-result page can hand a route off to. "google" and
# "gpx" carry the full multi-stop loop; the rest are single-destination (they
# route to the first stop only). Enforced here rather than by a DB CHECK so a
# new target can be added without a migration.
VALID_NAV_APPS: tuple[str, ...] = ("google", "gpx", "apple", "waze", "geo")
DEFAULT_NAV_APP = "google"


@dataclass(frozen=True)
class UserPrefs:
    username: str
    home_lat: float
    home_lon: float
    home_label: str | None
    updated_at: datetime
    preferred_nav_app: str | None = None


@dataclass(frozen=True)
class UserCredentials:
    """Per-user API credentials (Phase tester-2). Any field None -> fall back to .env."""

    wigle_name: str | None = None
    wigle_token: str | None = None
    wdgowars_name: str | None = None
    wdgowars_token: str | None = None
    ors_api_key: str | None = None
    mapbox_api_key: str | None = None
    ntfy_topic: str | None = None

    def with_fallbacks(self) -> UserCredentials:
        """Return a copy with any None field replaced by the corresponding .env value.

        This is the "effective" view: when the user has saved their own ORS key
        we use it; otherwise we use the system one. Empty-string env values are
        treated as not-set (consistent with the existing client `or` chains).
        """
        s = get_settings()

        def pick(user_val: str | None, env_val: str) -> str | None:
            if user_val:
                return user_val
            return env_val or None

        return UserCredentials(
            wigle_name=pick(self.wigle_name, s.wigle_name),
            wigle_token=pick(self.wigle_token, s.wigle_token),
            wdgowars_name=pick(self.wdgowars_name, s.wdgowars_name),
            wdgowars_token=pick(self.wdgowars_token, s.wdgowars_token),
            ors_api_key=pick(self.ors_api_key, s.ors_api_key),
            mapbox_api_key=pick(self.mapbox_api_key, s.mapbox_api_key),
            ntfy_topic=pick(self.ntfy_topic, s.ntfy_topic),
        )


def current_username(request: Request) -> str | None:
    """Return the authenticated username from the proxy header, or None.

    Returns None when:
      - Header is absent (local dev, or Caddy misconfigured).
      - Header is empty / whitespace-only.
      - Header contains characters outside the allowlist (defensive: don't
        let a misconfigured upstream become a weird-input vector).

    The header is lowercased before validation - basic_auth is technically
    case-insensitive for the comparison Caddy does, and we want one canonical
    key in the table.
    """
    raw = request.headers.get("x-forwarded-user", "") or ""
    candidate = raw.strip().lower()
    if not candidate:
        return None
    if not _USERNAME_RE.match(candidate):
        logger.warning("Rejected X-Forwarded-User %r (outside allowlist)", raw)
        return None
    return candidate


def get_prefs(username: str | None) -> UserPrefs | None:
    """Return the user's saved prefs row, or None if no row exists / no user."""
    if not username:
        return None
    with transaction() as conn:
        row = conn.execute(
            "SELECT username, home_lat, home_lon, home_label, updated_at, preferred_nav_app"
            " FROM user_prefs WHERE username = ?",
            (username,),
        ).fetchone()
    if row is None:
        return None
    return UserPrefs(
        username=row["username"],
        home_lat=float(row["home_lat"]),
        home_lon=float(row["home_lon"]),
        home_label=row["home_label"],
        updated_at=datetime.fromisoformat(row["updated_at"])
        if isinstance(row["updated_at"], str)
        else row["updated_at"],
        preferred_nav_app=row["preferred_nav_app"] or None,
    )


def set_prefs(
    username: str, home_lat: float, home_lon: float, home_label: str | None = None
) -> None:
    """Upsert the user's home. Raises ValueError if username is malformed."""
    if not _USERNAME_RE.match(username):
        raise ValueError(f"Invalid username {username!r}")
    if not (-90.0 <= home_lat <= 90.0) or not (-180.0 <= home_lon <= 180.0):
        raise ValueError(f"home_lat/home_lon out of range: ({home_lat}, {home_lon})")
    now = datetime.now(UTC).replace(tzinfo=None).isoformat()
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO user_prefs (username, home_lat, home_lon, home_label, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                home_lat = excluded.home_lat,
                home_lon = excluded.home_lon,
                home_label = excluded.home_label,
                updated_at = excluded.updated_at
            """,
            (username, home_lat, home_lon, home_label, now),
        )


def get_nav_app(username: str | None) -> str:
    """Return the user's preferred nav app, or DEFAULT_NAV_APP.

    Falls back to the default for: no user, no row, NULL column, or a stored
    value that is no longer in VALID_NAV_APPS (defensive against a removed
    target).
    """
    if not username:
        return DEFAULT_NAV_APP
    with transaction() as conn:
        row = conn.execute(
            "SELECT preferred_nav_app FROM user_prefs WHERE username = ?",
            (username,),
        ).fetchone()
    if row is None or not row["preferred_nav_app"]:
        return DEFAULT_NAV_APP
    value = str(row["preferred_nav_app"])
    return value if value in VALID_NAV_APPS else DEFAULT_NAV_APP


def set_nav_app(username: str, app: str) -> None:
    """Persist the user's preferred nav app. Raises ValueError on bad input.

    Creates the user_prefs row (seeded with env-default home) if the user has
    not saved a home yet - same placeholder pattern as set_credentials.
    """
    if not _USERNAME_RE.match(username):
        raise ValueError(f"Invalid username {username!r}")
    if app not in VALID_NAV_APPS:
        raise ValueError(f"Unknown nav app {app!r}; expected one of {VALID_NAV_APPS}")
    now = datetime.now(UTC).replace(tzinfo=None).isoformat()
    with transaction() as conn:
        existing = conn.execute(
            "SELECT 1 FROM user_prefs WHERE username = ?", (username,)
        ).fetchone()
        if not existing:
            s = get_settings()
            conn.execute(
                "INSERT INTO user_prefs (username, home_lat, home_lon, updated_at)"
                " VALUES (?, ?, ?, ?)",
                (username, s.home_lat, s.home_lon, now),
            )
        conn.execute(
            "UPDATE user_prefs SET preferred_nav_app = ?, updated_at = ? WHERE username = ?",
            (app, now, username),
        )


def effective_home(
    request: Request, fallback_lat: float, fallback_lon: float
) -> tuple[float, float, str | None]:
    """Resolve the home for a web request: always the caller-supplied fallback.

    Stateless model (DECISIONS.md 2026-07-04): each user's real home lives in their
    BROWSER (localStorage) and the map re-centers client-side. The server must NOT
    derive home from a proxy identity header - on the public tier `X-Forwarded-User`
    is spoofable, and looking a home up by it disclosed saved home addresses to
    anyone (security-pass 2026-07-05). So this returns the neutral fallback and no
    label; the browser fills the real values after load. `request` is retained for
    signature stability. See `get_prefs`/`current_username` (still used by the CLI).
    """
    return fallback_lat, fallback_lon, None


def is_admin(username: str | None) -> bool:
    """True iff the username is in settings.admin_users (lowercase, comma-list)."""
    if not username:
        return False
    return username.lower() in get_settings().admin_user_set


def get_credentials(username: str | None) -> UserCredentials:
    """Return the user's saved per-service credentials (or all-None when no row)."""
    if not username:
        return UserCredentials()
    with transaction() as conn:
        row = conn.execute(
            "SELECT wigle_name, wigle_token, wdgowars_name, wdgowars_token,"
            " ors_api_key, mapbox_api_key, ntfy_topic"
            " FROM user_prefs WHERE username = ?",
            (username,),
        ).fetchone()
    if row is None:
        return UserCredentials()
    return UserCredentials(
        wigle_name=row["wigle_name"] or None,
        wigle_token=row["wigle_token"] or None,
        wdgowars_name=row["wdgowars_name"] or None,
        wdgowars_token=row["wdgowars_token"] or None,
        ors_api_key=row["ors_api_key"] or None,
        mapbox_api_key=row["mapbox_api_key"] or None,
        ntfy_topic=row["ntfy_topic"] or None,
    )


# Fields the /settings form lets a user edit. Map from form field name to the
# user_prefs column name. Keeps form handling DRY and avoids accidentally
# accepting columns the user shouldn't write to (home_lat/lon use a different
# form path with geocoding).
_CRED_FIELDS: dict[str, str] = {
    "wigle_name": "wigle_name",
    "wigle_token": "wigle_token",
    "wdgowars_name": "wdgowars_name",
    "wdgowars_token": "wdgowars_token",
    "ors_api_key": "ors_api_key",
    "mapbox_api_key": "mapbox_api_key",
    "ntfy_topic": "ntfy_topic",
}


def set_credentials(username: str, updates: dict[str, str | None]) -> None:
    """Upsert per-service credentials. Only keys in _CRED_FIELDS are accepted.

    Empty-string / None values clear that column (so the user can revert to the
    .env fallback by submitting a blank field).
    """
    if not _USERNAME_RE.match(username):
        raise ValueError(f"Invalid username {username!r}")
    safe_updates: dict[str, str | None] = {}
    for form_key, raw_val in updates.items():
        if form_key not in _CRED_FIELDS:
            continue
        col = _CRED_FIELDS[form_key]
        if raw_val is None or not str(raw_val).strip():
            safe_updates[col] = None
        else:
            safe_updates[col] = str(raw_val).strip()
    if not safe_updates:
        return

    now = datetime.now(UTC).replace(tzinfo=None).isoformat()
    set_clauses = ", ".join(f"{col} = ?" for col in safe_updates)
    values = list(safe_updates.values())
    with transaction() as conn:
        # The row may not exist yet (user never saved a home but is setting creds
        # first). Insert a placeholder row pointing at the env defaults, then
        # update with the credential values.
        existing = conn.execute(
            "SELECT 1 FROM user_prefs WHERE username = ?", (username,)
        ).fetchone()
        if not existing:
            s = get_settings()
            conn.execute(
                "INSERT INTO user_prefs (username, home_lat, home_lon, updated_at)"
                " VALUES (?, ?, ?, ?)",
                (username, s.home_lat, s.home_lon, now),
            )
        conn.execute(
            f"UPDATE user_prefs SET {set_clauses}, updated_at = ? WHERE username = ?",
            [*values, now, username],
        )


def credential_fingerprints(
    creds: UserCredentials, effective: UserCredentials
) -> list[dict[str, str | None]]:
    """Build a UI-friendly view: per service, what's saved + what's effective.

    Returns one dict per service with keys:
      - label: human name
      - user_value_status: "set (last4=abcd)" or "unset"
      - effective_value_status: same for the post-fallback value
      - source: "your saved value" | "system default" | "not configured"
    """
    services = [
        ("WiGLE name", creds.wigle_name, effective.wigle_name),
        ("WiGLE token", _last4(creds.wigle_token), _last4(effective.wigle_token)),
        ("WDGoWars name", creds.wdgowars_name, effective.wdgowars_name),
        ("WDGoWars token", _last4(creds.wdgowars_token), _last4(effective.wdgowars_token)),
        ("ORS API key", _last4(creds.ors_api_key), _last4(effective.ors_api_key)),
        ("Mapbox API key", _last4(creds.mapbox_api_key), _last4(effective.mapbox_api_key)),
        ("ntfy topic", creds.ntfy_topic, effective.ntfy_topic),
    ]
    out: list[dict[str, str | None]] = []
    for label, user_val, eff_val in services:
        if user_val:
            source = "your saved value"
        elif eff_val:
            source = "system default"
        else:
            source = "not configured"
        out.append(
            {
                "label": label,
                "user_value_status": user_val or "(unset)",
                "effective_value_status": eff_val or "(none)",
                "source": source,
            }
        )
    return out


def _last4(secret: str | None) -> str | None:
    """Mask a secret to its last 4 chars, or return None unchanged."""
    if not secret:
        return None
    if len(secret) <= 4:
        return "****"
    return f"...{secret[-4:]}"
