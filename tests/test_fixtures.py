# -*- coding: utf-8 -*-
"""test_fixtures.py — Validate stage B fixtures against real ORFS data."""
import json, sys, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schemas.trial import TrialRecord, CheckpointRef, FailureClass

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "stage_b"


class OkTrialFixtureTest(unittest.TestCase):
    """Validate the ok_trial.json fixture (real gcd baseline run)."""

    @classmethod
    def setUpClass(cls):
        cls.trial = TrialRecord.from_dict(
            json.loads((FIXTURE_DIR / "ok_trial.json").read_text()))

    def test_fixture_exists_and_parses(self):
        self.assertEqual(self.trial.trial_id, "fixture-gcd-001")

    def test_lineage_is_full_restart(self):
        self.assertIsNone(self.trial.parent_trial_id)
        self.assertIsNone(self.trial.branch_stage)

    def test_status_is_ok(self):
        self.assertEqual(self.trial.status, "ok")

    def test_final_qor_complete(self):
        self.assertTrue(self.trial.is_complete)
        self.assertAlmostEqual(self.trial.final_qor["wns_ps"], -1460.26, places=1)
        self.assertAlmostEqual(self.trial.final_qor["tns_ps"], -61747.6, places=0)
        self.assertAlmostEqual(self.trial.final_qor["area_um2"], 5400.18, places=0)
        self.assertAlmostEqual(self.trial.final_qor["power_w"], 0.00938, places=4)

    def test_has_stage_results_for_all_stages(self):
        stages = [sr.stage for sr in self.trial.stage_results]
        self.assertIn("FP", stages)
        self.assertIn("PL", stages)
        self.assertIn("CTS", stages)
        self.assertIn("RT", stages)
        self.assertIn("finish", stages)

    def test_has_checkpoint(self):
        self.assertIsNotNone(self.trial.checkpoint)
        self.assertEqual(self.trial.checkpoint.stage, "finish")

    def test_params_recorded(self):
        self.assertIn("FP", self.trial.params)

    def test_roundtrip_preserves_all_data(self):
        d = self.trial.to_dict()
        t2 = TrialRecord.from_dict(d)
        self.assertEqual(t2.trial_id, self.trial.trial_id)
        self.assertEqual(t2.final_qor["wns_ps"], self.trial.final_qor["wns_ps"])
        self.assertEqual(len(t2.stage_results), len(self.trial.stage_results))


class OkCheckpointFixtureTest(unittest.TestCase):
    """Validate the ok_checkpoint.json fixture."""

    @classmethod
    def setUpClass(cls):
        cls.cp = CheckpointRef.from_dict(
            json.loads((FIXTURE_DIR / "ok_checkpoint.json").read_text()))

    def test_checkpoint_has_manifest(self):
        self.assertGreater(len(self.cp.artifact_manifest), 0)

    def test_manifest_has_hashes(self):
        for entry in self.cp.artifact_manifest:
            self.assertIn("sha256", entry)
            self.assertEqual(len(entry["sha256"]), 64)  # SHA-256 = 64 hex chars
            self.assertIn("size_bytes", entry)
            self.assertGreater(entry["size_bytes"], 0)

    def test_stage_is_finish(self):
        self.assertEqual(self.cp.stage, "finish")


class FailedTrialFixtureTest(unittest.TestCase):
    """Validate the failed_trial.json fixture (simulated PL crash)."""

    @classmethod
    def setUpClass(cls):
        cls.trial = TrialRecord.from_dict(
            json.loads((FIXTURE_DIR / "failed_trial.json").read_text()))

    def test_status_is_failed(self):
        self.assertEqual(self.trial.status, "failed")

    def test_failure_class_recorded(self):
        self.assertEqual(self.trial.failure, FailureClass.TOOL_CRASH)

    def test_failed_stage_is_pl(self):
        self.assertEqual(self.trial.failed_stage, "PL")

    def test_branch_from_parent(self):
        self.assertEqual(self.trial.parent_trial_id, "fixture-gcd-001")
        self.assertEqual(self.trial.branch_stage, "PL")

    def test_skipped_stages_after_failure(self):
        statuses = {sr.stage: sr.status for sr in self.trial.stage_results}
        self.assertEqual(statuses.get("FP"), "ok")
        self.assertEqual(statuses.get("PL"), "failed")
        self.assertEqual(statuses.get("CTS"), "skipped")
        self.assertEqual(statuses.get("RT"), "skipped")

    def test_elapsed_s_sum_includes_failed_stage(self):
        """Failed stages correctly report their elapsed time (not 0)."""
        pl_stage = [sr for sr in self.trial.stage_results if sr.stage == "PL"][0]
        self.assertEqual(pl_stage.elapsed_s, 15.0)

    def test_is_not_complete(self):
        self.assertFalse(self.trial.is_complete)


if __name__ == "__main__":
    unittest.main()
