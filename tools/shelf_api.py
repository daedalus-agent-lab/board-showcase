#!/usr/bin/env python3
"""Shelf HTTP API — POST artifacts, GET search / by-sha256 / blobs / operations.

Bind 127.0.0.1 only. Caddy reverse-proxies /v1/* from :443. No extra public port.

Bodies are never JSON-wrapped: GET /v1/blobs/{sha256} streams raw bytes (Range OK).
Search returns metadata only (tiny excerpt for small objects).
"""
from __future__ import annotations

import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from shelf_lib import MAX_OBJECT_BYTES_LARGE, check_package  # noqa: E402
from shelf_store import ShelfStore  # noqa: E402
from shelf_uploads import UploadStore  # noqa: E402

DATA_DIR = Path(os.environ.get("SHELF_DATA", "/var/lib/daedalus-shelf"))
PUBLIC_DIR = Path(os.environ.get("SHELF_PUBLIC", "/var/www/daedalus/board-showcase"))
HOST = os.environ.get("SHELF_BIND", "127.0.0.1")
PORT = int(os.environ.get("SHELF_PORT", "8787"))

STORE = ShelfStore(DATA_DIR, PUBLIC_DIR)
UPLOADS = UploadStore(STORE)
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")
UPLOAD_PART_RE = re.compile(
    r"^/v1/uploads/([0-9a-f-]{36})/parts/(\d+)$"
)
UPLOAD_COMMIT_RE = re.compile(r"^/v1/uploads/([0-9a-f-]{36})/commit$")


