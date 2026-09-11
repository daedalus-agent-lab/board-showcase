#!/usr/bin/env python3
"""The agreement check must fail on every way the three surfaces can disagree.

A checker that has only ever been run on a healthy shelf is not evidence. Each test below changes
exactly one surface away from the other two and requires a non-zero exit naming the right
invariant; the clean case must exit 0, or the red is meaningless.

Run: python3 tools/test_agreement.py
"""
from __future__ import annotations

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

CHECK = Path(os.environ.get("AGREEMENT_PY", str(HERE / "verify_agreement.py"))).resolve()


def _check(content: bytes, filename: str, key: str, retentions=None):
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
        retentions=retentions,
    )


class AgreementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        self.public = Path(self.tmp.name) / "webroot"
        self.store = ShelfStore(self.data, self.public)

    def tearDown(self):
        self.tmp.cleanup()

    def accept(self, filename: str, body: str, key: str, retentions=None) -> str:
        check = _check(body.encode(), filename, key, retentions=retentions)
        self.store.accept(content=body.encode(), check=check,
                          principal="tester-agent", idempotency_key=key)
        return check.sha256

    def publish(self):
        self.store.rebuild_search()
        self.store.publish_public()

    def run_check(self, *extra: str) -> tuple[int, str]:
        cmd = [sys.executable, str(CHECK), "--data", str(self.data),
               "--public", str(self.public), *extra]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        return r.returncode, r.stdout + r.stderr

    # ---------------------------------------------------------------- the baseline

    def test_agreeing_shelf_passes(self):
        self.accept("one.md", "# one\n", "key-agree-00000001")
        self.accept("two.md", "# two\n", "key-agree-00000002")
        self.publish()

        code, out = self.run_check()

        self.assertEqual(code, 0, out)
        self.assertIn("F3 mirror serves each advertised filename", out)

    # ---------------------------------------------------------------- forward breaks

    def test_mismatched_mirror_bytes_fail_the_filename_invariant(self):
        """The original defect: the catalog and the mirror disagree about the same URL."""
        self.accept("drift.md", "# the honest bytes\n", "key-drift-00000001")
        self.publish()
        (self.public / "drift.md").write_bytes(b"# bytes nobody advertised\n")

        code, out = self.run_check()

        self.assertEqual(code, 1, out)
        self.assertIn("F3", out)
        self.assertIn("drift.md serves", out)

    def test_missing_mirror_file_fails(self):
        self.accept("absent.md", "# here\n", "key-absent-00000001")
        self.publish()
        (self.public / "absent.md").unlink()

        code, out = self.run_check()

        self.assertEqual(code, 1, out)
        self.assertIn("absent.md not served by the mirror", out)

    def test_missing_blob_fails_the_digest_invariant(self):
        sha = self.accept("gone.md", "# here\n", "key-gone-00000001")
        self.publish()
        (self.data / "blobs" / sha).unlink()

        code, out = self.run_check()

        self.assertEqual(code, 1, out)
        self.assertIn("F1/F2", out)
        self.assertIn("not served", out)

    # ---------------------------------------------------------------- reverse breaks

    def test_evicted_object_left_in_the_mirror_fails(self):
        """The regression the whole tool exists for, observed from the outside this time."""
        sha = self.accept("evicted.md", "# withdrawn\n", "key-evicted-00000001")
        self.publish()
        subprocess.run([sys.executable, str(HERE / "evict.py"), str(self.data), sha,
                        "--public-dir", str(self.public), "--reason", "test"],
                       capture_output=True, text=True, timeout=180)
        # Put the withdrawn bytes back where the reader would look for them.
        (self.public / "evicted.md").write_bytes(b"# withdrawn\n")

        code, out = self.run_check()

        self.assertEqual(code, 1, out)
        self.assertIn("R1/R2", out)
        self.assertIn("mirror evicted.md still serves", out)

    def test_evicted_bytes_still_served_by_the_api_fail(self):
        sha = self.accept("api-gone.md", "# withdrawn\n", "key-apigone-00000001")
        self.publish()
        subprocess.run([sys.executable, str(HERE / "evict.py"), str(self.data), sha,
                        "--public-dir", str(self.public), "--reason", "test"],
                       capture_output=True, text=True, timeout=180)
        # A tombstone plus a live blob file is the state the API's 410 masks on disk.
        (self.data / "tombstones" / f"{sha}.json").unlink()

        code, out = self.run_check()

        # With no tombstone the object is simply an orphan, not a violation: the check reports it
        # under accounting, and only --strict turns that into a failure.
        self.assertEqual(code, 0, out)
        self.assertIn("A1 served but uncounted", out)
        strict_code, strict_out = self.run_check("--strict")
        self.assertEqual(strict_code, 1, strict_out)

    def test_mirror_withdrawal_index_missing_an_entry_fails(self):
        sha = self.accept("indexed.md", "# indexed\n", "key-indexed-00000001")
        self.publish()
        subprocess.run([sys.executable, str(HERE / "evict.py"), str(self.data), sha,
                        "--public-dir", str(self.public), "--reason", "test"],
                       capture_output=True, text=True, timeout=180)
        idx = json.loads((self.public / "tombstones.json").read_text())
        idx["by_sha256"].pop(sha)
        (self.public / "tombstones.json").write_text(json.dumps(idx))

        code, out = self.run_check()

        self.assertEqual(code, 1, out)
        self.assertIn("A2", out)

    # ---------------------------------------------------------------- promises

    def test_row_forbidding_a_mirror_copy_advertises_no_mirror_url(self):
        """The generator half of F4: do not promise a copy that publish_public() will not make."""
        self.accept("private.md", "# stays on the API only\n", "key-private-00000001",
                    retentions={"mirror_copy_allowed": False})
        self.publish()

        row = [a for a in self.store.load_manifest()["artifacts"] if a["filename"] == "private.md"][0]
        self.assertNotIn("mirror", row, "a row that forbids mirroring must not advertise a mirror URL")
        self.assertFalse((self.public / "private.md").exists())

        code, out = self.run_check()
        self.assertEqual(code, 0, out)
        self.assertIn("F4", out)

    def test_advertised_but_forbidden_mirror_url_fails(self):
        """The checker half of F4: a re-introduced promise must be caught, not tolerated."""
        self.accept("claimed.md", "# promised\n", "key-claimed-00000001",
                    retentions={"mirror_copy_allowed": False})
        self.publish()
        man = json.loads((self.data / "manifest.json").read_text())
        for a in man["artifacts"]:
            if a["filename"] == "claimed.md":
                a["mirror"] = "https://158.178.144.114/board-showcase/claimed.md"
        (self.data / "manifest.json").write_text(json.dumps(man, indent=2, sort_keys=True))

        code, out = self.run_check()

        self.assertEqual(code, 1, out)
        self.assertIn("F4", out)
        self.assertIn("forbids mirror copies yet advertises", out)

    # ---------------------------------------------------------------- fail-closed

    def test_unreadable_catalog_is_unknown_not_pass(self):
        """A check that cannot read its input must not report agreement."""
        code, out = self.run_check()

        self.assertNotEqual(code, 0, out)
        self.assertIn("UNKNOWN", out)

    def test_unexpected_agreement_endpoint_shape_is_unknown_not_zero(self):
        """A missing key must not be read as 'no orphans': that is how a live report said 0 while
        the shelf served 16 uncounted blobs. The parse is exercised against a stub server."""
        import http.server
        import threading

        class Stub(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path == "/mirror/manifest.json":
                    body = json.dumps({"artifacts": []}).encode()
                elif self.path == "/mirror/tombstones.json":
                    body = json.dumps({"by_sha256": {}, "by_name": {}}).encode()
                elif self.path == "/api/v1/shelf/agreement":
                    body = json.dumps({"orphans": {"meaning": "no count field here"}}).encode()
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):  # silence
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{srv.server_port}"
            r = subprocess.run([sys.executable, str(CHECK), "--api", f"{base}/api",
                                "--mirror", f"{base}/mirror"],
                               capture_output=True, text=True, timeout=180)
        finally:
            srv.shutdown()
        out = r.stdout + r.stderr
        self.assertIn("A1 served but uncounted", out)
        self.assertIn("UNKNOWN", out)
        self.assertNotIn("0 blob(s), 0 bytes", out)

    def test_missing_data_directory_is_not_a_pass(self):
        cmd = [sys.executable, str(CHECK), "--data", str(Path(self.tmp.name) / "nope")]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
