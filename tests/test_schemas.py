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
    MinimalObservation, DoomedDecision, GWTWDecision, DecisionTraceRef,
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

    # ------------------------------------------------------------------
    # Regression: session checkpoint isolation
    # ------------------------------------------------------------------
    def test_session_checkpoint_path_isolation(self):
        """Per-stage checkpoint (checkpoints/<stage>.json) is written in the
        correct session directory when CheckpointManager.create() receives runs_dir.

        Regression test for: P1 — CheckpointManager used default
        AGENTICPD_DIR/runs/ fallback instead of the session directory.
        """
        # Simulate a nested session dir (like runs/sky130hd_gcd/checkpoint_fork/<ts>/)
        session_runs = self.tmpdir / "session" / "sky130hd_gcd" / "checkpoint_fork" / "20260729_test"
        session_runs.mkdir(parents=True)

        # Create fake ORFS artifacts
        variant_dir = self.flow_dir / "results" / "sky130hd" / "gcd" / "agenticpd_iter0"
        variant_dir.mkdir(parents=True)
        (variant_dir / "2_floorplan.odb").write_text("fp data")
        (variant_dir / "2_floorplan.sdc").write_text("sdc data")

        # Trial with relative artifact_dir (production default)
        t = TrialRecord(
            trial_id="iso001",
            experiment_id="isolation-test",
            status="ok",
            artifact_dir="iter-0-iso001",  # relative path
        )
        (session_runs / "iter-0-iso001").mkdir(parents=True)

        cm = CheckpointManager(self.flow_dir)
        ph = CheckpointManager.param_hash({"FP": {"CORE_UTILIZATION": 38}})
        cp = cm.create(
            t, "FP", "sky130hd", "gcd", "agenticpd_iter0", ph,
            runs_dir=session_runs,
        )

        # Per-stage checkpoint must be in the session dir, not agenticpd/runs/
        # Stage D fix 2.1: checkpoints/<stage>.json replaces legacy checkpoint.json
        cp_path = session_runs / "iter-0-iso001" / "checkpoints" / "FP.json"
        self.assertTrue(cp_path.is_file(),
                        f"checkpoints/FP.json not found in session dir: {cp_path}")

    # ------------------------------------------------------------------
    # Regression: empty manifest rejection
    # ------------------------------------------------------------------
    def test_empty_manifest_rejected(self):
        """CheckpointManager.verify() returns False for empty artifact_manifest.

        Regression test for: P1 — empty manifest looped 0 times and
        returned (True, []) as if verification passed.
        """
        cm = CheckpointManager(self.flow_dir)
        empty_cp = CheckpointRef(
            checkpoint_id="cp-empty-001",
            source_trial_id="test001",
            stage="FP",
            param_hash="abc123",
            orfs_commit="unresolved",
            artifact_manifest=[],  # empty — should fail
        )
        ok, errors = cm.verify(empty_cp)
        self.assertFalse(ok, "Empty manifest should be rejected")
        self.assertTrue(any("Empty" in e for e in errors),
                        f"Error message should mention 'Empty': {errors}")

    # ------------------------------------------------------------------
    # Regression: StageResult paths must not contain absolute user paths
    # ------------------------------------------------------------------
    def test_stage_result_paths_are_relative(self):
        """StageResult persisted fields (log_path, command, report_path)
        must not contain absolute paths like /home/.

        Regression test for: P0 — absolute path leakage in trial.json.
        """
        import re
        abs_path_re = re.compile(r'^(/[a-zA-Z_][^ ]*)+')

        sr = StageResult(
            stage="CTS", status="ok", elapsed_s=8.5, exit_code=0,
            log_path="iter0_cts.make.log",
            command="make -C . DESIGN_CONFIG=./designs/sky130hd/gcd/config.mk FLOW_VARIANT=agenticpd_iter0 cts",
            start_time="2026-07-28T15:00:00+00:00",
            end_time="2026-07-28T15:00:08+00:00",
            report_path="reports/sky130hd/gcd/agenticpd_iter0/4_1_cts.json",
            stage_qor={"4_1_cts_ws_ps": -1200.0},
        )
        d = sr.to_dict()

        for field in ("log_path", "command", "report_path"):
            value = d.get(field, "")
            if value:
                self.assertIsNone(
                    abs_path_re.match(value),
                    f"{field} contains absolute path: {value!r}")
                # Specifically forbid /home/ or /Users/
                self.assertNotIn("/home/", value,
                                 f"{field} contains /home/ path: {value!r}")
                self.assertNotIn("/Users/", value,
                                 f"{field} contains /Users/ path: {value!r}")

    # ------------------------------------------------------------------
    # Stage C contract: StageResult new fields (command, start/end, report_path)
    # ------------------------------------------------------------------
    def test_stage_result_full_roundtrip(self):
        """StageResult with all Stage C fields survives to_dict/from_dict."""
        sr = StageResult(
            stage="CTS", status="ok", elapsed_s=8.5, exit_code=0,
            log_path="logs/sky130hd/gcd/iter0/4_cts.make.log",
            command="make -C <flow_dir> DESIGN_CONFIG=./designs/sky130hd/gcd/config.mk FLOW_VARIANT=agenticpd_iter0 cts",
            start_time="2026-07-28T15:00:00+00:00",
            end_time="2026-07-28T15:00:08+00:00",
            report_path="reports/sky130hd/gcd/agenticpd_iter0/4_1_cts.json",
            stage_qor={"4_1_cts_ws_ps": -1200.0},
        )
        d = sr.to_dict()
        # All new fields present in serialised dict
        self.assertEqual(d["command"], sr.command)
        self.assertEqual(d["start_time"], sr.start_time)
        self.assertEqual(d["end_time"], sr.end_time)
        self.assertEqual(d["report_path"], sr.report_path)
        # Roundtrip preserves identity
        sr2 = StageResult.from_dict(d)
        self.assertEqual(sr2.command, sr.command)
        self.assertEqual(sr2.report_path, sr.report_path)
        self.assertEqual(sr2.elapsed_s, 8.5)

    def test_stage_result_missing_new_fields_backward_compat(self):
        """Old JSON without new fields still deserialises (backward compat)."""
        old_dict = {
            "stage": "FP", "status": "ok", "elapsed_s": 10.0,
            "exit_code": 0,
            "stage_qor": {"2_1_floorplan_ws_ps": -1154.1},
        }
        sr = StageResult.from_dict(old_dict)
        self.assertIsNone(sr.command)
        self.assertIsNone(sr.start_time)
        self.assertIsNone(sr.end_time)
        self.assertIsNone(sr.report_path)

    def test_report_path_resolution_from_config(self):
        """report_path is resolved correctly via cfg.reports_dir (no duplication)."""
        # Create a mock ORFS reports directory structure
        variant = "agenticpd_iter0"
        reports_dir = self.flow_dir / "reports" / "sky130hd" / "gcd" / variant
        reports_dir.mkdir(parents=True)
        # Write a stage report JSON that parse_stage_qor would look for
        (reports_dir / "2_1_floorplan.json").write_text(
            '{"2_1_floorplan_ws_ps": -1154.1}')
        # Verify path structure: cfg.reports_dir IS the full path
        from config import FrameworkConfig
        cfg = FrameworkConfig(flow_dir=self.flow_dir, platform="sky130hd", design="gcd")
        self.assertEqual(
            str(cfg.reports_dir(variant)),
            str(self.flow_dir / "reports" / "sky130hd" / "gcd" / variant))
        # The report file exists where we expect it
        self.assertTrue((reports_dir / "2_1_floorplan.json").is_file())


