#!/usr/bin/env python3
"""Check that a mirror actually serves the bytes its manifest claims.

For every entry that names a file and a digest this fetches the file and recomputes the digest. It
reports, separately and by name: files that are missing, files whose bytes are not the ones the
manifest declares, entries with no digest (not content-addressed by design), entries whose author
forbade a mirror copy, and entries that are no longer live on the host but are kept here on purpose.

Exit 0 only if every content-addressed entry it can check matches. A missing or mismatched file is a
failure, never a note.

Usage: python3 verify_mirror.py [--base URL_OR_DIR] [--limit N]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

DEFAULT_BASE = "https://daedalus-agent-lab.github.io/board-showcase"
UA = {"User-Agent": "board-showcase-mirror-verifier"}


def fetch(base: str, name: str) -> bytes | None:
    if base.startswith("http"):
        url = f"{base.rstrip('/')}/{name}"
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except Exception:
            return None
    p = Path(base) / name
    return p.read_bytes() if p.is_file() else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--limit", type=int, default=0, help="check at most N content-addressed entries")
    args = ap.parse_args()

    raw = fetch(args.base, "manifest.json")
    if raw is None:
        print(f"MIRROR: FAILED — no manifest.json at {args.base}")
        return 2
    man = json.loads(raw)
    print(f"manifest v{man.get('version')}  sha256 {hashlib.sha256(raw).hexdigest()[:16]}…")
    print(f"  chain_start v{man.get('chain_start')}  entries {len(man.get('artifacts') or [])}")

    ok = 0
    missing: list[str] = []
    mismatched: list[str] = []
    link_only, forbidden, kept = [], [], []
    problems: list[str] = []
    checked = 0
    for a in man.get("artifacts") or []:
        digest, fname = a.get("sha256"), a.get("filename")
        if not digest or a.get("bytes") is None:
            link_only.append(a.get("name"))
            continue
        if (a.get("retentions") or {}).get("mirror_copy_allowed") is False:
            forbidden.append(fname)
            continue
        if a.get("live_on_host") is False:
            kept.append(fname)
        if not fname:
            problems.append(f"{digest[:16]}… has a digest but no filename to fetch")
            continue
        if args.limit and checked >= args.limit:
            continue
        body = fetch(args.base, fname)
        checked += 1
        if body is None:
            missing.append(fname)
            problems.append(f"missing: {fname} (declared {digest[:16]}…)")
            continue
        actual = hashlib.sha256(body).hexdigest()
        if actual != digest:
            mismatched.append(fname)
            problems.append(f"mismatch: {fname} is {actual[:16]}… but the manifest declares "
                            f"{digest[:16]}…")
        else:
            ok += 1

    print(f"  content-addressed entries fetched and matching: {ok}")
    print(f"  not content-addressed by design (no digest): {len(link_only)}")
    print(f"  mirror copy forbidden by the author: {len(forbidden)}")
    print(f"  kept here although no longer live on the host: {len(kept)}"
          + (f" — {', '.join(kept[:6])}{' …' if len(kept) > 6 else ''}" if kept else ""))
    print(f"  missing: {len(missing)}   mismatched: {len(mismatched)}")
    for p in problems[:10]:
        print(f"    {p}")
    print()
    if problems:
        print(f"MIRROR: {len(problems)} FAILED")
        return 1
    print("MIRROR: every content-addressed entry it can check is served byte-for-byte")
    return 0


if __name__ == "__main__":
    sys.exit(main())
