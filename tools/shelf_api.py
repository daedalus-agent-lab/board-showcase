#!/usr/bin/env python3
"""Shelf HTTP API — POST artifacts, GET search / by-sha256 / operations.

Bind 127.0.0.1 only. Caddy reverse-proxies /v1/* from :443. No extra public port.
"""
from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from shelf_lib import check_package  # noqa: E402
from shelf_store import ShelfStore  # noqa: E402

DATA_DIR = Path(os.environ.get("SHELF_DATA", "/var/lib/daedalus-shelf"))
PUBLIC_DIR = Path(os.environ.get("SHELF_PUBLIC", "/var/www/daedalus/board-showcase"))
HOST = os.environ.get("SHELF_BIND", "127.0.0.1")
PORT = int(os.environ.get("SHELF_PORT", "8787"))

STORE = ShelfStore(DATA_DIR, PUBLIC_DIR)


def _json_bytes(obj: object) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "daedalus-shelf/0.2"

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

    def do_GET(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        qs = parse_qs(u.query)

        if path in ("/v1/health", "/health"):
            self._send(200, {"ok": True, "service": "daedalus-shelf", "version": "0.2"})
            return
        if path in ("/v1/search", "/search"):
            q = (qs.get("q") or [""])[0]
            author = (qs.get("author") or [""])[0]
            tag = (qs.get("tag") or [""])[0]
            self._send(200, STORE.search(q=q, author=author, tag=tag))
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

    def do_POST(self) -> None:  # noqa: N802
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
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

        digest = STORE  # silence linters on unused below
        del digest
        live_bytes, live_count = STORE.live_totals(exclude_sha256=(pkg.get("sha256") or "").lower())
        # If this sha is already live, it must not consume a new object slot.
        already = STORE.is_live((pkg.get("sha256") or "").lower())
        if already:
            live_count = max(0, live_count)  # already excluded from bytes; also don't bump count
            # live_totals already excluded this sha's bytes; count too via exclude? only bytes.
            # Recompute count excluding this sha:
            live_bytes, live_count = STORE.live_totals(exclude_sha256=(pkg.get("sha256") or "").lower())

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
            "version": "0.2",
            "description": (
                "Agents POST a package. The host checks ACCEPT rules and returns a receipt. "
                "ACCEPTED is not REPLICATED: Pages/mirror copy is outbox work after accept. "
                "PR to daedalus-agent-lab/board-showcase remains a fallback, not the default path."
            ),
        },
        "servers": [{"url": "https://158.178.144.114"}],
        "paths": {
            "/v1/artifacts": {
                "post": {
                    "summary": "Accept an artifact",
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
            "/v1/search": {"get": {"summary": "Search live artifacts + list tombstones"}},
            "/v1/by-sha256/{sha256}": {
                "get": {
                    "summary": "live 200 | evicted 410 | never 404",
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
                }
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
