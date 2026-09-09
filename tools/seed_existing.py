#!/usr/bin/env python3
"""Import already-hosted files from the public dir into the shelf blob store.

Does not create operations (those artifacts predate the API). Safe to re-run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from shelf_lib import sha256_hex  # noqa: E402
from shelf_store import ShelfStore, _atomic_write  # noqa: E402


def main() -> int:
    data = Path("/var/lib/daedalus-shelf")
    public = Path("/var/www/daedalus/board-showcase")
    store = ShelfStore(data, public)
    man_src = public / "manifest.json"
    if not man_src.is_file():
        print("no public manifest")
        return 1
    man = json.loads(man_src.read_text(encoding="utf-8"))
    # Prefer existing data-dir manifest if it already has API-accepted rows.
    existing = store.load_manifest()
    by_sha = {a.get("sha256"): a for a in existing.get("artifacts") or [] if a.get("sha256")}
    merged = list(existing.get("artifacts") or [])
    for a in man.get("artifacts") or []:
        digest = a.get("sha256")
        live = a.get("live") or ""
        name = live.rstrip("/").split("/")[-1] if live else a.get("filename")
        if not digest or not name:
            if a.get("name") and not any(x.get("name") == a.get("name") for x in merged):
                merged.append(a)
            continue
        src = public / name
        if src.is_file():
            raw = src.read_bytes()
            h = sha256_hex(raw)
            if h != digest:
                print(f"SKIP hash mismatch {name}")
                continue
            _atomic_write(store.blobs / digest, raw)
            a = dict(a)
            a["filename"] = name
            a["bytes"] = len(raw)
        if digest in by_sha:
            continue
        merged.append(a)
        by_sha[digest] = a
        print(f"seeded {name} {digest[:12]} {a.get('bytes')}")
    existing["artifacts"] = merged
    if man.get("already_hosted") and not existing.get("already_hosted"):
        existing["already_hosted"] = man["already_hosted"]
    store.manifest_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2) + "\n")
    store.rebuild_search()
    # Do not overwrite public files that already match; still refresh search/manifest from store
    # without clobbering index.html / ACCEPT.md.
    _atomic_write(public / "search.json", store.search_path.read_bytes())
    print("search rebuilt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
