#!/usr/bin/env python3
"""Lift a tombstone: let a withdrawn digest be published again.

The intake guard refuses a package whose digest carries a tombstone, and tells the uploader to ask
the operator to lift it. Until this tool existed that advice had no referent: nothing in the repo
deleted a tombstone, so a digest retired by *quota housekeeping* could never be published again even
though the store's own receipts called it served history.

Lifting is deliberately a separate, explicit act:

  * it requires --reason, which is recorded on the tombstone being removed;
  * it prints the tombstone it is about to destroy, so the operator sees what is being forgotten;
  * it removes only the tombstone. It does not touch the manifest, the blob, the mirror or the
    quota, and it does not republish anything — the digest simply stops being refused, and the next
    publication under it behaves like any other.

Usage:
    python3 lift_tombstone.py DATA_DIR SHA256 [SHA256 ...] --reason "..." [--dry-run]

Exit codes: 0 lifted (or would lift), 1 nothing to lift, 2 usage.
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from shelf_store import ShelfStore  # noqa: E402


def take(argv, flag, default=None):
    if flag in argv:
        i = argv.index(flag)
        value = argv[i + 1]
        del argv[i:i + 2]
        return value
    return default


def main() -> int:
    argv = sys.argv[1:]
    dry = "--dry-run" in argv
    argv = [a for a in argv if a != "--dry-run"]
    reason = take(argv, "--reason")
    args = [a for a in argv if not a.startswith("--")]
    if len(args) < 2 or not reason or not reason.strip():
        print("usage: lift_tombstone.py DATA_DIR SHA256 ... --reason \"...\" [--dry-run]",
              file=sys.stderr)
        return 2
    data_dir, targets = Path(args[0]), args[1:]

    store = ShelfStore(data_dir)
    lifted, absent = 0, []
    for sha in targets:
        sha = sha.lower()
        tomb = store.tombstone_of(sha)
        if tomb is None:
            absent.append(sha)
            continue
        print(f"lifting {sha}")
        print(f"  tombstone being destroyed: filename={tomb.get('filename')!r} "
              f"bytes={tomb.get('bytes')} evicted_at={tomb.get('evicted_at')} "
              f"evicted_by={tomb.get('evicted_by')!r} reason={tomb.get('reason')!r}")
        print(f"  lifting reason: {reason.strip()}")
        if not dry:
            (store.tombstones / f"{sha}.json").unlink()
        lifted += 1
    if dry:
        print(f"dry run: would lift {lifted}, {len(absent)} had no tombstone")
        return 0 if lifted else 1
    print(f"lifted {lifted}; {len(absent)} had no tombstone"
          + (f": {', '.join(a[:12] for a in absent)}" if absent else ""))
    # The digest is no longer refused, but nothing was republished: the operator's next publication
    # is the act that makes it live again. Stated in the output so the tool is not mistaken for one.
    print("nothing was republished: the digest is simply no longer refused at intake")
    return 0 if lifted else 1


if __name__ == "__main__":
    sys.exit(main())
