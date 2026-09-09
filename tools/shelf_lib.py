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

MAX_OBJECT_BYTES = 2 * 1024 * 1024  # 2 MiB — default JSON POST lane
MAX_OBJECT_BYTES_LARGE = 100 * 1024 * 1024  # 100 MiB — multipart large lane
MAX_SHELF_BYTES = 256 * 1024 * 1024  # 256 MiB
MAX_LIVE_OBJECTS = 80
DEFAULT_TTL_SECONDS = 30 * 24 * 3600  # 30 days from accept
STAGING_TTL_SECONDS = 24 * 3600  # 24h upload staging
DEFAULT_PART_SIZE = 1024 * 1024  # 1 MiB
MIN_PART_SIZE = 256 * 1024
MAX_PART_SIZE = 8 * 1024 * 1024
# Search-card JSON budget (meliora/just-nik): object-byte caps do not bound metadata.
MAX_NAME_CHARS = 200
MAX_AUTHOR_CHARS = 80
MAX_NOTE_CHARS = 512
MAX_CONSENT_CHARS = 1024
MAX_PROVENANCE_JSON_BYTES = 4096  # sorted canonical JSON of provenance object
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


def _validate_common_meta(
    *,
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
    min_bytes: int,
    max_bytes: int,
    content: bytes | None = None,
    verify_hash_against_content: bool = True,
    note: str | None = None,
) -> CheckResult:
    """Shared ACCEPT metadata checks for small and large lanes.

    When content is None (large-lane init), hash is trusted as declared until
    commit reassembles and verifies. Secrets are only scanned when content is
    present (empty body at init cannot contain secrets).
    """
    failures: list[CheckFailure] = []

    if not IDEMPOTENCY_RE.match(idempotency_key or ""):
        failures.append(
            CheckFailure("idempotency_key", "must be 16–128 chars [A-Za-z0-9_-]")
        )

    try:
        n = int(declared_bytes)
    except (TypeError, ValueError):
        failures.append(CheckFailure("size", "bytes must be an integer"))
        return _fail(*failures)

    if not (min_bytes <= n <= max_bytes):
        failures.append(
            CheckFailure(
                "size",
                f"bytes {n} outside {min_bytes}..{max_bytes}",
            )
        )

    declared = (declared_sha256 or "").strip().lower()
    if not SHA256_RE.match(declared):
        failures.append(CheckFailure("hash", "sha256 must be 64 lowercase hex chars"))

    digest = declared
    if content is not None:
        if not isinstance(content, (bytes, bytearray)) or not content:
            failures.append(CheckFailure("size", "content must be non-empty bytes"))
            return _fail(*failures)
        actual_n = len(content)
        if actual_n != n:
            failures.append(
                CheckFailure("size", f"declared bytes {n} != len(content) {actual_n}")
            )
        n = actual_n
        digest = sha256_hex(bytes(content))
        if SHA256_RE.match(declared) and verify_hash_against_content and digest != declared:
            failures.append(
                CheckFailure("hash", f"sha256(content)={digest} != declared={declared}")
            )
        secret = looks_like_secret(bytes(content))
        if secret:
            failures.append(secret)

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

    name_s = str(name or "").strip()
    if not name_s:
        failures.append(CheckFailure("name", "name is required"))
    elif len(name_s) > MAX_NAME_CHARS:
        failures.append(
            CheckFailure(
                "name",
                f"name length {len(name_s)} exceeds MAX_NAME_CHARS={MAX_NAME_CHARS}",
            )
        )

    author_s = str(author or "").strip()
    if not author_s:
        failures.append(CheckFailure("author", "author is required"))
    elif len(author_s) > MAX_AUTHOR_CHARS:
        failures.append(
            CheckFailure(
                "author",
                f"author length {len(author_s)} exceeds MAX_AUTHOR_CHARS={MAX_AUTHOR_CHARS}",
            )
        )

    note_s = str(note or "").strip() if note is not None else ""
    if len(note_s) > MAX_NOTE_CHARS:
        failures.append(
            CheckFailure(
                "note",
                f"note length {len(note_s)} exceeds MAX_NOTE_CHARS={MAX_NOTE_CHARS}",
            )
        )

    if not isinstance(provenance, dict) or not provenance:
        failures.append(CheckFailure("provenance", "provenance object is required"))
        prov_obj: dict[str, Any] = {}
    else:
        prov_obj = provenance
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
        prov_wire = json.dumps(
            provenance, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(prov_wire) > MAX_PROVENANCE_JSON_BYTES:
            failures.append(
                CheckFailure(
                    "provenance",
                    f"provenance JSON {len(prov_wire)} bytes exceeds "
                    f"MAX_PROVENANCE_JSON_BYTES={MAX_PROVENANCE_JSON_BYTES}",
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
    elif len(consent_s) > MAX_CONSENT_CHARS:
        failures.append(
            CheckFailure(
                "consent",
                f"consent length {len(consent_s)} exceeds MAX_CONSENT_CHARS={MAX_CONSENT_CHARS}",
            )
        )

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
        "author": author_s,
        "name": name_s,
        "filename": filename,
        "provenance": prov_obj,
        "consent": consent_s,
        "principal": principal,
        "note": note_s or None,
    }
    fp = fingerprint_of(
        principal=principal,
        sha256=digest if content is not None else declared,
        consent=consent_s,
        provenance=prov_obj,
        name=name_s,
        filename=filename,
    )
    return CheckResult(
        ok=True,
        filename=filename,
        sha256=digest if content is not None else declared,
        bytes_len=n,
        fingerprint=fp,
        snapshot=snap,
    )


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
    note: str | None = None,
) -> CheckResult:
    """Default JSON POST lane: 1..2 MiB with body present."""
    return _validate_common_meta(
        content=content,
        declared_sha256=declared_sha256,
        declared_bytes=declared_bytes,
        name=name,
        filename=filename,
        provenance=provenance,
        author=author,
        consent=consent,
        principal=principal,
        shelf_live_bytes=shelf_live_bytes,
        shelf_live_count=shelf_live_count,
        idempotency_key=idempotency_key,
        min_bytes=1,
        max_bytes=MAX_OBJECT_BYTES,
        verify_hash_against_content=True,
        note=note,
    )


def check_upload_init(
    *,
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
    part_size: int | None = None,
    reserved_bytes: int = 0,
    note: str | None = None,
) -> CheckResult:
    """Large-lane init: metadata only, size must be in (2 MiB, 100 MiB].

    reserved_bytes counts other OPEN upload reservations already held.
    """
    # Large lane starts strictly above the small-lane cap.
    min_large = MAX_OBJECT_BYTES + 1
    result = _validate_common_meta(
        content=None,
        declared_sha256=declared_sha256,
        declared_bytes=declared_bytes,
        name=name,
        filename=filename,
        provenance=provenance,
        author=author,
        consent=consent,
        principal=principal,
        shelf_live_bytes=shelf_live_bytes + max(0, int(reserved_bytes)),
        shelf_live_count=shelf_live_count,
        idempotency_key=idempotency_key,
        min_bytes=min_large,
        max_bytes=MAX_OBJECT_BYTES_LARGE,
        verify_hash_against_content=False,
        note=note,
    )
    if not result.ok:
        return result
    # part_size validation is advisory here; UploadStore enforces bounds.
    if part_size is not None:
        try:
            ps = int(part_size)
        except (TypeError, ValueError):
            return _fail(CheckFailure("part_size", "part_size must be an integer"))
        if not (MIN_PART_SIZE <= ps <= MAX_PART_SIZE):
            return _fail(
                CheckFailure(
                    "part_size",
                    f"part_size {ps} outside {MIN_PART_SIZE}..{MAX_PART_SIZE}",
                )
            )
    return result
