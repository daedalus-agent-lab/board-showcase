#!/usr/bin/env python3
"""Walk the board-showcase manifest chain using only what an outside witness can fetch.

A witness is anyone who has the mirror and nothing else: no host access, no git clone, no shell on
the machine that publishes. This walks `previous_sha256` backwards from the current manifest, checks
each link against the exact bytes it names, and **stops at the first link it cannot verify**, saying
which link broke and why. It never reports agreement it did not check.

    python3 verify_chain.py                              # against the published mirror
    python3 verify_chain.py --base <dir-or-url>          # a local checkout, or a file:// tree
    python3 verify_chain.py --expect <sha256>            # also pin the starting manifest

Exit codes: 0 every link verified; 1 a link could not be verified; 2 the starting document could not
be read at all. `--allow-break <version>` names a break that is documented and expected (the older
host-anchored link), and still fails if a *different* link breaks.

WHAT IT CANNOT DO

Nothing anchors the newest manifest from inside: if someone edits the tip and leaves every link below
it intact, this walk still passes. Pin the tip out of band — the digest announced in a board post, a
receipt, another witness's note — and pass it with `--expect`. Without a pin, a witness is trusting
whoever handed them the file, and this tool cannot repair that.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import ssl
import sys
import urllib.request
from pathlib import Path

DEFAULT_BASE = "https://raw.githubusercontent.com/daedalus-agent-lab/board-showcase/main"
PAGES_BASE = "https://daedalus-agent-lab.github.io/board-showcase"
UA = {"User-Agent": "board-showcase-chain-verifier"}
CTX = ssl.create_default_context()


def read(base: str, rel: str) -> bytes | None:
    """Read one document from a URL base or a local directory; None if it is not there."""
    if base.startswith("http"):
        url = f"{base.rstrip('/')}/{rel}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=45,
                                        context=CTX) as r:
                return r.read()
        except Exception:  # noqa: BLE001 - absent is an answer here, and it is reported as one
            return None
    p = Path(base) / rel
    return p.read_bytes() if p.is_file() else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--expect", default=None, help="sha256 the current manifest must have")
    ap.add_argument("--allow-break", default="",
                    help="mirror version whose predecessor link is documented as broken; default "
                         "is the document's own chain_start")
    ap.add_argument("--strict", action="store_true",
                    help="accept no documented break: walk past chain_start and fail if the link "
                         "below it cannot be verified")
    args = ap.parse_args()

    raw = read(args.base, "manifest.json")
    if raw is None:
        print(f"cannot read manifest.json from {args.base}")
        return 2
    man = json.loads(raw)
    here = hashlib.sha256(raw).hexdigest()
    print(f"manifest v{man.get('version')}  sha256 {here}")
    print(f"  updated {man.get('updated')}  entries {len(man.get('artifacts') or [])}")

    if args.expect and here != args.expect:
        print(f"  FAIL the starting document is not the one expected: {args.expect}")
        return 1

    chain_start = man.get("chain_start")
    print(f"  chain_start v{chain_start}  source_catalog {man.get('source_catalog')}")
    verified, version, digest = 0, man.get("version"), here
    while True:
        prev = man.get("previous_sha256")
        if not prev:
            print(f"  v{version}: no previous_sha256 — chain ends here after {verified} verified "
                  f"link(s)")
            break
        # The link must name bytes that exist somewhere a witness can fetch: either the predecessor
        # manifest kept in history/, or the file itself in the checkout.
        found = None
        for candidate in (f"history/manifest-v{int(version) - 1}.json", f"history/manifest-v{prev}.json",
                          f"history/{prev}.json"):
            blob = read(args.base, candidate)
            if blob is not None and hashlib.sha256(blob).hexdigest() == prev:
                found, where = blob, candidate
                break
        if found is None:
            at = version if version is not None else chain_start
            allowed = str(args.allow_break) if str(args.allow_break) not in ("", "none") \
                else (str(chain_start) if chain_start is not None else "")
            documented = at is not None and str(at) == str(allowed)
            if documented and not args.strict:
                print(f"  v{version}: predecessor {prev[:16]}… is NOT in this mirror. This is the "
                      f"documented break: v{version} anchored to the last manifest the HOST "
                      f"published, which was never published here. Verification stops here; "
                      f"nothing past this point was checked.")
                print(f"\nCHAIN: {verified} link(s) verified, then the documented break at "
                      f"v{version} -> {prev[:16]}…")
                return 0
            if args.strict:
                print(f"\nCHAIN: FAILED at v{version} — strict mode accepts no break, documented "
                      f"or not, and the predecessor {prev[:16]}… is not in this mirror.")
                return 1
            print(f"  v{version}: predecessor {prev[:16]}… is NOT in this mirror and this break is "
                  f"NOT the documented one.")
            print(f"\nCHAIN: FAILED at v{version} — {verified} link(s) verified before the break")
            return 1
        verified += 1
        prev_man = json.loads(found)
        print(f"  v{version} -> v{prev_man.get('version')} verified ({where}, "
              f"{len(found)} bytes)")
        man, version, digest = prev_man, prev_man.get("version"), prev
        if chain_start is not None and int(version or 0) <= int(chain_start) and not args.strict:
            print(f"\nCHAIN: {verified} link(s) verified, walked back to the declared start "
                  f"v{chain_start}. Below it the chain is declared broken; run with --strict to "
                  f"keep walking and see where it actually ends.")
            return 0
        if not prev_man.get("previous_sha256"):
            print(f"\nCHAIN: {verified} link(s) verified, walked to the root v{version}, which "
                  f"declares no predecessor. Everything checked.")
            return 0


if __name__ == "__main__":
    sys.exit(main())
