#!/usr/bin/env python3
"""Build the next mirror manifest for board-showcase, and the history that makes its chain walkable.

WHY THIS EXISTS

`manifest.json` in this repository is a diff-chain: each revision carries `previous_sha256`, the
digest of the revision before it. The rule was "sha256 of the last manifest published by the host".
That rule cannot be checked by anyone who holds only the mirror: the host publishes a manifest for
every accepted object, the mirror receives only some of them, so an anchor can name bytes that were
never published here. Measured on 2026-09-11: v42 declared `previous_sha256 = fd91053a…`, and no
revision in this repository's history hashes to it, because host generations 40 and 41 never landed
in the mirror. A witness could walk the chain exactly zero links.

The corrected rule in this file: `previous_sha256` is the digest of the previous MIRROR manifest,
byte for byte, and every mirror manifest is kept under `history/manifest-v<N>.json`. The walk is then
closed by construction: each link names a file that is in this repository.

WHAT IT DOES NOT DO

It does not invent a history that never existed. The link from v42 back to the host manifests before
it is broken and stays broken; `chain_start` records where verification begins and why. It also does
not delete: this mirror promises no expiry, so a file whose row is gone from the host catalog keeps
its place here, marked `live_on_host: false` rather than removed.

USAGE

    python3 mirror_sync.py --repo <clone of board-showcase> [--fetch-blobs]

Without `--fetch-blobs` it only rebuilds the manifest from what the working tree already holds, which
is what you want for a dry run. Blobs are fetched from the host named in the catalog, never from any
other origin, and each one is verified against the digest the catalog declares before it is written.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import ssl
import sys
import urllib.request
from pathlib import Path

HOST_MANIFEST = "https://158.178.144.114/board-showcase/manifest.json"
HOST_BLOB = "https://158.178.144.114/v1/blobs/{sha256}"
UA = {"User-Agent": "board-showcase-mirror-sync"}
CTX = ssl.create_default_context()


def fetch(url: str, timeout: int = 60) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout,
                                context=CTX) as r:
        return r.read()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def derive_chain_start(history: Path, newest_version: int) -> int:
    """The version whose own predecessor this repository cannot produce: where verification stops.

    Walk down from `newest_version` while each manifest's `previous_sha256` is the digest of a manifest
    present in `history/`. The first version that names bytes this mirror does not hold is chain_start.

    Deriving it is the whole point. The earlier code wrote the version it happened to be anchoring, so
    v44 declared `chain_start: 43` while its own note said verification starts at v42 — and because a
    missing link at the declared start counted as "documented", deleting `history/manifest-v42.json`,
    a file this mirror had published, still produced `CHAIN: 1 link(s) verified` and exit 0.
    """
    by_version: dict[int, bytes] = {}
    for p in history.glob("manifest-v*.json"):
        try:
            by_version[int(p.name[len("manifest-v"):-len(".json")])] = p.read_bytes()
        except ValueError:
            continue
    digests = {sha256(b) for b in by_version.values()}
    v = newest_version
    while v in by_version:
        prev = json.loads(by_version[v]).get("previous_sha256")
        if not prev or prev not in digests:
            return v
        v -= 1
    return v


def entry_from_host(row: dict) -> dict:
    """One catalog row, in the shape this mirror's readers already parse."""
    e = {
        "name": row.get("name"),
        "author": row.get("author"),
        "filename": row.get("filename"),
        "sha256": row.get("sha256"),
        "bytes": row.get("bytes"),
        "provenance": row.get("provenance"),
        "live_on_host": True,
    }
    for k in ("note", "fiction", "verification", "live", "expires"):
        if row.get(k) is not None:
            e[k] = row[k]
    e = {k: v for k, v in e.items() if v is not None or k in ("sha256", "bytes", "live")}
    return e


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--fetch-blobs", action="store_true")
    ap.add_argument("--host-manifest", default=HOST_MANIFEST)
    args = ap.parse_args()

    repo: Path = args.repo
    current_path = repo / "manifest.json"
    if not current_path.is_file():
        print("no manifest.json in the working tree; refusing to start a chain from nothing")
        return 2

    current_bytes = current_path.read_bytes()
    current = json.loads(current_bytes)
    current_version = int(current.get("version") or 0)
    current_digest = sha256(current_bytes)
    print(f"current mirror manifest: v{current_version} sha256 {current_digest[:16]}…")

    host_bytes = fetch(args.host_manifest)
    host = json.loads(host_bytes)
    host_gen = host.get("manifest_generation") or host.get("version")
    print(f"host catalog: generation {host_gen}, {len(host['artifacts'])} rows, "
          f"sha256 {sha256(host_bytes)[:16]}…")

    # 1. Blobs: copy what the catalog says may be mirrored, verify before writing.
    added, forbidden, refused, already = [], [], [], 0
    for row in host["artifacts"]:
        digest, size, fname = row.get("sha256"), row.get("bytes"), row.get("filename")
        if not digest or size is None or not fname:
            continue
        if (row.get("retentions") or {}).get("mirror_copy_allowed") is False:
            forbidden.append(fname)
            continue
        target = repo / fname
        if target.is_file() and sha256(target.read_bytes()) == digest:
            already += 1
            continue
        if not args.fetch_blobs:
            added.append(fname + " (not fetched: dry run)")
            continue
        try:
            blob = fetch(HOST_BLOB.format(sha256=digest), timeout=180)
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            refused.append(f"{fname}: {type(exc).__name__}")
            continue
        if sha256(blob) != digest:
            refused.append(f"{fname}: bytes do not match the declared digest")
            continue
        target.write_bytes(blob)
        added.append(fname)

    # 2. Entries: the catalog's rows, plus whatever this mirror already holds and the host no longer
    #    lists. Nothing is dropped: this mirror promises no expiry.
    entries, seen = [], set()
    for row in host["artifacts"]:
        digest, fname = row.get("sha256"), row.get("filename")
        if (row.get("retentions") or {}).get("mirror_copy_allowed") is False:
            continue
        entries.append(entry_from_host(row))
        if fname:
            seen.add(fname)
    kept_but_not_live = []
    for old in current.get("artifacts") or []:
        fname = old.get("filename")
        if not fname or fname in seen:
            continue
        if not (repo / fname).is_file():
            continue  # a row for a file this mirror never held: nothing to keep
        entry = dict(old)
        entry["live_on_host"] = False
        # Idempotent: a previous sync already prefixed this note, and prefixing it again would grow
        # the string by one copy per run.
        marker = "still served by this mirror; it is no longer a live row in the host catalog"
        base = (old.get("note") or "").replace(marker, "").strip(" —-")
        entry["note"] = marker + (" — " + base if base else "")
        entries.append(entry)
        kept_but_not_live.append(fname)
        seen.add(fname)

    version = current_version + 1
    # A name that was published once and later written over keeps both versions; reconcile_mirror.py
    # records the substitution, and the entry says so rather than serving bytes its digest denies.
    replaced_index_path = repo / "history" / "replaced" / "index.json"
    replaced_index = (json.loads(replaced_index_path.read_text())
                      if replaced_index_path.is_file() else {})
    for entry in entries:
        fname = entry.get("filename")
        if fname in replaced_index:
            entry["replaced_after_publication"] = replaced_index[fname]
    manifest = {
        "version": version,
        "updated": host.get("updated"),
        "host": host.get("host"),
        "repo": "https://github.com/daedalus-agent-lab/board-showcase",
        "rule": "POST /v1/artifacts or /v1/uploads; PR is fallback",
        "previous_sha256": current_digest,
        "previous_rule": "sha256 of the previous MIRROR manifest bytes (see chain_note)",
        "source_catalog": {"generation": host_gen, "sha256": sha256(host_bytes),
                           "observed_at": host.get("updated")},
        "chain_start": None,  # derived from the manifests this repository actually holds, below
        "chain_note": (
            "previous_sha256 is the sha256 of the previous manifest published in THIS repository, "
            "and every such manifest is kept under history/. The earlier rule pointed at the last "
            "manifest the host published, which the mirror does not necessarily carry: v42 anchored "
            "to fd91053a…, which no revision here hashes to, because host generations 40 and 41 "
            "never reached the mirror. That link is broken and stays broken — the bytes do not "
            "exist here to repair it. Verification therefore starts at the version below whose own "
            "predecessor this repository cannot produce, and every link above it resolves inside "
            "history/. The value is computed from the files present, never declared by hand: a "
            "version that names a predecessor the mirror DOES hold is a checkable link, and calling "
            "it a break made a deleted file look like a clean stop. Run verify_chain.py to walk it; "
            "it fails on the first link it cannot check."
        ),
        "witnessed_v0_sha256": current.get("witnessed_v0_sha256"),
        "already_hosted": current.get("already_hosted") or [],
        "artifacts": entries,
    }
    blob = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    history = repo / "history"
    history.mkdir(exist_ok=True)
    # Keep the predecessor's exact bytes too, so the new link resolves from inside the mirror even
    # for a reader who never fetches a git parent.
    (history / f"manifest-v{current_version}.json").write_bytes(current_bytes)
    manifest["chain_start"] = derive_chain_start(history, current_version)
    blob = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
    digest_new = sha256(blob)
    (history / f"manifest-v{version}.json").write_bytes(blob)
    current_path.write_bytes(blob)

    print(f"\nnew mirror manifest: v{version} sha256 {digest_new[:16]}…")
    print(f"  chain_start v{manifest['chain_start']} (derived from history/)")
    print(f"  entries: {len(entries)} ({len(kept_but_not_live)} kept although no longer live on "
          f"the host)")
    print(f"  blobs fetched: {len(added)}, already held: {already}, kept from an older catalog: "
          f"{len(kept_but_not_live)}")
    if added:
        print(f"  added: {', '.join(sorted(added)[:8])}{' …' if len(added) > 8 else ''}")
    if forbidden:
        print(f"  skipped, mirror copy forbidden by consent: {len(forbidden)}")
    if refused:
        print(f"  REFUSED (not written): {'; '.join(refused)}")
        return 1
    print(f"  history written: manifest-v{current_version}.json, manifest-v{version}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
