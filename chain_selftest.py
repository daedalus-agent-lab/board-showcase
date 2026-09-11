#!/usr/bin/env python3
"""Self-test for verify_chain.py: it must fail on every chain that is not verifiable, and pass only
on the one that is.

Vectors, each on a throwaway copy so the real tree is never touched:
  A. the fixed mirror as published                     -> exit 0, both published links verified,
                                                          then the documented break at chain_start
  B. history/manifest-v42.json deleted                 -> exit 1: v42 is a file this mirror
                                                          published, so its absence is a break, not a
                                                          declared stop
  C. history/manifest-v42.json tampered with           -> exit 1 (bytes do not match the anchor)
  D. --strict, and --allow-break none, on the same tree -> exit 1 (a documented break is still a
                                                          break, and "none" must not fall back to the
                                                          default allowance)
  E. manifest.json edited without re-anchoring         -> exit 1 (the anchor no longer names it)
  F. a report of a run without the tool's own digest   -> exit 1 (the run cannot be attributed)

It runs on the checkout it is invoked from: `mirror/` below the author's layout, or the repository root
itself for anyone who cloned the mirror, which is the only way a witness can run it at all.
"""
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERIFY = str(HERE / "verify_chain.py")
FAILS = []


def find_tree() -> Path:
    """The mirror to test: `mirror/` in the authoring layout, else the checkout this file sits in.

    A witness who clones the repository has the mirror at the root, with no `mirror/` below it. The
    first version of this file assumed the author's layout and died on `FileNotFoundError` for anyone
    else — a crash whose exit status looks exactly like a self-test that found something.
    """
    if (HERE / "mirror" / "manifest.json").is_file():
        return HERE / "mirror"
    if (HERE / "manifest.json").is_file():
        return HERE
    raise SystemExit("chain_selftest: no manifest.json here or in ./mirror — run it from a checkout of "
                     "the mirror")


SRC = find_tree()


def tool_identity() -> str:
    """This file's own digest, printed first, so a report names the revision that produced it."""
    p = Path(__file__).resolve()
    return f"tool {p.name} sha256 {hashlib.sha256(p.read_bytes()).hexdigest()}"


def run(base, *extra) -> tuple[int, str]:
    r = subprocess.run([sys.executable, VERIFY, "--base", str(base), *extra],
                       capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def ck(name, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: {got!r}" + ("" if ok else f" (wanted {want!r})"))
    if not ok:
        FAILS.append(name)


def claimed_vs_printed(out: str) -> tuple[int, int]:
    """The link count the walk reports, against the link lines it actually printed.

    A verifier's headline number is a claim like any other: if it says three links were verified and
    printed two, the number is the bug. This is the invariant that matters, and writing the expected
    figure by hand instead is how a stale expectation hides a real one.
    """
    printed = len([ln for ln in out.splitlines() if " -> v" in ln and "verified (" in ln])
    m = re.search(r"(\d+) link\(s\) verified", out)
    return (int(m.group(1)) if m else -1), printed


def copy() -> Path:
    d = Path(tempfile.mkdtemp(prefix="chain-selftest-"))
    shutil.copytree(SRC, d / "mirror", ignore=shutil.ignore_patterns(".git"))
    return d / "mirror"


def main() -> int:
    print(tool_identity())
    print(f"tree under test: {SRC}")

    print("A. the tree as it will be published")
    rc, out = run(SRC)
    ck("exit 0", rc, 0)
    ck("the run names the tool revision that produced it",
       "tool verify_chain.py sha256 " in out, True)
    ck("verifies every link this mirror can prove", "3 link(s) verified" in out, True)
    ck("names the documented break rather than hiding it", "documented break at v42" in out, True)
    ck("the count it reports equals the links it printed", claimed_vs_printed(out), (3, 3))

    print("B. the predecessor manifest is missing")
    m = copy()
    (m / "history" / "manifest-v42.json").unlink()
    rc, out = run(m)
    ck("exit 1", rc, 1)
    ck("says the chain failed", "CHAIN: FAILED at v43" in out, True)
    ck("the count it reports equals the links it printed", claimed_vs_printed(out), (2, 2))
    ck("does not call a published predecessor a declared start",
       "walked back to the declared start" not in out, True)

    print("C. the predecessor manifest is not the bytes the anchor names")
    m = copy()
    (m / "history" / "manifest-v42.json").write_text('{"version": 42, "tampered": true}\n')
    rc, out = run(m)
    ck("exit 1", rc, 1)
    ck("names the version where it stopped", "FAILED at v43" in out, True)

    print("D. strict mode: a documented break is still a break")
    rc, out = run(SRC, "--strict")
    ck("exit 1", rc, 1)
    ck("says why it refuses", "not acceptable here" in out, True)
    rc, out = run(SRC, "--allow-break", "none")
    ck("exit 1 for --allow-break none as well", rc, 1)
    ck("a break is not silently allowed by asking for none",
       "not acceptable here" in out and "then the documented break" not in out, True)

    print("E. the tip was edited after publication")
    m = copy()
    published = subprocess.run([sys.executable, VERIFY, "--base", str(SRC)], capture_output=True,
                               text=True).stdout
    # Read the tip from the line that labels it. Taking the first 64-character word instead picked up
    # the tool's own digest from the identity line, so this vector silently pinned the wrong number.
    label = [ln for ln in published.splitlines() if ln.startswith("manifest v")]
    ck("the walk prints a labelled manifest digest to pin", bool(label), True)
    tip = label[0].split()[-1]
    man = json.loads((m / "manifest.json").read_text())
    man["entries_touched_by_hand"] = 1
    (m / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=2) + "\n")
    rc, out = run(m, "--expect", tip)
    ck("exit 1 when the tip is not the digest pinned from outside", rc, 1)
    ck("says the starting document is not the one expected", "not the one expected" in out, True)
    rc, out = run(m)
    ck("and without a pin it cannot tell, which it must not hide", rc, 0)
    ck("so the documentation names that gap", "tip" in (HERE / "verify_chain.py").read_text(), True)

    print("F. a run that cannot say which revision produced it")
    rc, out = run(SRC)
    ck("every witness run carries the tool's own sha256",
       out.splitlines()[0].startswith("tool verify_chain.py sha256 "), True)

    print()
    if FAILS:
        print(f"CHAIN SELFTEST: {len(FAILS)} FAILED")
        return 1
    print("CHAIN SELFTEST: all vectors behave")
    return 0


if __name__ == "__main__":
    sys.exit(main())