class FailureClassTest(unittest.TestCase):
    def test_from_exit_code_zero(self):
        self.assertEqual(FailureClass.from_exit_code(0), FailureClass.NONE)

    def test_from_exit_code_signal(self):
        self.assertEqual(FailureClass.from_exit_code(-11), FailureClass.TOOL_CRASH)

    def test_from_exit_code_timeout(self):
        self.assertEqual(FailureClass.from_exit_code(0, timed_out=True),
                         FailureClass.TIMEOUT)


# =============================================================================
# Stage C contract: checkpoint compatibility tests (P1-2 fix)
# =============================================================================

class CheckpointCompatibilityTest(unittest.TestCase):
    """Verify is_compatible() per ParamSpec.affects — formal unit tests.

    These tests cover the checkpoint invalidation rules required by the
    Stage C plan: FP/CTS/RT checkpoints, SETUP_SLACK_MARGIN's cross-stage
    effect, unknown parameters, and edge cases.
    """

    def setUp(self):
        import tempfile
        self.tmpdir = Path(tempfile.mkdtemp())
        self.flow_dir = self.tmpdir / "flow"
        self.runs_dir = self.tmpdir / "runs"
        self.runs_dir.mkdir(parents=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir)

    def _make_cp(self, stage, param_hash="abc123"):
        """Create a minimal CheckpointRef for compatibility testing."""
        return CheckpointRef(
            checkpoint_id=CheckpointRef.make_id("test001", stage),
            source_trial_id="test001",
            stage=stage, param_hash=param_hash, orfs_commit="unresolved",
        )

    def _base_params(self):
        return {"FP": {"CORE_UTILIZATION": 38}, "PL": {}, "CTS": {}, "RT": {}}

    # ------------------------------------------------------------------
    # FP checkpoint compat
    # ------------------------------------------------------------------
    def test_fp_checkpoint_core_util_change_incompatible(self):
        """CORE_UTILIZATION affects FP/PL/CTS/RT → invalidates FP checkpoint."""
        cm = CheckpointManager(self.flow_dir)
        cp = self._make_cp("FP")
        new_p = {"FP": {"CORE_UTILIZATION": 50}, "PL": {}, "CTS": {}, "RT": {}}
        self.assertFalse(cm.is_compatible(cp, new_p, self._base_params()))

    def test_fp_checkpoint_rt_only_change_compatible(self):
        """GRT_CONGESTION_ITERATIONS affects RT only → FP checkpoint still valid."""
        cm = CheckpointManager(self.flow_dir)
        cp = self._make_cp("FP")
        new_p = {"FP": {"CORE_UTILIZATION": 38}, "PL": {}, "CTS": {},
                 "RT": {"GRT_CONGESTION_ITERATIONS": 50}}
        self.assertTrue(cm.is_compatible(cp, new_p, self._base_params()))

    def test_cts_checkpoint_rt_layer_adj_incompatible(self):
        """FASTROUTE_LAYER_ADJUSTMENT affects FP/PL/CTS/RT → CTS checkpoint incompatible."""
        cm = CheckpointManager(self.flow_dir)
        cp = self._make_cp("CTS")
        new_p = {"FP": {"CORE_UTILIZATION": 38}, "PL": {}, "CTS": {},
                 "RT": {"FASTROUTE_LAYER_ADJUSTMENT": 0.25}}
        self.assertFalse(cm.is_compatible(cp, new_p, self._base_params()))

    def test_fp_checkpoint_same_params_compatible(self):
        """No parameter changes → always compatible."""
        cm = CheckpointManager(self.flow_dir)
        cp = self._make_cp("FP")
        self.assertTrue(cm.is_compatible(cp, self._base_params(), self._base_params()))

    # ------------------------------------------------------------------
    # CTS checkpoint compat
    # ------------------------------------------------------------------
    def test_cts_checkpoint_setup_slack_margin_change_incompatible(self):
        """SETUP_SLACK_MARGIN affects FP/PL/CTS/RT → invalidates CTS checkpoint."""
        cm = CheckpointManager(self.flow_dir)
        cp = self._make_cp("CTS")
        new_p = {"FP": {"CORE_UTILIZATION": 38}, "PL": {}, "CTS": {"SETUP_SLACK_MARGIN": 0.1}, "RT": {}}
        self.assertFalse(cm.is_compatible(cp, new_p, self._base_params()))

    def test_cts_checkpoint_grt_iters_change_compatible(self):
        """GRT_CONGESTION_ITERATIONS affects RT only → CTS checkpoint still valid."""
        cm = CheckpointManager(self.flow_dir)
        cp = self._make_cp("CTS")
        new_p = {"FP": {"CORE_UTILIZATION": 38}, "PL": {}, "CTS": {},
                 "RT": {"GRT_CONGESTION_ITERATIONS": 50}}
        self.assertTrue(cm.is_compatible(cp, new_p, self._base_params()))

    # ------------------------------------------------------------------
    # RT checkpoint compat
    # ------------------------------------------------------------------
    def test_rt_checkpoint_setup_slack_margin_change_incompatible(self):
        """SETUP_SLACK_MARGIN affects FP/PL/CTS/RT → invalidates RT checkpoint."""
        cm = CheckpointManager(self.flow_dir)
        cp = self._make_cp("RT")
        new_p = {"FP": {"CORE_UTILIZATION": 38}, "PL": {}, "CTS": {"SETUP_SLACK_MARGIN": 0.15}, "RT": {}}
        self.assertFalse(cm.is_compatible(cp, new_p, self._base_params()))

    # ------------------------------------------------------------------
    # Unknown parameter
    # ------------------------------------------------------------------
    def test_unknown_param_conservative_incompatible(self):
        """Unknown parameter → conservative fallback: assume incompatible."""
        cm = CheckpointManager(self.flow_dir)
        cp = self._make_cp("FP")
        new_p = {"FP": {"UNKNOWN_PARAM_X": 42}, "PL": {}, "CTS": {}, "RT": {}}
        self.assertFalse(cm.is_compatible(cp, new_p, self._base_params()))

    # ------------------------------------------------------------------
    # Edge: multi-param change, only one invalidates
    # ------------------------------------------------------------------
    def test_multi_param_one_incompatible_means_incompatible(self):
        """If any changed param invalidates the checkpoint → incompatible."""
        cm = CheckpointManager(self.flow_dir)
        cp = self._make_cp("CTS")
        # FASTROUTE_LAYER_ADJUSTMENT alone→compatible, but SETUP_SLACK_MARGIN→incompatible
        new_p = {"FP": {"CORE_UTILIZATION": 38}, "PL": {}, "CTS": {"SETUP_SLACK_MARGIN": 0.05},
                 "RT": {"FASTROUTE_LAYER_ADJUSTMENT": 0.15}}
        self.assertFalse(cm.is_compatible(cp, new_p, self._base_params()))


