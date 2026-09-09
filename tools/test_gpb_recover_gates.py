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
            require_authority=False,  # identity-only table from ministry #28394
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

    def test_authority_sentinel_refuses_without_policy_epoch(self):
        """just-nik #28461 + huddora #28524: absent epoch = SKIP; carried live = UNKNOWN."""
        self._write(
            "mine_fresh_no_epoch",
            {
                "request_id": "mine_fresh_noepoch_ab",
                "target": "/v1/posts/x/replies",
                "payload": {"body": "x"},
                "state": "OPEN",
                "owner": self.owner,
                "created_at": self.now - 60,
            },
        )
        self._write(
            "mine_fresh_epoch_a",
            {
                "request_id": "mine_fresh_epochA_ab",
                "target": "/v1/posts/x/replies",
                "payload": {"body": "x"},
                "state": "OPEN",
                "owner": self.owner,
                "created_at": self.now - 60,
                "policy_epoch": "epoch-A",
            },
        )
        self._write(
            "mine_fresh_epoch_b",
            {
                "request_id": "mine_fresh_epochB_ab",
                "target": "/v1/posts/x/replies",
                "payload": {"body": "x"},
                "state": "OPEN",
                "owner": self.owner,
                "created_at": self.now - 60,
                "policy_epoch": "epoch-B",
            },
        )
        # Carried live_policy_epoch without linearizable_read → UNKNOWN, not ALLOW.
        report_carried = self.gpb.recover(
            k="unused",
            owner=self.owner,
            replay_horizon_s=self.horizon,
            dry_run=True,
            now=self.now,
            live_policy_epoch="epoch-B",
        )
        by_c = {r["request_id"]: r for r in report_carried["results"]}
        self.assertEqual(by_c["mine_fresh_noepoch_ab"]["decision"], "SKIP")
        self.assertIn("authority unknown", by_c["mine_fresh_noepoch_ab"]["reason"])
        self.assertEqual(by_c["mine_fresh_epochA_ab"]["decision"], "UNKNOWN")
        self.assertIn("self-attestation", by_c["mine_fresh_epochA_ab"]["reason"])
        self.assertEqual(by_c["mine_fresh_epochB_ab"]["decision"], "UNKNOWN")
        self.assertEqual(by_c["mine_fresh_epochB_ab"]["action"], "skipped")
        self.assertEqual(report_carried["counts"]["unknown"], 2)
        self.assertEqual(report_carried["counts"]["replay"], 0)

        # Linearizable read: mismatch SKIP, match would_resend (CHECKED_AGAINST).
        report = self.gpb.recover(
            k="unused",
            owner=self.owner,
            replay_horizon_s=self.horizon,
            dry_run=True,
            now=self.now,
            live_policy_epoch="epoch-B",
            epoch_source="linearizable_read",
        )
        by_id = {r["request_id"]: r for r in report["results"]}
        self.assertEqual(by_id["mine_fresh_noepoch_ab"]["action"], "skipped")
        self.assertIn("authority unknown", by_id["mine_fresh_noepoch_ab"]["reason"])
        self.assertEqual(by_id["mine_fresh_epochA_ab"]["action"], "skipped")
        self.assertEqual(by_id["mine_fresh_epochA_ab"]["decision"], "SKIP")
        self.assertIn("revoked", by_id["mine_fresh_epochA_ab"]["reason"])
        self.assertEqual(by_id["mine_fresh_epochB_ab"]["action"], "would_resend")
        self.assertEqual(report["counts"]["no_authority"], 2)
        self.assertEqual(report["scope"]["live_policy_epoch"], "epoch-B")
        self.assertEqual(report["scope"]["epoch_source"], "linearizable_read")
        self.assertTrue(report["scope"]["require_authority"])

    def test_authority_on_by_default_blocks_legacy_journal(self):
        self._write(
            "legacy_identity_only",
            {
                "request_id": "legacy_identity_only1",
                "target": "/v1/posts/x/replies",
                "payload": {"body": "x"},
                "state": "OPEN",
                "owner": self.owner,
                "created_at": self.now - 60,
            },
        )
        report = self.gpb.recover(
            k="unused",
            owner=self.owner,
            replay_horizon_s=self.horizon,
            dry_run=True,
            now=self.now,
        )
        self.assertEqual(report["counts"]["replay"], 0)
        self.assertEqual(report["counts"]["no_authority"], 1)
        self.assertEqual(report["results"][0]["decision"], "SKIP")

    def test_unknown_when_live_epoch_missing_on_epoch_bearing_record(self):
        """huddora #28524: looks valid but unverified → UNKNOWN, not SKIP."""
        self._write(
            "mine_fresh_epoch_only",
            {
                "request_id": "mine_fresh_epoch_only1",
                "target": "/v1/posts/x/replies",
                "payload": {"body": "x"},
                "state": "OPEN",
                "owner": self.owner,
                "created_at": self.now - 60,
                "policy_epoch": "epoch-A",
            },
        )
        report = self.gpb.recover(
            k="unused",
            owner=self.owner,
            replay_horizon_s=self.horizon,
            dry_run=True,
            now=self.now,
            live_policy_epoch=None,
        )
        self.assertEqual(report["results"][0]["decision"], "UNKNOWN")
        self.assertEqual(report["counts"]["unknown"], 1)
        self.assertEqual(report["counts"]["replay"], 0)

    def test_horizon_required(self):
        with self.assertRaises(SystemExit):
            self.gpb.recover(k="x", owner=self.owner, replay_horizon_s=None, dry_run=True)

    def test_owner_required(self):
        with self.assertRaises(SystemExit):
            self.gpb.recover(k="x", owner="", replay_horizon_s=10, dry_run=True)


if __name__ == "__main__":
    unittest.main()
