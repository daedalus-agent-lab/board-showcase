#!/usr/bin/env python3
"""Import already-hosted files from the public dir into the shelf blob store.

Does not create operations (those artifacts predate the API). Safe to re-run.

The mirror's manifest is a *stale* view by construction: it is written by publish_public() and can
disagree with the store after an eviction whose sweep has not run yet. Merging it back row by row
would therefore put a live row back for a digest that has been withdrawn — the exact divergence the
intake guard exists to prevent, arriving through a different door. Rows whose digest carries a
tombstone are skipped, with the tombstone's reason printed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from shelf_lib import sha256_hex  # noqa: E402
from shelf_store import ShelfStore, _atomic_write  # noqa: E402

DEFAULT_DATA = Path("/var/lib/daedalus-shelf")
DEFAULT_PUBLIC = Path("/var/www/daedalus/board-showcase")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default=str(DEFAULT_DATA))
    ap.add_argument("--public-dir", default=str(DEFAULT_PUBLIC))
    args = ap.parse_args()
    data = Path(args.data_dir)
    public = Path(args.public_dir)
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
    withheld = 0
    for a in man.get("artifacts") or []:
        digest = a.get("sha256")
        live = a.get("live") or ""
        name = live.rstrip("/").split("/")[-1] if live else a.get("filename")
        if not digest or not name:
            if a.get("name") and not any(x.get("name") == a.get("name") for x in merged):
                merged.append(a)
            continue
        # Withdrawn is withdrawn, whoever asks: the mirror manifest is not the authority on
        # liveness, the tombstone is.
        tomb = store.tombstone_of(str(digest).lower())
        if tomb is not None:
            withheld += 1
            print(f"WITHHELD {name} {str(digest)[:12]} — tombstoned: {tomb.get('reason', 'no reason')}")
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
