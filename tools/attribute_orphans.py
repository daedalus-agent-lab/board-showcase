#!/usr/bin/env python3
"""Attribute served-but-unexplained blobs, and say what the attribution rests on.

A blob is an orphan when it is served and nothing on the shelf says why: no live row, no tombstone,
no supersedes entry. Some of them are older replacements from before the write path recorded
`supersedes` at all — the displacement happened, the receipt was never written. This tool
reconstructs those, and it does so into a *different* field (`attributed.json` -> `predecessors`),
never into `supersedes`, because a reconstruction and a witnessed receipt are different claims and
must stay distinguishable to whatever reads them later.

The evidence it accepts, all of it on disk:
  - an operation receipt naming this digest and a filename;
  - exactly one live row owning that filename (the store keeps one row per name);
  - no tombstone under that filename (a tombstone would mean the name was freed and possibly reused,
    which breaks the chain);
  - the blob's bytes hash to the digest the filename is made of.

Usage:
  attribute_orphans.py [--data DIR] [--check] [--write]

Without --write it changes nothing and prints what it would write. Exit 1 if a candidate fails an
evidence check, so a scheduled run fails loudly rather than attributing quietly.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

BASIS = "filename-and-acceptance-receipt"


def _read_json(p: Path) -> Any:
    return json.loads(p.read_text())


def candidates(data: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """(attributable entries, refusal reasons). A refusal names the digest and the failed check."""
    man = _read_json(data / "manifest.json")
    rows = man.get("artifacts") or []
    live = [r for r in rows if r.get("sha256") and r.get("bytes") is not None]
    owners: dict[str, list[str]] = {}
    for r in live:
        owners.setdefault(r["filename"], []).append(r["sha256"])
    witnessed = {r.get("supersedes", {}).get("sha256") for r in rows if r.get("supersedes")}
    tombed = {p.stem for p in (data / "tombstones").glob("*.json")}
    # The evidence line every entry carries says "no tombstone under that FILENAME", and the reason
    # given is that a freed name could have been reused by an unrelated object — which breaks the
    # chain the pointer asserts. That is a claim about names, and it needs the tombstones read by
    # filename: `tombed` above holds digests, so testing a blob's own digest against it answers a
    # different question and lets the false case through.
    tombed_names: set[str] = set()
    for p in (data / "tombstones").glob("*.json"):
        try:
            name = _read_json(p).get("filename")
        except Exception:  # noqa: BLE001
            continue
        if name:
            tombed_names.add(str(name))
    known_live = {r.get("sha256") for r in live}

    receipt_filename: dict[str, set[str]] = {}
    receipt_ids: dict[str, list[str]] = {}
    for p in sorted((data / "operations").glob("*.json")):
        try:
            o = _read_json(p)
        except Exception:  # noqa: BLE001
            continue
        h = o.get("sha256")
        if not h:
            continue
        if o.get("filename"):
            receipt_filename.setdefault(h, set()).add(o["filename"])
        receipt_ids.setdefault(h, []).append(p.stem)

    entries, refusals = [], []
    for blob in sorted((data / "blobs").glob("*")):
        d = blob.name
        if not blob.is_file() or d in known_live or d in tombed or d in witnessed:
            continue
        names = receipt_filename.get(d) or set()
        if not names:
            refusals.append(f"{d[:12]}: no acceptance receipt names a filename for this digest")
            continue
        if hashlib.sha256(blob.read_bytes()).hexdigest() != d:
            refusals.append(f"{d[:12]}: served bytes do not hash to the name they are stored under")
            continue
        hit = [(n, owners[n]) for n in sorted(names) if owners.get(n)]
        if not hit:
            refusals.append(f"{d[:12]}: receipt names {sorted(names)}, no live row owns it")
            continue
        if len(hit) > 1:
            refusals.append(f"{d[:12]}: receipt names {[n for n, _ in hit]}, more than one is live")
            continue
        name, owner = hit[0]
        if len(owner) != 1:
            refusals.append(f"{d[:12]}: filename {name!r} has {len(owner)} live owners")
            continue
        if name in tombed_names:
            refusals.append(
                f"{d[:12]}: {name!r} was withdrawn at least once, so the live row owning it now may "
                f"be an unrelated object that took the freed name — the chain is not reconstructible")
            continue
        receipt_names = ", ".join(sorted(receipt_ids[d])[:3])
        entries.append({
            "sha256": d,
            "superseded_by": owner[0],
            "filename": name,
            "basis": BASIS,
            "evidence": [
                f"operation receipt {receipt_names} names filename {name!r} for {d[:12]}",
                f"live row {owner[0][:12]} owns {name!r} ({len(live)} named rows, "
                f"{len(owners)} distinct filenames)",
                f"no tombstone under {name!r} (a freed name could have been reused for another "
                f"object, which would break this chain)",
            ],
            "witnessed": False,
            "limitation": (
                "the receipt does not prove the row was ever live long enough to be seen: accept() "
                "writes the receipt before the manifest, so a crash in that window leaves a receipt "
                "for an artifact that never served. This is why the entry is not written as "
                "supersedes."),
        })
    # Several unexplained blobs can have held the same name at different times. The pointer each one
    # gets names the CURRENT owner of that name, which is not necessarily its immediate successor:
    # if A and B both held `x.md` and C owns it now, B was probably displaced by C and A by B. Say
    # so, rather than letting one pointer imply a chain nobody recorded.
    shared: dict[str, int] = {}
    for e in entries:
        shared[e["filename"]] = shared.get(e["filename"], 0) + 1
    for e in entries:
        if shared[e["filename"]] > 1:
            e["chain_ambiguous"] = True
            e["evidence"].append(
                f"{shared[e['filename']]} unexplained blob(s) held {e['filename']!r}; this pointer "
                f"names the current owner, not necessarily the immediate successor")
    return entries, refusals


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="/var/lib/daedalus-shelf")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    data = Path(a.data)

    entries, refusals = candidates(data)
    total = sum((data / "blobs" / e["sha256"]).stat().st_size for e in entries)
    for e in entries:
        print(f"attributable {e['sha256'][:12]}  {e['filename']!r} -> {e['superseded_by'][:12]}  "
              f"basis={e['basis']}")
    for r in refusals:
        print(f"refused      {r}")
    print(f"{len(entries)} attributable blob(s), {total} bytes; {len(refusals)} refused")

    if refusals:
        print("refusing to write: a candidate failed an evidence check", file=sys.stderr)
        return 1
    if not entries:
        print("nothing to attribute")
        return 0

    doc = {
        "note": ("Reconstructed predecessors. These are NOT witnessed replacements: the write path "
                 "never recorded them, they are inferred from receipts and the current catalog. Read "
                 "`limitation` before treating one as fact."),
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "entries": {e["sha256"]: e for e in entries},
    }
    if not a.write:
        print(json.dumps(doc, indent=2)[:1200] + " …")
        print("(dry run; pass --write to record)")
        return 0
    out = data / "attributed.json"
    out.write_text(json.dumps(doc, indent=2, sort_keys=True))
    print(f"wrote {out} with {len(entries)} entries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
