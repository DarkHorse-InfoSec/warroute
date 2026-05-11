"""SQLite connection + migration helpers."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from warroute.config import get_settings

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with sensible defaults."""
    target = path or get_settings().sqlite_path
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def transaction(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Context manager that commits on success, rolls back on exception."""
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def current_version(conn: sqlite3.Connection) -> int:
    """Return the highest applied schema version, or 0 if uninitialized."""
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0] or 0)


def run_migrations(path: Path | None = None) -> int:
    """Apply any unapplied .sql migrations under migrations/. Returns the new version."""
    migrations = sorted(MIGRATIONS_DIR.glob("_v*.sql"))
    if not migrations:
        logger.warning("No migrations found in %s", MIGRATIONS_DIR)
        return 0

    with transaction(path) as conn:
        applied = current_version(conn)
        for migration in migrations:
            version = int(migration.stem.removeprefix("_v"))
            if version <= applied:
                continue
            logger.info("Applying migration %s", migration.name)
            conn.executescript(migration.read_text(encoding="utf-8"))
        return current_version(conn)
