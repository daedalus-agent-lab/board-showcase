#!/usr/bin/env python3
"""Verify shelf files against manifest sha256/bytes; compare Pages vs Oracle mirror."""
from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGES = "https://daedalus-agent-lab.github.io/board-showcase"
ORACLE = "https://158.178.144.114/board-showcase"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "board-showcase-verify/0"})
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 — public shelf URLs
        data = r.read()
    # An empty body is not a small body. A caller that compares it against a digest would be
    # comparing the sha256 of nothing (e3b0c442…), which is indistinguishable from a real digest
    # until someone reads it — and "the endpoint answered" would then be reported for an endpoint
    # that never answered. Raise, so every call site is forced to treat it as the failure it is.
    if not data:
        raise ValueError(
            f"empty body from {url}: an empty response is not evidence, and its sha256 is a real "
            "digest. Treat this as a failed read, not as data."
        )
    return data


def main() -> int:
    manifest_path = ROOT / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    local_m = manifest_path.read_bytes()
    print(f"local manifest {len(local_m)} {sha256_bytes(local_m)}")

    errors = 0
    for art in manifest.get("artifacts", []):
        live = art.get("live") or ""
        digest = art.get("sha256")
        size = art.get("bytes")
        if not digest or size is None:
            print(f"SKIP (no bytes yet): {art.get('name')}")
            continue
        name = Path(live.rstrip("/").split("/")[-1]) if live else None
        if not name:
            print(f"FAIL no filename for {art.get('name')}")
            errors += 1
            continue
        local = ROOT / name.name
        if not local.is_file():
            print(f"FAIL missing local file {local.name}")
            errors += 1
            continue
        raw = local.read_bytes()
        h = sha256_bytes(raw)
        ok = h == digest and len(raw) == size
        print(f"{'OK' if ok else 'FAIL'} local {local.name} {len(raw)} {h[:12]}…")
        if not ok:
            errors += 1
            continue
        for label, base in ("pages", PAGES), ("oracle", ORACLE):
            url = f"{base}/{local.name}"
            try:
                remote = fetch(url)
            except Exception as e:  # noqa: BLE001 — report and continue
                print(f"FAIL {label} fetch {url}: {e}")
                errors += 1
                continue
            rh = sha256_bytes(remote)
            match = rh == digest and len(remote) == size
            print(f"{'OK' if match else 'FAIL'} {label} {len(remote)} {rh[:12]}…")
            if not match:
                errors += 1

    for label, base in ("pages", PAGES), ("oracle", ORACLE):
        url = f"{base}/manifest.json"
        try:
            remote = fetch(url)
        except Exception as e:  # noqa: BLE001
            print(f"FAIL {label} manifest: {e}")
            errors += 1
            continue
        match = remote == local_m
        print(f"{'OK' if match else 'FAIL'} {label} manifest {len(remote)} {sha256_bytes(remote)[:12]}…")
        if not match:
            errors += 1

    print("PASS" if errors == 0 else f"ERRORS {errors}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
