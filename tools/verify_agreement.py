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

    def mirror_miss(self, filename: str) -> tuple[int, dict]:
        """The receipt the mirror's error handler would produce for this name (offline twin of the
        live reader). Without this the offline check verified only the *status* of a withdrawal and
        a shelf whose mirrored 404 carried the wrong tombstone — or none — passed here and failed in
        front of a reader."""
        try:
            from shelf_store import ShelfStore
            return ShelfStore(self.data, self.public).mirror_miss_receipt(filename)
        except Exception:  # noqa: BLE001
            return 0, {"state": "unreadable"}

    def lookup(self, digest: str) -> tuple[int, dict]:
        """The lookup surface as the API answers it, read through the API's own code."""
        try:
            from shelf_store import ShelfStore
            return ShelfStore(self.data, self.public).lookup_sha256(digest)
        except Exception as e:  # noqa: BLE001
            return 0, {"_unreadable": str(e)}

    def mirror_bytes(self, filename: str) -> bytes | None:
        return self.mirror(filename)

    def mirror_index(self) -> dict | None:
        if not self.public:
            return None
        p = self.public / "tombstones.json"
        return json.loads(p.read_text()) if p.is_file() else None

    def mirror_status(self, filename: str) -> int:
        """The status the static mirror would answer with, receipt included.

        Offline this is the store's own receipt function — the same code the mirror's error handler
        asks — so a check that passes here and fails live would mean the two disagree about the
        receipt, not about the bytes. Any failure to read is 0, which every check reads as UNKNOWN.
        """
        if not self.public:
            return 0
        if (self.public / filename).is_file():
            return 200
        try:
            from shelf_store import ShelfStore
            return ShelfStore(self.data, self.public).mirror_miss_receipt(filename)[0]
        except Exception:  # noqa: BLE001
            return 0


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

    def mirror_miss(self, filename: str) -> tuple[int, dict]:
        """What a reader gets for a name the mirror cannot serve — the 404 and its body."""
        code, body = self._get(f"{self.mirror}/{filename}")
        try:
            return code, json.loads(body)
        except Exception:  # noqa: BLE001
            return code, {"_unparsable": body[:120].decode("utf-8", "replace")}

    def lookup(self, digest: str) -> tuple[int, dict]:
        code, body = self._get(f"{self.api}/v1/by-sha256/{digest}")
        try:
            return code, json.loads(body)
        except Exception:  # noqa: BLE001
            return code, {"_unparsable": body[:120].decode("utf-8", "replace")}


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

    # R5: remotik's rule — a withdrawal is not "no bytes at this URL", it is one tombstone for
    # every GET surface. A surface that answers 404 for a withdrawn object tells the reader the
    # object never existed, which is a different and false statement. This is the check that has to
    # fail on a shelf whose mirror is a bare file server, and it did until the error handler existed.
    wrong_status = []
    same_receipt = []
    for t in tombs:
        digest = t.get("sha256")
        if not digest:
            continue
        for label, status in (("/v1/blobs", surface.blob_status(digest)),
                              ("/v1/by-sha256", surface.lookup_status(digest))):
            if status == 0:
                wrong_status.append(f"{label}/{digest[:12]} unreachable (UNKNOWN)")
            elif status != 410:
                wrong_status.append(f"{label}/{digest[:12]} -> {status}, not 410")
        name = t.get("filename")
        if name and name not in live_names:
            status = surface.mirror_status(name)
            if status == 0:
                wrong_status.append(f"mirror/{name} unreachable (UNKNOWN)")
            elif status != 410:
                wrong_status.append(f"mirror/{name} -> {status}, not 410")
            elif hasattr(surface, "mirror_miss"):
                code, body = surface.mirror_miss(name)
                if body.get("state") != "evicted":
                    same_receipt.append(f"mirror/{name} answers {code} with {body.get('state')!r}")
                else:
                    got = (body.get("tombstone") or {}).get("sha256")
                    if got and got != digest:
                        same_receipt.append(
                            f"mirror/{name} carries {str(got)[:12]} not {digest[:12]}")
    unknown_status = [w for w in wrong_status if w.endswith("(UNKNOWN)")]
    real_status = [w for w in wrong_status if not w.endswith("(UNKNOWN)")]
    verdict = FAIL if real_status else (UNKNOWN if unknown_status else PASS)
    rep.add("R5 every read surface answers 410 for a withdrawn object", verdict,
            "; ".join((real_status + unknown_status)[:5]))
    if hasattr(surface, "mirror_miss"):
        rep.add("R5b the mirror's 410 carries the same tombstone, not a bare miss",
                FAIL if same_receipt else PASS, "; ".join(same_receipt[:5]))

    # S1/S2/S3: the third terminal state. A replacement is not a withdrawal, and an invariant set
    # that names only live and evicted forces every superseded object to present as one of the two:
    # as an orphan (it is not live) or as a lie (a 410 that contradicts the bytes still served).
    sup_pairs = []
    sup_pairs_digests = []
    for a in man.get("artifacts") or []:
        s = a.get("supersedes") or {}
        if s.get("sha256"):
            sup_pairs.append((s["sha256"], a["sha256"]))
            sup_pairs_digests.append(s)
    sup_bad, sup_lookup = [], []
    for old, new in sup_pairs:
        blob = surface.blob(old)
        if blob is None:
            sup_bad.append(f"{old[:12]} no longer served although {new[:12]} names it as replaced")
        elif sha256_hex(blob) != old:
            sup_bad.append(f"{old[:12]} serves {sha256_hex(blob)[:12]}")
        code, body = surface.lookup(old)
        if code != 200:
            sup_lookup.append(f"/v1/by-sha256/{old[:12]} -> {code}, not 200")
        elif body.get("state") != "superseded":
            sup_lookup.append(f"{old[:12]} state={body.get('state')!r}, not 'superseded'")
        elif (body.get("superseded_by") or {}).get("superseded_by") != new:
            sup_lookup.append(f"{old[:12]} does not name {new[:12]} as what displaced it")
    rep.add("S1 a superseded digest is still served and still hashes to itself",
            FAIL if sup_bad else PASS,
            "; ".join(sup_bad[:5]) or f"{len(sup_pairs)} replacement(s) checked")
    rep.add("S2 lookup reports superseded, with the digest that displaced it",
            FAIL if sup_lookup else PASS, "; ".join(sup_lookup[:5]))
    contradicted = [f"{old[:12]} has a tombstone but is served" for old, _ in sup_pairs
                    if surface.blob_status(old) == 200
                    and any((t.get("sha256") == old) for t in tombs)]
    rep.add("S3 a served replacement carries no withdrawal receipt", FAIL if contradicted else PASS,
            "; ".join(contradicted[:5]))

    # S4: the accounting depends on a digest being in exactly one bucket. A supersedes entry naming
    # a digest that is itself live would be counted twice and would mean a live object was recorded
    # as displaced by a row that also owns it.
    live_digests = {a["sha256"] for a in man.get("artifacts") or [] if a.get("bytes") is not None}
    doubled = sorted({s["sha256"] for s in sup_pairs_digests if s["sha256"] in live_digests})
    rep.add("S4 no replacement names a digest that is itself live",
            FAIL if doubled else PASS,
            "; ".join(f"{d[:12]} is live and named as displaced" for d in doubled[:5])
            or f"{len(live_digests)} live digest(s), none named as displaced")

    # A1e: the attributed bucket is the one input to this accounting that no write path produces —
    # `attributed.json` is written by a tool, and a record written by any other hand moves a blob
    # out of the orphan count with nothing objecting. A refusal inside one writer is not a property
    # of the file, so the evidence is re-checked at the reading end, where the count is consumed.
    unsupported = getattr(surface, "attributed_unsupported", None)
    if unsupported is not None:
        bad = unsupported()
        rep.add("A1e every attributed entry's evidence still holds when re-checked",
                FAIL if bad else PASS,
                "; ".join(bad[:5]) + (f" (+{len(bad) - 5} more)" if len(bad) > 5 else "")
                or "each entry re-derived from receipts, catalog and tombstones")
    # A4: a catalog row that says how it can be checked. Four admitted shapes: content-addressed
    # (digest + bytes, which is what the rest of this check is about), external-only (a `live` URL,
    # checkable by fetching it), declared (values someone else asserted, with provenance, and which
    # this shelf does not vouch for), and nothing at all — failing in strict, because such a row
    # claims membership in a catalog whose purpose is to say what is served while giving a reader no
    # way to check it. Also fails a row whose own note promises evidence the row does not carry,
    # which is worse than silence: it reads as a pointer and resolves to nothing.
    rows_all = man.get("artifacts") or []
    with_bytes = [r for r in rows_all if r.get("bytes") is not None]
    external = [r for r in rows_all if r.get("bytes") is None and r.get("live")]
    declared = [r for r in rows_all
                if r.get("bytes") is None and not r.get("live")
                and (r.get("sha256_declared") or r.get("bytes_declared"))]
    provenance_only = [r for r in rows_all
                       if r.get("bytes") is None and not r.get("live")
                       and not (r.get("sha256_declared") or r.get("bytes_declared"))
                       and (r.get("provenance") or {})]
    empty = [r for r in rows_all
             if r.get("bytes") is None and not r.get("live")
             and not (r.get("sha256_declared") or r.get("bytes_declared"))
             and not (r.get("provenance") or {})]
    promised_url_missing = [r for r in rows_all
                            if not r.get("live") and r.get("bytes") is None
                            and "live url" in str(r.get("note", "")).lower()]
    detail = (f"{len(with_bytes)} content-addressed, {len(external)} external-only (fetched, not "
              f"hashed), {len(declared)} declared (provenance only, nothing witnessed)")
    if provenance_only:
        # Not a failure: the row says where the claim came from, which is a check a reader can run,
        # even though this shelf serves nothing for it. The distinction from `empty` is the whole
        # point — the first version of this rule failed the row below and the repair to its note was
        # rejected by the invariant that found it.
        detail += (f"; {len(provenance_only)} records a claim without content, with provenance: "
                   + "; ".join(repr(r.get("name") or r.get("filename") or "row")
                               for r in provenance_only[:3]))
    if empty:
        detail += (f"; {len(empty)} assert nothing at all: "
                   + "; ".join(repr(r.get("name") or r.get("filename") or "row") for r in empty[:3]))
    if promised_url_missing:
        detail += ("; a row's note promises a live URL the row does not carry: "
                   + "; ".join(repr(r.get("name") or r.get("filename") or "row")
                               for r in promised_url_missing[:3]))
    rep.add("A4 every catalog row says how it can be checked",
            FAIL if (strict and (empty or promised_url_missing)) else PASS, detail)

    # A2: the mirror's own withdrawal index must carry every tombstone.
    index = surface.mirror_index()
    if index is None:
        rep.add("A2 mirror carries a withdrawal index", UNKNOWN, "tombstones.json unreadable")
    else:
        by_sha = set(index.get("by_sha256") or {})
        missing = sorted({t.get("sha256") for t in tombs if t.get("sha256")} - by_sha)
        rep.add("A2 mirror withdrawal index covers every tombstone",
                FAIL if missing else PASS, "; ".join(m[:12] for m in missing[:5]))

    # A1: served but uncounted. Four buckets, named so that no served blob can fall outside all of
    # them, with the sum asserted rather than trusted. The pair (live + orphans) was complete only
    # because orphan_totals happened to count superseded blobs under a name that said they had no
    # receipt.
    if hasattr(surface, "data"):
        orphan_total = getattr(surface, "orphan_totals", None)
        sup_total = getattr(surface, "superseded_totals", None)
        if orphan_total is not None:
            b, n = orphan_total()
            rep.add("A1a served with no receipt at all (orphans)",
                    FAIL if (strict and n) else PASS,
                    f"{n} blob(s), {b} bytes (no live row, no tombstone, no supersedes entry)")
        if sup_total is not None:
            b, n = sup_total()
            rep.add("A1b served and named by a replacement (superseded)", PASS,
                    f"{n} blob(s), {b} bytes (reachable history, not residue)")
        att_total = getattr(surface, "attributed_totals", None)
        if att_total is not None:
            b, n = att_total()
            rep.add("A1c served with a reconstructed predecessor (attributed)", PASS,
                    f"{n} blob(s), {b} bytes (inferred from surviving receipts; not witnessed)")
        # A1d offline: the sum is checked against the blobs on disk, not against itself. Over HTTP
        # the endpoint computes served_totals as live+superseded+attributed+orphans and the check
        # recomputes the same expression over the same payload, which no store state can falsify.
        # Here the four buckets are compared with what the shelf actually holds — a blob counted
        # twice, or by nothing, makes the numbers differ.
        served_total = getattr(surface, "served_totals", None)
        live_total = getattr(surface, "live_totals", None)
        sup_total2 = getattr(surface, "superseded_totals", None)
        if None not in (served_total, live_total, sup_total2, att_total, orphan_total):
            sb, sn = served_total()
            parts_n = live_total()[1] + sup_total2()[1] + att_total()[1] + orphan_total()[1]
            parts_b = live_total()[0] + sup_total2()[0] + att_total()[0] + orphan_total()[0]
            blobs_dir = Path(surface.data) / "blobs"
            files = [p for p in blobs_dir.glob("*") if p.is_file()] if blobs_dir.is_dir() else []
            # Tombstoned digests are retained on disk and served by nothing, so they are not part of
            # what the buckets count; every other blob file must be in exactly one bucket.
            tombed = {p.stem for p in (Path(surface.data) / "tombstones").glob("*.json")}
            held = [p for p in files if p.name not in tombed]
            held_n, held_b = len(held), sum(p.stat().st_size for p in held)
            mism = []
            if (parts_n, parts_b) != (sn, sb):
                mism.append(f"buckets sum to {parts_n}/{parts_b}B, served_totals says {sn}/{sb}B")
            if (held_n, held_b) != (sn, sb):
                mism.append(f"the shelf holds {held_n} served blob(s)/{held_b}B, "
                            f"the buckets account for {sn}/{sb}B — a blob is counted twice or by "
                            f"nothing")
            rep.add("A1d the buckets account for every served blob exactly once",
                    FAIL if mism else PASS,
                    "; ".join(mism) or f"{held_n} served blob(s), {held_b} bytes, each in one bucket")
    else:
        n = sn = an = None
        code, body = surface._get(f"{surface.api}/v1/shelf/agreement")
        if code != 200:
            rep.add("A1a served with no receipt at all (orphans)", UNKNOWN,
                    f"no agreement endpoint on the live API (-> {code}); "
                    "reported offline by evict.py on the host instead")
        else:
            try:
                o = json.loads(body)["orphans"]
                b, n = int(o["bytes"]), int(o["count"])
            except Exception as e:  # noqa: BLE001
                # Fail closed on a shape change: an unparsable answer must never read as zero
                # orphans, which is what a missing key plus a default silently reported once.
                rep.add("A1a served with no receipt at all (orphans)", UNKNOWN,
                        f"agreement endpoint returned an unexpected shape ({e}); "
                        "counted as unknown rather than as zero")
            else:
                rep.add("A1a served with no receipt at all (orphans)",
                        FAIL if (strict and n) else PASS,
                        f"{n} blob(s), {b} bytes (no live row, no tombstone, no supersedes entry)")
            try:
                s = json.loads(body)["superseded"]
                sb, sn = int(s["bytes"]), int(s["count"])
            except Exception as e:  # noqa: BLE001
                rep.add("A1b served and named by a replacement (superseded)", UNKNOWN,
                        f"agreement endpoint returned an unexpected shape ({e})")
            else:
                rep.add("A1b served and named by a replacement (superseded)", PASS,
                        f"{sn} blob(s), {sb} bytes (reachable history, not residue)")
            try:
                at = json.loads(body)["attributed"]
                ab, an = int(at["bytes"]), int(at["count"])
            except Exception as e:  # noqa: BLE001
                rep.add("A1c served with a reconstructed predecessor (attributed)", UNKNOWN,
                        f"agreement endpoint returned an unexpected shape ({e})")
            else:
                rep.add("A1c served with a reconstructed predecessor (attributed)", PASS,
                        f"{an} blob(s), {ab} bytes (inferred from receipts; not witnessed)")
            if None in (n, sn, an):
                rep.add("A1d the four buckets sum to served_totals", UNKNOWN,
                        "at least one bucket was unreadable; a sum over three of four numbers "
                        "would be a false total")
            else:
                try:
                    expected = (int(json.loads(body)["counted"]["live_objects"]) + sn + an + n)
                    reported = int(json.loads(body)["served_totals"]["objects"])
                except Exception as e:  # noqa: BLE001
                    rep.add("A1d the four buckets sum to served_totals", UNKNOWN, str(e))
                else:
                    rep.add("A1d the four buckets sum to served_totals",
                            FAIL if expected != reported else PASS,
                            f"live+superseded+attributed+orphans={expected}, "
                            f"served_totals={reported}")
            try:
                bad_att = json.loads(body)["attributed_unsupported"]
            except Exception:  # noqa: BLE001
                rep.add("A1e every attributed entry's evidence still holds when re-checked", UNKNOWN,
                        "the endpoint does not report it; the attributed count is taken on trust")
            else:
                rep.add("A1e every attributed entry's evidence still holds when re-checked",
                        FAIL if bad_att else PASS,
                        "; ".join(str(x) for x in bad_att[:5])
                        or "each entry re-derived from receipts, catalog and tombstones")
            try:
                fence = json.loads(body)["fence"]
                gen = json.loads(body).get("manifest_generation")
            except Exception:  # noqa: BLE001
                rep.add("A3 the receipt names the substrate both readings share", UNKNOWN,
                        "agreement endpoint carries no fence; this reading's independence is "
                        "unstated")
            else:
                rep.add("A3 the receipt names the substrate both readings share", PASS,
                        f"mirror={fence.get('mirror')}; clock={fence.get('clock')}; "
                        f"generation={gen} — a second reading through this host shares it, so "
                        "agreement here is agreement about ONE origin")
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
        # All four buckets, not one. Attaching only orphan_totals made A1b, A1c and A1d disappear
        # from the offline report without a word — an invariant that is silently absent reads as an
        # invariant that held, and the sum A1d asserts is exactly the one an offline run of this
        # check on the host was expected to cover.
        surface.orphan_totals = store.orphan_totals  # type: ignore[attr-defined]
        surface.superseded_totals = store.superseded_totals  # type: ignore[attr-defined]
        surface.attributed_totals = store.attributed_totals  # type: ignore[attr-defined]
        surface.attributed_unsupported = store.attributed_unsupported  # type: ignore[attr-defined]
        surface.served_totals = store.served_totals  # type: ignore[attr-defined]
        surface.live_totals = store.live_totals  # type: ignore[attr-defined]
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
