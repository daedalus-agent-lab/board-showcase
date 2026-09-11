#!/usr/bin/env python3
"""A tombstoned digest must not come back to life through the intake door.

The defect this file exists for, found by running a state-transition input (evict, then re-upload
the identical bytes): intake accepted the resurrection, so a live artifact existed whose
/v1/blobs/{sha256} answered 410 — the name surface served the object while the digest surface
reported it evicted. One object, two surfaces, two answers, and the digest is exactly the address a
third party pins. 410 there now meant "these bytes are live under a name".

The rule: eviction retires the *address*, and intake is where that is enforced. Refusing is the
fail-closed choice — the alternative (letting the digest answer 200 again) would silently undo an
operator's decision to retire an object.

The guard sits after the idempotency replay branch on purpose: an exact retry of an accepted
package is a replay of a historical receipt, not a new publication, and must keep working.

Discrimination — the suite must go red when the guard is removed, which is how it was shown to
test something (the guard block is the `if tomb is not None:` return in accept()):

    python3 - <<'PY'
    import re, pathlib
    src = pathlib.Path("tools/shelf_store.py").read_text()
    start = src.index("            # A tombstoned digest is a retired address.")
    end = src.index("            # Stage bytes first", start)
    pathlib.Path("tools/_mutant_store.py").write_text(src[:start] + src[end:])
    PY
    STORE_PY=tools/_mutant_store.py python3 tools/test_tombstone_intake.py   # must FAIL

Keep the mutant beside the store (tools/), not elsewhere: shelf_store imports shelf_lib from its own
directory, so a mutant in another directory fails on import and every test goes red — a suite-wide
red that looks like a broken check but is only a misplaced file.

Run: python3 tools/test_tombstone_intake.py
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from shelf_lib import check_package, sha256_hex  # noqa: E402

STORE_PY = Path(os.environ.get("STORE_PY", str(HERE / "shelf_store.py"))).resolve()
EVICT = Path(os.environ.get("EVICT_PY", str(HERE / "evict.py"))).resolve()

_spec = importlib.util.spec_from_file_location("shelf_store_under_test", STORE_PY)
_store_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_store_mod)
ShelfStore = _store_mod.ShelfStore


def _check(content: bytes, filename: str, key: str):
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


class TombstoneIntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        self.public = Path(self.tmp.name) / "webroot"
        self.store = ShelfStore(self.data, self.public)

    def tearDown(self):
        self.tmp.cleanup()

    def accept(self, filename: str, body: str, key: str) -> dict:
        check = _check(body.encode(), filename, key)
        return self.store.accept(
            content=body.encode(), check=check, principal="tester-agent", idempotency_key=key
        )

    def evict(self, *shas: str):
        return subprocess.run(
            [sys.executable, str(EVICT), str(self.data), *shas,
             "--public-dir", str(self.public), "--reason", "evicted for the intake test"],
            capture_output=True, text=True, timeout=120,
        )

    # ------------------------------------------------------------------ the defect

    def test_resurrection_of_a_tombstoned_digest_is_refused(self):
        """The measured defect: evict, then re-upload the identical bytes."""
        body = "these bytes will be evicted and then offered again\n"
        first = self.accept("retired.txt", body, "key-retired-000001")
        self.assertEqual(first["http"], 201, first)
        sha = first["receipt"]["sha256"] if "receipt" in first else first["sha256"]
        self.store.publish_public()

        r = self.evict(sha)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        opened, meta, path = self.store.open_blob(sha)
        self.assertEqual(opened, 410, meta)

        again = self.accept("retired.txt", body, "key-retired-000002")
        self.assertEqual(again.get("error"), "DIGEST_TOMBSTONED", again)
        self.assertEqual(again.get("http"), 409, again)

    def test_a_refused_resurrection_leaves_no_live_row_and_no_revival(self):
        """A refusal must not half-publish: the digest stays 410 and the name is not served."""
        body = "no half state: the refusal must be complete\n"
        first = self.accept("halfstate.txt", body, "key-half-00000001")
        sha = first["receipt"]["sha256"] if "receipt" in first else first["sha256"]
        self.store.publish_public()
        self.assertTrue((self.public / "halfstate.txt").is_file(), "fixture: mirror copy first")
        self.evict(sha)

        self.accept("halfstate.txt", body, "key-half-00000002")

        opened, meta, path = self.store.open_blob(sha)
        self.assertEqual(opened, 410, meta)
        self.assertIsNone(path)
        self.assertFalse((self.public / "halfstate.txt").exists(),
                         "the refusal still left a served copy of retired bytes")
        self.store.rebuild_search()
        listed = self.store.search("halfstate")
        names = [h.get("filename") for h in (listed.get("artifacts") or [])]
        self.assertNotIn("halfstate.txt", names, listed)

    def test_the_refusal_names_the_tombstone(self):
        """Fail-closed is only useful if the uploader can see why, and that the retirement was a choice."""
        body = "the refusal must name the tombstone that stopped it\n"
        first = self.accept("named.txt", body, "key-named-0000001")
        sha = first["receipt"]["sha256"] if "receipt" in first else first["sha256"]
        self.evict(sha)

        again = self.accept("named.txt", body, "key-named-0000002")
        self.assertEqual(again.get("error"), "DIGEST_TOMBSTONED", again)
        self.assertIn(sha, again.get("message", ""), again)
        self.assertIn("tombstone", again, again)
        self.assertEqual(again["tombstone"].get("sha256"), sha, again)

    def test_an_exact_retry_still_replays(self):
        """The guard must not break the retry rule: same key and content replays the receipt."""
        body = "an exact retry is a replay, not a publication\n"
        first = self.accept("replay.txt", body, "key-replay-0000001")
        second = self.accept("replay.txt", body, "key-replay-0000001")
        self.assertEqual(second["status"], "replay", second)
        self.assertEqual(second["http"], 200, second)

    def test_different_bytes_under_a_tombstoned_name_are_still_accepted(self):
        """The guard blocks the retired digest, not the freed name."""
        first = self.accept("reused.txt", "the first occupant\n", "key-reuse-0000001")
        self.assertEqual(first.get("http"), 201, first)
        sha = first["receipt"]["sha256"] if "receipt" in first else first["sha256"]
        self.evict(sha)

        second = self.accept("reused.txt", "the second occupant, different bytes\n", "key-reuse-0000002")
        self.assertIn(second.get("status"), ("accepted", None), second)
        self.assertEqual(second.get("http"), 201, second)


if __name__ == "__main__":
    unittest.main(verbosity=2)
