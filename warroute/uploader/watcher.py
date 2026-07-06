"""Watch SPOOL_DIR for newly arrived CSVs and ingest them.

Uses the watchdog library on cross-platform inotify-equivalent. Triggers on
file close-write so we don't pick up half-written rsync/Syncthing transfers.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

from watchdog.events import FileClosedEvent, FileSystemEvent, PatternMatchingEventHandler
from watchdog.observers import Observer

from warroute.config import get_settings
from warroute.uploader.orchestrator import ingest

logger = logging.getLogger(__name__)


class _CsvHandler(PatternMatchingEventHandler):
    def __init__(self, loop: asyncio.AbstractEventLoop, source: str) -> None:
        super().__init__(patterns=["*.csv", "*.CSV"], ignore_directories=True)
        self._loop = loop
        self._source = source

    def _kick(self, src_path: str) -> None:
        path = Path(src_path)
        logger.info("Spool detected: %s", path.name)

        async def _run() -> None:
            try:
                result = await ingest(path, source=self._source)
            except Exception:
                logger.exception("Ingest failed: %s", path)
                return
            if result.already_seen:
                logger.info("Already seen %s (session %s); skipped.", path.name, result.session_id)
            else:
                logger.info(
                    "Ingested %s -> session %s, %d new APs, wigle=%s wdgowars=%s",
                    path.name,
                    result.session_id,
                    result.new_aps,
                    "ok" if hasattr(result.wigle, "success") else result.wigle,
                    "ok" if hasattr(result.wdgowars, "success") else result.wdgowars,
                )

        asyncio.run_coroutine_threadsafe(_run(), self._loop)

    def on_closed(self, event: FileSystemEvent) -> None:
        if isinstance(event, FileClosedEvent):
            self._kick(str(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        # rsync writes to a tempfile and renames it on completion - catch that path too.
        dest = getattr(event, "dest_path", None)
        if dest:
            self._kick(str(dest))


def watch(spool_dir: Path | None = None, source: str = "wigle-android") -> None:
    """Block forever, ingesting CSVs as they appear."""
    target = spool_dir or get_settings().spool_dir
    target.mkdir(parents=True, exist_ok=True)
    logger.info("Watching %s for new CSVs", target)

    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()

    handler = _CsvHandler(loop=loop, source=source)
    observer = Observer()
    observer.schedule(handler, str(target), recursive=False)
    observer.start()

    try:
        observer.join()
    except KeyboardInterrupt:
        logger.info("Stopping watcher")
    finally:
        observer.stop()
        observer.join()
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=5)
