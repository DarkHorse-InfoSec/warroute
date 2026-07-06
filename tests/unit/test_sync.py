"""Opt-in E2E config sync endpoints (server-side blob store).

The crypto is client-side (WebCrypto) and verified in-browser; these cover the
server's dumb-blob-store contract: store, retrieve, delete, validation, guards.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from warroute.db import run_migrations
from warroute.web.app import create_app
from warroute.web.routes.sync import reset_rate_state

_ID = "a" * 64  # a valid-shaped sync id (64 hex chars)
_ID2 = "b" * 64


@pytest.fixture(autouse=True)
def _reset() -> None:
    reset_rate_state()


@pytest.fixture
def client() -> Iterator[TestClient]:
    run_migrations()
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_put_then_get_roundtrip(client: TestClient) -> None:
    r = client.put(f"/sync/{_ID}", json={"ciphertext": "encrypted-blob-xyz"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    g = client.get(f"/sync/{_ID}")
    assert g.status_code == 200
    assert g.json()["ciphertext"] == "encrypted-blob-xyz"
    assert g.json()["updated_at"]


def test_get_missing_is_404(client: TestClient) -> None:
    assert client.get(f"/sync/{_ID2}").status_code == 404


def test_put_replaces(client: TestClient) -> None:
    client.put(f"/sync/{_ID}", json={"ciphertext": "first"})
    client.put(f"/sync/{_ID}", json={"ciphertext": "second"})
    assert client.get(f"/sync/{_ID}").json()["ciphertext"] == "second"


def test_delete_removes(client: TestClient) -> None:
    client.put(f"/sync/{_ID}", json={"ciphertext": "blob"})
    assert client.delete(f"/sync/{_ID}").status_code == 200
    assert client.get(f"/sync/{_ID}").status_code == 404


def test_invalid_sync_id_rejected(client: TestClient) -> None:
    assert client.put("/sync/not-hex", json={"ciphertext": "x"}).status_code == 400
    assert client.get("/sync/tooShort").status_code == 400
    assert client.put(f"/sync/{'A' * 64}", json={"ciphertext": "x"}).status_code == 400  # uppercase


def test_empty_ciphertext_rejected(client: TestClient) -> None:
    assert client.put(f"/sync/{_ID}", json={"ciphertext": ""}).status_code == 400


def test_oversized_ciphertext_rejected(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNC_MAX_BYTES", "100")
    from warroute.config import get_settings

    get_settings.cache_clear()
    assert client.put(f"/sync/{_ID}", json={"ciphertext": "x" * 200}).status_code == 413


def test_rate_limit(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNC_RATE_PER_MIN", "3")
    from warroute.config import get_settings

    get_settings.cache_clear()
    reset_rate_state()
    # 3 allowed, 4th is throttled (all within the same minute window).
    assert client.get(f"/sync/{_ID}").status_code in (200, 404)
    assert client.get(f"/sync/{_ID}").status_code in (200, 404)
    assert client.get(f"/sync/{_ID}").status_code in (200, 404)
    assert client.get(f"/sync/{_ID}").status_code == 429


def test_server_stores_opaque_ciphertext_only(client: TestClient) -> None:
    """The server never sees plaintext: whatever ciphertext goes in comes back
    byte-identical, and nothing is interpreted."""
    blob = "iv.base64||AESGCM-ciphertext-the-server-cannot-read"
    client.put(f"/sync/{_ID}", json={"ciphertext": blob})
    from warroute.db import transaction

    with transaction() as conn:
        row = conn.execute(
            "SELECT ciphertext FROM synced_configs WHERE sync_id = ?", (_ID,)
        ).fetchone()
    assert row["ciphertext"] == blob  # stored verbatim, opaque
