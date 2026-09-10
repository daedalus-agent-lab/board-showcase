"""Large-lane multipart upload: init → parts → commit.

Storage under SHELF_DATA:
  uploads/{upload_id}/meta.json
  uploads/{upload_id}/parts/{n}
  uploads/{upload_id}/terminal.json   (survives part deletion)
  keys/upload__{principal}__{key}.json  (distinct from artifact keys)

State machine: OPEN → COMMITTING → ACCEPTED | FAILED
               OPEN → EXPIRED (staging TTL 24h)
Quota reservation converts to accepted usage once, same durable txn as accept.
"""
from __future__ import annotations

import json
import math
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from shelf_lib import (
    DEFAULT_PART_SIZE,
    DEFAULT_TTL_SECONDS,
    MAX_OBJECT_BYTES,
    MAX_OBJECT_BYTES_LARGE,
    MAX_PART_SIZE,
    MIN_PART_SIZE,
    STAGING_TTL_SECONDS,
    check_upload_init,
    looks_like_secret,
    reject_body,
    sha256_hex,
)
from shelf_store import LOCK, ShelfStore, _atomic_write, _key_name, _read_json, _write_json

UPLOAD_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
PART_SHA_RE = re.compile(r"^[0-9a-f]{64}$")


def _upload_key_name(principal: str, key: str) -> str:
    """Distinct from artifact keys so (P,K) namespaces do not collide."""
    return "upload__" + _key_name(principal, key)


def _ceil_parts(nbytes: int, part_size: int) -> int:
    return max(1, int(math.ceil(nbytes / part_size)))


