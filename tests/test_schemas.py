# -*- coding: utf-8 -*-
"""test_schemas.py — Stage B integration tests.

Tests TrialManager + CheckpointManager with mock ORFS artifacts,
verifying the six questions every trial must answer:

  1. parent_trial_id and branch_stage    — "where did this trial come from?"
  2. param_diff                          — "what parameters changed?"
  3. stage_results[*].elapsed_s          — "how long did each stage take?"
  4. failure + failed_stage              — "is it recoverable?"
  5. artifact_dir                        — "where are the final files?"
  6. final_qor                           — "what is the final QoR?"

All tests use temp directories; no network, no LLM, no EDA.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schemas.trial import (
    TrialRecord, StageResult, CheckpointRef, FailureClass,
    append_trial_to_jsonl, load_trials_from_jsonl,
)
from managers import TrialManager
from managers import CheckpointManager


class TrialRecordIntegrationTest(unittest.TestCase):
    """End-to-end: create trial, simulate a flow run, persist, reload."""

    def setUp(self):
        import tempfile
        self.tmpdir = Path(tempfile.mkdtemp())
        self.runs_dir = self.tmpdir / "runs"
        self.flow_dir = self.tmpdir / "flow"
        self.mgr = TrialManager(self.runs_dir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir)

    # ------------------------------------------------------------------
    # Helper: create a simple successful trial
    # ------------------------------------------------------------------
    def _make_success_trial(self, trial_id="ok001", parent=None, branch=None):
        t = TrialRecord(
            trial_id=trial_id,
            experiment_id="smoke-gcd-v1",
            parent_trial_id=parent,
            branch_stage=branch,
            status="ok",
            start_time="2026-07-27T15:00:00+00:00",
            params={"FP": {"CORE_UTILIZATION": 38}},
            final_qor={"wns_ps": -1460.3, "tns_ps": -61747.6,
                       "area_um2": 5400.2, "power_w": 0.00938},
            stage_results=[
                StageResult("FP", "ok", 10.0, 0, stage_qor={"2_1_floorplan_ws_ps": -1154.1}),
                StageResult("PL", "ok", 15.0, 0, stage_qor={"3_5_place_dp_ws_ps": -1300.0}),
                StageResult("CTS", "ok", 8.0, 0, stage_qor={"4_1_cts_ws_ps": -1200.0}),
                StageResult("RT", "ok", 30.0, 0, stage_qor={"5_route_ws_ps": -1450.0}),
                StageResult("finish", "ok", 5.0, 0),
            ],
            artifact_dir=str(self.runs_dir / trial_id),
        )
        return t

    # ------------------------------------------------------------------
    # Q1: parent / branch — where did this trial come from?
    # ------------------------------------------------------------------
    def test_lineage_full_restart(self):
        t = self._make_success_trial("t001")
        self.mgr._write_trial(t)
        loaded = self.mgr.get("t001")
        self.assertIsNone(loaded.parent_trial_id)
        self.assertIsNone(loaded.branch_stage)

    def test_lineage_branch_from_cts(self):
        t = self._make_success_trial("t002", parent="t001", branch="CTS")
        self.mgr._write_trial(t)
        loaded = self.mgr.get("t002")
        self.assertEqual(loaded.parent_trial_id, "t001")
        self.assertEqual(loaded.branch_stage, "CTS")

    # ------------------------------------------------------------------
    # Q2: param_diff — what changed?
    # ------------------------------------------------------------------
    def test_param_diff_roundtrip(self):
        t = self._make_success_trial("t003")
        t.param_diff = {
            "CORE_UTILIZATION": {"from": 38, "to": 30},
            "CTS_CLUSTER_SIZE": {"from": None, "to": 60},
        }
        self.mgr._write_trial(t)
        loaded = self.mgr.get("t003")
        self.assertEqual(loaded.param_diff["CORE_UTILIZATION"]["from"], 38)
        self.assertEqual(loaded.param_diff["CORE_UTILIZATION"]["to"], 30)

    # ------------------------------------------------------------------
    # Q3: stage_results[*].elapsed_s — how long?
    # ------------------------------------------------------------------
    def test_stage_elapsed_sum(self):
        t = self._make_success_trial("t004")
        self.assertAlmostEqual(t.elapsed_s, 68.0)  # 10+15+8+30+5

    def test_stage_elapsed_on_failure(self):
        """Failed stages still record elapsed time (fixes the 'elapsed=0' bug)."""
        t = TrialRecord(
            trial_id="t005", experiment_id="test", status="failed",
            failure=FailureClass.TOOL_CRASH, artifact_dir=str(self.runs_dir / "t005"),
            stage_results=[
                StageResult("FP", "ok", 10.0, 0),
                StageResult("PL", "failed", 12.0, -11, failure=FailureClass.TOOL_CRASH),
            ],
        )
        self.mgr._write_trial(t)
        loaded = self.mgr.get("t005")
        self.assertEqual(loaded.elapsed_s, 22.0)
        self.assertEqual(loaded.stage_results[1].elapsed_s, 12.0)

    # ------------------------------------------------------------------
    # Q4: failure — recoverable?
    # ------------------------------------------------------------------
    def test_failure_classification_timeout(self):
        t = TrialRecord(
            trial_id="t006", experiment_id="test", status="failed",
            failure=FailureClass.TIMEOUT,
            error_message="3600s limit exceeded",
            artifact_dir=str(self.runs_dir / "t006"),
            stage_results=[
                StageResult("FP", "ok", 10.0, 0),
                StageResult("PL", "failed", 3600.0, None,
                           failure=FailureClass.TIMEOUT),
            ],
        )
        self.mgr._write_trial(t)
        loaded = self.mgr.get("t006")
        self.assertEqual(loaded.failure, FailureClass.TIMEOUT)
        self.assertEqual(loaded.failed_stage, "PL")
        # Timeout is NOT recoverable without checkpoint
        self.assertIsNone(loaded.checkpoint)

    def test_failure_with_checkpoint_implies_recoverable(self):
        """A failed trial with a valid checkpoint can be resumed."""
        cp = CheckpointRef(
            checkpoint_id="cp-t007-FP",
            source_trial_id="t007",
            stage="FP", param_hash="abc", orfs_commit="x",
        )
        t = TrialRecord(
            trial_id="t007", experiment_id="test", status="failed",
            failure=FailureClass.TOOL_CRASH,
            checkpoint=cp,
            artifact_dir=str(self.runs_dir / "t007"),
            stage_results=[
                StageResult("FP", "ok", 10.0, 0),
                StageResult("PL", "failed", 5.0, -11, failure=FailureClass.TOOL_CRASH),
            ],
        )
        self.mgr._write_trial(t)
        loaded = self.mgr.get("t007")
        self.assertIsNotNone(loaded.checkpoint)
        self.assertEqual(loaded.checkpoint.stage, "FP")

    # ------------------------------------------------------------------
    # Q5: artifact_dir — where are the files?
    # ------------------------------------------------------------------
    def test_artifact_dir_recorded(self):
        t = self._make_success_trial("t008")
        self.mgr._write_trial(t)
        loaded = self.mgr.get("t008")
        self.assertEqual(loaded.artifact_dir, str(self.runs_dir / "t008"))

    # ------------------------------------------------------------------
    # Q6: final_qor — what's the result?
    # ------------------------------------------------------------------
    def test_final_qor_complete(self):
        t = self._make_success_trial("t009")
        self.mgr._write_trial(t)
        loaded = self.mgr.get("t009")
        self.assertTrue(loaded.is_complete)
        self.assertAlmostEqual(loaded.final_qor["wns_ps"], -1460.3)

    def test_final_qor_none_for_failed(self):
        t = TrialRecord(
            trial_id="t010", experiment_id="test", status="failed",
            failure=FailureClass.TOOL_CRASH,
            artifact_dir=str(self.runs_dir / "t010"),
        )
        self.assertFalse(t.is_complete)

    # ------------------------------------------------------------------
    # JSONL index integrity
    # ------------------------------------------------------------------
    def test_jsonl_dedup_on_reload(self):
        """Multiple updates to same trial_id produce only one record in list."""
        t = self._make_success_trial("t011")
        self.mgr._write_trial(t)
        self.mgr._append_index(t)
        self.mgr._append_index(t)  # simulate three updates
        self.mgr._append_index(t)
        loaded = load_trials_from_jsonl(self.runs_dir / "trials.jsonl")
        self.assertEqual(len(loaded), 1)

    def test_jsonl_corrupt_line_skipped(self):
        """A corrupt JSON line should not crash the reader."""
        jl = self.runs_dir / "trials.jsonl"
        t = self._make_success_trial("t012")
        append_trial_to_jsonl(t, jl)
        # Append garbage
        with open(jl, "a", encoding="utf-8") as f:
            f.write("NOT JSON\n")
        loaded = load_trials_from_jsonl(jl)
        self.assertEqual(len(loaded), 1)

    # ------------------------------------------------------------------
    # CheckpointManager integration
    # ------------------------------------------------------------------
    def test_checkpoint_create_and_verify(self):
        """Create fake ORFS artifacts, checkpoint them, verify integrity."""
        # Create fake artifact files
        variant_dir = self.flow_dir / "results" / "sky130hd" / "gcd" / "iter0"
        variant_dir.mkdir(parents=True)
        (variant_dir / "2_floorplan.odb").write_text("floorplan data")
        (variant_dir / "2_floorplan.sdc").write_text("sdc data")

        t = self._make_success_trial("t013")
        self.mgr._write_trial(t)

        cm = CheckpointManager(self.flow_dir)
        ph = CheckpointManager.param_hash({"FP": {"CORE_UTILIZATION": 38}})
        cp = cm.create(t, "FP", "sky130hd", "gcd", "iter0", ph)

        ok, errors = cm.verify(cp)
        self.assertTrue(ok, f"verify failed: {errors}")
        self.assertGreater(len(cp.artifact_manifest), 0)

        # Tamper and re-verify
        (variant_dir / "2_floorplan.odb").write_text("tampered")
        ok2, _ = cm.verify(cp)
        self.assertFalse(ok2)

    def test_param_hash_deterministic(self):
        h1 = CheckpointManager.param_hash({"FP": {"A": 1}, "PL": {"B": 2}})
        h2 = CheckpointManager.param_hash({"PL": {"B": 2}, "FP": {"A": 1}})
        self.assertEqual(h1, h2)


class FailureClassTest(unittest.TestCase):
    def test_from_exit_code_zero(self):
        self.assertEqual(FailureClass.from_exit_code(0), FailureClass.NONE)

    def test_from_exit_code_signal(self):
        self.assertEqual(FailureClass.from_exit_code(-11), FailureClass.TOOL_CRASH)

    def test_from_exit_code_timeout(self):
        self.assertEqual(FailureClass.from_exit_code(0, timed_out=True),
                         FailureClass.TIMEOUT)


if __name__ == "__main__":
    unittest.main()
