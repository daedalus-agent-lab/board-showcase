#!/usr/bin/env python3
"""Evict shelf objects through the store's own tombstone mechanism.

An object is evicted by (1) writing tombstones/{sha256}.json, (2) dropping its entry from
manifest.json, (3) deleting its copy from the static mirror, (4) rebuilding the search index,
(5) republishing the public catalog. The blob file is left in place: open_blob/get_blob consult
the tombstone first and return 410, so the digest stays recoverable by the operator while the
public catalog stops advertising it.

Step 3 matters: publish_public() copies live blobs into the web root and never removes the copy
of an object that stops being live, so without it the evicted bytes stay served from the mirror
at their old filename while /v1/blobs/{sha} already answers 410 — two surfaces, two answers.

Usage: python3 evict.py DATA_DIR SHA256 [SHA256 ...] [--public-dir DIR] [--reason "..."] [--dry-run]
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shelf_store import _atomic_write, _filename_from_live, ShelfStore  # noqa: E402


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
    reason = take(argv, "--reason", "operator eviction of own filler probe")
    public = take(argv, "--public-dir")
    args = [a for a in argv if not a.startswith("--")]
    # A data dir with no digests is a usage error, not a successful no-op: an eviction that
    # silently does nothing reads like a completed cleanup.
    if len(args) < 2:
        print("usage: evict.py DATA_DIR SHA256 ... [--public-dir DIR] [--reason R] [--dry-run]",
              file=sys.stderr)
        return 2
    data_dir, targets = Path(args[0]), args[1:]

    store = ShelfStore(data_dir, Path(public) if public else None)
    man = store.load_manifest()
    before_b, before_n = store.live_totals()
    arts = man.get("artifacts") or []
    index = {a.get("sha256"): a for a in arts if a.get("sha256")}
    doomed = {s for s in targets if s in index}
    missing = [s for s in targets if s not in index]
    # A filename is only removed from the mirror when no surviving live artifact uses it.
    still_used = {a.get("filename") or _filename_from_live(a.get("live"))
                  for a in arts if a.get("sha256") not in doomed}

    evicted, mirror_expected, mirror_kept = [], [], []
    for sha in sorted(doomed):
        a = index[sha]
        name = a.get("filename") or _filename_from_live(a.get("live"))
        evicted.append(a)
        if not name:
            continue
        if name in still_used:
            mirror_kept.append(name)
        else:
            mirror_expected.append(name)
        print(f"evict {sha[:12]} {name} {a.get('bytes')}B")

    # This tool no longer deletes mirror files: publish_public() is a projection, and a second
    # deleter is the second write surface that reappears as drift the first time one of the two
    # paths is skipped. What this tool must do is state what it expects and then report what
    # actually happened — an earlier version printed "remove mirror" while removing nothing.
    if mirror_expected and not dry:
        print(f"withdrawn by the projection sweep: {' '.join(mirror_expected)}")
    print(f"mirror kept (filename still live elsewhere): {mirror_kept}")
    orphans = store.orphan_totals()
    if orphans[1]:
        # Bytes the shelf serves with no receipt at all: no live row, no tombstone, no supersedes
        # entry. Naming them here is the point — an eviction decision made on `live objects` alone
        # is made on an understated number, and an object whose digest nothing explains is the one
        # that is genuinely unreachable later.
        print(f"served with no receipt {orphans[1]} blob(s), {orphans[0]} bytes "
              f"(no live row, no tombstone, no supersedes entry)")
    sup = store.superseded_totals()
    if sup[1]:
        print(f"served with a replacement receipt {sup[1]} blob(s), {sup[0]} bytes "
              f"(superseded, counted: reachable history rather than residue)")
    if dry:
        print(f"dry run: would evict {len(evicted)}, {len(missing)} not found")
        return 0

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for a in evicted:
        tomb = {
            "sha256": a["sha256"],
            "state": "evicted",
            "filename": a.get("filename") or _filename_from_live(a.get("live")),
            "bytes": a.get("bytes"),
            "author": a.get("author"),
            "accepted_at": a.get("accepted_at"),
            "evicted_at": now,
            "evicted_by": "daedalus-protocore",
            "reason": reason,
        }
        _atomic_write(store.tombstones / f"{a['sha256']}.json",
                      json.dumps(tomb, indent=2, sort_keys=True).encode())
    man["artifacts"] = [a for a in arts if a.get("sha256") not in doomed]
    man["manifest_generation"] = int(man.get("manifest_generation") or 0) + 1
    _atomic_write(store.manifest_path, json.dumps(man, indent=2, sort_keys=True).encode())
    store.rebuild_search()
    # publish_public() is now a projection of the catalog: it writes the live copies, the receipts,
    # and sweeps files that no live row claims — so the explicit per-name deletion this tool used to
    # do afterwards is redundant, and the sweep is reported rather than silent.
    swept = store.publish_public()
    after_b, after_n = store.live_totals()
    print(f"live objects {before_n} -> {after_n}; live bytes {before_b} -> {after_b}")
    print(f"tombstones {len(list(store.tombstones.glob('*.json')))}; not found {missing}")
    # The expectation is checked against the result, so an eviction whose copy stays served says so
    # here instead of leaving the mirror and the catalog disagreeing in silence.
    missed = [n for n in mirror_expected if (store.public_dir / n).is_file()] if store.public_dir else []
    if swept:
        print(f"mirror swept {len(swept)} stale file(s): {' '.join(swept[:5])}"
              + (" …" if len(swept) > 5 else ""))
    if missed:
        print(f"STILL SERVED after eviction: {' '.join(missed)} — the copy outlived the catalog row")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
