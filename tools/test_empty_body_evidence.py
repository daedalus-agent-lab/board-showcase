#!/usr/bin/env python3
"""An empty response is an error, not a verdict.

Measured twice in one working session, on real checks:

  * a probe ran with its base-URL variable unset, so every response was empty. Each empty body was
    hashed (e3b0c442… is a perfectly valid sha256), compared, found different, and printed as a
    mismatch. Sixteen non-requests became a table of sixteen findings that looked like a shelf bug.
  * a second probe compared an owner digest against the empty-body digest and reported every
    correct row as a failure.

Both were caught only because the check failed loudly. Had the comparison run the other way — an
expected value that happens to be the empty digest, or a check that treats "no data" as "no
mismatch" — the same instrument would have reported PASS for work it never did.

These tests pin the rule for the repo's own client: a read that returns nothing raises, so no call
site can compare it, and the failure names what happened.

Run: python3 tools/test_empty_body_evidence.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from verify_shelf import fetch  # noqa: E402

EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


class EmptyBodyTests(unittest.TestCase):
    def test_an_empty_body_raises_instead_of_returning_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty.txt"
            empty.write_bytes(b"")
            with self.assertRaises(ValueError) as caught:
                fetch(empty.as_uri())
            message = str(caught.exception)
            self.assertIn("empty body", message)
            self.assertIn("not evidence", message)

    def test_the_empty_digest_is_named_so_nobody_mistakes_it_for_data(self):
        # The digest is real; that is exactly why it must never be silently compared.
        import hashlib

        self.assertEqual(hashlib.sha256(b"").hexdigest(), EMPTY_SHA256)
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty.txt"
            empty.write_bytes(b"")
            try:
                fetch(empty.as_uri())
            except ValueError as e:
                self.assertNotIn(EMPTY_SHA256, str(e), "the message must not be mistakable for a digest")

    def test_a_body_of_whitespace_is_data_and_does_not_raise(self):
        # The rule is about *nothing*, not about smallness: a one-byte body is an answer.
        with tempfile.TemporaryDirectory() as tmp:
            tiny = Path(tmp) / "tiny.txt"
            tiny.write_bytes(b"\n")
            self.assertEqual(fetch(tiny.as_uri()), b"\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
