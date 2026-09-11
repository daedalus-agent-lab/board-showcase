#!/usr/bin/env python3
"""The agreement check must fail on every way the three surfaces can disagree.

A checker that has only ever been run on a healthy shelf is not evidence. Each test below changes
exactly one surface away from the other two and requires a non-zero exit naming the right
invariant; the clean case must exit 0, or the red is meaningless.

Run: python3 tools/test_agreement.py
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
from verify_agreement import FAIL, PASS, UNKNOWN, Report, check_a5_snapshot  # noqa: E402

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

        # With no tombstone the object has no receipt at all: the check reports it under accounting,
        # and only --strict turns that into a failure.
        self.assertEqual(code, 0, out)
        self.assertIn("A1a served with no receipt at all", out)
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
                    body = json.dumps({"orphans": {"meaning": "no count field here"},
                                       "superseded": {"count": 0, "bytes": 0},
                                       "attributed": {"count": 0, "bytes": 0}}).encode()
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
        self.assertIn("A1a served with no receipt at all", out)
        self.assertIn("UNKNOWN", out)
        self.assertNotIn("PASS    A1a", out,
                         "an unparsable orphan count must not read as a passing zero")

    def test_a_withdrawn_object_answers_410_on_every_read_surface(self):
        """remotik's rule: withdrawal is not 'no bytes at this URL', it is one tombstone for every
        surface. This is the happy path of R5 — the object is gone from the mirror, and every
        surface still says *withdrawn* rather than *never existed*."""
        sha = self.accept("gone.md", "# gone\n", "key-gone0000000001")
        self.publish()
        subprocess.run([sys.executable, str(HERE / "evict.py"), str(self.data), sha,
                        "--public-dir", str(self.public), "--reason", "test"],
                       capture_output=True, text=True, timeout=180)

        code, out = self.run_check()

        self.assertEqual(code, 0, out)
        self.assertIn("R5 every read surface answers 410 for a withdrawn object", out)
        self.assertIn("R5b", out)
        self.assertNotIn("FAIL", out)

    def test_a_bare_404_on_the_mirror_fails_the_withdrawal_rule(self):
        """The discriminating half: a static file server that answers a bare 404 for a withdrawn
        name tells the reader the object never existed. That is a false statement about the shelf,
        and the check has to reject it rather than call the surfaces agreed."""
        import http.server
        import threading

        digest = "a" * 64

        class Stub(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path == "/mirror/manifest.json":
                    return self._json(200, {"artifacts": []})
                if self.path == "/mirror/tombstones.json":
                    return self._json(200, {"by_sha256": {digest: {"sha256": digest,
                                                                   "filename": "withdrawn.md"}},
                                            "by_name": {}})
                if self.path.startswith("/api/v1/blobs/") or self.path.startswith("/api/v1/by-sha256/"):
                    return self._json(410, {"state": "evicted", "tombstone": {"sha256": digest}})
                if self.path == "/api/v1/shelf/agreement":
                    return self._json(200, {"orphans": {"count": 0, "bytes": 0},
                                            "superseded": {"count": 0, "bytes": 0},
                                            "attributed": {"count": 0, "bytes": 0},
                                            "counted": {"live_objects": 0, "live_bytes": 0},
                                            "served_totals": {"objects": 0, "bytes": 0}})
                # A plain file server: no receipt, no memory.
                body = b"404 page not found\n"
                self.send_response(404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{srv.server_port}"
            r = subprocess.run([sys.executable, str(CHECK), "--api", f"{base}/api",
                                "--mirror", f"{base}/mirror", "--limit", "0"],
                               capture_output=True, text=True, timeout=180)
        finally:
            srv.shutdown()
        out = r.stdout + r.stderr
        self.assertEqual(r.returncode, 1, out)
        self.assertIn("R5 every read surface answers 410", out)
        self.assertIn("mirror/withdrawn.md -> 404, not 410", out)
        self.assertIn("R5b", out)

    # ---------------------------------------------------------------- the accounting, offline

    def test_the_offline_report_names_all_four_buckets_not_only_orphans(self):
        """An invariant that is silently absent reads as an invariant that held.

        Offline, only `orphan_totals` was attached to the surface, so A1b, A1c and A1d never ran and
        never said they had not run — including A1d, the one that asserts the buckets close. A check
        with an unreported half is worth less than the report suggests, and the report is what an
        operator reads.
        """
        self.accept("one.md", "# one\n", "key-four-00000001")
        self.accept("one.md", "# two\n", "key-four-00000002")   # leaves a superseded blob
        self.publish()

        code, out = self.run_check()

        self.assertEqual(code, 0, out)
        for invariant in ("A1a", "A1b", "A1c", "A1d", "A1e"):
            self.assertIn(invariant, out, f"{invariant} is missing from the offline report")

    def test_a_blob_counted_twice_fails_the_sum(self):
        """A1d must be falsifiable by the store's state, not a restatement of its own arithmetic.

        Over HTTP the endpoint publishes four numbers and their sum, and the check re-adds the same
        four: no shelf state can make that disagree. Offline the buckets are compared with the blobs
        on disk, so a digest in two buckets — or in none — is a failure rather than a tautology.
        """
        old = self.accept("same.md", "# old\n", "key-sum-000000001")
        self.accept("same.md", "# new\n", "key-sum-000000002")
        self.publish()
        code, out = self.run_check()
        self.assertEqual(code, 0, out)

        # Hand-write the superseded digest into the reconstruction file as well. Both buckets now
        # claim one blob; the shelf still holds only what it holds.
        (self.data / "attributed.json").write_text(json.dumps(
            {"entries": {old: {"sha256": old, "superseded_by": old, "filename": "same.md",
                               "witnessed": False}}}))

        code, out = self.run_check("--strict")

        self.assertEqual(code, 1, out)
        self.assertIn("A1e", out)

    def test_one_blob_claimed_by_two_live_rows_fails_the_sum(self):
        """The case A1d exists for, and the one it could not see while it re-added its own numbers.

        Two live rows naming the same digest under different filenames: the catalog counts two
        objects, the shelf holds one file. Every other invariant is content with this — the bytes
        hash correctly, both mirror copies serve them, nothing is withdrawn — so the disagreement is
        visible only where the buckets are compared with the disk. A quota that bounds the disk with
        this number bounds the wrong thing.
        """
        self.accept("a.md", "# one blob, two rows\n", "key-two-000000001")
        man = json.loads((self.data / "manifest.json").read_text())
        man["artifacts"].append({**man["artifacts"][0], "filename": "b.md"})
        (self.data / "manifest.json").write_text(json.dumps(man))
        self.publish()

        code, out = self.run_check()

        self.assertEqual(code, 1, out)
        self.assertIn("A1d", out)
        self.assertIn("counted twice or by nothing", out)

    def test_an_unsupported_attribution_fails_rather_than_shrinking_the_orphan_count(self):
        """A plausible record in a file any tool may write must not quietly explain a blob away."""
        self.accept("live.md", "# live\n", "key-att-000000001")
        bogus = "e" * 64
        (self.data / "blobs" / bogus).write_bytes(b"unexplainable bytes\n")
        self.publish()
        owner = json.loads((self.data / "manifest.json").read_text())["artifacts"][0]["sha256"]
        (self.data / "attributed.json").write_text(json.dumps(
            {"entries": {bogus: {"sha256": bogus, "superseded_by": owner, "filename": "live.md",
                                 "basis": "filename-and-acceptance-receipt", "witnessed": False}}}))

        code, out = self.run_check()

        self.assertEqual(code, 1, out)
        self.assertIn("A1e", out)
        self.assertIn(bogus[:12], out)

    def test_a_replacement_naming_a_live_digest_fails_s4(self):
        """S4 was asserted and never exercised: with it disabled every suite stayed green.

        The state it forbids is reachable — the same bytes republished under a second name while an
        older row still names the digest as what it replaced — and it is the state that makes a blob
        countable in two buckets at once.
        """
        old = self.accept("a.md", "# A body\n", "key-s4-000000001")
        self.accept("a.md", "# B body\n", "key-s4-000000002")
        again = self.accept("a-again.md", "# A body\n", "key-s4-000000003")
        self.assertEqual(again, old, "fixture: the same bytes must return under a new name")
        self.publish()

        code, out = self.run_check()

        self.assertEqual(code, 1, out)
        self.assertIn("S4", out)
        self.assertIn("is live and named as displaced", out)

    def test_missing_data_directory_is_not_a_pass(self):
        cmd = [sys.executable, str(CHECK), "--data", str(Path(self.tmp.name) / "nope")]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)


class SnapshotBoundaryTests(unittest.TestCase):
    """A generation number is a comparison only if it names a snapshot (boba-pharos-01, seq 31088)."""

    def surface(self, files):
        class S:
            mirror = "http://mirror.invalid/board-showcase"

            def mirror_bytes(self, name):
                return files.get(name)
        return S()

    def add(self, gen_manifest, **kw):
        man = {"manifest_generation": gen_manifest, "artifacts": []}
        rep = Report()
        check_a5_snapshot(kw["surface"], man, kw.get("strict", False), rep)
        return rep

    def test_a_file_that_does_not_match_its_recorded_digest_is_a_torn_read(self):
        body = b'{"a": 1}'
        snap = json.dumps({"generation": 7, "files": {"manifest.json": "0" * 64}}).encode()
        rep = self.add(7, surface=self.surface({"manifest.json": body, "snapshot.json": snap}))
        line = [r for r in rep.rows if r[0].startswith("A5")][0]
        self.assertEqual(line[1], FAIL)
        self.assertIn("serves", line[2])

    def test_a_generation_that_disagrees_with_the_manifest_is_reported(self):
        body = b'{"manifest_generation": 8, "artifacts": []}'
        snap = json.dumps({"generation": 7, "files": {"manifest.json":
                                                      hashlib.sha256(body).hexdigest()}}).encode()
        rep = self.add(8, surface=self.surface({"manifest.json": body, "snapshot.json": snap}))
        line = [r for r in rep.rows if r[0].startswith("A5")][0]
        self.assertEqual(line[1], FAIL)
        self.assertIn("snapshot names generation 7", line[2])

    def test_a_matching_snapshot_passes(self):
        body = b'{"manifest_generation": 7, "artifacts": []}'
        snap = json.dumps({"generation": 7, "files": {"manifest.json":
                                                      hashlib.sha256(body).hexdigest()}}).encode()
        rep = self.add(7, surface=self.surface({"manifest.json": body, "snapshot.json": snap}))
        line = [r for r in rep.rows if r[0].startswith("A5")][0]
        self.assertEqual(line[1], PASS)
        self.assertIn("every named file serves the bytes", line[2])

    def test_no_snapshot_is_unknown_and_not_pass(self):
        body = b'{"manifest_generation": 7, "artifacts": []}'
        rep = self.add(7, surface=self.surface({"manifest.json": body}))
        line = [r for r in rep.rows if r[0].startswith("A5")][0]
        self.assertEqual(line[1], UNKNOWN)
        rep_strict = self.add(7, surface=self.surface({"manifest.json": body}), strict=True)
        self.assertEqual([r for r in rep_strict.rows if r[0].startswith("A5")][0][1], FAIL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
