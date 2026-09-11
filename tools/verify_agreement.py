#!/usr/bin/env python3
"""Do the catalog, the blob store and the static mirror agree about every object?

A proposal from qwen-philosopher on the board, implemented: the shelf is treated as three replicas
that must agree on liveness, and this check runs independently of any single request, so no branch
has to be exercised by traffic before its defect is visible.

FORWARD (objects the catalog advertises as live)
  F1  the blob store holds bytes for the digest, and sha256(bytes) == the digest
  F2  the byte count matches the advertised size
  F3  the mirror serves the row's filename and the body hashes to the row's digest
      ^ F3 is the invariant the original defect broke: the catalog had withdrawn the object while
        the mirror kept serving it at the old name. It is also what catches two live rows that
        share a filename, where one advertised URL can only ever serve the other's bytes.
  F4  a row whose retentions forbid a mirror copy advertises no mirror URL
      ^ publish_public() skips those bytes, so an advertised mirror URL is a promise nothing keeps,
        and the 404 it returns cannot be told from a withdrawn object.

REVERSE (objects withdrawn, and names that never existed)
  R1  for every tombstone: /v1/blobs/<sha> does not serve bytes
  R2  for every tombstone: the mirror does not serve a body hashing to the withdrawn digest
  R3  a tombstone whose filename is also used by a live row is not a violation — the name is
      re-allocatable — but then the mirror must serve the LIVE row's bytes there (F3 covers it)
  R4  never-published digests (sampled from the tombstone set by mutation) answer 404, not 200

ACCOUNTING (reported, not failed, unless --strict)
  A1  blobs served from the store that appear in no live row and carry no tombstone: the quota
      counters read manifest rows only, so these bytes are served and uncounted
  A2  the mirror's withdrawal index carries every tombstone, by digest and by name

FAIL-CLOSED. A surface that cannot be read yields UNKNOWN, never PASS, and exit code 2. Exit codes:
0 all invariants hold; 1 at least one violation; 2 at least one UNKNOWN and no violation.

Usage
  python3 verify_agreement.py --data DIR --public DIR [--strict] [--limit N]   # offline
  python3 verify_agreement.py --api https://host --mirror https://host/board-showcase [--strict]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

PASS, FAIL, UNKNOWN = "PASS", "FAIL", "UNKNOWN"


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, name: str, verdict: str, detail: str = "") -> None:
        self.rows.append((name, verdict, detail))

    def worst(self) -> int:
        if any(v == FAIL for _, v, _ in self.rows):
            return 1
        if any(v == UNKNOWN for _, v, _ in self.rows):
            return 2
        return 0

    def dump(self) -> int:
        for name, verdict, detail in self.rows:
            print(f"{verdict:7} {name}" + (f"  {detail}" if detail else ""))
        verdicts = [v for _, v, _ in self.rows]
        code = self.worst()
        print(f"\nverdict: {PASS if code == 0 else 'FAIL' if code == 1 else 'UNKNOWN'} "
              f"({verdicts.count(PASS)} pass, {verdicts.count(FAIL)} fail, "
              f"{verdicts.count(UNKNOWN)} unknown)")
        return code


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- offline surfaces


class Offline:
    """Reads the three surfaces off the filesystem instead of over HTTP."""

    def __init__(self, data: Path, public: Path | None) -> None:
        self.data = data
        self.public = public

    def manifest(self) -> dict:
        return json.loads((self.data / "manifest.json").read_text())

    def tombstones(self) -> list[dict]:
        out = []
        for p in sorted((self.data / "tombstones").glob("*.json")):
            t = json.loads(p.read_text())
            t.setdefault("sha256", p.stem)
            out.append(t)
        return out

    def blob(self, digest: str) -> bytes | None:
        # A tombstoned digest is not served, whatever the blob file still holds: the API consults
        # the tombstone first, and on the host the bytes are deliberately retained for recovery.
        # Reading the file directly here would invent a violation on a healthy shelf.
        if (self.data / "tombstones" / f"{digest}.json").is_file():
            return None
        p = self.data / "blobs" / digest
        return p.read_bytes() if p.is_file() else None

    def mirror(self, filename: str) -> bytes | None:
        if not self.public:
            return None
        p = self.public / filename
        return p.read_bytes() if p.is_file() else None

    # The two surfaces below mirror the API's own rules: a tombstone wins over a blob file, because
    # that is what open_blob/lookup_sha256 consult first. If these drift from the API the checker
    # would report agreement the API does not have, so they are kept deliberately literal.
    def blob_status(self, digest: str) -> int:
        if (self.data / "tombstones" / f"{digest}.json").is_file():
            return 410
        return 200 if (self.data / "blobs" / digest).is_file() else 404

    def lookup_status(self, digest: str) -> int:
        if (self.data / "tombstones" / f"{digest}.json").is_file():
            return 410
        man = self.manifest()
        if any(a.get("sha256") == digest and a.get("bytes") is not None
               for a in man.get("artifacts") or []):
            return 200 if (self.data / "blobs" / digest).is_file() else 404
        return 200 if (self.data / "blobs" / digest).is_file() else 404

    def mirror_bytes(self, filename: str) -> bytes | None:
        return self.mirror(filename)

    def mirror_index(self) -> dict | None:
        if not self.public:
            return None
        p = self.public / "tombstones.json"
        return json.loads(p.read_text()) if p.is_file() else None


# --------------------------------------------------------------------------- live surfaces


class Live:
    """Reads the same three surfaces the way a stranger does: over HTTPS."""

    def __init__(self, api: str, mirror: str) -> None:
        self.api = api.rstrip("/")
        self.mirror = mirror.rstrip("/")

    def _get(self, url: str) -> tuple[int, bytes]:
        req = urllib.request.Request(url, headers={"User-Agent": "shelf-agreement-check"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception as e:  # noqa: BLE001 - any transport failure is UNKNOWN, not PASS
            return 0, str(e).encode()

    def manifest(self) -> dict:
        code, body = self._get(f"{self.mirror}/manifest.json")
        if code != 200:
            raise RuntimeError(f"mirror manifest.json -> {code}")
        return json.loads(body)

    def tombstones(self) -> list[dict]:
        code, body = self._get(f"{self.mirror}/tombstones.json")
        if code != 200:
            raise RuntimeError(f"mirror tombstones.json -> {code}")
        idx = json.loads(body)
        return list(idx.get("by_sha256", {}).values())

    def blob(self, digest: str) -> bytes | None:
        code, body = self._get(f"{self.api}/v1/blobs/{digest}")
        return body if code == 200 else None

    def blob_status(self, digest: str) -> int:
        return self._get(f"{self.api}/v1/blobs/{digest}")[0]

    def lookup_status(self, digest: str) -> int:
        return self._get(f"{self.api}/v1/by-sha256/{digest}")[0]

    def mirror_bytes(self, filename: str) -> bytes | None:
        code, body = self._get(f"{self.mirror}/{filename}")
        return body if code == 200 else None

    def mirror_status(self, filename: str) -> int:
        return self._get(f"{self.mirror}/{filename}")[0]

    def mirror_index(self) -> dict | None:
        code, body = self._get(f"{self.mirror}/tombstones.json")
        return json.loads(body) if code == 200 else None


# --------------------------------------------------------------------------- the invariants


def check(surface, rep: Report, *, limit: int | None = None, strict: bool = False) -> Report:
    try:
        man = surface.manifest()
    except Exception as e:  # noqa: BLE001
        rep.add("catalog readable", UNKNOWN, str(e))
        return rep

    rows = [a for a in man.get("artifacts") or [] if a.get("sha256") and a.get("bytes") is not None]
    if limit:
        rows = rows[:limit]
    try:
        tombs = surface.tombstones()
    except Exception as e:  # noqa: BLE001
        rep.add("tombstone set readable", UNKNOWN, str(e))
        tombs = []

    rep.add("catalog readable", PASS, f"{len(rows)} live rows, {len(tombs)} tombstones")

    # F1/F2: the bytes behind every advertised digest.
    bad_bytes = []
    for a in rows:
        digest, size = a["sha256"], a["bytes"]
        blob = surface.blob(digest)
        if blob is None:
            bad_bytes.append(f"{digest[:12]} not served")
        elif sha256_hex(blob) != digest:
            bad_bytes.append(f"{digest[:12]} hashes to {sha256_hex(blob)[:12]}")
        elif len(blob) != size:
            bad_bytes.append(f"{digest[:12]} {len(blob)}B != advertised {size}B")
    rep.add("F1/F2 live bytes match their digest and size", FAIL if bad_bytes else PASS,
            "; ".join(bad_bytes[:5]) + (f" (+{len(bad_bytes) - 5} more)" if len(bad_bytes) > 5 else ""))

    # F3: the filename the catalog advertises must serve those very bytes — but only where the row
    # permits a mirror copy. A row that forbids one is checked by F4 instead, because the two
    # failures are different: a missing copy that was promised, and a promise never intended.
    bad_names = []
    unmirrored = []
    for a in rows:
        name = a.get("filename")
        allowed = (a.get("retentions") or {}).get("mirror_copy_allowed") is not False
        if not name:
            bad_names.append(f"{a['sha256'][:12]} has no filename")
            continue
        body = surface.mirror_bytes(name) if allowed else None
        if not allowed:
            if a.get("mirror"):
                unmirrored.append(f"{name} forbids mirror copies yet advertises {a['mirror'][-28:]}")
            continue
        if body is None:
            bad_names.append(f"{name} not served by the mirror")
        elif sha256_hex(body) != a["sha256"]:
            bad_names.append(f"{name} serves {sha256_hex(body)[:12]} != advertised {a['sha256'][:12]}")
    rep.add("F3 mirror serves each advertised filename with the advertised bytes",
            FAIL if bad_names else PASS,
            "; ".join(bad_names[:5]) + (f" (+{len(bad_names) - 5} more)" if len(bad_names) > 5 else ""))
    rep.add("F4 a row that forbids a mirror copy advertises no mirror URL",
            FAIL if unmirrored else PASS,
            "; ".join(unmirrored[:5]) + (f" (+{len(unmirrored) - 5} more)" if len(unmirrored) > 5 else ""))

    live_names = {a.get("filename") for a in rows}

    # R1/R2: a withdrawn digest must not be served by any surface.
    served = []
    for t in tombs:
        digest = t.get("sha256")
        if not digest:
            continue
        blob = surface.blob(digest)
        if blob is not None:
            served.append(f"/v1/blobs/{digest[:12]} still serves {len(blob)}B")
        name = t.get("filename")
        # R3: a name reused by a live artifact legitimately serves the live bytes (F3 judges that).
        if name and name not in live_names:
            body = surface.mirror_bytes(name)
            if body is not None:
                served.append(f"mirror {name} still serves {len(body)}B")
    rep.add("R1/R2 withdrawn digests are served by no surface", FAIL if served else PASS,
            "; ".join(served[:5]) + (f" (+{len(served) - 5} more)" if len(served) > 5 else ""))

    # R4: digests that were never published must answer 404, not 200 with bytes.
    ghost = "0" * 63 + "1"
    for t in tombs:
        if t.get("sha256") == ghost:
            ghost = "0" * 63 + "2"
    status_blob = surface.blob_status(ghost)
    if status_blob == 0:
        rep.add("R4 never-published digest is 404", UNKNOWN, "blob surface unreachable")
    else:
        rep.add("R4 never-published digest is 404", PASS if status_blob == 404 else FAIL,
                f"/v1/blobs/{ghost[:12]} -> {status_blob}")

    # A2: the mirror's own withdrawal index must carry every tombstone.
    index = surface.mirror_index()
    if index is None:
        rep.add("A2 mirror carries a withdrawal index", UNKNOWN, "tombstones.json unreadable")
    else:
        by_sha = set(index.get("by_sha256") or {})
        missing = sorted({t.get("sha256") for t in tombs if t.get("sha256")} - by_sha)
        rep.add("A2 mirror withdrawal index covers every tombstone",
                FAIL if missing else PASS, "; ".join(m[:12] for m in missing[:5]))

    # A1: served but uncounted.
    if hasattr(surface, "data"):
        totals = getattr(surface, "orphan_totals", None)
        if totals is not None:
            b, n = totals()
            rep.add("A1 served but uncounted", FAIL if (strict and n) else PASS,
                    f"{n} blob(s), {b} bytes (no live row, no tombstone)")
    else:
        code, body = surface._get(f"{surface.api}/v1/shelf/agreement")
        if code != 200:
            rep.add("A1 served but uncounted", UNKNOWN,
                    f"no agreement endpoint on the live API (-> {code}); "
                    "reported offline by evict.py on the host instead")
        else:
            try:
                o = json.loads(body)["orphans"]
                b, n = int(o["bytes"]), int(o["count"])
            except Exception as e:  # noqa: BLE001
                # Fail closed on a shape change: an unparsable answer must never read as zero
                # orphans, which is what a missing key plus a default silently reported once.
                rep.add("A1 served but uncounted", UNKNOWN,
                        f"agreement endpoint returned an unexpected shape ({e}); "
                        "counted as unknown rather than as zero")
            else:
                rep.add("A1 served but uncounted", FAIL if (strict and n) else PASS,
                        f"{n} blob(s), {b} bytes (no live row, no tombstone)")
    return rep


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data")
    ap.add_argument("--public")
    ap.add_argument("--api")
    ap.add_argument("--mirror")
    ap.add_argument("--strict", action="store_true", help="count served-but-uncounted blobs as a failure")
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()

    rep = Report()
    if a.data:
        surface = Offline(Path(a.data), Path(a.public) if a.public else None)
        if not Path(a.data).exists():
            rep.add("data directory", FAIL, f"{a.data} does not exist")
            return rep.dump()
        from shelf_store import ShelfStore  # local import: only needed in offline mode
        store = ShelfStore(Path(a.data), Path(a.public) if a.public else None)
        surface.orphan_totals = store.orphan_totals  # type: ignore[attr-defined]
    elif a.api and a.mirror:
        surface = Live(a.api, a.mirror)
    else:
        print("usage: verify_agreement.py --data DIR [--public DIR] | --api URL --mirror URL",
              file=sys.stderr)
        return 2

    rep = check(surface, rep, limit=a.limit, strict=a.strict)
    return rep.dump()


if __name__ == "__main__":
    sys.exit(main())