# =============================================================================
# Regression: YAML loader validation (no execution-semantic defaults)
# =============================================================================

class YamlLoaderValidationTest(unittest.TestCase):
    """Verify that load_experiment_config() rejects YAML with missing required
    keys rather than silently using hardcoded execution-semantic defaults."""

    def setUp(self):
        import tempfile
        self.tmpdir = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir)

    def _write_yaml(self, content: str) -> str:
        p = self.tmpdir / "test_config.yaml"
        p.write_text(content, encoding="utf-8")
        return str(p)

    def _valid_yaml(self) -> str:
        return """\
schema_version: 1
experiment_id: test-experiment-v1
platform: sky130hd
design: gcd
search_space:
  baseline:
    FP:
      CORE_UTILIZATION: 38
    PL: {}
    CTS: {}
    RT:
      FASTROUTE_LAYER_ADJUSTMENT: 0.2
  actions:
    A_test:
      description: "test action"
      stage: RT
      param: GRT_CONGESTION_ITERATIONS
      from: 30
      to: 50
      affects: ["RT"]
checkpoint:
  fork_stage: CTS
  source: agenticpd_baseline
acceptance:
  qor_tolerances:
    wns_ps: 1.0
    tns_ps: 5.0
"""

    def test_valid_yaml_loads_without_error(self):
        """Valid YAML loads successfully and returns all required keys."""
        from tools.checkpoint_fork_verify import load_experiment_config
        path = self._write_yaml(self._valid_yaml())
        exp_cfg = load_experiment_config(path)
        self.assertEqual(exp_cfg["experiment_id"], "test-experiment-v1")
        self.assertEqual(exp_cfg["platform"], "sky130hd")
        self.assertEqual(exp_cfg["checkpoint_stage"], "CTS")
        self.assertEqual(exp_cfg["checkpoint_source"], "agenticpd_baseline")
        self.assertIn("FP", exp_cfg["baseline_params"])
        self.assertEqual(len(exp_cfg["actions"]), 1)
        self.assertIn("wns_ps", exp_cfg["qor_tolerances"])

    def test_missing_experiment_id_fails(self):
        """Missing experiment_id → sys.exit(1)."""
        from tools.checkpoint_fork_verify import load_experiment_config
        content = self._valid_yaml().replace("experiment_id: test-experiment-v1", "")
        with self.assertRaises(SystemExit):
            load_experiment_config(self._write_yaml(content))

    def test_missing_checkpoint_fork_stage_fails(self):
        """Missing checkpoint.fork_stage → sys.exit(1)."""
        from tools.checkpoint_fork_verify import load_experiment_config
        content = self._valid_yaml().replace("fork_stage: CTS", "")
        with self.assertRaises(SystemExit):
            load_experiment_config(self._write_yaml(content))

    def test_missing_baseline_fails(self):
        """Missing search_space.baseline → sys.exit(1)."""
        from tools.checkpoint_fork_verify import load_experiment_config
        content = """\
schema_version: 1
experiment_id: test-experiment-v1
platform: sky130hd
design: gcd
search_space:
  actions:
    A_test:
      description: "test action"
      stage: RT
      param: GRT_CONGESTION_ITERATIONS
      from: 30
      to: 50
      affects: ["RT"]
checkpoint:
  fork_stage: CTS
  source: agenticpd_baseline
acceptance:
  qor_tolerances:
    wns_ps: 1.0
    tns_ps: 5.0
"""
        with self.assertRaises(SystemExit):
            load_experiment_config(self._write_yaml(content))

    def test_missing_actions_fails(self):
        """Missing search_space.actions → sys.exit(1)."""
        from tools.checkpoint_fork_verify import load_experiment_config
        content = self._valid_yaml().replace("actions:", "actions_missing:")
        with self.assertRaises(SystemExit):
            load_experiment_config(self._write_yaml(content))

    def test_missing_action_param_fails(self):
        """Action missing required 'param' field → sys.exit(1)."""
        from tools.checkpoint_fork_verify import load_experiment_config
        content = self._valid_yaml().replace("param: GRT_CONGESTION_ITERATIONS", "")
        with self.assertRaises(SystemExit):
            load_experiment_config(self._write_yaml(content))

    def test_missing_action_stage_fails(self):
        """Action missing required 'stage' field → sys.exit(1)."""
        from tools.checkpoint_fork_verify import load_experiment_config
        content = self._valid_yaml().replace("stage: RT", "")
        with self.assertRaises(SystemExit):
            load_experiment_config(self._write_yaml(content))

    def test_missing_action_from_to_fails(self):
        """Action missing 'from'/'to' → sys.exit(1)."""
        from tools.checkpoint_fork_verify import load_experiment_config
        content = self._valid_yaml().replace("from: 30", "")
        with self.assertRaises(SystemExit):
            load_experiment_config(self._write_yaml(content))

    def test_missing_checkpoint_source_fails(self):
        """Missing checkpoint.source → sys.exit(1)."""
        from tools.checkpoint_fork_verify import load_experiment_config
        content = self._valid_yaml().replace("source: agenticpd_baseline", "")
        with self.assertRaises(SystemExit):
            load_experiment_config(self._write_yaml(content))

    def test_missing_qor_tolerances_fails(self):
        """Missing acceptance.qor_tolerances.wns_ps → sys.exit(1)."""
        from tools.checkpoint_fork_verify import load_experiment_config
        content = self._valid_yaml().replace("wns_ps: 1.0", "")
        with self.assertRaises(SystemExit):
            load_experiment_config(self._write_yaml(content))