class UploadStore:
    """Multipart staging + commit into ShelfStore.accept."""

    def __init__(
        self,
        shelf: ShelfStore,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.shelf = shelf
        self.data_dir = shelf.data_dir
        self.uploads = self.data_dir / "uploads"
        self.keys = self.data_dir / "keys"
        self.uploads.mkdir(parents=True, exist_ok=True)
        self.keys.mkdir(parents=True, exist_ok=True)
        self._clock = clock or time.time

    def _now_iso(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._clock()))

    def _upload_dir(self, upload_id: str) -> Path:
        return self.uploads / upload_id

    def _meta_path(self, upload_id: str) -> Path:
        return self._upload_dir(upload_id) / "meta.json"

    def _terminal_path(self, upload_id: str) -> Path:
        return self._upload_dir(upload_id) / "terminal.json"

    def _parts_dir(self, upload_id: str) -> Path:
        return self._upload_dir(upload_id) / "parts"

    def _part_path(self, upload_id: str, n: int) -> Path:
        return self._parts_dir(upload_id) / str(n)

    def _part_meta_path(self, upload_id: str, n: int) -> Path:
        return self._parts_dir(upload_id) / f"{n}.json"

    def _load_meta(self, upload_id: str) -> dict[str, Any] | None:
        p = self._meta_path(upload_id)
        if p.is_file():
            return _read_json(p)
        return None

    def _load_terminal(self, upload_id: str) -> dict[str, Any] | None:
        p = self._terminal_path(upload_id)
        if p.is_file():
            return _read_json(p)
        return None

    def _write_meta(self, upload_id: str, meta: dict[str, Any]) -> None:
        _write_json(self._meta_path(upload_id), meta)

    def _write_terminal(self, upload_id: str, terminal: dict[str, Any]) -> None:
        ud = self._upload_dir(upload_id)
        ud.mkdir(parents=True, exist_ok=True)
        _write_json(self._terminal_path(upload_id), terminal)

    def lookup_upload_key(self, principal: str, key: str) -> dict[str, Any] | None:
        p = self.keys / _upload_key_name(principal, key)
        if p.is_file():
            return _read_json(p)
        return None

    def reserved_bytes(self, *, exclude_upload_id: str | None = None) -> int:
        """Sum of OPEN (non-expired) quota reservations."""
        total = 0
        if not self.uploads.is_dir():
            return 0
        now = self._clock()
        for d in self.uploads.iterdir():
            if not d.is_dir():
                continue
            if exclude_upload_id and d.name == exclude_upload_id:
                continue
            meta_p = d / "meta.json"
            if not meta_p.is_file():
                continue
            try:
                meta = _read_json(meta_p)
            except (OSError, json.JSONDecodeError):
                continue
            state = meta.get("state")
            if state != "OPEN":
                continue
            if float(meta.get("staging_expires_at_epoch") or 0) <= now:
                continue
            if meta.get("quota_released"):
                continue
            total += int(meta.get("bytes") or 0)
        return total

    def _expire_if_needed(self, meta: dict[str, Any]) -> dict[str, Any]:
        """OPEN past staging TTL → EXPIRED; release reservation once; keep terminal."""
        if meta.get("state") != "OPEN":
            return meta
        exp = float(meta.get("staging_expires_at_epoch") or 0)
        if exp > self._clock():
            return meta
        upload_id = meta["upload_id"]
        meta = dict(meta)
        meta["state"] = "EXPIRED"
        meta["expired_at"] = self._now_iso()
        meta["quota_released"] = True
        self._write_meta(upload_id, meta)
        terminal = {
            "upload_id": upload_id,
            "state": "EXPIRED",
            "operation_id": meta.get("operation_id"),
            "fingerprint": meta.get("fingerprint"),
            "principal": meta.get("principal"),
            "idempotency_key": meta.get("idempotency_key"),
            "sha256": meta.get("sha256"),
            "bytes": meta.get("bytes"),
            "expired_at": meta["expired_at"],
            "quota_released": True,
        }
        self._write_terminal(upload_id, terminal)
        # Drop parts but keep meta + terminal.
        parts = self._parts_dir(upload_id)
        if parts.is_dir():
            shutil.rmtree(parts, ignore_errors=True)
        return meta

    def init_upload(
        self,
        *,
        meta_body: dict[str, Any],
        principal: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """POST /v1/uploads — metadata only."""
        with LOCK:
            existing = self.lookup_upload_key(principal, idempotency_key)
            declared_sha = str(meta_body.get("sha256") or "").strip().lower()
            try:
                declared_bytes = int(meta_body.get("bytes") or 0)
            except (TypeError, ValueError):
                declared_bytes = 0
            name = str(meta_body.get("name") or "")
            filename = str(meta_body.get("filename") or "")
            author = str(meta_body.get("author") or "")
            consent = str(meta_body.get("consent") or "")
            provenance = (
                meta_body.get("provenance")
                if isinstance(meta_body.get("provenance"), dict)
                else {}
            )
            part_size_raw = meta_body.get("part_size", DEFAULT_PART_SIZE)
            try:
                part_size = int(part_size_raw)
            except (TypeError, ValueError):
                part_size = DEFAULT_PART_SIZE
            if part_size <= 0:
                part_size = DEFAULT_PART_SIZE
            ttl_raw = meta_body.get("ttl_seconds", DEFAULT_TTL_SECONDS)
            try:
                ttl_seconds = int(ttl_raw)
            except (TypeError, ValueError):
                ttl_seconds = DEFAULT_TTL_SECONDS
            if ttl_seconds <= 0:
                ttl_seconds = DEFAULT_TTL_SECONDS

            already = self.shelf.is_live(declared_sha)
            live_bytes, live_count = self.shelf.live_totals(
                exclude_sha256=declared_sha if already else None
            )
            reserved = self.reserved_bytes()

            note = (
                str(meta_body["note"])
                if meta_body.get("note") is not None
                else None
            )
            check = check_upload_init(
                declared_sha256=declared_sha,
                declared_bytes=declared_bytes,
                name=name,
                filename=filename,
                provenance=provenance,
                author=author,
                consent=consent,
                principal=principal,
                shelf_live_bytes=live_bytes,
                shelf_live_count=0 if already else live_count,
                idempotency_key=idempotency_key,
                part_size=part_size,
                reserved_bytes=reserved,
                note=note,
            )
            if not check.ok:
                body = reject_body(check)
                body["http"] = 422
                return body

            if existing:
                # Terminal / live upload binding for this (P,K).
                upload_id = existing.get("upload_id")
                prev_fp = existing.get("fingerprint")
                if prev_fp != check.fingerprint:
                    return {
                        "http": 409,
                        "error": "IDEMPOTENCY_CONFLICT",
                        "message": "same Idempotency-Key, different upload fingerprint",
                        "upload_id": upload_id,
                        "operation_id": existing.get("operation_id"),
                    }
                # Same fingerprint — replay. Prefer terminal, then meta.
                if upload_id:
                    terminal = self._load_terminal(upload_id)
                    meta = self._load_meta(upload_id)
                    if meta:
                        meta = self._expire_if_needed(meta)
                    state = (terminal or meta or {}).get("state")
                    if state == "EXPIRED":
                        return {
                            "http": 409,
                            "error": "UPLOAD_EXPIRED",
                            "message": (
                                "previous upload for this Idempotency-Key expired; "
                                "use a new key"
                            ),
                            "upload_id": upload_id,
                            "operation_id": (terminal or meta or {}).get("operation_id"),
                            "state": "EXPIRED",
                        }
                    if state == "ACCEPTED":
                        return {
                            "http": 200,
                            "upload_id": upload_id,
                            "operation_id": (terminal or meta or existing).get(
                                "operation_id"
                            ),
                            "part_size": (meta or existing).get("part_size"),
                            "parts": (meta or existing).get("parts"),
                            "put": f"/v1/uploads/{upload_id}/parts/{{n}}",
                            "commit": f"/v1/uploads/{upload_id}/commit",
                            "state": "ACCEPTED",
                            "replay": True,
                        }
                    if meta and meta.get("state") in ("OPEN", "COMMITTING"):
                        return {
                            "http": 200,
                            "upload_id": upload_id,
                            "operation_id": meta.get("operation_id"),
                            "part_size": meta.get("part_size"),
                            "parts": meta.get("parts"),
                            "put": f"/v1/uploads/{upload_id}/parts/{{n}}",
                            "commit": f"/v1/uploads/{upload_id}/commit",
                            "state": meta.get("state"),
                            "replay": True,
                        }
                    if state == "FAILED":
                        return {
                            "http": 409,
                            "error": "UPLOAD_FAILED",
                            "message": "previous upload for this key failed; use a new key",
                            "upload_id": upload_id,
                            "operation_id": (terminal or meta or {}).get("operation_id"),
                            "state": "FAILED",
                        }
                # Fall through only if key record is orphaned — recreate forbidden;
                # treat as conflict to avoid silent recreate.
                return {
                    "http": 409,
                    "error": "IDEMPOTENCY_CONFLICT",
                    "message": "upload key exists but staging record is incomplete",
                    "upload_id": upload_id,
                    "operation_id": existing.get("operation_id"),
                }

            # Fresh init.
            if not (MIN_PART_SIZE <= part_size <= MAX_PART_SIZE):
                return {
                    "http": 422,
                    "error": "REJECTED",
                    "reason": f"part_size: part_size {part_size} outside {MIN_PART_SIZE}..{MAX_PART_SIZE}",
                    "failures": [
                        {
                            "code": "part_size",
                            "message": f"part_size {part_size} outside {MIN_PART_SIZE}..{MAX_PART_SIZE}",
                        }
                    ],
                }

            upload_id = str(uuid.uuid4())
            op_id = str(uuid.uuid4())
            parts = _ceil_parts(check.bytes_len, part_size)
            now_epoch = self._clock()
            staging_exp = now_epoch + STAGING_TTL_SECONDS
            meta = {
                "upload_id": upload_id,
                "operation_id": op_id,
                "state": "OPEN",
                "created_at": self._now_iso(),
                "created_at_epoch": now_epoch,
                "staging_expires_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(staging_exp)
                ),
                "staging_expires_at_epoch": staging_exp,
                "principal": principal,
                "idempotency_key": idempotency_key,
                "fingerprint": check.fingerprint,
                "sha256": check.sha256,
                "bytes": check.bytes_len,
                "filename": check.filename,
                "name": check.snapshot["name"],
                "author": check.snapshot["author"],
                "provenance": check.snapshot["provenance"],
                "consent": check.snapshot["consent"],
                "part_size": part_size,
                "parts": parts,
                "ttl_seconds": ttl_seconds,
                "quota_reserved": True,
                "quota_released": False,
                "quota_converted": False,
            }
            ud = self._upload_dir(upload_id)
            ud.mkdir(parents=True, exist_ok=True)
            self._parts_dir(upload_id).mkdir(parents=True, exist_ok=True)
            self._write_meta(upload_id, meta)
            _write_json(
                self.keys / _upload_key_name(principal, idempotency_key),
                {
                    "upload_id": upload_id,
                    "operation_id": op_id,
                    "fingerprint": check.fingerprint,
                    "sha256": check.sha256,
                    "bytes": check.bytes_len,
                },
            )
            return {
                "http": 201,
                "upload_id": upload_id,
                "operation_id": op_id,
                "part_size": part_size,
                "parts": parts,
                "put": f"/v1/uploads/{upload_id}/parts/{{n}}",
                "commit": f"/v1/uploads/{upload_id}/commit",
                "state": "OPEN",
                "staging_expires_at": meta["staging_expires_at"],
            }

    def put_part(
        self,
        *,
        upload_id: str,
        n: int,
        body: bytes,
        declared_part_sha256: str | None = None,
    ) -> dict[str, Any]:
        """PUT /v1/uploads/{id}/parts/{n} — raw body."""
        if not UPLOAD_ID_RE.match(upload_id or ""):
            return {"http": 400, "error": "BAD_UPLOAD_ID"}
        if not isinstance(n, int) or n < 0:
            return {"http": 400, "error": "BAD_PART_INDEX"}

        with LOCK:
            meta = self._load_meta(upload_id)
            if not meta:
                terminal = self._load_terminal(upload_id)
                if terminal:
                    return {
                        "http": 409,
                        "error": "UPLOAD_NOT_OPEN",
                        "state": terminal.get("state"),
                        "upload_id": upload_id,
                    }
                return {"http": 404, "error": "NOT_FOUND", "upload_id": upload_id}

            meta = self._expire_if_needed(meta)
            if meta.get("state") != "OPEN":
                return {
                    "http": 409,
                    "error": "UPLOAD_NOT_OPEN",
                    "state": meta.get("state"),
                    "upload_id": upload_id,
                }

            parts_expected = int(meta["parts"])
            if n >= parts_expected:
                return {
                    "http": 400,
                    "error": "BAD_PART_INDEX",
                    "message": f"n must be 0..{parts_expected - 1}",
                }

            part_size = int(meta["part_size"])
            total = int(meta["bytes"])
            # Last part may be short; others must be exactly part_size.
            if n < parts_expected - 1:
                expected_len = part_size
            else:
                expected_len = total - part_size * (parts_expected - 1)
            if len(body) != expected_len:
                return {
                    "http": 400,
                    "error": "BAD_PART_SIZE",
                    "message": f"part {n} expected {expected_len} bytes, got {len(body)}",
                }

            digest = sha256_hex(body)
            if declared_part_sha256:
                d = declared_part_sha256.strip().lower()
                if not PART_SHA_RE.match(d):
                    return {"http": 400, "error": "BAD_PART_SHA256"}
                if d != digest:
                    return {
                        "http": 400,
                        "error": "PART_SHA256_MISMATCH",
                        "message": f"X-Part-Sha256={d} != actual={digest}",
                    }

            part_path = self._part_path(upload_id, n)
            part_meta_path = self._part_meta_path(upload_id, n)
            if part_path.is_file() and part_meta_path.is_file():
                prev = _read_json(part_meta_path)
                if prev.get("sha256") == digest and prev.get("bytes") == len(body):
                    # Identical re-PUT is free.
                    return {
                        "http": 200,
                        "n": n,
                        "bytes": len(body),
                        "sha256": digest,
                        "replay": True,
                    }
                # Different bytes → conflict (atomic winner already on disk).
                return {
                    "http": 409,
                    "error": "PART_CONFLICT",
                    "message": f"part {n} already stored with different bytes",
                    "n": n,
                }

            # Atomic write: bytes then meta (winner).
            self._parts_dir(upload_id).mkdir(parents=True, exist_ok=True)
            _atomic_write(part_path, body)
            _write_json(
                part_meta_path,
                {"n": n, "bytes": len(body), "sha256": digest, "stored_at": self._now_iso()},
            )
            return {"http": 200, "n": n, "bytes": len(body), "sha256": digest}

    def commit(self, *, upload_id: str) -> dict[str, Any]:
        """POST /v1/uploads/{id}/commit — assemble, verify, accept."""
        if not UPLOAD_ID_RE.match(upload_id or ""):
            return {"http": 400, "error": "BAD_UPLOAD_ID"}

        with LOCK:
            terminal = self._load_terminal(upload_id)
            meta = self._load_meta(upload_id)
            if terminal and terminal.get("state") == "ACCEPTED":
                op = self.shelf.get_operation(terminal["operation_id"])
                return {
                    "http": 200,
                    "receipt": op
                    or {
                        "operation_id": terminal["operation_id"],
                        "state": "ACCEPTED",
                        "sha256": terminal.get("sha256"),
                        "upload_id": upload_id,
                    },
                    "replay": True,
                }
            if not meta:
                if terminal:
                    return {
                        "http": 409,
                        "error": "UPLOAD_NOT_OPEN",
                        "state": terminal.get("state"),
                        "upload_id": upload_id,
                    }
                return {"http": 404, "error": "NOT_FOUND", "upload_id": upload_id}

            meta = self._expire_if_needed(meta)
            state = meta.get("state")
            if state == "ACCEPTED":
                op = self.shelf.get_operation(meta["operation_id"])
                return {
                    "http": 200,
                    "receipt": op,
                    "replay": True,
                }
            if state == "EXPIRED":
                return {
                    "http": 409,
                    "error": "UPLOAD_EXPIRED",
                    "state": "EXPIRED",
                    "upload_id": upload_id,
                }
            if state == "FAILED":
                return {
                    "http": 409,
                    "error": "UPLOAD_FAILED",
                    "state": "FAILED",
                    "upload_id": upload_id,
                    "message": meta.get("fail_reason"),
                }
            if state == "COMMITTING":
                # Another commit in progress / crashed mid-way: try to finish or report.
                return {
                    "http": 409,
                    "error": "UPLOAD_COMMITTING",
                    "state": "COMMITTING",
                    "upload_id": upload_id,
                    "message": "commit already in progress; retry shortly",
                }
            if state != "OPEN":
                return {
                    "http": 409,
                    "error": "UPLOAD_NOT_OPEN",
                    "state": state,
                    "upload_id": upload_id,
                }

            parts_expected = int(meta["parts"])
            missing = []
            for i in range(parts_expected):
                if not self._part_path(upload_id, i).is_file():
                    missing.append(i)
            if missing:
                return {
                    "http": 400,
                    "error": "PARTS_INCOMPLETE",
                    "message": f"missing parts: {missing[:20]}{'…' if len(missing) > 20 else ''}",
                    "missing": missing,
                }

            # Freeze against GC / expiry race.
            meta = dict(meta)
            meta["state"] = "COMMITTING"
            meta["committing_at"] = self._now_iso()
            self._write_meta(upload_id, meta)

            # Assemble outside the mental model of holding huge buffers when possible,
            # but 100 MiB is within process budget for this shelf.
            chunks: list[bytes] = []
            total = 0
            for i in range(parts_expected):
                chunk = self._part_path(upload_id, i).read_bytes()
                chunks.append(chunk)
                total += len(chunk)
            content = b"".join(chunks)
            del chunks

            expected_bytes = int(meta["bytes"])
            expected_sha = meta["sha256"]
            if total != expected_bytes or len(content) != expected_bytes:
                return self._fail_commit(
                    meta,
                    reason=f"assembled bytes {len(content)} != declared {expected_bytes}",
                )
            digest = sha256_hex(content)
            if digest != expected_sha:
                return self._fail_commit(
                    meta,
                    reason=f"assembled sha256 {digest} != declared {expected_sha}",
                )

            secret = looks_like_secret(content)
            if secret:
                return self._fail_commit(meta, reason=f"secrets: {secret.message}")

            # Re-run full small-lane-style accept checks but with large size allowed.
            # check_package caps at 2 MiB, so build CheckResult via large path + content.
            already = self.shelf.is_live(digest)
            live_bytes, live_count = self.shelf.live_totals(
                exclude_sha256=digest if already else None
            )
            # Reservation for THIS upload is about to convert — do not double-count it.
            reserved_others = self.reserved_bytes(exclude_upload_id=upload_id)
            from shelf_lib import _validate_common_meta  # local to avoid cycle noise

            check = _validate_common_meta(
                content=content,
                declared_sha256=expected_sha,
                declared_bytes=expected_bytes,
                name=meta["name"],
                filename=meta["filename"],
                provenance=meta["provenance"],
                author=meta["author"],
                consent=meta["consent"],
                principal=meta["principal"],
                shelf_live_bytes=live_bytes + reserved_others,
                shelf_live_count=0 if already else live_count,
                idempotency_key=meta["idempotency_key"],
                min_bytes=MAX_OBJECT_BYTES + 1,
                max_bytes=MAX_OBJECT_BYTES_LARGE,
                verify_hash_against_content=True,
            )
            if not check.ok:
                return self._fail_commit(meta, reason=check.reason_line())

            ttl_seconds = int(meta.get("ttl_seconds") or DEFAULT_TTL_SECONDS)
            # Convert reservation → accepted in same conceptual txn as accept.
            # Mark converted BEFORE accept so a crash after accept cannot double-release.
            meta["quota_converted"] = True
            meta["quota_released"] = True  # reservation no longer outstanding
            self._write_meta(upload_id, meta)

            result = self.shelf.accept(
                content=content,
                check=check,
                principal=meta["principal"],
                idempotency_key=meta["idempotency_key"],
                expires_at_epoch=self._clock() + ttl_seconds,
                ttl_seconds=ttl_seconds,
                key_namespace="upload-commit",
                hold_lock=True,
            )

            if result.get("http") not in (200, 201):
                # Roll back conversion flags? Reservation already consumed conceptually;
                # on reject after convert, release is fine (no accepted usage).
                meta["quota_converted"] = False
                # Do not re-open reservation after failed accept — mark failed.
                return self._fail_commit(
                    meta,
                    reason=result.get("message")
                    or result.get("error")
                    or "accept failed",
                    extra=result,
                )

            receipt = result.get("receipt") or {}
            # Prefer accept's operation_id for the durable receipt.
            accept_op = receipt.get("operation_id") or meta["operation_id"]
            meta["state"] = "ACCEPTED"
            meta["accepted_at"] = self._now_iso()
            meta["accept_operation_id"] = accept_op
            meta["expires_at"] = receipt.get("expires_at")
            self._write_meta(upload_id, meta)
            self._write_terminal(
                upload_id,
                {
                    "upload_id": upload_id,
                    "state": "ACCEPTED",
                    "operation_id": accept_op,
                    "init_operation_id": meta["operation_id"],
                    "fingerprint": meta["fingerprint"],
                    "principal": meta["principal"],
                    "idempotency_key": meta["idempotency_key"],
                    "sha256": digest,
                    "bytes": expected_bytes,
                    "accepted_at": meta["accepted_at"],
                    "expires_at": receipt.get("expires_at"),
                    "quota_converted": True,
                    "quota_released": True,
                },
            )
            # Staging parts may be dropped after accept (blob is in shelf.blobs).
            parts = self._parts_dir(upload_id)
            if parts.is_dir():
                shutil.rmtree(parts, ignore_errors=True)

            http = int(result.get("http") or 201)
            out = {"http": http, "receipt": receipt}
            if result.get("status") == "replay":
                out["replay"] = True
            return out

    def _fail_commit(
        self,
        meta: dict[str, Any],
        *,
        reason: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        upload_id = meta["upload_id"]
        meta = dict(meta)
        meta["state"] = "FAILED"
        meta["failed_at"] = self._now_iso()
        meta["fail_reason"] = reason
        # Release reservation once (if not already converted/released).
        if not meta.get("quota_released"):
            meta["quota_released"] = True
        self._write_meta(upload_id, meta)
        self._write_terminal(
            upload_id,
            {
                "upload_id": upload_id,
                "state": "FAILED",
                "operation_id": meta.get("operation_id"),
                "fingerprint": meta.get("fingerprint"),
                "principal": meta.get("principal"),
                "idempotency_key": meta.get("idempotency_key"),
                "sha256": meta.get("sha256"),
                "bytes": meta.get("bytes"),
                "failed_at": meta["failed_at"],
                "fail_reason": reason,
                "quota_released": True,
            },
        )
        # Drop parts; unreferenced assembled blob was never written to shelf.blobs.
        parts = self._parts_dir(upload_id)
        if parts.is_dir():
            shutil.rmtree(parts, ignore_errors=True)
        body: dict[str, Any] = {
            "http": 422,
            "error": "COMMIT_REJECTED",
            "reason": reason,
            "upload_id": upload_id,
            "state": "FAILED",
        }
        if extra:
            body["detail"] = {
                k: extra[k]
                for k in ("error", "message", "http", "status")
                if k in extra
            }
        return body
