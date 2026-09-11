#!/usr/bin/env python3
"""The reconstructed-predecessor path: a weaker claim, kept weaker.

An attributed blob has a pointer to what displaced it, and that pointer was inferred rather than
witnessed. These tests exist because the tempting shortcut — write the inference into `supersedes`
and let one code path read both — destroys the only thing a reader needs to know: whether the shelf
saw the replacement or worked it out afterwards.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shelf_store import ShelfStore  # noqa: E402

TOOL = Path(__file__).resolve().parent / "attribute_orphans.py"


class AttributionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.data = root / "data"
        self.public = root / "public"
        (self.public / "board-showcase").mkdir(parents=True)
        self.store = ShelfStore(self.data, self.public)
        for d in ("blobs", "operations", "keys", "tombstones"):
            (self.data / d).mkdir(parents=True, exist_ok=True)
        (self.data / "manifest.json").write_text(json.dumps({"artifacts": []}))

    def tearDown(self):
        self.tmp.cleanup()

    # ---------------------------------------------------------------- fixtures
    def seed(self, filename: str, body: bytes, key: str, digest: str | None = None) -> str:
        """Place a blob plus an acceptance receipt, bypassing accept() so a legacy shape is
        reproducible: bytes and receipt on disk, no manifest row, no supersedes entry."""
        import hashlib
        d = digest or hashlib.sha256(body).hexdigest()
        (self.data / "blobs" / d).write_bytes(body)
        (self.data / "operations" / f"{key}.json").write_text(json.dumps(
            {"sha256": d, "filename": filename, "bytes": len(body), "key": key}))
        return d

    def live_row(self, filename: str, body: bytes) -> str:
        import hashlib
        d = hashlib.sha256(body).hexdigest()
        (self.data / "blobs" / d).write_bytes(body)
        man = json.loads((self.data / "manifest.json").read_text())
        man["artifacts"].append({"sha256": d, "filename": filename, "bytes": len(body)})
        (self.data / "manifest.json").write_text(json.dumps(man))
        return d

    # ---------------------------------------------------------------- the claim
    def test_an_inferred_predecessor_is_neither_live_nor_witnessed(self):
        old = self.seed("same.md", b"# old\n", "key-old-00000001")
        new = self.live_row("same.md", b"# new\n")

        self.assertEqual(self.store.orphan_totals()[1], 1, "before attribution: unexplained")
        r = subprocess.run([sys.executable, str(TOOL), "--data", str(self.data), "--write"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.store.orphan_totals()[1], 0, "attributed is not orphan")
        self.assertEqual(self.store.attributed_totals()[1], 1)
        self.assertEqual(self.store.superseded_totals()[1], 0,
                         "an inference must not become a witnessed replacement")
        live_b, live_n = self.store.live_totals()
        att_b, att_n = self.store.attributed_totals()
        self.assertEqual(self.store.served_totals(), (live_b + att_b, live_n + att_n),
                         "the four buckets must still sum to what is served")

        code, body = self.store.lookup_sha256(old)
        self.assertEqual(code, 200)
        self.assertEqual(body["state"], "orphan_blob", "no write path witnessed this displacement")
        inf = body["predecessors_inferred"]
        self.assertEqual(inf["superseded_by"], new)
        self.assertFalse(inf["witnessed"])
        self.assertIn("limitation", json.loads((self.data / "attributed.json").read_text())
                      ["entries"][old])

    def test_a_witnessed_supersede_is_never_rewritten_by_reconstruction(self):
        old = self.seed("same.md", b"# old\n", "key-old-00000001")
        self.live_row("same.md", b"# new\n")
        man = json.loads((self.data / "manifest.json").read_text())
        man["artifacts"][0]["supersedes"] = {"sha256": old, "filename": "same.md",
                                             "reason": "filename_replaced"}
        (self.data / "manifest.json").write_text(json.dumps(man))

        r = subprocess.run([sys.executable, str(TOOL), "--data", str(self.data), "--write"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.store.attributed(), {},
                         "the write path already recorded it; reconstruction must not double-file it")
        self.assertEqual(self.store.superseded_totals()[1], 1)

    def test_no_receipt_means_no_attribution_and_the_tool_fails_loudly(self):
        digest = "aa" * 32
        (self.data / "blobs" / digest).write_bytes(b"nothing names me\n")
        r = subprocess.run([sys.executable, str(TOOL), "--data", str(self.data), "--write"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1, "a refused candidate must not exit quietly")
        self.assertIn("no acceptance receipt names a filename", r.stdout)
        self.assertFalse((self.data / "attributed.json").exists(),
                         "nothing may be written when a candidate was refused")

    def test_a_receipt_without_a_live_owner_is_refused(self):
        self.seed("gone.md", b"# old\n", "key-old-00000001")
        r = subprocess.run([sys.executable, str(TOOL), "--data", str(self.data)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no live row owns it", r.stdout)
        self.assertIn("0 attributable blob(s)", r.stdout)

    def test_two_blobs_that_held_one_name_are_flagged_as_an_ambiguous_chain(self):
        """A points at the current owner of the name; so does B. Only one of them can have been
        displaced by it. The pointer must not imply an immediate-successor relation nobody
        recorded — 9 of the 16 live attributions have this shape."""
        a = self.seed("shared.md", b"# first\n", "key-amb-00000001")
        b = self.seed("shared.md", b"# second\n", "key-amb-00000002")
        self.live_row("shared.md", b"# current\n")
        r = subprocess.run([sys.executable, str(TOOL), "--data", str(self.data), "--write"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        entries = json.loads((self.data / "attributed.json").read_text())["entries"]
        self.assertEqual(sorted(entries), sorted([a, b]))
        for digest in (a, b):
            self.assertTrue(entries[digest]["chain_ambiguous"])
            self.assertTrue(any("not necessarily the immediate successor" in e
                                for e in entries[digest]["evidence"]))
        self.assertEqual(self.store.orphan_totals()[1], 0)
        self.assertEqual(self.store.attributed_totals()[1], 2)

    def test_a_name_that_was_withdrawn_breaks_the_chain_and_is_refused(self):
        """The evidence line every entry prints — 'no tombstone under that filename' — asserted as a
        check, not as prose.

        The reason it gives is the scenario: a withdrawn name is free, so the live row owning it now
        may be an unrelated object that took it. The tool read the tombstone directory by digest,
        which answers a different question, and attributed anyway while printing the sentence that
        says it did not.
        """
        withdrawn = self.seed("x.md", b"# P, later withdrawn\n", "key-p-00000001")
        (self.data / "tombstones" / f"{withdrawn}.json").write_text(json.dumps(
            {"sha256": withdrawn, "state": "evicted", "filename": "x.md",
             "evicted_at": "2026-09-01T00:00:00Z", "reason": "capacity"}))
        self.live_row("x.md", b"# Q, an unrelated object that took the freed name\n")
        orphan = self.seed("x.md", b"# R, unexplained\n", "key-r-00000001")

        r = subprocess.run([sys.executable, str(TOOL), "--data", str(self.data)],
                           capture_output=True, text=True)

        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("refused", r.stdout)
        self.assertIn(orphan[:12], r.stdout)
        self.assertNotIn(f"attributable {orphan[:12]}", r.stdout)
        self.assertFalse((self.data / "attributed.json").exists())

    def test_a_record_written_by_hand_does_not_silently_leave_the_orphan_count(self):
        """`attributed.json` is the one input to the accounting no write path produces.

        The tool refuses a candidate that fails an evidence check — but a refusal inside one writer
        is not a property of the file. The same evidence is re-checked where the count is consumed,
        so a plausible record cannot move a blob out of the orphan bucket unnoticed.
        """
        self.live_row("live.md", b"# live\n")
        bogus = "e" * 64
        (self.data / "blobs" / bogus).write_bytes(b"unexplainable bytes\n")
        owner = json.loads((self.data / "manifest.json").read_text())["artifacts"][0]["sha256"]
        (self.data / "attributed.json").write_text(json.dumps(
            {"entries": {bogus: {"sha256": bogus, "superseded_by": owner, "filename": "live.md",
                                 "basis": "filename-and-acceptance-receipt",
                                 "evidence": ["fabricated"], "witnessed": False}}}))

        problems = self.store.attributed_unsupported()

        self.assertTrue(problems, "a hand-written record passed every check the store makes")
        self.assertIn(bogus[:12], " ".join(problems))

    def test_an_entry_claiming_to_be_witnessed_is_not_read_as_a_reconstruction(self):
        """`witnessed: false` is the whole point of the separate field. An entry that says otherwise
        is not a reconstruction, and reading it as one would give an inference the standing of a
        receipt — the exact confusion the second file exists to prevent."""
        old = self.seed("same.md", b"# old\n", "key-w-00000001")
        new = self.live_row("same.md", b"# new\n")
        (self.data / "attributed.json").write_text(json.dumps(
            {"entries": {old: {"sha256": old, "superseded_by": new, "filename": "same.md",
                               "witnessed": True}}}))

        self.assertEqual(self.store.attributed(), {},
                         "an entry claiming to be witnessed was read as a reconstruction")
        self.assertEqual(self.store.attributed_totals()[1], 0)
        self.assertEqual(self.store.orphan_totals()[1], 1,
                         "the blob is unexplained again, which is the honest answer")

    def test_bytes_that_do_not_hash_to_their_own_name_are_refused(self):
        """The tool's own hash check, asserted rather than assumed: with it disabled the suite stayed
        green, so the check that the served bytes are the bytes the digest names was untested.

        It is the check that separates a reconstruction from a guess — every other piece of evidence
        is about names, and names are what this blob would otherwise be attributed by.
        """
        corrupt = "f" * 64
        (self.data / "blobs" / corrupt).write_bytes(b"# bytes that are not this digest\n")
        (self.data / "operations" / "corrupt-op.json").write_text(json.dumps(
            {"sha256": corrupt, "filename": "same.md", "bytes": 33}))
        self.live_row("same.md", b"# new\n")

        r = subprocess.run([sys.executable, str(TOOL), "--data", str(self.data)],
                           capture_output=True, text=True)

        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("do not hash", r.stdout)
        self.assertFalse((self.data / "attributed.json").exists())

    def test_a_dry_run_writes_nothing(self):
        self.seed("same.md", b"# old\n", "key-old-00000001")
        self.live_row("same.md", b"# new\n")
        r = subprocess.run([sys.executable, str(TOOL), "--data", str(self.data)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("dry run", r.stdout)
        self.assertFalse((self.data / "attributed.json").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
