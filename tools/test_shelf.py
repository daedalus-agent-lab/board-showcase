#!/usr/bin/env python3
"""ACCEPT checks + store behaviour. Run: python3 tools/test_shelf.py"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from shelf_lib import check_package, sha256_hex  # noqa: E402
from shelf_store import ShelfStore  # noqa: E402


def _pkg(**over):
    body = over.pop("content", "# hello shelf\n")
    if isinstance(body, str):
        content = body.encode("utf-8")
    else:
        content = body
    base = dict(
        content=content,
        declared_sha256=sha256_hex(content),
        declared_bytes=len(content),
        name="Test card",
        filename="test-card.md",
        provenance={"thread": "8246bf16-1f79-466c-b757-0d011c414fdb"},
        author="tester",
        consent="Host this file on the board-showcase shelf and Oracle mirror.",
        principal="tester-agent",
        shelf_live_bytes=0,
        shelf_live_count=0,
        idempotency_key="test-idempotency-key-01",
    )
    base.update(over)
    return base


class CheckTests(unittest.TestCase):
    def test_ok(self):
        r = check_package(**_pkg())
        self.assertTrue(r.ok, r.reason_line())
        self.assertEqual(r.filename, "test-card.md")

    def test_hash_mismatch(self):
        r = check_package(**_pkg(declared_sha256="0" * 64))
        self.assertFalse(r.ok)
        self.assertTrue(any(f.code == "hash" for f in r.failures))

    def test_path_traversal(self):
        r = check_package(**_pkg(filename="../etc/passwd.md"))
        self.assertFalse(r.ok)
        self.assertTrue(any(f.code == "path" for f in r.failures))

    def test_bad_ext(self):
        r = check_package(**_pkg(filename="payload.exe"))
        self.assertFalse(r.ok)
        self.assertTrue(any(f.code == "type" for f in r.failures))

    def test_private_key_rejected(self):
        pem = b"-----BEGIN PRIVATE KEY-----\nMIIB\n-----END PRIVATE KEY-----\n"
        r = check_package(**_pkg(content=pem, filename="key.txt"))
        self.assertFalse(r.ok)
        self.assertTrue(any(f.code == "secrets" for f in r.failures))

    def test_github_token_rejected(self):
        r = check_package(
            **_pkg(content=b"token ghp_abcdefghijklmnopqrstuvwxyz0123\n", filename="note.txt")
        )
        self.assertFalse(r.ok)
        self.assertTrue(any(f.code == "secrets" for f in r.failures))

    def test_consent_required(self):
        r = check_package(**_pkg(consent="ok"))
        self.assertFalse(r.ok)
        self.assertTrue(any(f.code == "consent" for f in r.failures))

    def test_provenance_required(self):
        r = check_package(**_pkg(provenance={}))
        self.assertFalse(r.ok)
        self.assertTrue(any(f.code == "provenance" for f in r.failures))

    def test_fingerprint_changes_with_consent(self):
        a = check_package(**_pkg(consent="Host this file on the shelf, please."))
        b = check_package(**_pkg(consent="Different consent sentence for hosting."))
        self.assertTrue(a.ok and b.ok)
        self.assertNotEqual(a.fingerprint, b.fingerprint)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = ShelfStore(root / "data", root / "public")

    def tearDown(self):
        self.tmp.cleanup()

    def test_accept_search_lookup_replay_conflict(self):
        content = b"# card\nhello\n"
        check = check_package(**_pkg(content=content))
        self.assertTrue(check.ok, check.reason_line())
        r1 = self.store.accept(
            content=content, check=check, principal="tester-agent", idempotency_key="test-idempotency-key-01"
        )
        self.assertEqual(r1["http"], 201)
        self.assertEqual(r1["receipt"]["state"], "ACCEPTED")
        op_id = r1["receipt"]["operation_id"]

        # replay
        r2 = self.store.accept(
            content=content, check=check, principal="tester-agent", idempotency_key="test-idempotency-key-01"
        )
        self.assertEqual(r2["http"], 200)
        self.assertEqual(r2["receipt"]["operation_id"], op_id)

        # conflict: same key, different consent fingerprint
        check2 = check_package(
            **_pkg(content=content, consent="Another hosting consent, still explicit enough.")
        )
        r3 = self.store.accept(
            content=content, check=check2, principal="tester-agent", idempotency_key="test-idempotency-key-01"
        )
        self.assertEqual(r3["http"], 409)

        code, body = self.store.lookup_sha256(check.sha256)
        self.assertEqual(code, 200)
        self.assertEqual(body["state"], "live")

        code404, body404 = self.store.lookup_sha256("a" * 64)
        self.assertEqual(code404, 404)
        self.assertEqual(body404["state"], "never")

        found = self.store.search(q="hello")
        self.assertGreaterEqual(found["count"], 1)
        man_h = hashlib.sha256((Path(self.tmp.name) / "data" / "manifest.json").read_bytes()).hexdigest()
        self.assertEqual(found.get("source_manifest_sha256"), man_h)
        self.assertEqual(found.get("coverage"), "complete")

        op = self.store.get_operation(op_id)
        self.assertEqual(op["state"], "ACCEPTED")
        self.assertEqual(op["replication"]["oracle"], "pending")

        pub = Path(self.tmp.name) / "public" / "test-card.md"
        self.assertTrue(pub.is_file())
        self.assertEqual(pub.read_bytes(), content)

        # Chain: previous_sha256 must equal the bytes of the pre-accept manifest.
        # Capture a second accept and check the link.
        before = (Path(self.tmp.name) / "data" / "manifest.json").read_bytes()
        before_h = hashlib.sha256(before).hexdigest()
        content_b = b"# second\n"
        check_b = check_package(
            **_pkg(content=content_b, filename="second.md", name="Second", idempotency_key="test-idempotency-key-02")
        )
        r4 = self.store.accept(
            content=content_b,
            check=check_b,
            principal="tester-agent",
            idempotency_key="test-idempotency-key-02",
        )
        self.assertEqual(r4["http"], 201)
        after = json.loads((Path(self.tmp.name) / "data" / "manifest.json").read_text())
        self.assertEqual(after["previous_sha256"], before_h)
        self.assertNotEqual(after["previous_sha256"], hashlib.sha256((Path(self.tmp.name) / "data" / "manifest.json").read_bytes()).hexdigest())

    def test_tombstone_410(self):
        digest = hashlib.sha256(b"gone").hexdigest()
        self.store.tombstones.mkdir(parents=True, exist_ok=True)
        (self.store.tombstones / f"{digest}.json").write_text(
            json.dumps(
                {
                    "sha256": digest,
                    "evicted_at": "2026-09-09T00:00:00Z",
                    "reason": "capacity",
                    "last_verified_at": "2026-09-09T00:00:00Z",
                }
            )
        )
        code, body = self.store.lookup_sha256(digest)
        self.assertEqual(code, 410)
        self.assertEqual(body["state"], "evicted")
        self.assertEqual(body["tombstone"]["reason"], "capacity")


if __name__ == "__main__":
    r = unittest.main(verbosity=2, exit=False)
    sys.exit(0 if r.result.wasSuccessful() else 1)
