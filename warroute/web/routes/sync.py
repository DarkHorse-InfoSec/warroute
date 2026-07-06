"""/sync: opt-in end-to-end-encrypted config backup + cross-device restore.

DECISIONS.md 2026-07-04 (sync entry). Solves iOS Safari localStorage eviction +
cross-device. ZERO-KNOWLEDGE: the browser encrypts the config with a key derived
from a user-held sync code before upload; the server stores only opaque ciphertext
and never sees the code or the plaintext keys. `sync_id` is a SHA-256 the client
derives from the code, so the stored id does not reveal the code.

The server side is a dumb key-value blob store keyed by `sync_id`, guarded by a
per-IP rate limit + a ciphertext size cap. Knowing a `sync_id` only lets you fetch
ciphertext you cannot decrypt without the code, so no auth is needed here.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import defaultdict, deque
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from warroute.config import get_settings
from warroute.db import transaction

logger = logging.getLogger(__name__)
router = APIRouter()

# sync_id is a hex SHA-256 (64 lowercase hex chars). Reject anything else so the
# table can't be stuffed with arbitrary keys.
_SYNC_ID_RE = re.compile(r"^[0-9a-f]{64}$")

# Per-IP sliding-window rate limiter (single-process uvicorn; module state is fine).
_rate_lock = threading.Lock()
_rate_hits: dict[str, deque[float]] = defaultdict(deque)
_RATE_WINDOW_SEC = 60.0


class SyncPayload(BaseModel):
    ciphertext: str


def _client_ip(request: Request) -> str:
    return request.headers.get("x-real-ip") or (
        request.client.host if request.client else "unknown"
    )


def _rate_ok(client_ip: str, now: float, max_per_min: int) -> bool:
    with _rate_lock:
        hits = _rate_hits[client_ip]
        cutoff = now - _RATE_WINDOW_SEC
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(hits) >= max_per_min:
            return False
        hits.append(now)
        return True


def reset_rate_state() -> None:
    """Test helper: clear the in-memory per-IP rate window."""
    with _rate_lock:
        _rate_hits.clear()


def _check(request: Request, sync_id: str) -> None:
    """Validate the id + enforce the per-IP rate limit, or raise HTTPException."""
    if not _SYNC_ID_RE.match(sync_id):
        raise HTTPException(status_code=400, detail="Invalid sync id.")
    settings = get_settings()
    if not _rate_ok(_client_ip(request), time.monotonic(), settings.sync_rate_per_min):
        raise HTTPException(status_code=429, detail="Too many sync requests; slow down.")


@router.put("/{sync_id}")
async def put_sync(request: Request, sync_id: str, payload: SyncPayload) -> dict[str, object]:
    """Store (or replace) the encrypted config blob for this sync id."""
    _check(request, sync_id)
    settings = get_settings()
    ciphertext = payload.ciphertext or ""
    if not ciphertext:
        raise HTTPException(status_code=400, detail="Empty ciphertext.")
    if len(ciphertext.encode("utf-8")) > settings.sync_max_bytes:
        raise HTTPException(status_code=413, detail="Config too large.")
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    with transaction() as conn:
        conn.execute(
            "INSERT INTO synced_configs (sync_id, ciphertext, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(sync_id) DO UPDATE SET ciphertext = excluded.ciphertext,"
            " updated_at = excluded.updated_at",
            (sync_id, ciphertext, now),
        )
        # Global storage backstop: bound the table so no volume of distinct ids can
        # fill the disk. Evict the least-recently-updated rows past the cap (LRU).
        # A real user re-pushes on next change, so eviction is self-healing.
        max_rows = settings.sync_max_rows
        total = int(conn.execute("SELECT COUNT(*) AS n FROM synced_configs").fetchone()["n"])
        if total > max_rows:
            conn.execute(
                "DELETE FROM synced_configs WHERE sync_id IN ("
                " SELECT sync_id FROM synced_configs ORDER BY updated_at ASC LIMIT ?)",
                (total - max_rows,),
            )
    return {"ok": True, "updated_at": now}


@router.get("/{sync_id}")
async def get_sync(request: Request, sync_id: str) -> dict[str, object]:
    """Return the encrypted config blob for this sync id, or 404."""
    _check(request, sync_id)
    with transaction() as conn:
        row = conn.execute(
            "SELECT ciphertext, updated_at FROM synced_configs WHERE sync_id = ?",
            (sync_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No config for that code.")
    return {"ciphertext": row["ciphertext"], "updated_at": row["updated_at"]}


@router.delete("/{sync_id}")
async def delete_sync(request: Request, sync_id: str) -> dict[str, object]:
    """Delete the server-side copy for this sync id (stop syncing)."""
    _check(request, sync_id)
    with transaction() as conn:
        conn.execute("DELETE FROM synced_configs WHERE sync_id = ?", (sync_id,))
    return {"ok": True}
