"""Restart-loop guard around boot-time recovery.

If the work a boot does to recover (resuming runs from snapshots, re-sending answers)
is itself what crashes the process, a supervised deployment enters a tight crash and
respawn loop. The guard counts *unclean* boots — the previous process died without
clearing its running marker — inside a short window; at the threshold it tells the boot
to skip recovery for this once, so the service still starts and serves live traffic.
Operator restarts never trip it (a clean shutdown clears the marker) and every failure
of the guard itself fails open.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

WINDOW_MINUTES = 10
THRESHOLD = 3


def _atomic_write(path: Path, text: str) -> None:
    """Write text to path without ever leaving a truncated file behind.

    A crash between the two writes of a plain ``write_text`` (marker, then
    history) would leave the second file half-written; the guard's counter
    would then reset on the next boot and forget the crash loop it exists
    to detect. Write to a per-write temp file in the same directory and
    ``os.replace`` it into place: the old content stands until the new one
    is complete.

    The temp name carries a per-write uuid suffix, not just the pid: two
    coroutines in one process writing concurrently must not share a temp
    file and clobber each other mid-write. The file is flushed and fsynced
    before the replace, because a power cut right after ``os.replace`` can
    otherwise leave the destination as a 0-byte file — exactly the crash the
    guard counts. A failed write removes its orphaned temp file.

    Cross-platform durability: on Windows ``os.replace`` maps to
    ``MoveFileExW(MOVEFILE_REPLACE_EXISTING)`` and can transiently raise
    ``PermissionError`` (WinError 5/32) while a concurrent reader holds the
    target, so the replace is retried with a short backoff; on POSIX the
    rename is only durable once the *parent directory* is fsynced, so the
    parent is fsynced after the replace (best-effort — a failure there does
    not lose the already-replaced content).
    """
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        _replace_into_place(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if os.name != "nt":
        try:
            _fsync_dir(path.parent)
        except OSError as e:
            logger.warning("boot_guard: parent-dir fsync failed for %s: %s", path, e)


def _replace_into_place(tmp: Path, path: Path) -> None:
    """``os.replace`` with a bounded retry on Windows sharing violations.

    On Windows the replace can transiently fail with ``PermissionError``
    (WinError 5/32) while a concurrent reader holds the target without
    ``FILE_SHARE_DELETE``; back off and retry. A ``PermissionError`` that is
    not a sharing violation (a permanent access denial) is re-raised
    immediately. On POSIX the replace is atomic and needs no retry.
    """
    if os.name == "nt":
        for attempt in range(15):
            try:
                os.replace(tmp, path)
                return
            except PermissionError as e:
                if getattr(e, "winerror", None) not in (5, 32):
                    raise
                if attempt == 14:
                    raise
                time.sleep(0.002 * (attempt + 1))
    else:
        os.replace(tmp, path)


def _fsync_dir(directory: Path) -> None:
    """Fsync a directory so a preceding rename is durable across power loss."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _aware_utc(dt: datetime) -> datetime:
    """Normalize a parsed timestamp to aware UTC.

    The guard's writer always emits aware UTC (``datetime.now(UTC).isoformat()``),
    so a naive value in the history is anomalous (a manual edit or a future writer
    change). ``datetime.fromisoformat`` accepts a naive ISO string and returns a
    naive datetime; comparing it against the aware cutoff in ``on_boot`` raises
    ``TypeError`` and, because that comparison sits outside the parse ``except``,
    trips the outer fail-open before the history is ever rewritten — so one naive
    record would poison the count forever. Assume UTC rather than letting it do that.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class BootGuard:
    def __init__(self, state_dir: Path, *, window_minutes: int = WINDOW_MINUTES, threshold: int = THRESHOLD) -> None:
        self.marker = state_dir / "RUNNING"
        self.history = state_dir / "boot-history.json"
        self.window_minutes = window_minutes
        self.threshold = threshold
        self.unclean_boots = 0
        self.skip_recovery = False

    def on_boot(self) -> None:
        """Call first thing at start: records this boot, decides whether recovery is safe."""
        try:
            now = datetime.now(UTC)
            unclean = self.marker.exists()
            self.history.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(self.marker, now.isoformat())  # armed first: a failure below must not disarm the next boot
            times: list[datetime] = []
            if self.history.exists():
                try:
                    raw = json.loads(self.history.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError):
                    logger.warning("boot history unreadable; starting the count fresh", exc_info=True)
                    raw = []
                if not isinstance(raw, list):
                    logger.warning("boot history is not a list (%s); starting the count fresh", type(raw).__name__)
                    raw = []
                for t in raw:
                    if not isinstance(t, str):
                        continue
                    try:
                        times.append(_aware_utc(datetime.fromisoformat(t)))
                    except (ValueError, OverflowError):
                        logger.warning("boot history record %r unparseable; dropped", t)
            cutoff = now - timedelta(minutes=self.window_minutes)
            # A record from the future (a clock step back, a naive timestamp read as UTC) would
            # never age out of the window and could pin recovery off for good: it is not evidence.
            recent = [t for t in times if cutoff <= t <= now]
            if unclean:
                recent.append(now)
            self.unclean_boots = len(recent)
            self.skip_recovery = self.unclean_boots >= self.threshold
            _atomic_write(self.history, json.dumps([t.isoformat() for t in recent]))
            if unclean:
                logger.warning("unclean boot %d/%d within %d min%s", self.unclean_boots, self.threshold, self.window_minutes, " — skipping boot recovery" if self.skip_recovery else "")
        except Exception:  # noqa: BLE001 — the guard must never keep the bot from starting
            logger.warning("boot guard failed open", exc_info=True)
            self.skip_recovery = False

    def on_clean_shutdown(self) -> None:
        try:
            self.marker.unlink(missing_ok=True)
            _atomic_write(self.history, "[]")
        except OSError:
            pass


__all__ = ["THRESHOLD", "WINDOW_MINUTES", "BootGuard"]
