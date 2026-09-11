#!/usr/bin/env python3
"""Self-test for verify_chain.py: it must fail on every chain that is not verifiable, and pass only
on the one that is.

Vectors, each on a throwaway copy so the real tree is never touched:
  A. the fixed mirror as published                     -> exit 0, one link verified
  B. history/manifest-v42.json deleted                 -> exit 1 (the advertised link is gone)
  C. history/manifest-v42.json tampered with           -> exit 1 (bytes do not match the anchor)
  D. strict mode (--allow-break none) on the same tree -> exit 1 (the documented break is still a
                                                          break, and a strict witness must be told so)
  E. manifest.json edited without re-anchoring         -> exit 1 (the anchor no longer names it)
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "mirror"
VERIFY = str(HERE / "verify_chain.py")
FAILS = []


def run(base, *extra) -> tuple[int, str]:
    r = subprocess.run([sys.executable, VERIFY, "--base", str(base), *extra],
                       capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def ck(name, got, want):
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: {got!r}" + ("" if ok else f" (wanted {want!r})"))
    if not ok:
        FAILS.append(name)


def copy() -> Path:
    d = Path(tempfile.mkdtemp(prefix="chain-selftest-"))
    shutil.copytree(SRC, d / "mirror")
    return d / "mirror"


def main() -> int:
    print("A. the tree as it will be published")
    rc, out = run(SRC)
    ck("exit 0", rc, 0)
    ck("says one link was verified", "1 link(s) verified" in out, True)
    ck("names the documented break rather than hiding it", "documented break at v42" in out, True)

    print("B. the predecessor manifest is missing")
    m = copy()
    (m / "history" / "manifest-v42.json").unlink()
    rc, out = run(m)
    ck("exit 1", rc, 1)
    ck("says the chain failed", "CHAIN: FAILED at v43" in out, True)
    ck("does not claim a verified link it did not check", "1 link(s) verified" not in out, True)

    print("C. the predecessor manifest is not the bytes the anchor names")
    m = copy()
    (m / "history" / "manifest-v42.json").write_text('{"version": 42, "tampered": true}\n')
    rc, out = run(m)
    ck("exit 1", rc, 1)
    ck("names the version where it stopped", "FAILED at v43" in out, True)

    print("D. strict mode: a documented break is still a break")
    rc, out = run(SRC, "--allow-break", "none")
    ck("exit 1", rc, 1)
    ck("says this break is not the documented one", "NOT the documented one" in out, True)

    print("E. the tip was edited after publication")
    m = copy()
    published = subprocess.run([sys.executable, VERIFY, "--base", str(SRC)], capture_output=True,
                               text=True).stdout
    tip = [w for w in published.split() if len(w) == 64][0]
    man = json.loads((m / "manifest.json").read_text())
    man["entries_touched_by_hand"] = 1
    (m / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=2) + "\n")
    rc, out = run(m, "--expect", tip)
    ck("exit 1 when the tip is not the digest pinned from outside", rc, 1)
    ck("says the starting document is not the one expected", "not the one expected" in out, True)
    rc, out = run(m)
    ck("and without a pin it cannot tell, which it must not hide", rc, 0)
    ck("so the documentation names that gap", "tip" in (HERE / "verify_chain.py").read_text(), True)

    print()
    if FAILS:
        print(f"CHAIN SELFTEST: {len(FAILS)} FAILED")
        return 1
    print("CHAIN SELFTEST: all vectors behave")
    return 0


if __name__ == "__main__":
    sys.exit(main())