# =============================================================================
# Regression: explicit parameter passing + external config path safety
# =============================================================================

class VerifyScriptSmokeTest(unittest.TestCase):
    """Smoke tests for checkpoint_fork_verify.py that do NOT start ORFS.

    Verifies that functions accept explicit parameters (no undefined-variable
    crashes) and that external config paths are safely displayed.
    """

    def setUp(self):
        import tempfile
        self.tmpdir = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir)

    def test_functions_accept_explicit_params(self):
        """run_full_restart and run_checkpoint_fork accept baseline_params
        — no NameError on undefined variables.  Smoke-validates the function
        signatures with MockORFSRunner (no real EDA)."""
        from tools.checkpoint_fork_verify import (
            run_full_restart, run_checkpoint_fork,
            build_params,
        )
        from config import FrameworkConfig

        cfg = FrameworkConfig(flow_dir=self.tmpdir, run_dir=self.tmpdir)
        params = {"FP": {}, "PL": {}, "CTS": {}, "RT": {}}
        action = {
            "description": "test", "stage": "RT",
            "param_name": "GRT_CONGESTION_ITERATIONS",
            "baseline_value": 30, "changed_value": 50,
            "expect_compatible": True, "expect_qor_match": True,
        }

        # build_params accepts explicit baseline_params
        p = build_params(action, params)
        self.assertEqual(p["RT"]["GRT_CONGESTION_ITERATIONS"], 50)

        # run_full_restart accepts baseline_params (mock mode)
        result = run_full_restart(cfg, action, "test_action", params)
        self.assertIn("ok", result)

        # run_checkpoint_fork accepts baseline_params + checkpoint_stage
        # (mock mode — verify fails gracefully, function doesn't crash
        # on undefined variables)
        cp_data = {
            "checkpoint_id": "cp-test-FP", "source_trial_id": "test",
            "stage": "FP", "param_hash": "abc", "orfs_commit": "x",
            "created_at": "2026-01-01T00:00:00+00:00",
            "artifact_manifest": [],
            "artifact_dir": None,
        }
        fork_result = run_checkpoint_fork(
            cfg, action, "test_action", "baseline_var", cp_data,
            params, checkpoint_stage="FP",
        )
        # Even if fork fails, the function should return a dict without
        # crashing on undefined variables.
        self.assertIsInstance(fork_result, dict)
        self.assertIn("ok", fork_result)
        self.assertIn("mode", fork_result)
        self.assertEqual(fork_result["mode"], "checkpoint_fork")

    def test_external_config_path_safe_display(self):
        """External --config path uses safe <external>/basename display,
        never leaking absolute /home/... paths."""
        from tools.checkpoint_fork_verify import _AGENTICPD_DIR

        # Simulate an external path outside the project tree
        external = Path("/tmp/some_config.yaml")
        agenticpd = _AGENTICPD_DIR.resolve()

        if external.is_relative_to(agenticpd):
            self.skipTest("Test assumption invalid: /tmp is inside project")

        # Compute safe display name (same logic as main())
        if external.is_relative_to(agenticpd):
            display = str(external.relative_to(agenticpd))
        else:
            display = f"<external>/{external.name}"

        self.assertIn("<external>", display)
        self.assertNotIn("/tmp/", display)
        self.assertEqual(display, "<external>/some_config.yaml")


# =============================================================================
# Regression: log sanitization + command path relativization
# =============================================================================

