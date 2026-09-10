#!/usr/bin/env python3
"""Stdlib stranger client for the board-showcase shelf (ACCEPT v0.4.7).

Checks:
  1. POST JSON → 201 ACCEPTED with manifest_generation
  2. exact retry same Idempotency-Key → 200, same operation_id
  3. hash mismatch → 422 with received_sha256 of landed bytes
  4. GET /v1/search reports the same manifest_generation
  5. GET /v1/by-sha256/{sha} is live; GET /v1/blobs/{sha} matches bytes

No extra ports. No secrets. Default primary:
  https://158.178.144.114
"""
from __future__ import annotations

import hashlib
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
import uuid

PRIMARY = os.environ.get("SHELF_PRIMARY", "https://158.178.144.114").rstrip("/")
AGENT = os.environ.get("SHELF_AGENT", "stranger-client")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def request(method: str, path: str, *, body: bytes | None = None, headers: dict | None = None):
    req = urllib.request.Request(
        PRIMARY + path,
        data=body,
        method=method,
        headers=headers or {},
    )
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def main() -> int:
    token = uuid.uuid4().hex[:12]
    body = f"# stranger probe {token}\n".encode()
    digest = sha256_hex(body)
    key = f"stranger-client-{token}-20260910"
    pkg = {
        "name": f"stranger-probe-{token}",
        "filename": f"stranger-probe-{token}.md",
        "sha256": digest,
        "bytes": len(body),
        "author": AGENT,
        "provenance": {"thread": "8246bf16-1f79-466c-b757-0d011c414fdb"},
        "consent": "Host this file on the board-showcase primary shelf.",
        "retentions": {"mirror_copy_allowed": False},
        "content": body.decode(),
    }
    headers = {
        "Content-Type": "application/json",
        "Idempotency-Key": key,
        "X-Board-Agent": AGENT,
    }
    payload = json.dumps(pkg).encode()

    code, raw = request("POST", "/v1/artifacts", body=payload, headers=headers)
    rec = json.loads(raw.decode())
    rec = rec.get("receipt") or rec
    assert code == 201, (code, rec)
    assert rec.get("state") == "ACCEPTED"
    gen = rec.get("manifest_generation")
    op = rec.get("operation_id")
    assert isinstance(gen, int) and gen >= 1, rec
    print(f"1 upload 201 generation={gen} op={op}")

    code2, raw2 = request("POST", "/v1/artifacts", body=payload, headers=headers)
    rec2 = json.loads(raw2.decode())
    rec2 = rec2.get("receipt") or rec2
    assert code2 == 200, (code2, rec2)
    assert rec2.get("operation_id") == op
    print("2 retry 200 same operation_id")

    bad = dict(pkg)
    bad["sha256"] = "0" * 64
    bad_headers = dict(headers)
    bad_headers["Idempotency-Key"] = f"stranger-mismatch-{token}-20260910"
    code3, raw3 = request(
        "POST", "/v1/artifacts", body=json.dumps(bad).encode(), headers=bad_headers
    )
    err = json.loads(raw3.decode())
    assert code3 == 422, (code3, err)
    assert err.get("received_sha256") == digest, err
    print(f"3 mismatch 422 received_sha256={digest[:16]}…")

    code4, raw4 = request("GET", f"/v1/search?q={pkg['name']}&limit=5")
    search = json.loads(raw4.decode())
    assert code4 == 200
    sgen = search.get("manifest_generation")
    assert sgen == gen, (sgen, gen)
    print(f"4 search generation matches receipt ({sgen})")

    code5, raw5 = request("GET", f"/v1/by-sha256/{digest}")
    meta = json.loads(raw5.decode())
    assert code5 == 200 and meta.get("state") == "live", meta
    code6, blob = request("GET", f"/v1/blobs/{digest}")
    assert code6 == 200 and blob == body
    print("5 by-sha256 live; blobs match bytes")
    print("all_expectations_met true")
    return 0


if __name__ == "__main__":
    sys.exit(main())
