"""Boot guard atomic write: per-write temp names, fsync, no orphaned temps."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import daedalus.host.boot_guard as boot_guard
from daedalus.host.boot_guard import BootGuard, _atomic_write


def test_atomic_write_replaces_content_and_leaves_no_temp(tmp_path: Path) -> None:
    target = tmp_path / "boot-history.json"
    _atomic_write(target, "[]")
    _atomic_write(target, '["2026-09-07T00:00:00+00:00"]')
    assert target.read_text(encoding="utf-8") == '["2026-09-07T00:00:00+00:00"]'
    assert [p.name for p in tmp_path.iterdir()] == ["boot-history.json"]


def test_atomic_write_uses_distinct_temp_names(tmp_path: Path, monkeypatch) -> None:
    """Two writes in one process must not share a temp file (pid-only names collide)."""
    target = tmp_path / "marker"
    names: list[str] = []
    real_replace = boot_guard.os.replace

    def spy(src, dst):
        names.append(src.name)
        return real_replace(src, dst)

    monkeypatch.setattr(boot_guard.os, "replace", spy)
    _atomic_write(target, "1")
    _atomic_write(target, "2")
    assert len(names) == 2 and names[0] != names[1]
    assert all(n.startswith("marker.tmp.") for n in names)


def test_atomic_write_cleans_up_orphan_on_failure(tmp_path: Path, monkeypatch) -> None:
    """A failed replace must not leave a .tmp file behind on disk."""
    target = tmp_path / "marker"

    def boom(src, dst):
        raise PermissionError("simulated")

    monkeypatch.setattr(boot_guard.os, "replace", boom)
    with pytest.raises(PermissionError):
        _atomic_write(target, "x")
    assert [p.name for p in tmp_path.iterdir()] == []
    assert not target.exists()


def test_atomic_write_fsyncs_before_replace(tmp_path: Path, monkeypatch) -> None:
    """The data must hit disk before the rename, or a power cut leaves a 0-byte file."""
    import builtins

    target = tmp_path / "marker"
    order: list[str] = []
    real_replace = boot_guard.os.replace
    real_open = builtins.open

    class _SpyFile:
        def __init__(self, real):
            self._real = real

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self._real.__exit__(*exc)

        def write(self, text):
            order.append("write")
            return self._real.write(text)

        def flush(self):
            order.append("flush")
            return self._real.flush()

        def fileno(self):
            return self._real.fileno()

    def spy_open(path, *args, **kwargs):
        return _SpyFile(real_open(path, *args, **kwargs))

    def spy_replace(src, dst):
        order.append("replace")
        return real_replace(src, dst)

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(boot_guard.os, "name", "posix")
    monkeypatch.setattr(boot_guard.os, "replace", spy_replace)
    monkeypatch.setattr(boot_guard.os, "fsync", lambda fd: order.append("fsync"))
    _atomic_write(target, "x")
    # write, flush, file-fsync, replace, then the parent-dir fsync (POSIX durability).
    assert order == ["write", "flush", "fsync", "replace", "fsync"]
    assert target.read_text(encoding="utf-8") == "x"


def test_replace_retries_on_windows_sharing_violation(tmp_path: Path, monkeypatch) -> None:
    """On Windows a transient PermissionError on replace is retried, not fatal."""
    target = tmp_path / "marker"
    monkeypatch.setattr(boot_guard.os, "name", "nt")
    monkeypatch.setattr(boot_guard.time, "sleep", lambda s: None)
    calls = {"n": 0}
    real_replace = boot_guard.os.replace

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            err = PermissionError("WinError 32 sharing violation")
            err.winerror = 32
            raise err
        return real_replace(src, dst)

    monkeypatch.setattr(boot_guard.os, "replace", flaky)
    _atomic_write(target, "x")
    assert calls["n"] == 2  # first attempt failed, second succeeded
    assert target.read_text(encoding="utf-8") == "x"


def test_replace_does_not_retry_non_sharing_permission_error(tmp_path: Path, monkeypatch) -> None:
    """A PermissionError that is not a sharing violation (WinError 5/32) is re-raised immediately."""
    target = tmp_path / "marker"
    monkeypatch.setattr(boot_guard.os, "name", "nt")
    calls = {"n": 0}

    def denied(src, dst):
        calls["n"] += 1
        err = PermissionError("WinError 87 invalid parameter (not a sharing violation)")
        err.winerror = 87
        raise err

    monkeypatch.setattr(boot_guard.os, "replace", denied)
    with pytest.raises(PermissionError):
        _atomic_write(target, "x")
    assert calls["n"] == 1  # no retry on a non-sharing PermissionError
    assert [p.name for p in tmp_path.iterdir()] == []  # temp cleaned up


def test_replace_retries_exhaust_then_reraise(tmp_path: Path, monkeypatch) -> None:
    """15 sharing violations exhaust the retry budget and re-raise (no infinite retry)."""
    target = tmp_path / "marker"
    monkeypatch.setattr(boot_guard.os, "name", "nt")
    monkeypatch.setattr(boot_guard.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def always_violated(src, dst):
        calls["n"] += 1
        err = PermissionError("WinError 32 sharing violation")
        err.winerror = 32
        raise err

    monkeypatch.setattr(boot_guard.os, "replace", always_violated)
    with pytest.raises(PermissionError):
        _atomic_write(target, "x")
    assert calls["n"] == 15  # bounded
    assert not target.exists()
    assert [p.name for p in tmp_path.iterdir()] == []  # temp cleaned up


def test_naive_iso_date_does_not_poison_history(tmp_path: Path) -> None:
    """A parseable naive ISO date must not poison the unclean-boot count.

    Regression for the reviewer finding: ``datetime.fromisoformat`` accepts a
    naive ISO string and returns a naive datetime; comparing it against the
    aware cutoff raised ``TypeError`` outside the parse ``except``, tripping the
    outer fail-open before the history was ever rewritten. One naive record
    would then poison the count forever.
    """
    (tmp_path / "RUNNING").write_text("synthetic previous process")
    (tmp_path / "boot-history.json").write_text('["2026-01-01T12:00:00"]')  # naive, outside window
    results = []
    for _ in range(3):
        g = BootGuard(tmp_path)
        g.on_boot()
        results.append((g.unclean_boots, g.skip_recovery))
    # The count must accumulate and trip the threshold, not fail open every time.
    assert results == [(1, False), (2, False), (3, True)]
    # The poisoned naive record must be gone (the history was rewritten).
    assert "2026-01-01T12:00:00" not in (tmp_path / "boot-history.json").read_text(encoding="utf-8")


def test_mixed_history_preserves_aware_records(tmp_path: Path) -> None:
    """A naive record is normalized to UTC and kept alongside aware records."""
    now = datetime.now(UTC)
    aware_recent = (now - timedelta(minutes=1)).isoformat()
    naive_recent = (now - timedelta(minutes=1)).replace(tzinfo=None).isoformat()
    (tmp_path / "RUNNING").write_text("synthetic previous process")
    (tmp_path / "boot-history.json").write_text(json.dumps([aware_recent, naive_recent]))
    g = BootGuard(tmp_path)
    g.on_boot()
    # Both seeded records fall in the window and are counted, plus this boot.
    assert g.unclean_boots == 3


def test_unparseable_record_dropped_but_valid_kept(tmp_path: Path) -> None:
    """One garbage record is dropped; the valid aware records are not reset."""
    now = datetime.now(UTC)
    aware_recent = (now - timedelta(minutes=1)).isoformat()
    (tmp_path / "RUNNING").write_text("synthetic previous process")
    (tmp_path / "boot-history.json").write_text(json.dumps([aware_recent, "not-a-date"]))
    g = BootGuard(tmp_path)
    g.on_boot()
    # The aware record survives and is counted (with this boot); the garbage is dropped.
    assert g.unclean_boots == 2


def test_non_list_history_does_not_poison_count(tmp_path: Path) -> None:
    """A history file that is valid JSON but not a list must not fail-open.

    Regression for the reviewer finding: after the per-record rewrite, only
    ``json.loads`` was inside the parse ``except``, so ``json.loads("null")`` ->
    ``None`` then ``for t in None`` raised ``TypeError`` outside it and tripped
    the outer fail-open before the history was rewritten.
    """
    (tmp_path / "RUNNING").write_text("synthetic previous process")
    (tmp_path / "boot-history.json").write_text("null")
    results = []
    for _ in range(3):
        g = BootGuard(tmp_path)
        g.on_boot()
        results.append((g.unclean_boots, g.skip_recovery))
    assert results == [(1, False), (2, False), (3, True)]
    # The non-list payload must have been replaced by a rewritten list.
    assert json.loads((tmp_path / "boot-history.json").read_text(encoding="utf-8")) is not None


def test_a_record_from_the_future_is_not_evidence(tmp_path: Path) -> None:
    """A clock step or a naive stamp read as UTC must not pin recovery off for good."""
    future = (datetime.now(UTC) + timedelta(hours=3)).isoformat()
    (tmp_path / "boot-history.json").write_text(json.dumps([future, future, future]), encoding="utf-8")
    (tmp_path / "RUNNING").write_text("x", encoding="utf-8")  # an unclean boot
    guard = BootGuard(tmp_path, window_minutes=10, threshold=3)
    guard.on_boot()
    assert guard.unclean_boots == 1 and guard.skip_recovery is False
    assert json.loads((tmp_path / "boot-history.json").read_text(encoding="utf-8")) != [future, future, future]
