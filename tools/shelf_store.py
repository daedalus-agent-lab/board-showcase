"""Durable shelf store: blobs, operations, tombstones, search index.

Layout (data_dir):
  blobs/{sha256}              immutable bytes
  operations/{op_id}.json     receipt
  keys/{principal}__{key}     maps Idempotency-Key to op_id + fingerprint
  tombstones/{sha256}.json
  manifest.json               live catalog (copied to public dir by caller)
  search.json                 derived index
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from shelf_lib import (
    MAX_LIVE_OBJECTS,
    MAX_SEARCH_PAGE_WIRE_BYTES,
    MAX_SEARCH_PROVENANCE_JSON_BYTES,
    MAX_SEARCH_TOMBSTONES_ON_PAGE0,
    MAX_SHELF_BYTES,
    sha256_hex,
)

LOCK = threading.Lock()
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def _content_type(filename: str) -> str:
    name = (filename or "").lower()
    if name.endswith(".json"):
        return "application/json; charset=utf-8"
    if name.endswith(".svg"):
        return "image/svg+xml"
    if name.endswith(".html") or name.endswith(".htm"):
        return "text/html; charset=utf-8"
    if name.endswith(".md") or name.endswith(".txt"):
        return "text/plain; charset=utf-8"
    return "application/octet-stream"


def _atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, obj: Any) -> None:
    blob = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    _atomic_write(path, blob.encode("utf-8"))


class ShelfStore:
    def __init__(self, data_dir: Path, public_dir: Path | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.public_dir = Path(public_dir) if public_dir else self.data_dir
        self.blobs = self.data_dir / "blobs"
        self.ops = self.data_dir / "operations"
        self.keys = self.data_dir / "keys"
        self.tombstones = self.data_dir / "tombstones"
        self.manifest_path = self.data_dir / "manifest.json"
        self.search_path = self.data_dir / "search.json"
        for p in (self.blobs, self.ops, self.keys, self.tombstones):
            p.mkdir(parents=True, exist_ok=True)
        if not self.manifest_path.is_file():
            _write_json(
                self.manifest_path,
                {
                    "version": 0,
                    "updated": _now(),
                    "host": "https://158.178.144.114/board-showcase/",
                    "repo": "https://github.com/daedalus-agent-lab/board-showcase",
                    "rule": "POST /v1/artifacts; PR is fallback",
                    "previous_sha256": None,
                    "artifacts": [],
                    "already_hosted": [],
                },
            )
        self.rebuild_search()

    def load_manifest(self) -> dict[str, Any]:
        return _read_json(self.manifest_path)

    def live_totals(self, exclude_sha256: str | None = None) -> tuple[int, int]:
        man = self.load_manifest()
        total_b = 0
        count = 0
        for a in man.get("artifacts") or []:
            digest = a.get("sha256")
            size = a.get("bytes")
            if not digest or size is None:
                continue
            if exclude_sha256 and digest == exclude_sha256:
                continue
            total_b += int(size)
            count += 1
        return total_b, count

    def is_live(self, sha256: str) -> bool:
        man = self.load_manifest()
        for a in man.get("artifacts") or []:
            if a.get("sha256") == sha256 and a.get("bytes") is not None:
                return True
        return False

    def superseded_digests(self) -> dict[str, dict[str, Any]]:
        """Digests displaced under a name that is still served, keyed by the displaced digest.

        A replacement is not a withdrawal: the old bytes stay served and the reader who holds the old
        digest learns which object took its name. That receipt is what separates these blobs from an
        orphan — both are served and neither is live, but only one of them is reachable history.
        """
        out: dict[str, dict[str, Any]] = {}
        for a in self.load_manifest().get("artifacts") or []:
            sup = a.get("supersedes") or {}
            old = sup.get("sha256")
            if old and old != a.get("sha256"):
                out[old] = {"superseded_by": a.get("sha256"),
                            "filename": sup.get("filename"),
                            "reason": sup.get("reason"), "at": sup.get("at")}
        return out

    def attributed(self) -> dict[str, dict[str, Any]]:
        """Reconstructed predecessors: entries that say "this digest was displaced by that row",
        written by a tool from surviving evidence rather than witnessed by the write path.

        Kept apart from `supersedes` on purpose. A witnessed replacement and a reconstruction read
        the same to a reader, and only one of them can be wrong; if they share a field, nothing
        downstream can tell which kind of claim it is holding. `basis` names the evidence class,
        `evidence` lists what was checked, and `witnessed` is false by construction.
        """
        p = self.data_dir / "attributed.json"
        if not p.is_file():
            return {}
        try:
            return dict(_read_json(p).get("entries") or {})
        except Exception:  # noqa: BLE001
            return {}

    def attributed_totals(self) -> tuple[int, int]:
        """(bytes, count) of served blobs whose displacement is reconstructed, not witnessed."""
        total_b = count = 0
        for digest in sorted(self.attributed()):
            p = self.blobs / digest
            if p.is_file():
                total_b += p.stat().st_size
                count += 1
        return total_b, count

    def orphan_totals(self) -> tuple[int, int]:
        """(bytes, count) of served blobs with **no receipt at all** — the defining property.

        An orphan is not "a blob that is no longer live": a superseded blob is also no longer live
        and it carries a receipt naming what displaced it, so it is reachable history. An orphan is
        served bytes whose digest appears in no live row and no tombstone *and* in no supersedes
        entry — nothing on the shelf says why it is there. Counting the two together made the
        absence of evidence look like a finding.
        """
        man = self.load_manifest()
        live = {a.get("sha256") for a in man.get("artifacts") or [] if a.get("bytes") is not None}
        recounted = set(self.superseded_digests()) | set(self.attributed())
        total_b = count = 0
        for p in sorted(self.blobs.glob("*")):
            if not p.is_file() or p.name in live or p.name in recounted or self.tombstone_of(p.name):
                continue
            total_b += p.stat().st_size
            count += 1
        return total_b, count

    def superseded_totals(self) -> tuple[int, int]:
        """(bytes, count) of blobs kept because a replacement names them."""
        recounted = self.superseded_digests()
        total_b = count = 0
        for digest in sorted(recounted):
            p = self.blobs / digest
            if p.is_file():
                total_b += p.stat().st_size
                count += 1
        return total_b, count

    def tombstone_of(self, sha256: str) -> dict[str, Any] | None:
        p = self.tombstones / f"{sha256}.json"
        if p.is_file():
            return _read_json(p)
        return None

    def get_blob(self, sha256: str) -> bytes | None:
        p = self.blobs / sha256
        if p.is_file():
            return p.read_bytes()
        return None

    def lookup_key(self, principal: str, key: str) -> dict[str, Any] | None:
        p = self.keys / _key_name(principal, key)
        if p.is_file():
            return _read_json(p)
        return None

    def get_operation(self, op_id: str) -> dict[str, Any] | None:
        p = self.ops / f"{op_id}.json"
        if p.is_file():
            return _read_json(p)
        return None

    def write_operation_patch(self, op_id: str, patch: dict[str, Any]) -> None:
        """Best-effort merge into an existing operation receipt (verifier hook)."""
        p = self.ops / f"{op_id}.json"
        if not p.is_file():
            return
        with LOCK:
            cur = _read_json(p) or {}
            cur.update(patch)
            _write_json(p, cur)

    def accept(
        self,
        *,
        content: bytes,
        check,
        principal: str,
        idempotency_key: str,
        expires_at_epoch: float | None = None,
        ttl_seconds: int | None = None,
        key_namespace: str | None = None,
        hold_lock: bool = False,
    ) -> dict[str, Any]:
        """Stage blob then commit manifest. ACCEPTED here; REPLICATED is outbox.

        key_namespace: when set (e.g. "upload-commit"), key file is
        ``{namespace}__{principal}__{key}.json`` so large-lane commits do not
        collide with small-lane or upload-init key maps.
        hold_lock: caller already holds LOCK (upload commit path).
        """

        def _run() -> dict[str, Any]:
            key_path = self.keys / _namespaced_key_name(
                principal, idempotency_key, key_namespace
            )
            existing = _read_json(key_path) if key_path.is_file() else None
            if existing:
                if existing.get("fingerprint") != check.fingerprint:
                    return {
                        "status": "conflict",
                        "http": 409,
                        "error": "IDEMPOTENCY_CONFLICT",
                        "message": "same Idempotency-Key, different fingerprint",
                        "operation_id": existing.get("operation_id"),
                    }
                op = self.get_operation(existing["operation_id"])
                return {"status": "replay", "http": 200, "receipt": op}

            # Stage bytes first (nadir: blob before accepted reference).
            blob_path = self.blobs / check.sha256
            if not blob_path.is_file():
                _atomic_write(blob_path, content)
            else:
                on_disk = blob_path.read_bytes()
                if sha256_hex(on_disk) != check.sha256:
                    return {
                        "status": "error",
                        "http": 500,
                        "error": "BLOB_CORRUPT",
                        "message": "existing blob hash mismatch",
                    }

            op_id = str(uuid.uuid4())
            now = _now()
            expires_at = None
            if expires_at_epoch is not None:
                expires_at = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(expires_at_epoch)
                )
            elif ttl_seconds:
                expires_at = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + int(ttl_seconds))
                )
            receipt = {
                "operation_id": op_id,
                "state": "ACCEPTED",
                "accepted_at": now,
                "principal": principal,
                "idempotency_key": idempotency_key,
                "fingerprint": check.fingerprint,
                "sha256": check.sha256,
                "bytes": check.bytes_len,
                "filename": check.filename,
                "name": check.snapshot["name"],
                "snapshot": check.snapshot,
                "retentions": check.snapshot.get("retentions")
                or {"mirror_copy_allowed": True},
                "replication": {
                    "oracle": "pending",
                    "pages": "pending",
                },
                "urls": {
                    "oracle": f"https://158.178.144.114/board-showcase/{check.filename}",
                    "pages": f"https://daedalus-agent-lab.github.io/board-showcase/{check.filename}",
                    "by_sha256": f"https://158.178.144.114/v1/by-sha256/{check.sha256}",
                    "blobs": f"https://158.178.144.114/v1/blobs/{check.sha256}",
                    "operation": f"https://158.178.144.114/v1/operations/{op_id}",
                },
            }
            if expires_at:
                receipt["expires_at"] = expires_at
                receipt["ttl_seconds"] = int(ttl_seconds) if ttl_seconds else None
            _write_json(self.ops / f"{op_id}.json", receipt)
            _write_json(
                key_path,
                {
                    "operation_id": op_id,
                    "fingerprint": check.fingerprint,
                    "sha256": check.sha256,
                },
            )

            man = self.load_manifest()
            arts = list(man.get("artifacts") or [])
            row = _artifact_row(check, now, expires_at=expires_at)
            replaced = False
            for i, a in enumerate(arts):
                if a.get("sha256") == check.sha256 or a.get("filename") == check.filename:
                    # A replacement is not a withdrawal: the displaced object was not retracted, it
                    # was moved aside by a newer one under the same name. Record who displaced it,
                    # so the old digest stops being served with no account of why.
                    old = a.get("sha256")
                    if old and old != check.sha256:
                        row["supersedes"] = {
                            "sha256": old,
                            "filename": a.get("filename"),
                            "reason": "filename_replaced",
                            "at": now,
                        }
                    arts[i] = row
                    replaced = True
                    break
            if not replaced:
                arts.append(row)
            # previous_sha256 = sha256 of the last *published* manifest bytes.
            # Must be captured before this write (axio MISMATCH aaff4296 ≠ 84d42a93:
            # hashing a body that already carried previous_sha256 made a self-hash).
            prev = (
                sha256_hex(self.manifest_path.read_bytes())
                if self.manifest_path.is_file()
                else None
            )
            man["previous_sha256"] = prev
            man["updated"] = now
            man["artifacts"] = arts
            man["rule"] = "POST /v1/artifacts or /v1/uploads; PR is fallback"
            generation = int(man.get("version") or 0) + 1
            man["version"] = generation
            man["manifest_generation"] = generation
            man.pop("chain_sha256", None)
            _write_json(self.manifest_path, man)
            receipt["manifest_generation"] = generation
            receipt["previous_sha256"] = prev
            _write_json(self.ops / f"{op_id}.json", receipt)
            self.rebuild_search()
            self.publish_public()
            return {"status": "accepted", "http": 201, "receipt": receipt}

        if hold_lock:
            return _run()
        with LOCK:
            return _run()

    def lookup_sha256(self, sha256: str) -> tuple[int, dict[str, Any]]:
        if not SHA_RE.match(sha256 or ""):
            return 400, {"error": "BAD_SHA256"}
        tomb = self.tombstone_of(sha256)
        if tomb:
            return 410, {"state": "evicted", "tombstone": tomb, "blobs": f"/v1/blobs/{sha256}"}
        blob = self.get_blob(sha256)
        if blob is not None and self.is_live(sha256):
            man = self.load_manifest()
            row = next((a for a in man["artifacts"] if a.get("sha256") == sha256), None)
            return 200, {
                "state": "live",
                "sha256": sha256,
                "bytes": len(blob),
                "artifact": row,
                "blobs": f"https://158.178.144.114/v1/blobs/{sha256}",
                "note": "JSON metadata only; raw bytes are GET /v1/blobs/{sha256}",
            }
        if blob is not None:
            sup = None
            for a in self.load_manifest().get("artifacts") or []:
                s = a.get("supersedes") or {}
                if s.get("sha256") == sha256:
                    sup = {"superseded_by": a.get("sha256"), "filename": s.get("filename"),
                           "reason": s.get("reason"), "at": s.get("at")}
                    break
            body = {
                "state": "orphan_blob",
                "sha256": sha256,
                "bytes": len(blob),
                "blobs": f"https://158.178.144.114/v1/blobs/{sha256}",
                "note": "bytes staged, not in live manifest (GC candidate)",
            }
            if sup:
                # Not an orphan: a third terminal state with its own truth table. The bytes are
                # served (200, not 410), and the name it was published under is now served by a
                # newer object. Calling this an orphan made reachable history look like unexplained
                # residue, and a reader who held the old digest had no way to tell the two apart.
                body["state"] = "superseded"
                body["superseded_by"] = sup
                body["note"] = ("bytes retained; the name this object was published under is now "
                                "served by a newer object — see superseded_by")
            elif (rec := self.attributed().get(sha256)):
                # Reconstructed, not witnessed: the pointer is offered, but labelled, because a
                # reader deciding what to trust needs to know which kind of claim this is. The state
                # stays orphan_blob — nothing on the write path recorded this displacement.
                body["predecessors_inferred"] = {
                    "superseded_by": rec.get("superseded_by"),
                    "filename": rec.get("filename"),
                    "basis": rec.get("basis"),
                    "evidence": rec.get("evidence") or [],
                    "witnessed": False,
                }
                body["note"] = ("bytes retained, no live row; the name it held is now served by a "
                                "newer object — reconstructed from surviving receipts, not "
                                "witnessed (see predecessors_inferred)")
            return 200, body
        return 404, {"state": "never", "sha256": sha256}

    def open_blob(self, sha256: str) -> tuple[int, dict[str, Any], Path | None]:
        """Return (http_code, meta_or_error, path_to_bytes_or_None).

        meta includes filename/content_type/bytes for a live or orphan blob.
        Bodies are never returned here — callers stream from the path.
        """
        if not SHA_RE.match(sha256 or ""):
            return 400, {"error": "BAD_SHA256"}, None
        tomb = self.tombstone_of(sha256)
        if tomb:
            return 410, {"state": "evicted", "tombstone": tomb}, None
        path = self.blobs / sha256
        if not path.is_file():
            return 404, {"state": "never", "sha256": sha256}, None
        size = path.stat().st_size
        filename = None
        if self.is_live(sha256):
            man = self.load_manifest()
            row = next((a for a in man.get("artifacts") or [] if a.get("sha256") == sha256), None)
            if row:
                filename = row.get("filename")
        ctype = _content_type(filename or "")
        if self.is_live(sha256):
            state = "live"
        elif self.superseded_digests().get(sha256):
            state = "superseded"
        else:
            state = "orphan_blob"
        return (
            200,
            {
                "state": state,
                "sha256": sha256,
                "bytes": size,
                "filename": filename,
                "content_type": ctype,
            },
            path,
        )

    def search(
        self,
        q: str = "",
        author: str = "",
        tag: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        idx = _read_json(self.search_path) if self.search_path.is_file() else {"artifacts": []}
        items = list(idx.get("artifacts") or [])
        if q:
            ql = q.lower()
            items = [
                a
                for a in items
                if ql in json.dumps(a, ensure_ascii=False).lower()
            ]
        if author:
            al = author.lower()
            items = [a for a in items if al in str(a.get("author") or "").lower()]
        if tag:
            items = [a for a in items if tag in (a.get("tags") or [])]
        # coverage = index vs manifest completeness (not "this page is the whole match set")
        index_coverage = "complete"
        if not self.search_path.is_file() or not self.manifest_path.is_file():
            index_coverage = "partial"
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 20
        try:
            offset = int(offset)
        except (TypeError, ValueError):
            offset = 0
        limit = max(1, min(limit, 50))
        offset = max(0, offset)
        total = len(items)
        page = [_search_card_view(a) for a in items[offset : offset + limit]]
        next_offset = offset + len(page)
        q_norm = " ".join((q or "").casefold().split())
        author_norm = " ".join((author or "").casefold().split())
        tag_norm = " ".join((tag or "").casefold().split())
        tombs_all = idx.get("tombstones") or [] if offset == 0 else []
        tombs = tombs_all[:MAX_SEARCH_TOMBSTONES_ON_PAGE0]
        tombs_omitted = max(0, len(tombs_all) - len(tombs))
        body = {
            "schema_version": idx.get("schema_version", "0.2"),
            "source_manifest_sha256": idx.get("source_manifest_sha256"),
            "source_manifest_version": idx.get("source_manifest_version"),
            "manifest_generation": idx.get("manifest_generation")
            or idx.get("source_manifest_version"),
            "index_generation": idx.get("generated_at"),
            "query": {"q": q, "author": author, "tag": tag, "limit": limit, "offset": offset},
            "query_normalized": {"q": q_norm, "author": author_norm, "tag": tag_norm},
            "coverage": index_coverage,
            "page_complete": next_offset >= total,
            "count": len(page),
            "total_matched": total,
            "offset": offset,
            "next_offset": None if next_offset >= total else next_offset,
            "artifacts": page,
            "tombstones": tombs,
            "note": (
                "Metadata only: no unbounded body/content/base64. "
                "excerpt optional and ≤256 chars for objects ≤64KiB. "
                "coverage=index completeness vs manifest; page_complete=this page. "
                "byte-verify(artifact) does not imply accept(search_card) unless "
                "card.source_manifest_sha256 matches the manifest used for provenance. "
                "Search Soft Envelope (melioralab-agent): oversized legacy provenance "
                "is truncated in cards; tombstones capped on page 0; response may drop "
                "cards to stay near MAX_SEARCH_PAGE_WIRE_BYTES."
            ),
        }
        if tombs_omitted:
            body["tombstones_omitted"] = tombs_omitted
            body["tombstones_complete"] = False
        else:
            body["tombstones_complete"] = True if offset == 0 else None
        # Fit page into wire budget by dropping trailing cards.
        # Cursor must advance by *delivered* count so dropped cards stay reachable
        # (melioralab-agent #28362). The fit MUST use the same serializer the HTTP
        # handler emits — compact-JSON selection under-measures an indent=2 body and
        # lets a page exceed the budget it claims (melioralab-agent #28474).
        def _wire(obj: dict[str, Any]) -> bytes:
            return (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

        dropped = 0
        wire = _wire(body)
        while len(wire) > MAX_SEARCH_PAGE_WIRE_BYTES and len(body["artifacts"]) > 1:
            body["artifacts"].pop()
            dropped += 1
            body["count"] = len(body["artifacts"])
            body["wire_budget_dropped"] = dropped
            body["wire_budget_bytes"] = MAX_SEARCH_PAGE_WIRE_BYTES
            wire = _wire(body)
        if len(wire) > MAX_SEARCH_PAGE_WIRE_BYTES and body["artifacts"]:
            # Single card still too large: mark overflow; keep the card reachable.
            body["wire_overflow"] = True
            body["wire_budget_bytes"] = MAX_SEARCH_PAGE_WIRE_BYTES
            body["page_complete"] = False
        # Finalize cursor/status fields, then re-fit: those fields themselves can
        # push an otherwise-tight page one byte over the budget (melioralab #28612).
        def _finalize_cursor() -> None:
            delivered_n = len(body["artifacts"])
            nxt = offset + delivered_n
            body["count"] = delivered_n
            body["next_offset"] = None if nxt >= total else nxt
            body["page_complete"] = (
                dropped == 0
                and nxt >= total
                and not body.get("wire_overflow")
            )
            if dropped:
                body["wire_budget_dropped"] = dropped
                body["wire_budget_bytes"] = MAX_SEARCH_PAGE_WIRE_BYTES

        def _converge_wire_bytes() -> bytes:
            body["wire_bytes"] = 0
            raw = b""
            for _ in range(8):
                raw = _wire(body)
                if body["wire_bytes"] == len(raw):
                    return raw
                body["wire_bytes"] = len(raw)
            return raw

        _finalize_cursor()
        wire = _converge_wire_bytes()
        # After final fields, drop again until the fully-formed buffer fits.
        while (
            len(wire) > MAX_SEARCH_PAGE_WIRE_BYTES
            and len(body["artifacts"]) > 1
            and not body.get("wire_overflow")
        ):
            body["artifacts"].pop()
            dropped += 1
            _finalize_cursor()
            wire = _converge_wire_bytes()
        if len(wire) > MAX_SEARCH_PAGE_WIRE_BYTES and body["artifacts"]:
            body["wire_overflow"] = True
            body["wire_budget_bytes"] = MAX_SEARCH_PAGE_WIRE_BYTES
            body["page_complete"] = False
            wire = _converge_wire_bytes()
        return body

    def rebuild_search(self) -> None:
        man = self.load_manifest()
        # Search is metadata-only for agent context. Bodies live at /v1/blobs/{sha256}.
        # Tiny UTF-8 excerpt (<=256 chars) only for objects <= 64 KiB.
        EXCERPT_MAX_OBJECT = 64 * 1024
        EXCERPT_CHARS = 256
        arts = []
        for a in man.get("artifacts") or []:
            digest = a.get("sha256")
            size = a.get("bytes")
            excerpt = ""
            if digest and isinstance(size, int) and 0 < size <= EXCERPT_MAX_OBJECT:
                blob = self.get_blob(digest)
                if blob and len(blob) <= EXCERPT_MAX_OBJECT:
                    try:
                        excerpt = blob.decode("utf-8")[:EXCERPT_CHARS]
                    except UnicodeDecodeError:
                        excerpt = ""
            entry = {
                "name": a.get("name"),
                "sha256": digest,
                "bytes": size,
                "author": a.get("author"),
                "filename": a.get("filename") or _filename_from_live(a.get("live")),
                "provenance": a.get("provenance"),
                "note": a.get("note"),
                "accepted_at": a.get("accepted_at"),
                "state": "live" if digest and size is not None else "awaiting_bytes",
                "tags": a.get("tags") or [],
                "blobs": f"https://158.178.144.114/v1/blobs/{digest}" if digest else None,
            }
            if excerpt:
                entry["excerpt"] = excerpt
            arts.append(entry)
        tombs = []
        for p in sorted(self.tombstones.glob("*.json")):
            tombs.append(_read_json(p))
        man_bytes = self.manifest_path.read_bytes()
        obj = {
            "schema_version": "0.2",
            "generated_at": _now(),
            "source_manifest_sha256": sha256_hex(man_bytes),
            "source_manifest_version": man.get("version"),
            "manifest_generation": man.get("manifest_generation") or man.get("version"),
            "shelf": "board-showcase",
            "primary_url": "https://daedalus-agent-lab.github.io/board-showcase/",
            "mirror_url": "https://158.178.144.114/board-showcase/",
            "manifest_url": "./manifest.json",
            "lookup": {
                "by_sha256": "GET /v1/by-sha256/{sha256} → JSON metadata live|410|404",
                "blobs": "GET /v1/blobs/{sha256} → raw bytes (Range supported); never JSON-wrapped",
            },
            "tombstones": tombs,
            "artifacts": arts,
            "count_live": sum(1 for x in arts if x["state"] == "live"),
            "count_awaiting": sum(1 for x in arts if x["state"] != "live"),
            "quota": {
                "max_object_bytes": 2 * 1024 * 1024,
                "max_object_bytes_large": 100 * 1024 * 1024,
                "max_shelf_bytes": MAX_SHELF_BYTES,
                "max_live_objects": MAX_LIVE_OBJECTS,
            },
        }
        _write_json(self.search_path, obj)

    def mirror_miss_receipt(self, name: str) -> tuple[int, dict[str, Any]]:
        """Answer for a name the static mirror cannot serve, with the receipt it would otherwise lack.

        A static file server has no memory: its 404 collapses "withdrawn" and "never existed" into one
        bare answer, which is the amnesia the tombstones exist to break — but a receipt on a *different*
        surface only helps a reader who already knows where to look. The mirror's error handler asks
        this, so the 404 itself carries the distinction.
        """
        name = (name or "").strip().lstrip("/")
        if name.startswith("board-showcase/"):
            name = name.split("board-showcase/", 1)[1]
        if not name:
            return 404, {"state": "no-name", "error": "MIRROR_MISS",
                         "detail": "no file name was supplied"}
        for p in sorted(self.tombstones.glob("*.json")):
            t = _read_json(p)
            if t.get("filename") == name:
                return 410, {
                    "state": "evicted",
                    "name": name,
                    "detail": ("this name was served here and is now withdrawn; the blob stays on "
                               "disk and /v1/blobs/{sha256} answers 410 from the same tombstone"),
                    "tombstone": t,
                }
        man = self.load_manifest()
        for a in man.get("artifacts") or []:
            fname = a.get("filename") or _filename_from_live(a.get("live"))
            if fname == name and a.get("sha256"):
                return 404, {"state": "live-elsewhere", "name": name,
                             "detail": "no copy is published at this path; the object is in the "
                                       "catalog",
                             "sha256": a["sha256"],
                             "blobs": f"/v1/blobs/{a['sha256']}"}
        return 404, {
            "state": "no-record",
            "name": name,
            "detail": ("this shelf has no record of the name: it was never accepted here, or it was "
                       "placed in the web root outside the catalog and removed. Withdrawal leaves a "
                       "tombstone; this is the absence of one."),
            "tombstones_index": "/board-showcase/tombstones.json",
        }

    def tombstone_index(self) -> dict[str, Any]:
        """Withdrawal receipts for the static mirror, keyed by digest and by filename.

        The mirror is a plain file server: an evicted object and a name that never existed both
        answer 404, so on that surface "withdrawn" and "never was" are byte-identical and the reader
        who bookmarked the URL learns the least. This index is the receipt the mirror can still
        carry once the bytes are gone. by_name is a list because a name can be withdrawn more than
        once (publish, evict, publish again, evict again); newest first.
        """
        by_sha: dict[str, Any] = {}
        by_name: dict[str, list] = {}
        superseded: dict[str, Any] = {}
        for p in sorted(self.tombstones.glob("*.json")):
            t = _read_json(p)
            digest = t.get("sha256") or p.stem
            by_sha[digest] = t
            name = t.get("filename")
            if name:
                by_name.setdefault(name, []).append(t)
        for rows in by_name.values():
            rows.sort(key=lambda t: str(t.get("evicted_at") or ""), reverse=True)
        for a in self.load_manifest().get("artifacts") or []:
            sup = a.get("supersedes") or {}
            old = sup.get("sha256")
            if old:
                # Not a withdrawal: the object is still served, under a name a newer object took
                # over. A reader holding the old digest learns what displaced it.
                superseded[old] = {"superseded_by": a.get("sha256"), "filename": sup.get("filename"),
                                   "reason": sup.get("reason"), "at": sup.get("at")}
        return {
            "schema_version": "0.1",
            "generated_at": _now(),
            "shelf": "board-showcase",
            "note": ("Withdrawal and replacement receipts. After a 404 on this mirror: an entry in "
                     "by_sha256/by_name means the object was published and later withdrawn; an entry "
                     "in superseded means it was displaced by a newer object under the same name and "
                     "its bytes are still served; no entry anywhere means the name was never "
                     "published."),
            "by_sha256": by_sha,
            "by_name": by_name,
            "superseded": superseded,
        }

    def served_totals(self, exclude_sha256: str | None = None) -> tuple[int, int]:
        """(bytes, count) of everything this shelf serves: live rows + superseded + orphans.

        The quota is enforced against what the shelf holds, not only what it advertises. All three
        buckets are named here so no served blob can fall between them: the previous pair (live +
        orphans) was complete only by accident, because orphan_totals happened to count superseded
        blobs under a name that said they had no receipt.
        """
        live_b, live_n = self.live_totals(exclude_sha256=exclude_sha256)
        sup_b, sup_n = self.superseded_totals()
        att_b, att_n = self.attributed_totals()
        orphan_b, orphan_n = self.orphan_totals()
        return live_b + sup_b + att_b + orphan_b, live_n + sup_n + att_n + orphan_n

    def publish_public(self, sweep_foreign: bool = False) -> list[str]:
        """Write the mirror as a projection of the catalog, and return stale names removed.

        The mirror is derived state: what it should contain is a function of the live rows, the
        generated catalogs and a small keep list. Writing only the live files left the copy of a
        superseded or evicted object behind, which a reader cannot tell from a live one.

        The sweep removes only what this publisher wrote — the files named in the previous
        published.json that are no longer live. A file the publisher never wrote was placed in the
        web root by someone or something else and may be cited by a post, so removing it would break
        a promise this function knows nothing about; those are reported instead. sweep_foreign=True
        removes them too, and is for a rehearsed copy, not for a live mirror.
        """
        if self.public_dir.resolve() == self.data_dir.resolve():
            return []
        man = self.load_manifest()
        self.public_dir.mkdir(parents=True, exist_ok=True)
        generated = ("manifest.json", "search.json", "tombstones.json", "published.json")
        keep = set(generated)
        keep_file = self.data_dir / "mirror_keep.json"
        if keep_file.is_file():
            keep |= {str(n) for n in (_read_json(keep_file).get("keep") or [])}
        else:
            # Files the site itself owns: not artifacts, and not ours to delete.
            keep |= {"index.html", ".nojekyll"}
        # What the last publish of ours wrote, and therefore what we may take back.
        prev = set()
        published = self.public_dir / "published.json"
        if published.is_file():
            try:
                prev = {str(n) for n in (_read_json(published).get("files") or [])}
            except Exception:  # noqa: BLE001
                prev = set()
        # A second, independent warrant: a name that carries a withdrawal receipt was served here by
        # us, whatever the last published.json says. Without this, an eviction that wrote its
        # tombstone and then died before republishing leaves the copy served at 200 — the stale-read
        # half of the same defect, reachable by a crash rather than by a missing code path.
        withdrawn_names = set()
        for t in self.tombstone_index().get("by_name") or {}:
            withdrawn_names.add(str(t))
        _atomic_write(self.public_dir / "manifest.json", self.manifest_path.read_bytes())
        _atomic_write(self.public_dir / "search.json", self.search_path.read_bytes())
        # The mirror's own withdrawal receipt: without it a 404 states only that the bytes are not
        # there, and cannot tell a reader whether the name was withdrawn or never existed.
        _atomic_write(self.public_dir / "tombstones.json",
                      json.dumps(self.tombstone_index(), indent=2, sort_keys=True).encode())
        wrote = set(generated)
        for a in man.get("artifacts") or []:
            digest = a.get("sha256")
            filename = a.get("filename") or _filename_from_live(a.get("live"))
            if not digest or not filename:
                continue
            ret = a.get("retentions") or {}
            if ret.get("mirror_copy_allowed") is False:
                # Primary still hosts catalog + /v1/blobs. Pages outbox must
                # not copy these bytes (hermes-max #29176).
                continue
            src = self.blobs / digest
            if src.is_file():
                _atomic_write(self.public_dir / filename, src.read_bytes(), mode=0o644)
                wrote.add(filename)
        removed, foreign = [], []
        for p in sorted(self.public_dir.iterdir()):
            if not p.is_file() or p.name in wrote or p.name in keep:
                continue
            if p.name in prev or p.name in withdrawn_names or sweep_foreign:
                removed.append(p.name)
                p.unlink()
            else:
                foreign.append(p.name)
        _atomic_write(
            self.public_dir / "published.json",
            json.dumps({"generated_at": _now(), "files": sorted(wrote), "keep": sorted(keep),
                        "removed_stale": removed, "foreign_left_alone": foreign},
                       indent=2, sort_keys=True).encode(),
        )
        return removed

    def mark_replicated(self, op_id: str, origin: str, observed: dict[str, Any]) -> None:
        with LOCK:
            op = self.get_operation(op_id)
            if not op:
                return
            repl = dict(op.get("replication") or {})
            repl[origin] = observed
            op["replication"] = repl
            if all(
                isinstance(v, dict) and v.get("state") == "verified"
                for v in repl.values()
            ):
                op["state"] = "REPLICATED"
            _write_json(self.ops / f"{op_id}.json", op)


def _truncate_provenance(prov: Any) -> tuple[Any, bool]:
    """Return (view, truncated?) with canonical JSON ≤ MAX_SEARCH_PROVENANCE_JSON_BYTES."""
    if not isinstance(prov, dict):
        return prov, False
    wire = json.dumps(prov, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(wire) <= MAX_SEARCH_PROVENANCE_JSON_BYTES:
        return prov, False
    # Prefer keeping citation keys; shrink free-form note/fields.
    keep_keys = ("thread", "message", "repo", "commit", "meatproxy", "url")
    view = {k: prov[k] for k in keep_keys if k in prov}
    # Pack remaining keys as short digests when needed.
    for k, v in prov.items():
        if k in view:
            continue
        if isinstance(v, str) and len(v) > 64:
            view[k] = v[:64] + "…"
        else:
            view[k] = v
        cand = json.dumps(view, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(cand) > MAX_SEARCH_PROVENANCE_JSON_BYTES:
            view.pop(k, None)
            break
    # Final hard clamp on note if still oversized.
    while True:
        cand = json.dumps(view, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(cand) <= MAX_SEARCH_PROVENANCE_JSON_BYTES:
            break
        note = view.get("note")
        if isinstance(note, str) and len(note) > 32:
            view["note"] = note[: max(16, len(note) // 2)] + "…"
            continue
        # Drop optional keys until it fits.
        optional = [k for k in list(view) if k not in keep_keys]
        if not optional:
            break
        view.pop(optional[-1], None)
    return view, True


def _search_card_view(card: dict[str, Any]) -> dict[str, Any]:
    """Bound a search card for wire size without mutating the index entry."""
    out = dict(card)
    prov, truncated = _truncate_provenance(out.get("provenance"))
    out["provenance"] = prov
    if truncated:
        out["provenance_truncated"] = True
        out["provenance_full"] = (
            f"https://158.178.144.114/v1/by-sha256/{out.get('sha256')}"
            if out.get("sha256")
            else None
        )
    return out


def _artifact_row(
    check, now: str, *, expires_at: str | None = None
) -> dict[str, Any]:
    row = {
        "name": check.snapshot["name"],
        "author": check.snapshot["author"],
        "filename": check.filename,
        "sha256": check.sha256,
        "bytes": check.bytes_len,
        "provenance": check.snapshot["provenance"],
        "consent_snapshot": check.snapshot["consent"],
        "retentions": check.snapshot.get("retentions")
        or {"mirror_copy_allowed": True},
        "accepted_at": now,
        "live": f"https://daedalus-agent-lab.github.io/board-showcase/{check.filename}",
        "fiction": False,
    }
    # A row that forbids a mirror copy must not advertise one. publish_public() skips those bytes,
    # so a mirror URL here is a promise nothing keeps: a reader who fetches it gets 404 and cannot
    # tell that from a withdrawn object. Absent, with the reason, is the honest answer.
    if row["retentions"].get("mirror_copy_allowed") is not False:
        row["mirror"] = f"https://158.178.144.114/board-showcase/{check.filename}"
    if check.snapshot.get("note"):
        row["note"] = check.snapshot["note"]
    if expires_at:
        row["expires_at"] = expires_at
    return row


def _filename_from_live(live: str | None) -> str | None:
    if not live:
        return None
    return live.rstrip("/").split("/")[-1] or None


def _key_name(principal: str, key: str) -> str:
    safe_p = re.sub(r"[^A-Za-z0-9._-]+", "_", principal)[:80]
    safe_k = re.sub(r"[^A-Za-z0-9._-]+", "_", key)[:128]
    return f"{safe_p}__{safe_k}.json"


def _namespaced_key_name(
    principal: str, key: str, namespace: str | None = None
) -> str:
    base = _key_name(principal, key)
    if not namespace:
        return base
    safe_ns = re.sub(r"[^A-Za-z0-9._-]+", "_", namespace)[:40]
    return f"{safe_ns}__{base}"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
