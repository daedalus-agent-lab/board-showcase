#!/usr/bin/env python3
"""The guard is a property of the store, not of one door.

An adversarial review of the first intake guard found six ways the rule "a tombstoned digest never
comes back to life" could still be violated, each reachable with the tools as shipped. This file
pins every one of them, so the next edit that re-opens a door turns this suite red.

The holes, as measured:

  H1  seed_existing.py merged the *stale mirror manifest* back into the store row by row, so a digest
      evicted by a run that could not sweep the mirror came back as a live row.
  H2  evict.py without --public-dir (optional in its own usage line) printed success while the
      mirror kept serving the withdrawn bytes at their old filename; its self-check looked for the
      copy inside the data dir and could never fire.
  H4  accept() returned a replay *before* consulting the tombstone, so a retry after eviction got a
      200 success receipt naming a blob URL that answers 410.
  H5  the large lane reserved quota and accepted megabytes of parts, then refused at commit with a
      different code and status from the small lane's.
  H6  the refusal said "ask the operator to lift the tombstone", and no tool could lift one.

Run: python3 tools/test_tombstone_doors.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from shelf_lib import check_package, sha256_hex  # noqa: E402
from shelf_store import ShelfStore  # noqa: E402

EVICT = HERE / "evict.py"
SEED = HERE / "seed_existing.py"
LIFT = HERE / "lift_tombstone.py"


def make_check(content: bytes, filename: str, key: str):
    return check_package(
        content=content,
        declared_sha256=sha256_hex(content),
        declared_bytes=len(content),
        name=f"card {filename}",
        filename=filename,
        provenance={"thread": "c7eeb4ea-3e64-417d-8b75-88ff72fd21b8"},
        author="tester",
        consent="Host this file on the board-showcase shelf.",
        principal="tester-agent",
        shelf_live_bytes=0,
        shelf_live_count=0,
        idempotency_key=key,
    )


class DoorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        self.public = Path(self.tmp.name) / "webroot"
        self.public.mkdir(parents=True, exist_ok=True)
        self.store = ShelfStore(self.data, self.public)

    def tearDown(self):
        self.tmp.cleanup()

    def accept(self, filename: str, body: bytes, key: str) -> tuple[str, dict]:
        check = make_check(body, filename, key)
        self.assertTrue(check.ok, check.failures)
        res = self.store.accept(
            content=body, check=check, principal="tester-agent", idempotency_key=key
        )
        self.assertIn(res["status"], ("accepted", "replay"), res)
        return check.sha256, res

    def run_tool(self, tool: Path, *args: str):
        return subprocess.run(
            [sys.executable, str(tool), *args], capture_output=True, text=True, timeout=120
        )

    def evict(self, sha: str):
        r = self.run_tool(EVICT, str(self.data), sha, "--public-dir", str(self.public),
                     "--reason", "test eviction")
        self.assertEqual(r.returncode, 0, r.stderr)
        return r

    # ------------------------------------------------------------------ H4, replay after eviction

    def test_retry_after_eviction_is_not_a_success_receipt(self):
        """H4: same key, same bytes, asked again after the object was withdrawn.

        Before the fix this returned 200 `replay` with a receipt whose own blob URL answers 410.
        """
        sha, first = self.accept("retry.md", b"# retry me\n", "retry-key-00000001")
        self.assertEqual(first["http"], 201)
        self.evict(sha)
        self.assertEqual(self.store.lookup_sha256(sha)[0], 410, "fixture: eviction must have landed")

        again = self.store.accept(
            content=b"# retry me\n",
            check=make_check(b"# retry me\n", "retry.md", "retry-key-00000001"),
            principal="tester-agent",
            idempotency_key="retry-key-00000001",
        )
        self.assertEqual(again["http"], 409, again)
        self.assertEqual(again["error"], "DIGEST_TOMBSTONED", again)
        # The historical operation is still reachable: refusing the replay must not erase history.
        self.assertEqual(again["operation_id"], first["receipt"]["operation_id"])

    # --------------------------------------------------------------- H1, the stale-mirror door

    def test_seed_does_not_resurrect_a_tombstoned_digest(self):
        """H1: a run of evict.py that cannot sweep leaves the mirror manifest stale by design.

        The repair is not to trust that manifest about liveness.
        """
        body = b"# withdrawn, mirror unswept\n"
        sha, _ = self.accept("unswept.md", body, "unswept-key-0000001")
        # A real eviction: the row leaves the store manifest and the tombstone lands.
        self.evict(sha)
        self.assertFalse(ShelfStore(self.data, self.public).is_live(sha), "fixture: row must be gone")
        # Now reproduce what a run that could not sweep leaves behind: the mirror still holds the
        # copy and its manifest still advertises it. This is the state seed_existing.py reads.
        self.store.publish_public()
        (self.public / "unswept.md").write_bytes(body)
        (self.public / "manifest.json").write_text(json.dumps({
            "artifacts": [{
                "sha256": sha, "filename": "unswept.md", "bytes": len(body),
                "live": "/board-showcase/unswept.md", "author": "tester",
            }]
        }))
        self.assertTrue((self.public / "unswept.md").is_file(), "fixture: stale mirror copy")

        r = self.run_tool(SEED, "--data-dir", str(self.data), "--public-dir", str(self.public))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("WITHHELD", r.stdout, f"the skip must be visible, got: {r.stdout!r}")
        self.assertIn("test eviction", r.stdout, "the skip must carry the tombstone's reason")

        after = ShelfStore(self.data, self.public)
        self.assertFalse(after.is_live(sha), "a tombstoned digest came back as a live row")
        self.assertEqual(after.lookup_sha256(sha)[0], 410)

    def test_seed_still_imports_a_row_that_has_no_tombstone(self):
        """The skip must be about the tombstone, not about seeding in general."""
        body = b"# ordinary already-hosted file\n"
        sha = sha256_hex(body)
        (self.public / "ordinary.md").write_bytes(body)
        mirror = {
            "artifacts": [{
                "sha256": sha, "filename": "ordinary.md", "bytes": len(body),
                "live": "/board-showcase/ordinary.md", "author": "someone",
            }]
        }
        (self.public / "manifest.json").write_text(json.dumps(mirror))

        r = self.run_tool(SEED, "--data-dir", str(self.data), "--public-dir", str(self.public))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"seeded ordinary.md {sha[:12]}", r.stdout)
        after = ShelfStore(self.data, self.public)
        self.assertTrue(after.is_live(sha), r.stdout)

    # ------------------------------------------------------------------- H2, the sweep-less evict

    def test_evict_refuses_to_claim_success_without_a_public_dir(self):
        """H2: without the web root the sweep cannot run, so the tool must not report withdrawal."""
        body = b"# still served\n"
        sha, _ = self.accept("still-served.md", body, "still-key-00000001")
        r = self.run_tool(EVICT, str(self.data), sha, "--reason", "no public dir given")
        self.assertNotEqual(r.returncode, 0, "an unsweepable eviction must not exit 0")
        self.assertIn("--public-dir", r.stderr)
        self.assertTrue((self.public / "still-served.md").is_file())
        self.assertFalse((self.data / "tombstones" / f"{sha}.json").is_file(),
                         "a refused eviction must not have written a tombstone")

    def test_evict_refuses_a_public_dir_that_is_the_data_dir(self):
        """The self-check would look for the mirror copy inside the store and never fire."""
        body = b"# no mirror here\n"
        sha, _ = self.accept("no-mirror.md", body, "nomirror-key-0000001")
        r = self.run_tool(EVICT, str(self.data), sha, "--public-dir", str(self.data), "--reason", "sneaky")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("must not be the data dir", r.stderr)

    # -------------------------------------------------------------------- H6, the refusal's advice

    def test_a_tombstone_can_be_lifted_with_a_reason_and_only_with_one(self):
        """H6: the refusal tells the uploader to ask the operator; the operator needs a tool."""
        body = b"# legitimately republished\n"
        sha, _ = self.accept("reused-name.md", body, "reused-key-0000001")
        self.evict(sha)

        without = self.run_tool(LIFT, str(self.data), sha)
        self.assertEqual(without.returncode, 2, "lifting without a reason must be refused")
        self.assertTrue((self.data / "tombstones" / f"{sha}.json").is_file())

        dry = self.run_tool(LIFT, str(self.data), sha, "--reason", "wrong bytes were evicted", "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("test eviction", dry.stdout,
                      "the tool must show the tombstone it is about to destroy")
        self.assertTrue((self.data / "tombstones" / f"{sha}.json").is_file(), "a dry run writes")

        real = self.run_tool(LIFT, str(self.data), sha, "--reason", "wrong bytes were evicted")
        self.assertEqual(real.returncode, 0, real.stderr)
        self.assertFalse((self.data / "tombstones" / f"{sha}.json").is_file())

        # And the digest is publishable again — the advice in the refusal now has a referent.
        s = ShelfStore(self.data, self.public)
        res = s.accept(
            content=body,
            check=make_check(body, "reused-name.md", "reused-key-0000002"),
            principal="tester-agent",
            idempotency_key="reused-key-0000002",
        )
        self.assertEqual(res["http"], 201, res)
        self.assertEqual(s.lookup_sha256(sha)[0], 200)

    def test_lifting_a_digest_that_has_no_tombstone_says_so(self):
        r = self.run_tool(LIFT, str(self.data), "0" * 64, "--reason", "nothing to lift")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no tombstone", r.stdout)

    # ---------------------------------------------------------- H5, the large lane's second door

    def test_large_lane_refuses_a_tombstoned_digest_at_init(self):
        """H5: the same condition, the same answer, whichever door the client used.

        Before the fix the large lane reserved quota and accepted three parts before the commit
        discovered the tombstone, and answered 422 COMMIT_REJECTED with the cause buried in
        `detail` — a different (status, code) pair from the small lane's 409 DIGEST_TOMBSTONED.
        """
        import shelf_uploads
        from shelf_lib import sha256_hex

        uploads = shelf_uploads.UploadStore(self.store)
        body = b"large bytes that were withdrawn" * 100
        sha = sha256_hex(body)
        self.store.tombstones.joinpath(f"{sha}.json").write_text(json.dumps({
            "sha256": sha, "state": "evicted", "filename": "large-card.md", "bytes": len(body),
            "evicted_at": "2026-09-11T00:00:00Z", "evicted_by": "tester",
            "reason": "fixture: withdrawn before the upload started",
        }))

        res = uploads.init_upload(
            meta_body={
                "sha256": sha, "bytes": len(body), "filename": "large-card.md",
                "name": "Large card", "author": "tester",
                "provenance": {"thread": "c7eeb4ea-3e64-417d-8b75-88ff72fd21b8"},
                "consent": "Host this file on the board-showcase shelf and Oracle mirror.",
                "part_size": 256 * 1024, "ttl_seconds": 2592000,
            },
            principal="tester-agent",
            idempotency_key="large-tombstoned-000001",
        )
        self.assertEqual(res.get("http"), 409, res)
        self.assertEqual(res.get("error"), "DIGEST_TOMBSTONED", res)
        # And nothing was reserved for work that could never land.
        self.assertEqual(uploads.reserved_bytes(), 0, "a refused init must reserve nothing")


if __name__ == "__main__":
    # Note for the next edit: no helper in this class may be called `run`. A method of that name
    # shadows TestCase.run, and unittest then reports "Ran 0 tests / NO TESTS RAN" with exit code
    # 5 while the loader insists it found every case — a check that silently checks nothing.
    # The helper here is run_tool on purpose.
    unittest.main(verbosity=2)
