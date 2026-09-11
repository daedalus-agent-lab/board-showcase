#!/usr/bin/env python3
"""Repair catalog rows whose disclosure the row itself cannot support.

Two classes, both of them a row saying something a reader cannot check:

1. A row whose retentions forbid a mirror copy but which still advertises a mirror URL. The URL
   answers 404, which is indistinguishable from a withdrawn object to anyone who followed it. The
   generator no longer writes that field; this repairs rows written before the change.
2. A row with no digest, no bytes and no external URL whose note nevertheless says how to verify it
   ("verify by live URL only") — a pointer that resolves to nothing, which is worse than silence
   because it reads like evidence. Repaired to say what the row actually carries.

Both passes are idempotent: a second run reports nothing. Writes nothing without --apply.

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


def unsupported_notes(rows: list[dict]) -> list[tuple[str, str]]:
    """(name, note) for rows whose note promises a check the row cannot support.

    The bar for calling it unsupported: no bytes to hash, no URL to fetch, and a note that is not
    already the honest one. The first version of this looked for the words "url"/"verify" in the
    note — and the repaired note ("no digest, no bytes, no URL") contains both, so the second run
    re-repaired it. Words are the wrong test; the row's shape is the test, and `note_repaired` makes
    the pass idempotent by construction.
    """
    out = []
    for a in rows:
        note = str(a.get("note") or "")
        if a.get("bytes") is not None or a.get("live") or not note:
            continue
        if a.get("note_repaired") or note == honest_note(a):
            continue
        if "url" in note.lower() or "verify" in note.lower() or "check" in note.lower():
            out.append((a.get("name") or a.get("filename") or "row", note))
    return out


def honest_note(row: dict) -> str:
    """What this row can actually be checked by, in the row's own terms."""
    carries = []
    if row.get("sha256_declared") or row.get("bytes_declared"):
        carries.append("declared digest and size, asserted by the row's author and not witnessed "
                       "by this shelf")
    prov = row.get("provenance") or {}
    if prov:
        where = ", ".join(f"{k}={v}" for k, v in sorted(prov.items())[:2])
        carries.append(f"provenance to check the claim at its source ({where})")
    if not carries:
        return ("Nothing on this shelf verifies this row: no digest, no bytes, no URL, and no "
                "provenance. It records that a claim was made, not that anything is served.")
    return ("Not content-addressed: no digest or bytes this shelf can hash. This row carries "
            + "; ".join(carries) + ".")



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
    # Pass 1: mirror URLs a retention forbids.
    fixed = []
    for a in rows:
        ret = a.get("retentions") or {}
        if ret.get("mirror_copy_allowed") is False and a.get("mirror"):
            fixed.append((a.get("filename"), a.get("mirror")))
    print(f"rows: {len(rows)}; rows advertising a mirror URL they will not serve: {len(fixed)}")
    for name, url in fixed[:20]:
        print(f"  {name}  {url}")

    # Pass 2: notes promising a check the row cannot support.
    unsupported = unsupported_notes(rows)
    print(f"rows whose note promises evidence the row does not carry: {len(unsupported)}")
    for name, note in unsupported[:20]:
        print(f"  {name!r}  {note[:90]}")

    if not fixed and not unsupported:
        print("nothing to repair (idempotent)")
        return 0
    if not apply:
        print("dry run: pass --apply to repair and republish")
        return 0

    for a in rows:
        ret = a.get("retentions") or {}
        if ret.get("mirror_copy_allowed") is False and a.get("mirror"):
            # Keep an explicit null rather than deleting the key: a reader can then tell "no mirror
            # copy, by retention" from "this row predates the field".
            a["mirror"] = None
            a["mirror_note"] = "no mirror copy: retentions.mirror_copy_allowed is false"
    for a in rows:
        if any(a.get("name") == n or a.get("filename") == n for n, _ in unsupported):
            before = a.get("note")
            a["note"] = honest_note(a)
            a["note_repaired"] = {"was": before,
                                  "why": ("the previous note promised a check this row cannot "
                                          "support (no digest, no bytes, no URL); the claim is "
                                          "kept, the unbacked instruction removed")}
    man["manifest_generation"] = int(man.get("manifest_generation") or 0) + 1
    _atomic_write(store.manifest_path, json.dumps(man, indent=2, sort_keys=True).encode())
    store.rebuild_search()
    store.publish_public()
    print(f"cleared {len(fixed)} mirror URLs, repaired {len(unsupported)} note(s); "
          f"manifest generation -> {man['manifest_generation']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
