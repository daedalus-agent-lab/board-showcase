#!/usr/bin/env python3
"""The note a catalog row carries must describe the row it is attached to.

Two failure shapes are pinned here. A row that promises a check it cannot support (no digest, no
bytes, no URL, but a note telling the reader how to verify it) — worse than silence, because it
reads like evidence. And a row that carries declared values: checkable, but not by this shelf, so
the repair must keep the claim and drop only the unbacked instruction.

The tool must also be idempotent and must not touch rows it has no business touching.
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

TOOL = Path(__file__).resolve().parent / "repair_mirror_claims.py"


class NoteRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.data = root / "data"
        self.public = root / "public"
        self.store = ShelfStore(self.data, self.public)
        for d in ("blobs", "operations", "keys", "tombstones"):
            (self.data / d).mkdir(parents=True, exist_ok=True)
        (self.data / "manifest.json").write_text(json.dumps({"artifacts": [], "manifest_generation": 1}))

    def tearDown(self):
        self.tmp.cleanup()

    def run_tool(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(TOOL), str(self.data), "--public-dir", str(self.public), *extra],
            capture_output=True, text=True)

    def write_rows(self, rows: list[dict], generation: int = 1) -> None:
        (self.data / "manifest.json").write_text(
            json.dumps({"artifacts": rows, "manifest_generation": generation}))

    def test_a_note_promising_a_check_the_row_cannot_support_is_repaired(self):
        self.write_rows([{
            "name": "Bureau of impractical inventions", "author": "@someone", "sha256": None,
            "bytes": None, "live": None, "verification": "link-only",
            "note": "Not content-addressed: sha256/bytes null; verify by live URL only.",
            "provenance": {"thread": "76f8a207"},
        }])
        dry = self.run_tool()
        self.assertIn("promises evidence the row does not carry: 1", dry.stdout)
        self.assertIn("dry run", dry.stdout)
        self.assertIsNone(self.store.load_manifest()["artifacts"][0].get("note_repaired"))

        r = self.run_tool("--apply")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        row = self.store.load_manifest()["artifacts"][0]
        self.assertNotIn("live URL only", row["note"])
        self.assertIn("provenance", row["note"])
        self.assertIn("check the claim at its source (thread=76f8a207)", row["note"])
        self.assertIn(dry.stdout.split("  '")[1].split("'  ")[1][:40], row["note_repaired"]["was"])
        self.assertEqual(row["author"], "@someone", "the repair is the shelf's note, not the author's")
        self.assertEqual(row["provenance"], {"thread": "76f8a207"})

    def test_a_row_asserting_nothing_says_so(self):
        self.write_rows([{"name": "empty", "sha256": None, "bytes": None, "live": None,
                          "note": "verify by live URL only"}])
        self.run_tool("--apply")
        note = self.store.load_manifest()["artifacts"][0]["note"]
        self.assertIn("Nothing on this shelf verifies this row", note)
        self.assertIn("a claim was made, not that anything is served", note)

    def test_declared_values_are_kept_and_labelled(self):
        self.write_rows([{"name": "SAR-010", "sha256": None, "bytes": None, "live": None,
                          "sha256_declared": "ab" * 32, "bytes_declared": 6018,
                          "note": "Not content-addressed: verify by live URL only.",
                          "provenance": {"repo": "x/y"}}])
        self.run_tool("--apply")
        row = self.store.load_manifest()["artifacts"][0]
        self.assertIn("declared digest and size", row["note"])
        self.assertIn("repo=x/y", row["note"])
        self.assertEqual(row["sha256_declared"], "ab" * 32, "the claim stays; the instruction goes")

    def test_rows_that_can_be_checked_are_left_alone(self):
        self.write_rows([
            {"name": "external", "sha256": None, "bytes": None,
             "live": "https://example.invalid/x", "note": "verify by live URL only"},
            {"name": "shelf", "sha256": "cd" * 32, "bytes": 12, "note": "verify by live URL only"},
        ])
        r = self.run_tool("--apply")
        self.assertIn("promises evidence the row does not carry: 0", r.stdout)
        for row in self.store.load_manifest()["artifacts"]:
            self.assertNotIn("note_repaired", row)

    def test_a_second_run_repairs_nothing(self):
        self.write_rows([{"name": "empty", "sha256": None, "bytes": None, "live": None,
                          "note": "verify by live URL only"}])
        self.run_tool("--apply")
        again = self.run_tool("--apply")
        self.assertIn("nothing to repair (idempotent)", again.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
