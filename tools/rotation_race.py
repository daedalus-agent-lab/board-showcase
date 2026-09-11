#!/usr/bin/env python3
"""Does a reader ever see a partial file while the publisher rotates it?

The claim under test: `_atomic_write` (tempfile in the same directory, fsync, os.replace) means a
reader sees either the old bytes or the new ones, never a mixture. A test that only ever observes
whole files proves nothing, so this runs the same measurement against a deliberately non-atomic
writer and reports both. If the control does not tear, the experiment is not measuring anything.

Also measures removal: on POSIX an unlinked file whose descriptor a reader already holds keeps
serving its bytes, so a sweep cannot truncate an in-flight download.
"""
from __future__ import annotations

import hashlib

import sys
import tempfile
import threading

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shelf_store import _atomic_write  # noqa: E402

A = b"A" * 200_000
B = b"B" * 200_000
DIGESTS = {hashlib.sha256(A).hexdigest()[:12], hashlib.sha256(B).hexdigest()[:12]}


def race(writer, rounds: int) -> dict:
    """Rewrite one path `rounds` times while a reader hammers it. Returns what the reader saw."""
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "artifact.bin"
    writer(path, A)
    stop = threading.Event()
    seen: dict[str, int] = {}
    bad: list[str] = []

    def reader() -> None:
        while not stop.is_set():
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                continue  # between unlink and replace is not a tear, it is an absence
            d = hashlib.sha256(data).hexdigest()[:12]
            seen[d] = seen.get(d, 0) + 1
            if d not in DIGESTS:
                bad.append(f"{len(data)} bytes, digest {d}")

    t = threading.Thread(target=reader)
    t.start()
    for i in range(rounds):
        writer(path, A if i % 2 else B)
    stop.set()
    t.join()
    tmp.cleanup()
    return {"rounds": rounds, "reads": sum(seen.values()), "whole": seen, "torn": len(bad),
            "example": bad[:2]}


def atomic(path: Path, data: bytes) -> None:
    _atomic_write(path, data)


def in_place(path: Path, data: bytes) -> None:
    """The control: truncate and write in chunks, the way a naive publisher does."""
    with open(path, "wb") as f:
        for i in range(0, len(data), 8192):
            f.write(data[i:i + 8192])
            f.flush()


def unlink_with_open_reader() -> dict:
    """A sweep during an in-flight read: does the reader still get the whole object?"""
    tmp = tempfile.TemporaryDirectory()
    path = Path(tmp.name) / "artifact.bin"
    path.write_bytes(A)
    with open(path, "rb") as f:
        first = f.read(1024)
        path.unlink()  # the sweep
        rest = f.read()
    tmp.cleanup()
    whole = hashlib.sha256(first + rest).hexdigest()[:12]
    return {"served_after_unlink": len(first) + len(rest), "digest_matches": whole in DIGESTS,
            "path_gone": not path.exists()}


def main() -> int:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    print("atomic write (tmp + fsync + os.replace):")
    r1 = race(atomic, rounds)
    print(f"  {r1['reads']} concurrent reads over {r1['rounds']} rotations, "
          f"distinct whole contents seen: {sorted(r1['whole'])}, torn: {r1['torn']}")

    print("control, same measurement, writer writes in place:")
    r2 = race(in_place, rounds)
    print(f"  {r2['reads']} concurrent reads over {r2['rounds']} rotations, "
          f"distinct contents seen: {len(r2['whole'])}, torn: {r2['torn']} {r2['example']}")

    print("unlink during an in-flight read:")
    print(" ", unlink_with_open_reader())

    ok = r1["torn"] == 0
    discriminating = r2["torn"] > 0
    print(f"\nno tear through the publisher: {'yes' if ok else 'NO'}; "
          f"the measurement can detect a tear: {'yes' if discriminating else 'NO — test is vacuous'}")
    return 0 if (ok and discriminating) else 1


if __name__ == "__main__":
    sys.exit(main())
