"""ACCEPT v0.2 checks: hash, size, quota, type, path, secrets, provenance, consent.

Used by the Oracle POST /v1/artifacts server and by the local test suite.
No network. No git. Callers persist blobs and write receipts.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

MAX_OBJECT_BYTES = 2 * 1024 * 1024  # 2 MiB
MAX_SHELF_BYTES = 256 * 1024 * 1024  # 256 MiB
MAX_LIVE_OBJECTS = 80
FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
ALLOWED_EXT = {".md", ".json", ".svg", ".txt", ".html"}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")

PRIVATE_KEY_MARKERS = (
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN RSA PRIVATE KEY-----",
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
    b"-----BEGIN EC PRIVATE KEY-----",
    b"-----BEGIN DSA PRIVATE KEY-----",
)
# High-entropy token-ish lines (GitHub classic, gh p.a.t, PEM-ish blobs).
TOKEN_LINE_RE = re.compile(
    rb"(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9]{20,})"
)


@dataclass
class CheckFailure:
    code: str
    message: str


@dataclass
class CheckResult:
    ok: bool
    failures: list[CheckFailure] = field(default_factory=list)
    filename: str = ""
    sha256: str = ""
    bytes_len: int = 0
    fingerprint: str = ""
    snapshot: dict[str, Any] = field(default_factory=dict)

    def reason_line(self) -> str:
        if self.ok:
            return "ok"
        return "; ".join(f"{f.code}: {f.message}" for f in self.failures)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fail(*items: CheckFailure) -> CheckResult:
    return CheckResult(ok=False, failures=list(items))


def _normalize_filename(name: str) -> str:
    name = unicodedata.normalize("NFC", name).strip()
    return name


def looks_like_secret(content: bytes) -> CheckFailure | None:
    if any(m in content for m in PRIVATE_KEY_MARKERS):
        return CheckFailure("secrets", "private-key header found")
    if TOKEN_LINE_RE.search(content):
        return CheckFailure("secrets", "high-entropy token pattern found")
    return None


def fingerprint_of(
    *,
    principal: str,
    sha256: str,
    consent: str,
    provenance: dict[str, Any],
    name: str,
    filename: str,
) -> str:
    """Versioned request fingerprint: content hash is not enough (nadir-codex)."""
    payload = {
        "v": 1,
        "principal": principal,
        "sha256": sha256,
        "consent": consent,
        "provenance": provenance,
        "name": name,
        "filename": filename,
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return sha256_hex(blob)


def check_package(
    *,
    content: bytes,
    declared_sha256: str,
    declared_bytes: int,
    name: str,
    filename: str,
    provenance: dict[str, Any],
    author: str,
    consent: str,
    principal: str,
    shelf_live_bytes: int,
    shelf_live_count: int,
    idempotency_key: str,
) -> CheckResult:
    failures: list[CheckFailure] = []

    if not IDEMPOTENCY_RE.match(idempotency_key or ""):
        failures.append(
            CheckFailure("idempotency_key", "must be 16–128 chars [A-Za-z0-9_-]")
        )

    if not isinstance(content, (bytes, bytearray)) or not content:
        failures.append(CheckFailure("size", "content must be non-empty bytes"))
        return _fail(*failures)

    n = len(content)
    if not (1 <= n <= MAX_OBJECT_BYTES):
        failures.append(
            CheckFailure("size", f"bytes {n} outside 1..{MAX_OBJECT_BYTES}")
        )

    digest = sha256_hex(bytes(content))
    declared = (declared_sha256 or "").strip().lower()
    if not SHA256_RE.match(declared):
        failures.append(CheckFailure("hash", "sha256 must be 64 lowercase hex chars"))
    elif digest != declared:
        failures.append(
            CheckFailure("hash", f"sha256(content)={digest} != declared={declared}")
        )
    if int(declared_bytes) != n:
        failures.append(
            CheckFailure("size", f"declared bytes {declared_bytes} != len(content) {n}")
        )

    filename = _normalize_filename(filename or "")
    if not FILENAME_RE.match(filename):
        failures.append(
            CheckFailure(
                "path",
                "filename must match [A-Za-z0-9._-]{1,120}, no '..', no absolute path",
            )
        )
    else:
        if ".." in filename or filename.startswith("/") or "/" in filename:
            failures.append(CheckFailure("path", "filename must be a single path segment"))
        ext = ""
        if "." in filename:
            ext = "." + filename.rsplit(".", 1)[-1].lower()
        if ext not in ALLOWED_EXT:
            failures.append(
                CheckFailure("type", f"extension {ext or '(none)'} not in {sorted(ALLOWED_EXT)}")
            )

    secret = looks_like_secret(bytes(content))
    if secret:
        failures.append(secret)

    if not name or not str(name).strip():
        failures.append(CheckFailure("name", "name is required"))
    if not author or not str(author).strip():
        failures.append(CheckFailure("author", "author is required"))

    if not isinstance(provenance, dict) or not provenance:
        failures.append(CheckFailure("provenance", "provenance object is required"))
    else:
        cites = any(
            provenance.get(k)
            for k in ("thread", "message", "repo", "commit", "meatproxy", "url")
        )
        if not cites:
            failures.append(
                CheckFailure(
                    "provenance",
                    "need at least one of thread, message, repo, commit, meatproxy, url",
                )
            )

    consent_s = (consent or "").strip()
    if len(consent_s) < 8:
        failures.append(
            CheckFailure(
                "consent",
                "explicit hosting consent required (short sentence in the package)",
            )
        )

    # Quota: existing live objects; a new sha256 that is already live does not add.
    # Caller passes current live totals excluding this digest if already present.
    if n + shelf_live_bytes > MAX_SHELF_BYTES:
        failures.append(
            CheckFailure(
                "quota",
                f"shelf would exceed {MAX_SHELF_BYTES} bytes ({shelf_live_bytes}+{n})",
            )
        )
    if shelf_live_count >= MAX_LIVE_OBJECTS:
        failures.append(
            CheckFailure("quota", f"shelf already has {shelf_live_count} live objects")
        )

    if failures:
        return _fail(*failures)

    snap = {
        "author": str(author).strip(),
        "name": str(name).strip(),
        "filename": filename,
        "provenance": provenance,
        "consent": consent_s,
        "principal": principal,
    }
    fp = fingerprint_of(
        principal=principal,
        sha256=digest,
        consent=consent_s,
        provenance=provenance,
        name=str(name).strip(),
        filename=filename,
    )
    return CheckResult(
        ok=True,
        filename=filename,
        sha256=digest,
        bytes_len=n,
        fingerprint=fp,
        snapshot=snap,
    )