class LogSanitizationTest(unittest.TestCase):
    """Prove sanitize_make_log() and _relativize_cmd_arg() remove absolute
    paths from session logs and command strings."""

    def setUp(self):
        import tempfile
        self.tmpdir = Path(tempfile.mkdtemp())
        self.flow_dir = self.tmpdir / "flow"
        self.run_dir = self.tmpdir / "run"
        self.flow_dir.mkdir(parents=True)
        self.run_dir.mkdir(parents=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir)

    def test_sanitize_make_log_removes_absolute_paths(self):
        """sanitize_make_log replaces project_root and run_dir with placeholders."""
        from orfs.runner import sanitize_make_log
        from config import FrameworkConfig

        cfg = FrameworkConfig(flow_dir=self.flow_dir, run_dir=self.run_dir)
        log_path = self.run_dir / "test.log"
        project_root = str(cfg.flow_dir.parent)

        # Write a log containing absolute paths from both flow/ and tools/
        content = (
            f"[INFO] Reading {self.flow_dir}/results/sky130hd/gcd/base/2_floorplan.odb\n"
            f"[INFO] Writing {self.run_dir}/fastroute_iter0.tcl\n"
            f"[INFO] Executing {project_root}/tools/OpenROAD/bin/openroad ...\n"
            f"Normal line without paths\n"
        )
        log_path.write_text(content, encoding="utf-8")

        sanitize_make_log(log_path, cfg)

        cleaned = log_path.read_text(encoding="utf-8")
        self.assertNotIn(str(self.flow_dir), cleaned,
                         "flow_dir absolute path must be replaced")
        self.assertNotIn(str(self.run_dir), cleaned,
                         "run_dir absolute path must be replaced")
        self.assertNotIn(project_root, cleaned,
                         "project_root absolute path must be replaced")
        self.assertNotIn("/home/", cleaned,
                         "No /home/ paths should remain")
        self.assertNotIn("/Users/", cleaned,
                         "No /Users/ paths should remain")
        self.assertIn("${PROJECT_ROOT}", cleaned,
                      "project_root should be replaced with ${PROJECT_ROOT}")
        self.assertIn("${RUN_DIR}", cleaned,
                      "run_dir should be replaced with ${RUN_DIR}")
        self.assertIn("Normal line without paths", cleaned,
                      "Non-path lines should be preserved")

    def test_relativize_cmd_arg_fastroute_tcl(self):
        """_relativize_cmd_arg converts KEY=<abs_fastroute_path> to KEY=<rel>."""
        from orfs.runner import _relativize_cmd_arg
        from config import FrameworkConfig

        cfg = FrameworkConfig(flow_dir=self.flow_dir, run_dir=self.run_dir)

        # FASTROUTE_TCL pattern: KEY=absolute_path under run_dir
        abs_tcl = str(self.run_dir / "fastroute_iter0.tcl")
        arg = f"FASTROUTE_TCL={abs_tcl}"
        result = _relativize_cmd_arg(arg, cfg)
        self.assertEqual(result, "FASTROUTE_TCL=fastroute_iter0.tcl",
                         f"Expected relative FASTROUTE_TCL, got {result}")

    def test_relativize_cmd_arg_flow_dir_subpath(self):
        """_relativize_cmd_arg converts absolute paths under flow_dir."""
        from orfs.runner import _relativize_cmd_arg
        from config import FrameworkConfig

        cfg = FrameworkConfig(flow_dir=self.flow_dir, run_dir=self.run_dir)

        # -C argument
        self.assertEqual(_relativize_cmd_arg(str(cfg.flow_dir), cfg), ".")

        # Subpath under flow_dir
        abs_report = str(self.flow_dir / "reports" / "sky130hd" / "gcd" / "base")
        result = _relativize_cmd_arg(abs_report, cfg)
        self.assertEqual(result, "reports/sky130hd/gcd/base")

    def test_relativize_cmd_arg_external_path_unchanged(self):
        """_relativize_cmd_arg leaves paths outside project tree unchanged."""
        from orfs.runner import _relativize_cmd_arg
        from config import FrameworkConfig

        cfg = FrameworkConfig(flow_dir=self.flow_dir, run_dir=self.run_dir)

        # External path (not under flow_dir or run_dir)
        ext = "/usr/bin/make"
        self.assertEqual(_relativize_cmd_arg(ext, cfg), ext)

        # Non-path arg
        self.assertEqual(_relativize_cmd_arg("FLOW_VARIANT=test", cfg), "FLOW_VARIANT=test")


# =============================================================================
# Regression: checkpoint creation ordering (after checkpoint_stage, before RT)
# =============================================================================

