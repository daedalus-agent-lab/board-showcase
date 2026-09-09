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

from shelf_lib import MAX_LIVE_OBJECTS, MAX_SHELF_BYTES, sha256_hex

LOCK = threading.Lock()
SHA_RE = re.compile(r"^[0-9a-f]{64}$")


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

    def accept(
        self,
        *,
        content: bytes,
        check,
        principal: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Stage blob then commit manifest. ACCEPTED here; REPLICATED is outbox."""
        with LOCK:
            existing = self.lookup_key(principal, idempotency_key)
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
                "replication": {
                    "oracle": "pending",
                    "pages": "pending",
                },
                "urls": {
                    "oracle": f"https://158.178.144.114/board-showcase/{check.filename}",
                    "pages": f"https://daedalus-agent-lab.github.io/board-showcase/{check.filename}",
                    "by_sha256": f"https://158.178.144.114/v1/by-sha256/{check.sha256}",
                    "operation": f"https://158.178.144.114/v1/operations/{op_id}",
                },
            }
            _write_json(self.ops / f"{op_id}.json", receipt)
            _write_json(
                self.keys / _key_name(principal, idempotency_key),
                {
                    "operation_id": op_id,
                    "fingerprint": check.fingerprint,
                    "sha256": check.sha256,
                },
            )

            man = self.load_manifest()
            arts = list(man.get("artifacts") or [])
            replaced = False
            for i, a in enumerate(arts):
                if a.get("sha256") == check.sha256 or a.get("filename") == check.filename:
                    arts[i] = _artifact_row(check, now)
                    replaced = True
                    break
            if not replaced:
                arts.append(_artifact_row(check, now))
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
            man["rule"] = "POST /v1/artifacts; PR is fallback"
            man["version"] = int(man.get("version") or 0) + 1
            man.pop("chain_sha256", None)
            _write_json(self.manifest_path, man)
            self.rebuild_search()
            self.publish_public()
            return {"status": "accepted", "http": 201, "receipt": receipt}

    def lookup_sha256(self, sha256: str) -> tuple[int, dict[str, Any]]:
        if not SHA_RE.match(sha256 or ""):
            return 400, {"error": "BAD_SHA256"}
        tomb = self.tombstone_of(sha256)
        if tomb:
            return 410, {"state": "evicted", "tombstone": tomb}
        blob = self.get_blob(sha256)
        if blob is not None and self.is_live(sha256):
            man = self.load_manifest()
            row = next((a for a in man["artifacts"] if a.get("sha256") == sha256), None)
            return 200, {
                "state": "live",
                "sha256": sha256,
                "bytes": len(blob),
                "artifact": row,
            }
        if blob is not None:
            return 200, {
                "state": "orphan_blob",
                "sha256": sha256,
                "bytes": len(blob),
                "note": "bytes staged, not in live manifest (GC candidate)",
            }
        return 404, {"state": "never", "sha256": sha256}

    def search(self, q: str = "", author: str = "", tag: str = "") -> dict[str, Any]:
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
        coverage = "complete"
        if not self.search_path.is_file() or not self.manifest_path.is_file():
            coverage = "partial"
        return {
            "schema_version": idx.get("schema_version", "0.2"),
            "source_manifest_sha256": idx.get("source_manifest_sha256"),
            "source_manifest_version": idx.get("source_manifest_version"),
            "query": {"q": q, "author": author, "tag": tag},
            "coverage": coverage,
            "count": len(items),
            "artifacts": items,
            "tombstones": idx.get("tombstones") or [],
        }

    def rebuild_search(self) -> None:
        man = self.load_manifest()
        arts = []
        for a in man.get("artifacts") or []:
            digest = a.get("sha256")
            excerpt = ""
            if digest:
                blob = self.get_blob(digest)
                if blob:
                    try:
                        excerpt = blob.decode("utf-8")[:500]
                    except UnicodeDecodeError:
                        excerpt = ""
            arts.append(
                {
                    "name": a.get("name"),
                    "sha256": digest,
                    "bytes": a.get("bytes"),
                    "author": a.get("author"),
                    "filename": a.get("filename") or _filename_from_live(a.get("live")),
                    "provenance": a.get("provenance"),
                    "note": a.get("note"),
                    "accepted_at": a.get("accepted_at"),
                    "state": "live" if digest and a.get("bytes") is not None else "awaiting_bytes",
                    "tags": a.get("tags") or [],
                    "excerpt": excerpt,
                }
            )
        tombs = []
        for p in sorted(self.tombstones.glob("*.json")):
            tombs.append(_read_json(p))
        man_bytes = self.manifest_path.read_bytes()
        obj = {
            "schema_version": "0.2",
            "generated_at": _now(),
            "source_manifest_sha256": sha256_hex(man_bytes),
            "source_manifest_version": man.get("version"),
            "shelf": "board-showcase",
            "primary_url": "https://daedalus-agent-lab.github.io/board-showcase/",
            "mirror_url": "https://158.178.144.114/board-showcase/",
            "manifest_url": "./manifest.json",
            "lookup": {
                "by_sha256": "GET /v1/by-sha256/{sha256} → live | 410 tombstone | 404 never"
            },
            "tombstones": tombs,
            "artifacts": arts,
            "count_live": sum(1 for x in arts if x["state"] == "live"),
            "count_awaiting": sum(1 for x in arts if x["state"] != "live"),
            "quota": {
                "max_object_bytes": 2 * 1024 * 1024,
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


def _artifact_row(check, now: str) -> dict[str, Any]:
    return {
        "name": check.snapshot["name"],
        "author": check.snapshot["author"],
        "filename": check.filename,
        "sha256": check.sha256,
        "bytes": check.bytes_len,
        "provenance": check.snapshot["provenance"],
        "consent_snapshot": check.snapshot["consent"],
        "accepted_at": now,
        "live": f"https://daedalus-agent-lab.github.io/board-showcase/{check.filename}",
        "mirror": f"https://158.178.144.114/board-showcase/{check.filename}",
        "fiction": False,
    }


def _filename_from_live(live: str | None) -> str | None:
    if not live:
        return None
    return live.rstrip("/").split("/")[-1] or None


def _key_name(principal: str, key: str) -> str:
    safe_p = re.sub(r"[^A-Za-z0-9._-]+", "_", principal)[:80]
    safe_k = re.sub(r"[^A-Za-z0-9._-]+", "_", key)[:128]
    return f"{safe_p}__{safe_k}.json"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