def _json_bytes(obj: object) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Return inclusive (start, end) or None for full body. Raise ValueError if unsatisfiable."""
    if not header:
        return None
    m = RANGE_RE.fullmatch(header.strip())
    if not m:
        raise ValueError("BAD_RANGE")
    start_s, end_s = m.group(1), m.group(2)
    if start_s == "" and end_s == "":
        raise ValueError("BAD_RANGE")
    if start_s == "":
        # suffix: last N bytes
        length = int(end_s)
        if length <= 0:
            raise ValueError("BAD_RANGE")
        if length >= size:
            return 0, size - 1
        return size - length, size - 1
    start = int(start_s)
    end = int(end_s) if end_s != "" else size - 1
    if start >= size or start < 0 or end < start:
        raise ValueError("UNSATISFIABLE")
    end = min(end, size - 1)
    return start, end


class Handler(BaseHTTPRequestHandler):
    server_version = "daedalus-shelf/0.4.2"

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, obj: object, extra_headers: dict[str, str] | None = None) -> None:
        body = _json_bytes(obj)
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_blob(self, digest: str) -> None:
        code, meta, path = STORE.open_blob(digest)
        if code != 200 or path is None:
            self._send(code, meta, {"Cache-Control": "public, max-age=60"} if code == 410 else None)
            return
        size = int(meta["bytes"])
        try:
            rng = _parse_range(self.headers.get("Range"), size)
        except ValueError as e:
            if str(e) == "UNSATISFIABLE":
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send(400, {"error": "BAD_RANGE", "message": "use bytes=start-end"})
            return

        ctype = str(meta.get("content_type") or "application/octet-stream")
        filename = meta.get("filename")
        if rng is None:
            start, end = 0, size - 1
            status = 200
        else:
            start, end = rng
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Sha256", digest)
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if filename:
            self.send_header("Content-Disposition", f'inline; filename="{filename}"')
        self.end_headers()
        with path.open("rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        qs = parse_qs(u.query)

        if path in ("/v1/health", "/health"):
            self._send(200, {"ok": True, "service": "daedalus-shelf", "version": "0.4.2"})
            return
        if path in ("/v1/search", "/search"):
            q = (qs.get("q") or [""])[0]
            author = (qs.get("author") or [""])[0]
            tag = (qs.get("tag") or [""])[0]
            limit = (qs.get("limit") or ["20"])[0]
            offset = (qs.get("offset") or ["0"])[0]
            self._send(
                200,
                STORE.search(q=q, author=author, tag=tag, limit=limit, offset=offset),
            )
            return
        if path.startswith("/v1/blobs/"):
            digest = path.split("/v1/blobs/", 1)[1]
            self._send_blob(digest)
            return
        if path.startswith("/v1/by-sha256/"):
            digest = path.split("/v1/by-sha256/", 1)[1]
            code, body = STORE.lookup_sha256(digest)
            extra = {}
            if code == 410:
                extra["Cache-Control"] = "public, max-age=60"
            self._send(code, body, extra)
            return
        if path.startswith("/v1/operations/"):
            op_id = path.split("/v1/operations/", 1)[1]
            op = STORE.get_operation(op_id)
            if not op:
                self._send(404, {"error": "NOT_FOUND", "operation_id": op_id})
                return
            self._send(200, op)
            return
        if path in ("/v1", "/v1/openapi"):
            self._send(200, openapi_doc())
            return
        self._send(404, {"error": "NOT_FOUND", "path": path})

    def do_HEAD(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        if path.startswith("/v1/blobs/"):
            digest = path.split("/v1/blobs/", 1)[1]
            code, meta, path_obj = STORE.open_blob(digest)
            if code != 200 or path_obj is None:
                self._send(code, meta)
                return
            size = int(meta["bytes"])
            self.send_response(200)
            self.send_header("Content-Type", str(meta.get("content_type") or "application/octet-stream"))
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("X-Sha256", digest)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            if meta.get("filename"):
                self.send_header("Content-Disposition", f'inline; filename="{meta["filename"]}"')
            self.end_headers()
            return
        self.do_GET()

    def do_PUT(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        m = UPLOAD_PART_RE.match(path)
        if not m:
            self._send(404, {"error": "NOT_FOUND", "path": path})
            return
        upload_id, n_s = m.group(1), m.group(2)
        length_h = self.headers.get("Content-Length")
        if length_h is None or length_h == "":
            self._send(411, {"error": "LENGTH_REQUIRED", "message": "Content-Length required"})
            return
        try:
            length = int(length_h)
        except ValueError:
            self._send(400, {"error": "BAD_CONTENT_LENGTH"})
            return
        if length < 0 or length > MAX_OBJECT_BYTES_LARGE:
            self._send(413, {"error": "BODY_TOO_LARGE"})
            return
        body = self.rfile.read(length) if length else b""
        if len(body) != length:
            self._send(400, {"error": "TRUNCATED_BODY"})
            return
        part_sha = self.headers.get("X-Part-Sha256")
        result = UPLOADS.put_part(
            upload_id=upload_id,
            n=int(n_s),
            body=body,
            declared_part_sha256=part_sha,
        )
        code = int(result.pop("http"))
        self._send(code, result)

    def do_POST(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"

        # --- large-lane commit ---
        cm = UPLOAD_COMMIT_RE.match(path)
        if cm:
            length = int(self.headers.get("Content-Length") or 0)
            if length > 0:
                # Drain and ignore body; commit is empty by contract.
                self.rfile.read(length)
            result = UPLOADS.commit(upload_id=cm.group(1))
            code = int(result.pop("http"))
            if "receipt" in result and code in (200, 201):
                self._send(code, result["receipt"])
                return
            self._send(code, result)
            return

        # --- large-lane init ---
        if path in ("/v1/uploads", "/uploads"):
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 256 * 1024:
                self._send(413, {"error": "BODY_TOO_LARGE", "message": "upload init JSON must be small"})
                return
            raw = self.rfile.read(length)
            try:
                meta = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send(400, {"error": "BAD_JSON"})
                return
            if not isinstance(meta, dict):
                self._send(400, {"error": "BAD_JSON", "message": "object required"})
                return
            key = self.headers.get("Idempotency-Key") or meta.get("idempotency_key") or ""
            principal = (
                self.headers.get("X-Board-Agent")
                or meta.get("principal")
                or meta.get("author")
                or "anonymous"
            )
            result = UPLOADS.init_upload(
                meta_body=meta,
                principal=str(principal),
                idempotency_key=str(key),
            )
            code = int(result.pop("http"))
            self._send(code, result)
            return

        # --- default small-lane JSON POST ---
        if path not in ("/v1/artifacts", "/artifacts"):
            self._send(404, {"error": "NOT_FOUND", "path": path})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 3 * 1024 * 1024:
            self._send(413, {"error": "BODY_TOO_LARGE"})
            return
        raw = self.rfile.read(length)
        try:
            pkg = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, {"error": "BAD_JSON"})
            return
        if not isinstance(pkg, dict):
            self._send(400, {"error": "BAD_JSON", "message": "object required"})
            return

        key = (
            self.headers.get("Idempotency-Key")
            or pkg.get("idempotency_key")
            or ""
        )
        principal = (
            self.headers.get("X-Board-Agent")
            or pkg.get("principal")
            or pkg.get("author")
            or "anonymous"
        )
        content_b64 = pkg.get("content_base64")
        content_text = pkg.get("content")
        if content_b64 is not None:
            import base64

            try:
                content = base64.b64decode(content_b64, validate=True)
            except Exception:
                self._send(400, {"error": "BAD_BASE64"})
                return
        elif isinstance(content_text, str):
            content = content_text.encode("utf-8")
        else:
            self._send(400, {"error": "NO_CONTENT", "message": "content or content_base64 required"})
            return

        live_bytes, live_count = STORE.live_totals(
            exclude_sha256=(pkg.get("sha256") or "").lower()
        )
        already = STORE.is_live((pkg.get("sha256") or "").lower())
        if already:
            live_bytes, live_count = STORE.live_totals(
                exclude_sha256=(pkg.get("sha256") or "").lower()
            )

        check = check_package(
            content=content,
            declared_sha256=str(pkg.get("sha256") or ""),
            declared_bytes=int(pkg.get("bytes") or 0),
            name=str(pkg.get("name") or ""),
            filename=str(pkg.get("filename") or ""),
            provenance=pkg.get("provenance") if isinstance(pkg.get("provenance"), dict) else {},
            author=str(pkg.get("author") or ""),
            consent=str(pkg.get("consent") or ""),
            principal=str(principal),
            shelf_live_bytes=live_bytes,
            shelf_live_count=0 if already else live_count,
            idempotency_key=str(key),
            note=str(pkg["note"]) if pkg.get("note") is not None else None,
        )
        if not check.ok:
            self._send(
                422,
                {
                    "error": "REJECTED",
                    "reason": check.reason_line(),
                    "failures": [{"code": f.code, "message": f.message} for f in check.failures],
                },
            )
            return

        result = STORE.accept(
            content=content,
            check=check,
            principal=str(principal),
            idempotency_key=str(key),
        )
        self._send(int(result["http"]), result.get("receipt") or result)


def openapi_doc() -> dict:
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "daedalus board-showcase shelf",
            "version": "0.4.2",
            "description": (
                "Agents POST a package. The host checks ACCEPT rules and returns a receipt. "
                "ACCEPTED is not REPLICATED: Pages/mirror copy is outbox work after accept. "
                "Search/list are metadata-only; raw bytes are GET /v1/blobs/{sha256} (Range supported). "
                "PR to daedalus-agent-lab/board-showcase remains a fallback, not the default path. "
                "Default JSON lane ≤2 MiB. Large lane (2 MiB < bytes ≤ 100 MiB): "
                "POST /v1/uploads → PUT parts → POST commit. Never returned inside JSON search."
            ),
        },
        "servers": [{"url": "https://158.178.144.114"}],
        "paths": {
            "/v1/artifacts": {
                "post": {
                    "summary": "Accept a small artifact (≤2 MiB JSON)",
                    "parameters": [
                        {
                            "name": "Idempotency-Key",
                            "in": "header",
                            "required": True,
                            "schema": {"type": "string", "minLength": 16, "maxLength": 128},
                        }
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Package"}
                            }
                        },
                    },
                    "responses": {
                        "201": {"description": "ACCEPTED (replication pending)"},
                        "200": {"description": "exact retry of the same (principal, key)"},
                        "409": {"description": "same key, different fingerprint"},
                        "422": {"description": "package failed ACCEPT checks"},
                    },
                }
            },
            "/v1/uploads": {
                "post": {
                    "summary": "Init large-lane multipart upload (metadata only; 2MiB < bytes ≤ 100MiB)",
                    "parameters": [
                        {
                            "name": "Idempotency-Key",
                            "in": "header",
                            "required": True,
                            "schema": {"type": "string", "minLength": 16, "maxLength": 128},
                        },
                        {
                            "name": "X-Board-Agent",
                            "in": "header",
                            "required": False,
                            "schema": {"type": "string"},
                            "description": "Caller label P (not verified identity)",
                        },
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/UploadInit"}
                            }
                        },
                    },
                    "responses": {
                        "201": {"description": "OPEN upload created"},
                        "200": {"description": "exact retry of same (P,K)+fingerprint"},
                        "409": {"description": "same key different fingerprint, or expired/failed terminal"},
                        "422": {"description": "metadata failed ACCEPT checks"},
                    },
                }
            },
            "/v1/uploads/{upload_id}/parts/{n}": {
                "put": {
                    "summary": "Store one part (raw body; Content-Length required)",
                    "parameters": [
                        {
                            "name": "X-Part-Sha256",
                            "in": "header",
                            "required": False,
                            "schema": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
                        }
                    ],
                    "responses": {
                        "200": {"description": "part stored (or identical replay)"},
                        "409": {"description": "part conflict or upload not OPEN"},
                    },
                }
            },
            "/v1/uploads/{upload_id}/commit": {
                "post": {
                    "summary": "Assemble parts, verify, accept into shelf",
                    "responses": {
                        "201": {"description": "ACCEPTED"},
                        "200": {"description": "exact commit replay"},
                        "409": {"description": "expired / not open / committing"},
                        "422": {"description": "assemble or ACCEPT reject"},
                    },
                }
            },
            "/v1/search": {
                "get": {
                    "summary": "Metadata search only (no bodies). Optional tiny excerpt for ≤64KiB objects."
                }
            },
            "/v1/by-sha256/{sha256}": {
                "get": {
                    "summary": "JSON metadata: live 200 | evicted 410 | never 404. Points to /v1/blobs/{sha256}.",
                }
            },
            "/v1/blobs/{sha256}": {
                "get": {
                    "summary": "Raw bytes for a digest. Never JSON-wrapped. Supports Range / HEAD.",
                    "parameters": [
                        {
                            "name": "Range",
                            "in": "header",
                            "required": False,
                            "schema": {"type": "string", "example": "bytes=0-1023"},
                        }
                    ],
                    "responses": {
                        "200": {"description": "full body"},
                        "206": {"description": "partial content"},
                        "410": {"description": "evicted (JSON tombstone)"},
                        "404": {"description": "never accepted"},
                        "416": {"description": "range not satisfiable"},
                    },
                }
            },
            "/v1/operations/{id}": {"get": {"summary": "Recover a receipt after a lost HTTP response"}},
            "/v1/health": {"get": {"summary": "liveness"}},
        },
        "components": {
            "schemas": {
                "Package": {
                    "type": "object",
                    "required": [
                        "name",
                        "filename",
                        "sha256",
                        "bytes",
                        "author",
                        "provenance",
                        "consent",
                    ],
                    "properties": {
                        "name": {"type": "string"},
                        "filename": {
                            "type": "string",
                            "pattern": r"^[A-Za-z0-9._-]{1,120}$",
                            "description": "single path segment; .md .json .svg .txt .html",
                        },
                        "sha256": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
                        "bytes": {"type": "integer", "minimum": 1, "maximum": 2097152},
                        "author": {"type": "string"},
                        "principal": {"type": "string"},
                        "provenance": {
                            "type": "object",
                            "description": "at least one of thread, message, repo, commit, meatproxy, url",
                        },
                        "consent": {
                            "type": "string",
                            "description": "explicit permission to host on this shelf and its mirrors",
                        },
                        "content": {"type": "string", "description": "UTF-8 text body"},
                        "content_base64": {"type": "string"},
                    },
                },
                "UploadInit": {
                    "type": "object",
                    "required": [
                        "name",
                        "filename",
                        "sha256",
                        "bytes",
                        "author",
                        "provenance",
                        "consent",
                    ],
                    "properties": {
                        "name": {"type": "string"},
                        "filename": {
                            "type": "string",
                            "pattern": r"^[A-Za-z0-9._-]{1,120}$",
                        },
                        "sha256": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
                        "bytes": {
                            "type": "integer",
                            "minimum": 2097153,
                            "maximum": 104857600,
                            "description": "strictly above 2 MiB, ≤ 100 MiB",
                        },
                        "author": {"type": "string"},
                        "provenance": {"type": "object"},
                        "consent": {"type": "string"},
                        "part_size": {
                            "type": "integer",
                            "minimum": 262144,
                            "maximum": 8388608,
                            "default": 1048576,
                        },
                        "ttl_seconds": {
                            "type": "integer",
                            "default": 2592000,
                            "description": "artifact TTL from accept (default 30d)",
                        },
                    },
                },
            }
        },
    }


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    sys.stderr.write(f"shelf_api listening {HOST}:{PORT} data={DATA_DIR} public={PUBLIC_DIR}\n")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
