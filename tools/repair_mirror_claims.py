#!/usr/bin/env python3
"""Clear the mirror URL from rows whose retentions forbid a mirror copy.

Such a row advertised https://158.178.144.114/board-showcase/<name> while publish_public() skipped
the copy, so the URL answered 404 — indistinguishable from a withdrawn object to any reader who
followed it. The generator no longer writes that field; this repairs rows written before the change.

Idempotent: a second run reports zero rows. Writes nothing without --apply.

Usage: python3 repair_mirror_claims.py DATA_DIR [--public-dir DIR] [--apply]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shelf_store import _atomic_write, ShelfStore  # noqa: E402


def take(argv, flag):
    if flag in argv:
        i = argv.index(flag)
        return argv[i + 1]
    return None


def main() -> int:
    argv = sys.argv[1:]
    apply = "--apply" in argv
    argv = [a for a in argv if a != "--apply"]
    public = take(argv, "--public-dir")
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print("usage: repair_mirror_claims.py DATA_DIR [--public-dir DIR] [--apply]", file=sys.stderr)
        return 2

    store = ShelfStore(Path(args[0]), Path(public) if public else None)
    man = store.load_manifest()
    rows = man.get("artifacts") or []
    fixed = []
    for a in rows:
        ret = a.get("retentions") or {}
        if ret.get("mirror_copy_allowed") is False and a.get("mirror"):
            fixed.append((a.get("filename"), a.get("mirror")))
    print(f"rows: {len(rows)}; rows advertising a mirror URL they will not serve: {len(fixed)}")
    for name, url in fixed[:20]:
        print(f"  {name}  {url}")

    if not fixed:
        print("nothing to repair (idempotent)")
        return 0
    if not apply:
        print("dry run: pass --apply to clear the field and republish")
        return 0

    for a in rows:
        ret = a.get("retentions") or {}
        if ret.get("mirror_copy_allowed") is False and a.get("mirror"):
            # Keep an explicit null rather than deleting the key: a reader can then tell "no mirror
            # copy, by retention" from "this row predates the field".
            a["mirror"] = None
            a["mirror_note"] = "no mirror copy: retentions.mirror_copy_allowed is false"
    man["manifest_generation"] = int(man.get("manifest_generation") or 0) + 1
    _atomic_write(store.manifest_path, json.dumps(man, indent=2, sort_keys=True).encode())
    store.rebuild_search()
    store.publish_public()
    print(f"cleared {len(fixed)} mirror URLs; manifest generation -> {man['manifest_generation']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
