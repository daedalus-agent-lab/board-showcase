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
        begin = "BEGIN" + " PRIVATE " + "KEY"
        endm = "END" + " PRIVATE " + "KEY"
        pem = ("-----" + begin + "-----\nMIIB\n-----" + endm + "-----\n").encode()
        r = check_package(**_pkg(content=pem, filename="key.txt"))
        self.assertFalse(r.ok)
        self.assertTrue(any(f.code == "secrets" for f in r.failures))

    def test_github_token_rejected(self):
        tok = "gh" + "p_" + ("abcdefghijklmnopqrstuvwxyz0123")
        r = check_package(
            **_pkg(content=("token " + tok + "\n").encode(), filename="note.txt")
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

    def test_provenance_json_budget(self):
        from shelf_lib import MAX_PROVENANCE_JSON_BYTES

        huge = {"thread": "8246bf16-1f79-466c-b757-0d011c414fdb", "note": "Q" * 5000}
        r = check_package(**_pkg(provenance=huge))
        self.assertFalse(r.ok)
        self.assertTrue(any(f.code == "provenance" for f in r.failures))
        # Boundary: just under the cap still passes.
        import json as _json

        note_len = MAX_PROVENANCE_JSON_BYTES
        while True:
            cand = {
                "thread": "8246bf16-1f79-466c-b757-0d011c414fdb",
                "note": "Q" * note_len,
            }
            wire = len(
                _json.dumps(cand, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
            )
            if wire <= MAX_PROVENANCE_JSON_BYTES:
                break
            note_len -= 1
        ok = check_package(**_pkg(provenance=cand))
        self.assertTrue(ok.ok, ok.reason_line())

    def test_name_and_note_budget(self):
        from shelf_lib import MAX_NAME_CHARS, MAX_NOTE_CHARS

        r = check_package(**_pkg(name="N" * (MAX_NAME_CHARS + 1)))
        self.assertFalse(r.ok)
        self.assertTrue(any(f.code == "name" for f in r.failures))
        r2 = check_package(**_pkg(note="T" * (MAX_NOTE_CHARS + 1)))
        self.assertFalse(r2.ok)
        self.assertTrue(any(f.code == "note" for f in r2.failures))
        ok = check_package(**_pkg(name="N" * MAX_NAME_CHARS, note="T" * MAX_NOTE_CHARS))
        self.assertTrue(ok.ok, ok.reason_line())
        self.assertEqual(ok.snapshot.get("note"), "T" * MAX_NOTE_CHARS)

    def test_fingerprint_changes_with_consent(self):
        a = check_package(**_pkg(consent="Host this file on the shelf, please."))
        b = check_package(**_pkg(consent="Different consent sentence for hosting."))
        self.assertTrue(a.ok and b.ok)
        self.assertNotEqual(a.fingerprint, b.fingerprint)

    def test_search_card_provenance_truncated(self):
        from shelf_lib import MAX_SEARCH_PROVENANCE_JSON_BYTES
        from shelf_store import _search_card_view

        card = {
            "name": "x",
            "sha256": "a" * 64,
            "bytes": 1,
            "author": "t",
            "filename": "x.txt",
            "provenance": {
                "thread": "8246bf16-1f79-466c-b757-0d011c414fdb",
                "note": "Q" * 8000,
            },
            "note": None,
        }
        view = _search_card_view(card)
        wire = json.dumps(
            view["provenance"], sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
        self.assertLessEqual(len(wire), MAX_SEARCH_PROVENANCE_JSON_BYTES)
        self.assertTrue(view.get("provenance_truncated"))
        self.assertIn("by-sha256", view.get("provenance_full") or "")
        # Original card untouched.
        self.assertEqual(len(card["provenance"]["note"]), 8000)


class VerifierHookTests(unittest.TestCase):
    def test_unavailable_on_bad_endpoint(self):
        from verifier_hook import request_attestation

        out = request_attestation(
            base_url="http://127.0.0.1:1",
            submitter="tester",
            sha256="a" * 64,
            pinned_ref="thread",
            timeout_s=0.2,
            poll_s=0.0,
        )
        self.assertEqual(out.get("status"), "unavailable")

    def test_env_off_returns_none(self):
        from verifier_hook import maybe_attest_from_env
        import os

        os.environ.pop("SHELF_VERIFIER_URL", None)
        self.assertIsNone(
            maybe_attest_from_env(submitter="t", sha256="a" * 64, pinned_ref="x")
        )


class SoftEnvelopeCursorTests(unittest.TestCase):
    def test_dropped_cards_remain_reachable(self):
        """meliora #28362: next_offset must follow delivered count, not original slice."""
        import shelf_store
        from shelf_store import ShelfStore

        # Patch the name used inside shelf_store (imported by value at load time).
        original = shelf_store.MAX_SEARCH_PAGE_WIRE_BYTES
        shelf_store.MAX_SEARCH_PAGE_WIRE_BYTES = 700
        try:
            with tempfile.TemporaryDirectory() as td:
                store = ShelfStore(Path(td), Path(td) / "public")
                arts = []
                for i in range(3):
                    arts.append(
                        {
                            "name": f"card-{i}",
                            "sha256": f"{i:064x}",
                            "bytes": 10,
                            "author": "t",
                            "filename": f"c{i}.txt",
                            "provenance": {"note": ("Z" * 1200) + str(i)},
                            "note": None,
                            "state": "live",
                            "tags": [],
                        }
                    )
                idx = {
                    "schema_version": "0.2",
                    "generated_at": "t",
                    "source_manifest_sha256": "0" * 64,
                    "artifacts": arts,
                    "tombstones": [],
                }
                (Path(td) / "search.json").write_text(json.dumps(idx))
                (Path(td) / "manifest.json").write_text(json.dumps({"version": 1, "artifacts": []}))
                page0 = store.search(q="", limit=2, offset=0)
                self.assertGreaterEqual(page0.get("wire_budget_dropped") or 0, 1)
                self.assertEqual(page0["count"], len(page0["artifacts"]))
                self.assertEqual(page0["next_offset"], page0["offset"] + page0["count"])
                page1 = store.search(q="", limit=2, offset=page0["next_offset"])
                names0 = {a["name"] for a in page0["artifacts"]}
                all_names = names0 | {a["name"] for a in page1["artifacts"]}
                if "card-0" in names0 and "card-1" not in names0:
                    self.assertIn("card-1", all_names)
                # Same serializer the store uses (compact separators).
                final = (json.dumps(page0, ensure_ascii=False, indent=2) + "\n").encode()
                self.assertEqual(page0["wire_bytes"], len(final))
                if not page0.get("wire_overflow"):
                    self.assertLessEqual(
                        page0["wire_bytes"], shelf_store.MAX_SEARCH_PAGE_WIRE_BYTES
                    )
        finally:
            shelf_store.MAX_SEARCH_PAGE_WIRE_BYTES = original

    def test_budget_holds_under_http_serializer(self):
        """meliora #28474: selection must use the serializer that emits the HTTP body."""
        import shelf_store
        from shelf_store import ShelfStore

        with tempfile.TemporaryDirectory() as td:
            store = ShelfStore(Path(td), Path(td) / "public")
            arts = []
            for i in range(50):
                arts.append(
                    {
                        "name": f"card-{i:03d}",
                        "sha256": f"{i:064x}",
                        "bytes": 100,
                        "author": "t",
                        "filename": f"c{i}.txt",
                        "provenance": {"note": "P" * 1400 + str(i)},
                        "note": "я" * 680,
                        "state": "live",
                        "tags": [],
                    }
                )
            idx = {
                "schema_version": "0.2",
                "generated_at": "t",
                "source_manifest_sha256": "0" * 64,
                "artifacts": arts,
                "tombstones": [],
            }
            (Path(td) / "search.json").write_text(json.dumps(idx))
            (Path(td) / "manifest.json").write_text(json.dumps({"version": 1, "artifacts": []}))
            page = store.search(q="", limit=50, offset=0)
            http_body = (json.dumps(page, ensure_ascii=False, indent=2) + "\n").encode()
            self.assertEqual(page["wire_bytes"], len(http_body))
            if not page.get("wire_overflow"):
                self.assertLessEqual(len(http_body), shelf_store.MAX_SEARCH_PAGE_WIRE_BYTES)
            self.assertEqual(page["next_offset"], page["offset"] + page["count"])

    def test_budget_refit_after_final_fields(self):
        """meliora #28612: next_offset/page_complete/wire_bytes can tip a tight page over."""
        import shelf_store
        from shelf_store import ShelfStore

        original = shelf_store.MAX_SEARCH_PAGE_WIRE_BYTES
        try:
            # Tight budget so selection lands near the edge after final fields.
            shelf_store.MAX_SEARCH_PAGE_WIRE_BYTES = 65536
            with tempfile.TemporaryDirectory() as td:
                store = ShelfStore(Path(td), Path(td) / "public")
                arts = []
                for i in range(50):
                    prov_note = ("я" * 696) if i else (("я" * 696) + ("a" * 53))
                    arts.append(
                        {
                            "name": f"card-{i:03d}",
                            "sha256": f"{i:064x}",
                            "bytes": 100,
                            "author": "t",
                            "filename": f"c{i}.txt",
                            "provenance": {"note": prov_note},
                            "note": None,
                            "state": "live",
                            "tags": [],
                        }
                    )
                idx = {
                    "schema_version": "0.2",
                    "generated_at": "t",
                    "source_manifest_sha256": "0" * 64,
                    "artifacts": arts,
                    "tombstones": [],
                }
                (Path(td) / "search.json").write_text(json.dumps(idx))
                (Path(td) / "manifest.json").write_text(json.dumps({"version": 1, "artifacts": []}))
                page = store.search(q="", limit=50, offset=0)
                http_body = (json.dumps(page, ensure_ascii=False, indent=2) + "\n").encode()
                self.assertEqual(page["wire_bytes"], len(http_body))
                if not page.get("wire_overflow"):
                    self.assertLessEqual(len(http_body), shelf_store.MAX_SEARCH_PAGE_WIRE_BYTES)
        finally:
            shelf_store.MAX_SEARCH_PAGE_WIRE_BYTES = original


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
        self.assertTrue(found.get("page_complete"))
        page = self.store.search(q="", limit=1, offset=0)
        self.assertEqual(page["count"], 1)
        self.assertGreaterEqual(page["total_matched"], 1)
        if page["total_matched"] > 1:
            self.assertFalse(page["page_complete"])
            self.assertEqual(page["next_offset"], 1)
        hit = found["artifacts"][0]
        self.assertIn("blobs", hit)
        self.assertTrue(hit["blobs"].endswith(check.sha256))
        # search may carry a tiny excerpt for small objects, never more than 256 chars
        if "excerpt" in hit:
            self.assertLessEqual(len(hit["excerpt"]), 256)
        # objects larger than 64 KiB must not get an excerpt at all
        big = b"x" * (64 * 1024 + 1)
        big_check = check_package(
            **_pkg(
                content=big,
                filename="big.txt",
                name="Big",
                idempotency_key="test-idempotency-key-big",
            )
        )
        # size lane still rejects >2MiB; 64KiB+1 is allowed and must omit excerpt
        self.assertTrue(big_check.ok, big_check.reason_line())
        self.store.accept(
            content=big,
            check=big_check,
            principal="tester-agent",
            idempotency_key="test-idempotency-key-big",
        )
        big_hit = next(a for a in self.store.search(q="Big")["artifacts"] if a.get("sha256") == big_check.sha256)
        self.assertNotIn("excerpt", big_hit)
        self.assertTrue(big_hit["blobs"].endswith(big_check.sha256))

        code_b, meta_b, path_b = self.store.open_blob(check.sha256)
        self.assertEqual(code_b, 200)
        self.assertEqual(meta_b["bytes"], len(content))
        self.assertIsNotNone(path_b)
        self.assertEqual(path_b.read_bytes(), content)
        self.assertEqual(body.get("blobs"), f"https://158.178.144.114/v1/blobs/{check.sha256}")

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



class UploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = ShelfStore(root / "data", root / "public")
        from shelf_uploads import UploadStore

        self.clock = {"t": 1_000_000.0}

        def _clock():
            return self.clock["t"]

        self.uploads = UploadStore(self.store, clock=_clock)
        self.part_size = 256 * 1024  # MIN_PART_SIZE

    def tearDown(self):
        self.tmp.cleanup()

    def _large_body(self, nbytes: int, seed: bytes = b"L") -> bytes:
        # Deterministic filler without secrets.
        rep = (seed * ((nbytes // len(seed)) + 1))[:nbytes]
        return rep

    def _init_meta(self, content: bytes, **over):
        from shelf_lib import sha256_hex

        meta = dict(
            sha256=sha256_hex(content),
            bytes=len(content),
            filename="large-card.md",
            name="Large card",
            author="tester",
            provenance={"thread": "8246bf16-1f79-466c-b757-0d011c414fdb"},
            consent="Host this large file on the board-showcase shelf and Oracle mirror.",
            part_size=self.part_size,
            ttl_seconds=2592000,
        )
        meta.update(over)
        return meta

    def test_reject_over_100mib(self):
        from shelf_lib import MAX_OBJECT_BYTES_LARGE, check_upload_init

        huge = MAX_OBJECT_BYTES_LARGE + 1
        r = check_upload_init(
            declared_sha256="a" * 64,
            declared_bytes=huge,
            name="Too big",
            filename="too-big.md",
            provenance={"thread": "8246bf16-1f79-466c-b757-0d011c414fdb"},
            author="tester",
            consent="Host this large file on the board-showcase shelf and Oracle mirror.",
            principal="tester-agent",
            shelf_live_bytes=0,
            shelf_live_count=0,
            idempotency_key="test-idempotency-key-lg01",
        )
        self.assertFalse(r.ok)
        self.assertTrue(any(f.code == "size" for f in r.failures))

    def test_reject_small_on_large_lane(self):
        from shelf_lib import check_upload_init

        r = check_upload_init(
            declared_sha256="a" * 64,
            declared_bytes=1024,  # must use small lane
            name="Small",
            filename="small.md",
            provenance={"thread": "8246bf16-1f79-466c-b757-0d011c414fdb"},
            author="tester",
            consent="Host this large file on the board-showcase shelf and Oracle mirror.",
            principal="tester-agent",
            shelf_live_bytes=0,
            shelf_live_count=0,
            idempotency_key="test-idempotency-key-lg02",
        )
        self.assertFalse(r.ok)
        self.assertTrue(any(f.code == "size" for f in r.failures))
        # Also via init_upload API
        body = self._large_body(1024)
        res = self.uploads.init_upload(
            meta_body=self._init_meta(body, bytes=1024, sha256="a" * 64),
            principal="tester-agent",
            idempotency_key="test-idempotency-key-lg02",
        )
        self.assertEqual(res["http"], 422)

    def test_init_replay_and_conflict(self):
        from shelf_lib import MAX_OBJECT_BYTES

        content = self._large_body(MAX_OBJECT_BYTES + 1000)
        meta = self._init_meta(content)
        r1 = self.uploads.init_upload(
            meta_body=meta, principal="tester-agent", idempotency_key="test-idempotency-key-lg03"
        )
        self.assertEqual(r1["http"], 201, r1)
        upload_id = r1["upload_id"]
        op_id = r1["operation_id"]

        r2 = self.uploads.init_upload(
            meta_body=meta, principal="tester-agent", idempotency_key="test-idempotency-key-lg03"
        )
        self.assertEqual(r2["http"], 200)
        self.assertEqual(r2["upload_id"], upload_id)
        self.assertEqual(r2["operation_id"], op_id)
        self.assertTrue(r2.get("replay"))

        meta2 = dict(meta)
        meta2["consent"] = "Different hosting consent sentence, still explicit."
        r3 = self.uploads.init_upload(
            meta_body=meta2, principal="tester-agent", idempotency_key="test-idempotency-key-lg03"
        )
        self.assertEqual(r3["http"], 409)
        self.assertEqual(r3.get("error"), "IDEMPOTENCY_CONFLICT")

    def test_part_conflict_commit_success_replay(self):
        from shelf_lib import MAX_OBJECT_BYTES, sha256_hex

        content = self._large_body(MAX_OBJECT_BYTES + 5000, seed=b"ABCDEFGH")
        meta = self._init_meta(content)
        init = self.uploads.init_upload(
            meta_body=meta, principal="tester-agent", idempotency_key="test-idempotency-key-lg04"
        )
        self.assertEqual(init["http"], 201, init)
        upload_id = init["upload_id"]
        parts = init["parts"]
        ps = init["part_size"]

        # Put all parts
        for n in range(parts):
            start = n * ps
            chunk = content[start : start + ps]
            r = self.uploads.put_part(upload_id=upload_id, n=n, body=chunk)
            self.assertEqual(r["http"], 200, r)

        # Identical re-PUT free
        chunk0 = content[:ps]
        r_replay = self.uploads.put_part(upload_id=upload_id, n=0, body=chunk0)
        self.assertEqual(r_replay["http"], 200)
        self.assertTrue(r_replay.get("replay"))

        # Different bytes → 409
        bad = b"Z" * len(chunk0)
        r_bad = self.uploads.put_part(upload_id=upload_id, n=0, body=bad)
        self.assertEqual(r_bad["http"], 409)
        self.assertEqual(r_bad.get("error"), "PART_CONFLICT")

        commit = self.uploads.commit(upload_id=upload_id)
        self.assertEqual(commit["http"], 201, commit)
        receipt = commit["receipt"]
        self.assertEqual(receipt["state"], "ACCEPTED")
        self.assertEqual(receipt["sha256"], sha256_hex(content))
        self.assertIn("expires_at", receipt)
        op_id = receipt["operation_id"]

        # Commit replay
        commit2 = self.uploads.commit(upload_id=upload_id)
        self.assertEqual(commit2["http"], 200)
        self.assertEqual(commit2["receipt"]["operation_id"], op_id)

        # Blob live
        code, body = self.store.lookup_sha256(sha256_hex(content))
        self.assertEqual(code, 200)
        self.assertEqual(body["state"], "live")
        # No excerpt for large object
        hit = next(
            a
            for a in self.store.search(q="Large")["artifacts"]
            if a.get("sha256") == sha256_hex(content)
        )
        self.assertNotIn("excerpt", hit)

    def test_staging_expiry(self):
        from shelf_lib import MAX_OBJECT_BYTES, STAGING_TTL_SECONDS

        content = self._large_body(MAX_OBJECT_BYTES + 2000)
        meta = self._init_meta(content)
        init = self.uploads.init_upload(
            meta_body=meta, principal="tester-agent", idempotency_key="test-idempotency-key-lg05"
        )
        self.assertEqual(init["http"], 201, init)
        upload_id = init["upload_id"]

        # Advance clock past staging TTL
        self.clock["t"] += STAGING_TTL_SECONDS + 10
        # put_part should reject
        ps = init["part_size"]
        r = self.uploads.put_part(upload_id=upload_id, n=0, body=content[:ps])
        self.assertEqual(r["http"], 409)
        self.assertIn(r.get("error"), ("UPLOAD_NOT_OPEN", "UPLOAD_EXPIRED"))

        # Re-init with same key must not silently recreate
        again = self.uploads.init_upload(
            meta_body=meta, principal="tester-agent", idempotency_key="test-idempotency-key-lg05"
        )
        self.assertEqual(again["http"], 409)
        self.assertEqual(again.get("error"), "UPLOAD_EXPIRED")



if __name__ == "__main__":
    r = unittest.main(verbosity=2, exit=False)
    sys.exit(0 if r.result.wasSuccessful() else 1)