class CheckpointCreationOrderTest(unittest.TestCase):
    """Verify checkpoint is created immediately after checkpoint_stage
    completes and BEFORE downstream stages run — the core Stage C semantic."""

    def setUp(self):
        import tempfile
        self.tmpdir = Path(tempfile.mkdtemp())
        self.call_log = []  # records ("stage", stage_name) or ("checkpoint", stage)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir)

    def _fake_execute_stage(self, cfg, stage, params, variant, iteration):
        """Stub execute_stage that logs the call and returns ok."""
        self.call_log.append(("stage", stage))
        from schemas.trial import StageResult
        return StageResult(
            stage=stage, status="ok", elapsed_s=0.01, exit_code=0,
            stage_qor={},
        )

    def _recording_create(self, trial, stage, **kwargs):
        """Stub CheckpointManager.create that logs the call."""
        self.call_log.append(("checkpoint", stage))
        from schemas.trial import CheckpointRef
        return CheckpointRef(
            checkpoint_id=CheckpointRef.make_id(trial.trial_id, stage),
            source_trial_id=trial.trial_id,
            stage=stage, param_hash="abc", orfs_commit="unresolved",
            artifact_manifest=[
                {"file": f"results/sky130hd/gcd/v/2_floorplan.odb",
                 "size_bytes": 100, "sha256": "abc123"},
            ],
        )

    def test_checkpoint_created_after_cts_before_rt(self):
        """CheckpointManager.create() is called after CTS stage completes
        and before RT stage begins."""
        from unittest.mock import patch, MagicMock
        from tools.checkpoint_fork_verify import run_baseline_per_stage
        from config import FrameworkConfig
        from managers import TrialManager, CheckpointManager
        from orfs.interface import MockORFSRunner

        cfg = FrameworkConfig(flow_dir=self.tmpdir, run_dir=self.tmpdir)
        runner = MockORFSRunner(cfg)
        tm = TrialManager(self.tmpdir)
        cm = CheckpointManager(self.tmpdir)
        params = {"FP": {}, "PL": {}, "CTS": {}, "RT": {}}

        # Replace execute_stage and cm.create with recording stubs
        orig_create = cm.create
        cm.create = lambda *a, **kw: self._recording_create(*a, **kw)

        with patch(
            "tools.checkpoint_fork_verify.execute_stage",
            self._fake_execute_stage,
        ):
            baseline = run_baseline_per_stage(
                cfg, runner, tm, cm, params, "sky130hd", "gcd",
                experiment_id="order-test-v1",
                baseline_variant="agenticpd_baseline",
                checkpoint_stage="CTS",
            )

        self.assertTrue(baseline.get("ok"))

        # Extract call ordering
        stage_calls = [(t, s) for t, s in self.call_log if t == "stage"]
        cp_calls = [(t, s) for t, s in self.call_log if t == "checkpoint"]

        # Must have at least FP, PL, CTS, RT stages
        stage_names = [s for _, s in stage_calls]
        self.assertIn("FP", stage_names)
        self.assertIn("PL", stage_names)
        self.assertIn("CTS", stage_names)
        self.assertIn("RT", stage_names)

        # Find indices
        cts_idx = stage_names.index("CTS")
        rt_idx = stage_names.index("RT")
        cp_idx_in_log = self.call_log.index(cp_calls[0]) if cp_calls else -1
        cts_idx_in_log = self.call_log.index(("stage", "CTS"))
        rt_idx_in_log = self.call_log.index(("stage", "RT"))

        # Checkpoint must be created after CTS, before RT
        self.assertGreater(
            cp_idx_in_log, cts_idx_in_log,
            "checkpoint must be created AFTER CTS stage")
        self.assertLess(
            cp_idx_in_log, rt_idx_in_log,
            "checkpoint must be created BEFORE RT stage")

        # Verify the checkpoint stage matches
        self.assertEqual(cp_calls[0][1], "CTS")

    def test_finish_failure_persists_failed_trial(self):
        """When finish fails, the trial is persisted with status=failed,
        failed_stage='finish', non-None failure, and non-None end_time."""
        from unittest.mock import patch
        from tools.checkpoint_fork_verify import run_baseline_per_stage
        from config import FrameworkConfig
        from managers import TrialManager, CheckpointManager
        from orfs.interface import MockORFSRunner, RunResult
        from schemas.trial import FailureClass

        cfg = FrameworkConfig(flow_dir=self.tmpdir, run_dir=self.tmpdir)
        runner = MockORFSRunner(cfg)
        tm = TrialManager(self.tmpdir)
        cm = CheckpointManager(self.tmpdir)
        params = {"FP": {}, "PL": {}, "CTS": {}, "RT": {}}

        # Replace CheckpointManager.create to avoid real file I/O
        def _stub_create(trial, stage, **kw):
            from schemas.trial import CheckpointRef
            return CheckpointRef(
                checkpoint_id=CheckpointRef.make_id(trial.trial_id, stage),
                source_trial_id=trial.trial_id,
                stage=stage, param_hash="abc", orfs_commit="unresolved",
                artifact_manifest=[],
            )
        cm.create = _stub_create

        with patch(
            "tools.checkpoint_fork_verify.execute_stage",
            self._fake_execute_stage,
        ):
            # Make run_finish return a failed RunResult
            fail_result = RunResult(
                ok=False, variant="test",
                error="finish make exit code 1; log tail:\nerror detail",
                failed_stage="finish",
                elapsed_s=0.5,
            )

            with patch.object(runner, "run_finish", return_value=fail_result):
                with self.assertRaises(SystemExit):
                    run_baseline_per_stage(
                        cfg, runner, tm, cm, params, "sky130hd", "gcd",
                        experiment_id="finish-fail-test",
                        baseline_variant="agenticpd_baseline",
                        checkpoint_stage="CTS",
                    )

        # Load the persisted trial and verify audit fields
        all_trials = tm.list_all()
        self.assertEqual(len(all_trials), 1, "Exactly one trial should be persisted")
        t = all_trials[0]

        self.assertEqual(t.status, "failed",
                         f"status should be 'failed', got '{t.status}'")
        self.assertEqual(t.failed_stage, "finish",
                         f"failed_stage should be 'finish', got '{t.failed_stage}'")
        self.assertIsNotNone(t.failure,
                             "failure should be non-None")
        self.assertIsNotNone(t.end_time,
                             "end_time should be non-None")
        self.assertIsNotNone(t.error_message,
                             "error_message should be non-None")

        # Verify a failed finish StageResult was appended
        finish_srs = [sr for sr in t.stage_results if sr.stage == "finish"]
        self.assertEqual(len(finish_srs), 1, "Should have exactly one finish StageResult")
        self.assertEqual(finish_srs[0].status, "failed")
        self.assertIsNotNone(finish_srs[0].failure)

    def test_success_finish_appends_stage_result(self):
        """On successful finish, a StageResult(stage='finish', status='ok')
        is appended so per-stage audit covers all 5 stages."""
        from unittest.mock import patch
        from tools.checkpoint_fork_verify import run_baseline_per_stage
        from config import FrameworkConfig
        from managers import TrialManager, CheckpointManager

        cfg = FrameworkConfig(flow_dir=self.tmpdir, run_dir=self.tmpdir)
        tm = TrialManager(self.tmpdir)
        cm = CheckpointManager(self.tmpdir)
        params = {"FP": {}, "PL": {}, "CTS": {}, "RT": {}}

        # Stub cm.create with a real CheckpointRef (not a bare mock)
        from schemas.trial import CheckpointRef
        def _stub_create(trial, stage, **kw):
            return CheckpointRef(
                checkpoint_id=CheckpointRef.make_id(trial.trial_id, stage),
                source_trial_id=trial.trial_id,
                stage=stage, param_hash="abc", orfs_commit="unresolved",
                artifact_manifest=[],
            )
        cm.create = _stub_create

        with patch(
            "tools.checkpoint_fork_verify.execute_stage",
            self._fake_execute_stage,
        ):
            from orfs.interface import MockORFSRunner
            runner = MockORFSRunner(cfg)
            baseline = run_baseline_per_stage(
                cfg, runner, tm, cm, params, "sky130hd", "gcd",
                experiment_id="success-finish-test",
                baseline_variant="agenticpd_baseline",
                checkpoint_stage="CTS",
            )

        self.assertTrue(baseline.get("ok"))

        # Load persisted trial
        all_trials = tm.list_all()
        self.assertEqual(len(all_trials), 1)
        t = all_trials[0]

        # Should have FP, PL, CTS, RT, finish = 5 StageResults
        self.assertEqual(len(t.stage_results), 5,
                         f"Expected 5 StageResults (FP+PL+CTS+RT+finish), got {len(t.stage_results)}")
        self.assertEqual(t.stage_results[-1].stage, "finish")
        self.assertEqual(t.stage_results[-1].status, "ok")
        self.assertGreater(t.stage_results[-1].elapsed_s, 0,
                           "Finish elapsed_s should be > 0")
        finish_sr = t.stage_results[-1]
        self.assertIsNotNone(finish_sr.command,
                             "Finish StageResult.command should not be None")
        self.assertIsNotNone(finish_sr.start_time,
                             "Finish StageResult.start_time should not be None")
        self.assertIsNotNone(finish_sr.end_time,
                             "Finish StageResult.end_time should not be None")
        self.assertIsNotNone(finish_sr.exit_code,
                             "Finish StageResult.exit_code should not be None")
        self.assertEqual(finish_sr.exit_code, 0,
                         "Finish StageResult.exit_code should be 0 on success")
        self.assertIsNotNone(finish_sr.log_path,
                             "Finish StageResult.log_path should not be None")
        self.assertIsNotNone(finish_sr.report_path,
                             "Finish StageResult.report_path should not be None")


