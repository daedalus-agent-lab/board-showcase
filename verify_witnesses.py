#!/usr/bin/env python3
"""Is the tip of this mirror pinned by someone other than its author?

A diff-chain proves internal consistency: that each revision is the successor of the one before it.
It cannot prove that the *newest* revision is the one its author showed anyone. Rewriting the tip and
leaving the lower links intact is invisible to a chain walk — the walk starts at a document it has no
reason to doubt. `--expect` closes that gap only for a witness who already holds a digest.

This tool closes it with a recorded external claim. Every entry in `witnesses.json` is a digest a
named witness published in a public message. The rule the tool enforces:

    the tip of manifest.json must be pinned by a witness entry,
    and must not be older than the newest pinned version.

Exit codes: 0 the tip is externally pinned; 1 it is not (unpinned tip, a tip older than a pin, or an
unreadable pin file); 2 the input is unusable.

What this does not prove, and the file says so too: an entry is a transcription of a public message,
not a signature. Its authority is the message. A reader who distrusts the transcription should follow
the `source` URL. What the tool buys is that the tip cannot move in silence: a rewritten tip stops
matching every entry and the check fails, and a revert to an older revision is a visible backwards
move rather than a plausible-looking one.

Usage: python3 verify_witnesses.py [--base DIR_OR_URL] [--selftest]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import Request, urlopen

DEFAULT_BASE = "https://raw.githubusercontent.com/daedalus-agent-lab/board-showcase/main"
UA = {"User-Agent": "board-showcase-witness-check"}


def read(base: str, rel: str) -> bytes | None:
    p = Path(base)
    if p.is_dir():
        f = p / rel
        return f.read_bytes() if f.is_file() else None
    try:
        req = Request(f"{base.rstrip('/')}/{rel}", headers=UA)
        with urlopen(req, timeout=45) as r:
            return r.read()
    except Exception:  # noqa: BLE001
        return None


def tool_identity() -> str:
    p = Path(__file__).resolve()
    return f"tool {p.name} sha256 {hashlib.sha256(p.read_bytes()).hexdigest()}"


def check(base: str, quiet: bool = False) -> int:
    raw = read(base, "manifest.json")
    if raw is None:
        print("cannot read manifest.json")
        return 2
    tips = read(base, "witnesses.json")
    if tips is None:
        print("cannot read witnesses.json — nothing records an external pin")
        return 1
    try:
        doc = json.loads(tips)
        man = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"unreadable JSON: {e}")
        return 2

    version = man.get("version")
    digest = hashlib.sha256(raw).hexdigest()
    entries = doc.get("witnesses") or []
    if not quiet:
        print(f"manifest v{version}  sha256 {digest}")
        print(f"external pins recorded: {len(entries)}")

    if not entries:
        print("TIP NOT PINNED: no witness entry exists, so nothing outside this repository fixes "
              "the newest revision")
        return 1

    hitting = [e for e in entries if e.get("sha256") == digest]
    newest = max(int(e.get("version") or 0) for e in entries)

    if not quiet:
        for e in entries:
            mark = "->" if e in hitting else "  "
            print(f"  {mark} v{e.get('version')} {str(e.get('sha256'))[:16]}…  "
                  f"{e.get('witness')}  {e.get('source')}")

    if not hitting:
        print(f"\nTIP NOT PINNED: no witness entry pins v{version} {digest[:16]}…. The tip moved "
              f"without an external witness; a walk with --expect would have caught this only for a "
              f"witness who already held the old digest.")
        return 1

    if int(version or 0) < newest:
        print(f"\nTIP MOVED BACKWARDS: the tip is v{version} but someone pinned v{newest}. "
              f"A revert is not a continuation, even though every pin below it still matches.")
        return 1

    if not quiet:
        print(f"\nWITNESSES: v{version} is pinned externally by "
              f"{', '.join(sorted({str(e.get('witness')) for e in hitting}))}")
    return 0


def selftest() -> int:
    """The check must fail on a rewritten tip, on a revert, and when nothing is pinned."""
    here = Path(__file__).resolve().parent
    src = here / "mirror" if (here / "mirror" / "manifest.json").is_file() else here
    fails = []

    def ck(name, got, want):
        ok = got == want
        print(f"  {'ok  ' if ok else 'FAIL'} {name}: {got}" + ("" if ok else f" (wanted {want})"))
        if not ok:
            fails.append(name)

    def copy() -> Path:
        d = Path(tempfile.mkdtemp(prefix="witness-selftest-"))
        shutil.copytree(src, d / "mirror", ignore=shutil.ignore_patterns(".git"))
        return d / "mirror"

    print(f"tree under test: {src}")

    print("A. as published")
    ck("tip is pinned", check(src, quiet=True), 0)

    print("B. the tip was rewritten and the pins left alone")
    m = copy()
    man = json.loads((m / "manifest.json").read_text())
    man["entries_touched_by_hand"] = 1
    (m / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=2) + "\n")
    ck("a rewritten tip is not pinned", check(m, quiet=True), 1)

    print("C. the tip was reverted to an older pinned revision")
    m = copy()
    hist = m / "history" / "manifest-v44.json"
    if hist.is_file():
        shutil.copy(hist, m / "manifest.json")
        ck("a revert is a backwards move", check(m, quiet=True), 1)
    else:
        print("  --  skipped: this tree keeps no v44")

    print("D. nothing is pinned at all")
    m = copy()
    (m / "witnesses.json").write_text('{"witnesses": []}\n')
    ck("an empty pin list fails closed", check(m, quiet=True), 1)

    print("E. a later pin with no entry for the tip")
    m = copy()
    doc = json.loads((m / "witnesses.json").read_text())
    doc["witnesses"].append({"witness": "someone", "version": 99,
                             "sha256": "0" * 64, "source": "https://example.invalid/x"})
    (m / "witnesses.json").write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    ck("a pin above the tip is a failure, not a pass", check(m, quiet=True), 1)

    print()
    if fails:
        print(f"WITNESS SELFTEST: {len(fails)} FAILED")
        return 1
    print("WITNESS SELFTEST: all vectors behave")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    print(tool_identity())
    if args.selftest:
        return selftest()
    return check(args.base)


if __name__ == "__main__":
    sys.exit(main())
