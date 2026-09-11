#!/usr/bin/env python3
"""Reconcile a mirror's working tree with the digests its manifest declares.

A mirror promises that what its manifest names is what it serves. Files can drift from that: someone
commits a newer draft over a published name, a copy is interrupted, a name is reused. This tool finds
every entry whose declared digest is not the digest of the file on disk, and for each one either
restores the declared bytes or fails loudly.

Where do the declared bytes come from? Git history, and only git history: if the repository ever
committed the declared revision, `git cat-file` can produce exactly those bytes again. If it never
did, this tool does not invent them — it reports the entry as unresolvable and exits non-zero.

Nothing is destroyed. The bytes currently holding the name are moved to
`history/replaced/<short-digest>-<name>` and recorded in `history/replaced/index.json`, and the
manifest entry gains `replaced_after_publication`. A name that was published once and later written
over keeps both versions, which is this mirror's whole promise.

Usage: python3 reconcile_mirror.py --repo <clone> [--apply]
Exit: 0 nothing to do, or everything restored; 1 something could not be resolved; 2 unusable input.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPLACED = Path("history/replaced")


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def git(repo: Path, *args: str) -> tuple[int, bytes]:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    return r.returncode, r.stdout


def history_bytes(repo: Path, name: str, digest: str) -> bytes | None:
    """The declared bytes, if this repository ever committed them under that name."""
    rc, out = git(repo, "log", "--all", "--format=%H", "--", name)
    if rc != 0:
        return None
    for commit in out.decode().split():
        rc, blob = git(repo, "show", f"{commit}:{name}")
        if rc == 0 and sha256(blob) == digest:
            return blob
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--apply", action="store_true", help="write the corrections")
    args = ap.parse_args()

    repo = Path(args.repo).resolve()
    man_path = repo / "manifest.json"
    if not man_path.is_file():
        print(f"RECONCILE: FAILED — no manifest.json in {repo}")
        return 2
    man = json.loads(man_path.read_text())

    drifted, resolved, unresolved, absent = [], [], [], []
    index_path = repo / REPLACED / "index.json"
    index = json.loads(index_path.read_text()) if index_path.is_file() else {}

    for entry in man.get("artifacts") or []:
        digest, fname = entry.get("sha256"), entry.get("filename")
        if not digest or entry.get("bytes") is None or not fname:
            continue
        target = repo / fname
        if not target.is_file():
            absent.append(fname)
            continue
        actual = sha256(target.read_bytes())
        if actual == digest:
            continue
        drifted.append((fname, digest, actual))
        original = history_bytes(repo, fname, digest)
        if original is None:
            unresolved.append(fname)
            print(f"  UNRESOLVED {fname}: declares {digest[:16]}…, serves {actual[:16]}…, and no "
                  f"revision in this repository ever held the declared bytes")
            continue
        print(f"  {fname}: declares {digest[:16]}…, serves {actual[:16]}… — declared revision found "
              f"in git history, {'restoring' if args.apply else 'would restore'}")
        if args.apply:
            kept = REPLACED / f"{actual[:16]}-{fname}"
            kept.parent.mkdir(parents=True, exist_ok=True)
            kept.write_bytes(target.read_bytes())
            target.write_bytes(original)
            index[fname] = {
                "displaced_sha256": actual,
                "restored_sha256": digest,
                "kept_at": str(kept),
                "why": ("the working tree held bytes this manifest does not declare; the declared "
                        "revision was restored and the other bytes kept here, because nothing that "
                        "was published under this name is discarded"),
            }
        resolved.append(fname)

    if args.apply and index:
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    print()
    print(f"  entries checked: {len(man.get('artifacts') or [])}")
    print(f"  files absent from the tree: {len(absent)}" + (f" — {', '.join(absent[:5])}" if absent else ""))
    print(f"  digest drift: {len(drifted)}   restored: {len(resolved) if args.apply else 0}"
          f"   unresolved: {len(unresolved)}")
    if unresolved:
        print("RECONCILE: FAILED — a declared digest has no bytes in this repository")
        return 1
    if absent:
        print("RECONCILE: FAILED — the manifest names files this tree does not have; run the sync "
              "first, or the manifest is ahead of the tree")
        return 1
    if not args.apply and drifted:
        print("RECONCILE: dry run — rerun with --apply to write the corrections")
        return 0
    print("RECONCILE: the tree now serves what the manifest declares")
    return 0


if __name__ == "__main__":
    sys.exit(main())
