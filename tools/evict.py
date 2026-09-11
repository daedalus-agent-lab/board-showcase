#!/usr/bin/env python3
"""Evict shelf objects through the store's own tombstone mechanism.

An object is evicted by (1) writing tombstones/{sha256}.json, (2) dropping its entry from
manifest.json, (3) deleting its copy from the static mirror, (4) rebuilding the search index,
(5) republishing the public catalog. The blob file is left in place: open_blob/get_blob consult
the tombstone first and return 410, so the digest stays recoverable by the operator while the
public catalog stops advertising it.

A digest that has no manifest row but a blob file on disk is a **superseded duplicate** — the shelf
still serves bytes a newer row owns under the same filename. There is no row to drop, so this tool
writes the tombstone and leaves the manifest alone; that is the only way such bytes stop counting
against the object quota.

Step 3 matters: publish_public() copies live blobs into the web root and never removes the copy
of an object that stops being live, so without it the evicted bytes stay served from the mirror
at their old filename while /v1/blobs/{sha} already answers 410 — two surfaces, two answers.

Usage: python3 evict.py DATA_DIR SHA256 [SHA256 ...] [--public-dir DIR] [--reason "..."] [--dry-run]
"""
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shelf_store import ShelfStore, _atomic_write, _filename_from_live


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

    # The mirror copy is removed by publish_public()'s sweep, and the sweep needs to know where the
    # web root is. Without --public-dir the store falls back to the data dir as its "mirror", the
    # sweep runs over nothing, and the tool exits 0 having withdrawn bytes that are still served at
    # their old filename — a success report for a public URL that answers 200. Refuse instead:
    # failing loudly is recoverable, a false "withdrawn" is not.
    if public or not dry:
        if not public:
            print("evict.py: --public-dir is required unless --dry-run is given. Without it the "
                  "mirror copy of an evicted object stays served from the web root while "
                  "/v1/blobs/<sha> answers 410. Pass the web root, e.g. "
                  "--public-dir /var/www/daedalus/board-showcase.", file=sys.stderr)
            return 2
        if Path(public).resolve() == data_dir.resolve():
            print("evict.py: --public-dir must not be the data dir: the mirror sweep would look for "
                  "served copies inside the store that holds the blobs, and would never find one.",
                  file=sys.stderr)
            return 2

    store = ShelfStore(data_dir, Path(public) if public else None)
    man = store.load_manifest()
    before_b, before_n = store.served_totals()
    arts = man.get("artifacts") or []
    index = {a.get("sha256"): a for a in arts if a.get("sha256")}
    doomed = {s for s in targets if s in index}
    missing = [s for s in targets if s not in index]
    # Digests with no manifest row but a blob file on disk: superseded duplicates the shelf still
    # serves, because a newer row owns the filename. They have no row to drop, so only a tombstone
    # can retire them; without one they keep consuming the object quota as bytes no reader can name.
    blob_only = [s for s in missing if (store.blobs / s).is_file()]
    unknown = [s for s in missing if s not in blob_only]
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
    att = store.attributed()
    duplicates = []
    for sha in sorted(blob_only):
        info = att.get(sha) or {}
        ev = info.get("evidence") or []
        m = re.search(r"names filename '([^']+)'", ev[0]) if ev else None
        name = m.group(1) if m else None
        duplicates.append({
            "sha256": sha,
            "filename": name,
            "bytes": (store.blobs / sha).stat().st_size,
            "basis": info.get("basis"),
        })
        print(f"tombstone {sha[:12]} {name or '(name not reconstructed)'} "
              f"{duplicates[-1]['bytes']}B — no live row, superseded duplicate")

    if dry:
        print(f"dry run: would evict {len(evicted)} live row(s), tombstone "
              f"{len(duplicates)} superseded duplicate(s), {len(unknown)} not found")
        return 0

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # A tombstone is the record of *why* and *when* a digest was retired, and the log is the only
    # thing that can answer "was this retired before, and why" after the bytes stop being served.
    # Writing a second tombstone over the first would make that record last-writer-wins: the reason
    # and date of the original eviction are lost, and `lift_tombstone` would then destroy the
    # replacement alone. So an existing tombstone is never rewritten. Correcting a reason is two
    # deliberate steps — lift, then evict again — which is the only way history is ever removed.
    rewritten = []
    pending = [dict(d, _kind="duplicate") for d in duplicates]
    pending += [dict(a, _kind="row",
                     filename=a.get("filename") or _filename_from_live(a.get("live")))
                for a in evicted]
    for d in pending:
        p = store.tombstones / f"{d['sha256']}.json"
        if p.is_file():
            try:
                old = json.loads(p.read_text())
            except Exception:
                old = {}
            rewritten.append(d["sha256"])
            print(f"already retired {d['sha256'][:12]} at {old.get('evicted_at', '?')} "
                  f"(reason: {old.get('reason', '?')!r}); record unchanged. To retire these bytes "
                  f"under a new reason, lift the tombstone first: lift_tombstone.py DATA_DIR "
                  f"{d['sha256']} --reason '...'")
            continue
        tomb = {
            "sha256": d["sha256"],
            "state": "evicted",
            "filename": d["filename"],
            "bytes": d["bytes"],
            "author": d.get("author") or "daedalus-protocore",
            "evicted_at": now,
            "evicted_by": "daedalus-protocore",
            "reason": reason,
        }
        if d.get("accepted_at"):
            tomb["accepted_at"] = d["accepted_at"]
        if d["_kind"] == "duplicate":
            tomb["note"] = ("superseded duplicate: a live row owns this filename; the blob file is "
                            "retained on disk, the tombstone stops it being served and counted"
                            + (f"; attribution basis {d['basis']}" if d.get("basis") else ""))
        _atomic_write(p, json.dumps(tomb, indent=2, sort_keys=True).encode())
    if rewritten:
        print(f"already retired, record unchanged: {len(rewritten)} digest(s)")
    man["artifacts"] = [a for a in arts if a.get("sha256") not in doomed]
    man["manifest_generation"] = int(man.get("manifest_generation") or 0) + 1
    _atomic_write(store.manifest_path, json.dumps(man, indent=2, sort_keys=True).encode())
    store.rebuild_search()
    # publish_public() is now a projection of the catalog: it writes the live copies, the receipts,
    # and sweeps files that no live row claims — so the explicit per-name deletion this tool used to
    # do afterwards is redundant, and the sweep is reported rather than silent.
    swept = store.publish_public()
    after_b, after_n = store.served_totals()
    print(f"served objects {before_n} -> {after_n}; served bytes {before_b} -> {after_b}")
    print(f"tombstones {len(list(store.tombstones.glob('*.json')))}; not found {unknown}")
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
