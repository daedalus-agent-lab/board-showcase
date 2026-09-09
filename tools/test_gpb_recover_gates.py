"""Synthetic fixtures for gpb recover() ownership+freshness gates (ministry #28394)."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "gpb_six_traps_client.py"


def load_gpb(intent_dir: Path):
    spec = importlib.util.spec_from_file_location("gpb_client", CLIENT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    mod.INTENT_DIR = str(intent_dir)
    mod.INTENT_DONE = str(intent_dir / "done")
    return mod


class RecoverGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.gpb = load_gpb(self.dir)
        self.now = 1_000_000
        self.owner = "0cb5b346-c5bc-4460-b07c-a981d7522a20"
        self.horizon = 86400

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, name: str, rec: dict):
        path = self.dir / f"{name}.json"
        path.write_text(json.dumps(rec, ensure_ascii=False, sort_keys=True))

    def test_four_fixtures_ministry_table(self):
        payload = {"body": "x"}
        self._write(
            "legacy_v1",
            {
                "request_id": "legacy_v1_abcdefghij",
                "target": "/v1/posts/x/replies",
                "payload": payload,
                "state": "OPEN",
                # owner absent
            },
        )
        self._write(
            "mine_fresh",
            {
                "request_id": "mine_fresh_abcdefgh",
                "target": "/v1/posts/x/replies",
                "payload": payload,
                "state": "OPEN",
                "owner": self.owner,
                "created_at": self.now - 600,
            },
        )
        self._write(
            "mine_stale",
            {
                "request_id": "mine_stale_abcdefgh",
                "target": "/v1/posts/x/replies",
                "payload": payload,
                "state": "OPEN",
                "owner": self.owner,
                "created_at": self.now - 144000,
            },
        )
        self._write(
            "theirs",
            {
                "request_id": "theirs_abcdefghijkl",
                "target": "/v1/posts/x/replies",
                "payload": payload,
                "state": "OPEN",
                "owner": "35a3c627-dead-beef-cafe-000000000001",
                "created_at": self.now - 100,
            },
        )
        report = self.gpb.recover(
            k="unused",
            owner=self.owner,
            replay_horizon_s=self.horizon,
            dry_run=True,
            now=self.now,
        )
        by_id = {r["request_id"]: r for r in report["results"]}
        self.assertEqual(report["counts"]["open"], 4)
        self.assertEqual(report["counts"]["replay"], 1)
        self.assertEqual(report["counts"]["stale"], 1)
        self.assertEqual(report["counts"]["skip"], 2)
        self.assertEqual(by_id["legacy_v1_abcdefghij"]["decision"], "SKIP")
        self.assertEqual(by_id["mine_fresh_abcdefgh"]["action"], "would_resend")
        self.assertEqual(by_id["mine_stale_abcdefgh"]["decision"], "STALE")
        self.assertEqual(by_id["theirs_abcdefghijkl"]["decision"], "SKIP")
        self.assertEqual(report["scope"]["replay_horizon_s"], 86400)
        self.assertTrue(report["scope"]["dry_run"])

    def test_horizon_required(self):
        with self.assertRaises(SystemExit):
            self.gpb.recover(k="x", owner=self.owner, replay_horizon_s=None, dry_run=True)

    def test_owner_required(self):
        with self.assertRaises(SystemExit):
            self.gpb.recover(k="x", owner="", replay_horizon_s=10, dry_run=True)


if __name__ == "__main__":
    unittest.main()
