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
            return 200, {
                "state": "orphan_blob",
                "sha256": sha256,
                "bytes": len(blob),
                "blobs": f"https://158.178.144.114/v1/blobs/{sha256}",
                "note": "bytes staged, not in live manifest (GC candidate)",
            }
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
        return (
            200,
            {
                "state": "live" if self.is_live(sha256) else "orphan_blob",
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

    def publish_public(self) -> None:
        """Copy live blobs + catalog into the static web root (Oracle file_server)."""
        if self.public_dir.resolve() == self.data_dir.resolve():
            return
        man = self.load_manifest()
        self.public_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.public_dir / "manifest.json", self.manifest_path.read_bytes())
        _atomic_write(self.public_dir / "search.json", self.search_path.read_bytes())
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
        "mirror": f"https://158.178.144.114/board-showcase/{check.filename}",
        "fiction": False,
    }
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
