#!/usr/bin/env python3
"""Eviction must reach both surfaces, keep what is still live, and destroy nothing.

The defect this file exists for: publish_public() copies live blobs into the web root and never
removes the copy of an object that stops being live. An object evicted through the store's own
tombstone path therefore kept being served from /board-showcase/<name> at its old filename while
/v1/blobs/<sha256> answered 410 — one authority, two surfaces, two answers.

Each test runs the tool as a subprocess through its documented CLI, so it observes what an operator
would run. EVICT_PY overrides the tool under test; pointing it at a mutant that skips the mirror
deletion MUST make this suite fail, which is how the suite was shown to discriminate:

    sed 's/^                p.unlink()/                pass/' tools/evict.py > /tmp/evict_mutant.py
    EVICT_PY=/tmp/evict_mutant.py python3 tools/test_evict.py   # must FAIL

Run: python3 tools/test_evict.py
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from shelf_lib import check_package, sha256_hex  # noqa: E402
from shelf_store import ShelfStore  # noqa: E402

EVICT = Path(os.environ.get("EVICT_PY", str(HERE / "evict.py"))).resolve()


def _check(content: bytes, filename: str, key: str):
    return check_package(
        content=content,
        declared_sha256=sha256_hex(content),
        declared_bytes=len(content),
        name=f"card {filename}",
        filename=filename,
        provenance={"thread": "8246bf16-1f79-466c-b757-0d011c414fdb"},
        author="tester",
        consent="Host this file on the board-showcase shelf.",
        principal="tester-agent",
        shelf_live_bytes=0,
        shelf_live_count=0,
        idempotency_key=key,
    )


class EvictionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        self.public = Path(self.tmp.name) / "webroot"
        self.store = ShelfStore(self.data, self.public)

    def tearDown(self):
        self.tmp.cleanup()

    def accept(self, filename: str, body: str, key: str) -> str:
        check = _check(body.encode(), filename, key)
        res = self.store.accept(
            content=body.encode(), check=check, principal="tester-agent", idempotency_key=key
        )
        self.assertIn(res["status"], ("accepted", "replay"), res)
        return check.sha256

    def evict(self, *shas: str, extra: tuple[str, ...] = ()):
        cmd = [sys.executable, str(EVICT), str(self.data), *shas,
               "--public-dir", str(self.public), "--reason", "capacity"]
        cmd.extend(extra)
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    def publish(self):
        self.store.rebuild_search()
        self.store.publish_public()

    def manifest(self) -> dict:
        return json.loads((self.data / "manifest.json").read_text())

    # ---------------------------------------------------------------- the defect

    def test_evicted_object_leaves_the_mirror_so_both_surfaces_agree(self):
        """The regression: the mirror copy of an evicted object must not outlive it.

        Fails on the pre-fix tool, where the mirror copy stayed served at the old filename while
        /v1/blobs/<sha> already answered 410.
        """
        sha = self.accept("gone.md", "# doomed\n", "key-gone-00000001")
        self.publish()
        self.assertTrue((self.public / "gone.md").is_file(), "fixture: mirror copy must exist first")

        r = self.evict(sha)
        self.assertEqual(r.returncode, 0, r.stderr)

        # Surface 1: the metadata lookup answers 410 for the digest.
        code, body = self.store.lookup_sha256(sha)
        self.assertEqual(code, 410, body)
        # Surface 2: the mirror no longer serves the old filename.
        self.assertFalse((self.public / "gone.md").exists(),
                         "the mirror still serves an evicted object at its old filename")
        # Same question, same answer, from both surfaces.
        opened, meta, path = self.store.open_blob(sha)
        self.assertEqual(opened, 410, meta)
        self.assertIsNone(path)

    def test_blob_file_survives_so_the_digest_stays_recoverable(self):
        """Eviction removes the public copy, never the bytes the operator holds."""
        sha = self.accept("kept.md", "# recoverable\n", "key-kept-00000001")
        self.publish()
        before = hashlib.sha256((self.data / "blobs" / sha).read_bytes()).hexdigest()

        self.assertEqual(self.evict(sha).returncode, 0)

        blob = self.data / "blobs" / sha
        self.assertTrue(blob.is_file(), "eviction destroyed the blob file")
        self.assertEqual(hashlib.sha256(blob.read_bytes()).hexdigest(), before)

    # ---------------------------------------------------------------- the keep rule

    def test_mirror_copy_kept_when_a_surviving_artifact_uses_the_filename(self):
        """One filename, two rows, one evicted: the shared URL must keep serving the survivor.

        Fails on a tool that unlinks public/<filename> unconditionally — that would break a live
        object's advertised URL to clean up an evicted one.

        The two rows are written into the manifest directly: accepting the same filename twice now
        supersedes the older row (see the orphan test below), so this shape can only arise from an
        older manifest or a hand edit, and the keep rule has to hold for it anyway.
        """
        dead = self.accept("shared.md", "# v1, to be evicted\n", "key-shared-00000001")
        alive = self.accept("shared.md", "# v2, still live\n", "key-shared-00000002")
        man = self.manifest()
        rows = [a for a in man["artifacts"] if a.get("sha256") == alive]
        # Insert the dead row *before* the survivor: publish_public() writes rows in order, so the
        # last row with a given filename owns the mirror path, and the survivor must own it here.
        idx = man["artifacts"].index(rows[0])
        man["artifacts"].insert(idx, {**rows[0], "sha256": dead, "bytes": len(b"# v1, to be evicted\n")})
        (self.data / "manifest.json").write_text(json.dumps(man, indent=2, sort_keys=True))
        self.publish()
        self.assertEqual((self.public / "shared.md").read_bytes(), b"# v2, still live\n",
                         "fixture: the surviving row must own the mirror path")

        self.assertEqual(self.evict(dead).returncode, 0)

        self.assertTrue((self.public / "shared.md").is_file(),
                        "the mirror deleted a filename that a live artifact still uses")
        self.assertEqual(self.store.lookup_sha256(dead)[0], 410)
        self.assertEqual(self.store.lookup_sha256(alive)[0], 200)
        self.assertEqual((self.public / "shared.md").read_bytes(), b"# v2, still live\n")

    # ---------------------------------------------------------------- supersede / orphans

    def test_supersede_leaves_the_old_bytes_labelled_orphan_not_live(self):
        """Re-publishing under one filename replaces the row; the old digest is not 'live'.

        Measured behaviour, worth pinning: lookup answers 200 with state orphan_blob, naming it a GC
        candidate, while the catalog counts only the surviving row.

        KNOWN GAP (named here, not hidden): `live_totals()` counts manifest rows, so bytes the shelf
        still serves as an orphan are invisible to the shelf's own quota. Repeatedly superseding one
        filename therefore grows the disk without moving the quota counters.
        """
        old = self.accept("same.md", "# old\n", "key-supersede-000001")
        new = self.accept("same.md", "# new\n", "key-supersede-000002")
        self.assertNotEqual(old, new)
        self.publish()

        rows = [a for a in self.manifest()["artifacts"] if a.get("filename") == "same.md"]
        self.assertEqual([a["sha256"] for a in rows], [new], "supersede must leave exactly one row")
        self.assertIsNone(self.store.tombstone_of(old), "no tombstone: nothing was withdrawn")

        code, body = self.store.lookup_sha256(old)
        self.assertEqual(code, 200)
        self.assertEqual(body["state"], "orphan_blob", body)
        self.assertIsNotNone(self.store.get_blob(old), "superseded bytes must stay served")

        # The gap, stated as an assertion so it is visible when someone closes it.
        counted = {a["sha256"] for a in self.manifest()["artifacts"]}
        self.assertNotIn(old, counted, "if orphans now count toward quota, update this test")
        self.assertEqual((self.public / "same.md").read_bytes(), b"# new\n")

        # What the tool must not hide: the served-but-uncounted bytes, named in its own summary.
        self.assertEqual(self.store.orphan_totals(), (len(b"# old\n"), 1))
        r = self.evict(old)
        self.assertIn("served but uncounted", r.stdout)
        self.assertIn("1 blob(s)", r.stdout)
        self.assertEqual(self.store.orphan_totals(), (len(b"# old\n"), 1),
                         "reporting an orphan must not change the count")

    # ---------------------------------------------------------------- the record

    def test_tombstone_records_reason_and_time_and_manifest_drops_the_entry(self):
        sha = self.accept("record.md", "# recorded\n", "key-record-00000001")
        self.publish()
        gen_before = int(self.manifest().get("manifest_generation") or 0)

        self.assertEqual(self.evict(sha).returncode, 0)

        tomb = self.store.tombstone_of(sha)
        self.assertIsNotNone(tomb, "no tombstone was written")
        self.assertEqual(tomb["reason"], "capacity")
        self.assertTrue(tomb.get("evicted_at"))
        self.assertEqual(tomb["bytes"], len(b"# recorded\n"))
        self.assertEqual([a for a in self.manifest()["artifacts"] if a.get("sha256") == sha], [],
                         "the evicted object is still advertised in the manifest")
        self.assertGreater(int(self.manifest()["manifest_generation"]), gen_before)
        # The published catalog carries the tombstone too, not just the private one.
        pub = json.loads((self.public / "search.json").read_text())
        self.assertIn(sha, {t.get("sha256") for t in pub.get("tombstones") or []})

    def test_dry_run_writes_nothing(self):
        sha = self.accept("dry.md", "# dry\n", "key-dry-00000001")
        self.publish()
        man_before = (self.data / "manifest.json").read_bytes()

        r = self.evict(sha, extra=("--dry-run",))

        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((self.public / "dry.md").is_file(), "dry run removed the mirror copy")
        self.assertEqual((self.data / "manifest.json").read_bytes(), man_before)
        self.assertIsNone(self.store.tombstone_of(sha))
        self.assertEqual(self.store.lookup_sha256(sha)[0], 200)

    def test_unknown_digest_is_reported_and_no_tombstone_is_invented(self):
        """A digest that was never live is 'not found', never a fabricated tombstone."""
        ghost = "0" * 64

        r = self.evict(ghost)

        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("not found", r.stdout + r.stderr)
        self.assertIn(ghost, r.stdout + r.stderr)
        self.assertIsNone(self.store.tombstone_of(ghost))
        self.assertEqual(self.store.lookup_sha256(ghost)[0], 404)

    def test_missing_digest_argument_is_a_usage_error(self):
        """A data dir with no digests is a usage error, not a silent no-op."""
        r = subprocess.run([sys.executable, str(EVICT), str(self.data)],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage", (r.stdout + r.stderr).lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