# =============================================================================
# Stage D decision contract tests
# =============================================================================

class StageDDecisionContractTest(unittest.TestCase):
    """Multi-stage decision trace round-trip, backward compat, enum validation,
    and paused lifecycle."""

    # ------------------------------------------------------------------
    # Multi-stage round-trip: PL + CTS decisions survive without overwrite
    # ------------------------------------------------------------------
    def test_doomed_multi_stage_roundtrip(self):
        """A Trial with doomed_decisions at both PL and CTS preserves
        both entries after to_dict/from_dict round-trip."""
        dd_pl = DoomedDecision(
            risk_class="soft_bad", risk_score=0.3,
            reason_codes=["timing_negative"],
            rule_version="1.0.0",
        )
        dd_cts = DoomedDecision(
            risk_class="survivor", risk_score=0.9,
            reason_codes=["timing_ok"],
            rule_version="1.0.0",
        )
        tr = TrialRecord(
            trial_id="multi_doomed",
            status="ok",
            doomed_decisions=[dd_pl, dd_cts],
        )
        self.assertEqual(len(tr.doomed_decisions), 2)
        self.assertEqual(tr.doomed_decisions[0].risk_class, "soft_bad")
        self.assertEqual(tr.doomed_decisions[1].risk_class, "survivor")

        tr2 = TrialRecord.from_dict(tr.to_dict())
        self.assertEqual(len(tr2.doomed_decisions), 2,
                         "PL+CTS both survive round-trip")
        self.assertEqual(tr2.doomed_decisions[0].risk_class, "soft_bad")
        self.assertEqual(tr2.doomed_decisions[1].risk_class, "survivor")

    def test_gwtw_multi_stage_roundtrip(self):
        """A Trial with gwtw_decisions at both PL and CTS preserves
        both entries after to_dict/from_dict round-trip."""
        gd_pl = GWTWDecision(
            action="continue", decision_stage="PL", rank=2,
            scheduler_version="1.0.0",
        )
        gd_cts = GWTWDecision(
            action="finish", decision_stage="CTS", rank=1,
            scheduler_version="1.0.0",
        )
        tr = TrialRecord(
            trial_id="multi_gwtw",
            status="ok",
            gwtw_decisions=[gd_pl, gd_cts],
        )
        self.assertEqual(len(tr.gwtw_decisions), 2)
        self.assertEqual(tr.gwtw_decisions[0].action, "continue")
        self.assertEqual(tr.gwtw_decisions[1].action, "finish")

        tr2 = TrialRecord.from_dict(tr.to_dict())
        self.assertEqual(len(tr2.gwtw_decisions), 2,
                         "PL+CTS both survive round-trip")
        self.assertEqual(tr2.gwtw_decisions[0].action, "continue")
        self.assertEqual(tr2.gwtw_decisions[1].action, "finish")

    # ------------------------------------------------------------------
    # Old JSON backward compat
    # ------------------------------------------------------------------
    def test_old_json_no_decision_keys(self):
        """Old Trial JSON without doomed_decisions or gwtw_decisions
        loads with empty lists."""
        old = {
            "trial_id": "old_no_decisions",
            "experiment_id": "legacy",
            "status": "ok",
            "params": {"FP": {}},
            "stage_results": [],
        }
        tr = TrialRecord.from_dict(old)
        self.assertEqual(tr.doomed_decisions, [])
        self.assertEqual(tr.gwtw_decisions, [])
        # Round-trip preserves emptiness
        tr2 = TrialRecord.from_dict(tr.to_dict())
        self.assertEqual(tr2.doomed_decisions, [])
        self.assertEqual(tr2.gwtw_decisions, [])

    def test_legacy_singular_key_accepted(self):
        """Old JSON with singular 'doomed_decision' / 'gwtw_decision'
        keys is still accepted and promoted to lists."""
        legacy = {
            "trial_id": "legacy_singular",
            "experiment_id": "legacy",
            "status": "ok",
            "params": {"FP": {}},
            "doomed_decision": {
                "risk_class": "hard_dead",
                "reason_codes": ["stage_failed"],
                "input_evidence": {"stage": "PL"},
            },
            "gwtw_decision": {
                "action": "pause",
                "decision_stage": "PL",
            },
            "stage_results": [],
        }
        tr = TrialRecord.from_dict(legacy)
        self.assertEqual(len(tr.doomed_decisions), 1,
                         "Legacy singular doomed_decision → list of 1")
        self.assertEqual(tr.doomed_decisions[0].risk_class, "hard_dead")
        self.assertEqual(len(tr.gwtw_decisions), 1,
                         "Legacy singular gwtw_decision → list of 1")
        self.assertEqual(tr.gwtw_decisions[0].action, "pause")

    # ------------------------------------------------------------------
    # Enum validation
    # ------------------------------------------------------------------
    def test_doomed_decision_rejects_invalid_risk_class(self):
        with self.assertRaises(ValueError):
            DoomedDecision(risk_class="unknown_class")

    def test_doomed_decision_accepts_valid_risk_classes(self):
        for rc in ("hard_dead", "soft_bad", "survivor"):
            dd = DoomedDecision(risk_class=rc)
            self.assertEqual(dd.risk_class, rc)

    def test_gwtw_decision_rejects_invalid_action(self):
        with self.assertRaises(ValueError):
            GWTWDecision(action="delete_trial", decision_stage="PL")

    def test_gwtw_decision_rejects_invalid_decision_stage(self):
        with self.assertRaises(ValueError):
            GWTWDecision(action="continue", decision_stage="RT")

    def test_gwtw_decision_accepts_valid_values(self):
        for action in ("continue", "pause", "audit_continue", "fork", "finish"):
            gd = GWTWDecision(action=action, decision_stage="PL")
            self.assertEqual(gd.action, action)
        for stage in ("PL", "CTS"):
            gd = GWTWDecision(action="continue", decision_stage=stage)
            self.assertEqual(gd.decision_stage, stage)

    # ------------------------------------------------------------------
    # Paused lifecycle: checkpoint preserved, no final_qor required
    # ------------------------------------------------------------------
    def test_paused_trial_preserves_checkpoint(self):
        cp = CheckpointRef(
            checkpoint_id="cp-paused-PL",
            source_trial_id="paused_trial",
            stage="PL",
            param_hash="sha256:abc",
            orfs_commit="unresolved",
        )
        tr = TrialRecord(
            trial_id="paused_trial",
            status="paused",
            checkpoint=cp,
            final_qor=None,
            stage_results=[StageResult(stage="FP", status="ok", elapsed_s=10.0),
                           StageResult(stage="PL", status="ok", elapsed_s=30.0)],
        )
        self.assertEqual(tr.status, "paused")
        self.assertIsNotNone(tr.checkpoint)
        self.assertEqual(tr.checkpoint.checkpoint_id, "cp-paused-PL")
        self.assertIsNone(tr.final_qor)
        self.assertFalse(tr.is_complete)

        # Round-trip preserves paused state and checkpoint
        tr2 = TrialRecord.from_dict(tr.to_dict())
        self.assertEqual(tr2.status, "paused")
        self.assertIsNotNone(tr2.checkpoint)
        self.assertEqual(tr2.checkpoint.checkpoint_id, "cp-paused-PL")
        self.assertFalse(tr2.is_complete)

    def test_paused_trial_no_final_qor_is_legal(self):
        """A paused trial without final_qor must not raise or fail validation."""
        tr = TrialRecord(trial_id="paused_no_qor", status="paused",
                         checkpoint=CheckpointRef(
                             checkpoint_id="cp-x", source_trial_id="x",
                             stage="PL", param_hash="sha256:abc",
                             orfs_commit="unresolved"))
        self.assertIsNone(tr.final_qor)
        self.assertEqual(tr.status, "paused")
        # Round-trip
        tr2 = TrialRecord.from_dict(tr.to_dict())
        self.assertIsNone(tr2.final_qor)
        self.assertEqual(tr2.status, "paused")

    # ------------------------------------------------------------------
    # MinimalObservation round-trip
    # ------------------------------------------------------------------
    def test_minimal_observation_roundtrip(self):
        obs = MinimalObservation(
            trial_id="t001", stage="PL", status="ok",
            stage_wns_ps=-1200.0, stage_tns_ps=-5000.0,
            stage_elapsed_s=45.0, checkpoint_id="cp-t001-PL",
        )
        obs2 = MinimalObservation.from_dict(obs.to_dict())
        self.assertEqual(obs2.trial_id, "t001")
        self.assertEqual(obs2.stage_wns_ps, -1200.0)
        self.assertEqual(obs2.stage_tns_ps, -5000.0)

    # ------------------------------------------------------------------
    # DecisionTraceRef
    # ------------------------------------------------------------------
    def test_decision_trace_ref_roundtrip(self):
        ref = DecisionTraceRef(
            decision_id="dtr-001",
            trace_path="traces/decisions.jsonl",
        )
        self.assertEqual(ref.decision_id, "dtr-001")
        ref2 = DecisionTraceRef.from_dict(ref.to_dict())
        self.assertEqual(ref2.decision_id, "dtr-001")
        self.assertEqual(ref2.trace_path, "traces/decisions.jsonl")

    def test_decision_trace_ref_rejects_absolute_path(self):
        with self.assertRaises(ValueError):
            DecisionTraceRef(decision_id="x",
                           trace_path="/absolute/path/trace.jsonl")

    def test_decision_trace_ref_rejects_parent_traversal(self):
        with self.assertRaises(ValueError):
            DecisionTraceRef(decision_id="x",
                           trace_path="../escape/trace.jsonl")

    def test_decision_trace_ref_rejects_empty_path(self):
        with self.assertRaises(ValueError):
            DecisionTraceRef(decision_id="x", trace_path="")

    def test_trial_multi_trace_ref_roundtrip(self):
        """Trial with multiple decision_trace_refs survives round-trip."""
        tr = TrialRecord(
            trial_id="multi_ref",
            status="ok",
            decision_trace_refs=[
                DecisionTraceRef(decision_id="dtr-pl",
                                trace_path="traces/decisions.jsonl"),
                DecisionTraceRef(decision_id="dtr-cts",
                                trace_path="traces/decisions.jsonl"),
            ],
        )
        self.assertEqual(len(tr.decision_trace_refs), 2)
        tr2 = TrialRecord.from_dict(tr.to_dict())
        self.assertEqual(len(tr2.decision_trace_refs), 2)
        self.assertEqual(tr2.decision_trace_refs[0].decision_id, "dtr-pl")
        self.assertEqual(tr2.decision_trace_refs[1].decision_id, "dtr-cts")

    def test_old_json_decision_trace_refs_default_empty(self):
        """Old JSON without decision_trace_refs loads with empty list."""
        tr = TrialRecord.from_dict({
            "trial_id": "old_no_refs",
            "status": "ok",
            "params": {},
            "stage_results": [],
        })
        self.assertEqual(tr.decision_trace_refs, [])


if __name__ == "__main__":
    unittest.main()
