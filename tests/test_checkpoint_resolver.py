# -*- coding: utf-8 -*-
"""test_checkpoint_resolver.py — Stage D checkpoint resolution regression tests.

Pure Python, no LLM, no ORFS, no network.

Covers:
  - CTS request + RT-only params     → reuse CTS, effective=RT
  - CTS request + CTS params         → fallback PL, effective=CTS
  - CTS request + PL params          → fallback FP, effective=PL
  - CTS request + FP params          → full restart, effective=FP
  - manifest missing / hash mismatch → skip that checkpoint
  - unknown parameter                → conservative incompatible
  - old Trial JSON loads with execution_resolution=null
  - resolution fully persisted when requested ≠ effective
  - source trial association is correct (not dependent on last trial)
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from config import FrameworkConfig
from schemas.trial import (
    TrialRecord, StageResult, CheckpointRef, CheckpointAuditEntry,
    ExecutionResolution, FailureClass,
    _new_trial_id,
)
from managers import TrialManager, CheckpointManager
from optimization_tree import OptimizationTree, ROOT_ID
from checkpoint_resolver import resolve_checkpoint


class CheckpointResolverIntegrationTest(unittest.TestCase):
    """Integration tests for resolve_checkpoint().

    Each test builds a tree with nodes that have source_trial_ids, creates
    corresponding TrialRecords, and creates checkpoints at FP/PL/CTS stages.
    Then calls resolve_checkpoint and verifies the result.
    """

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.flow_dir = self.tmpdir / "flow"
        self.runs_dir = self.tmpdir / "runs"
        self.runs_dir.mkdir(parents=True)

        # Create minimal ORFS-like artifact dirs + dummy files for manifest
        # CheckpointManager's STAGE_ARTIFACTS defines which files each stage
        # checkpoint requires.  Create dummies for FP, PL, and CTS so
        # manifests are non-empty and verification passes.
        self.variant = "agenticpd_iter0"
        for cat in ("results", "logs", "reports"):
            d = self.flow_dir / cat / "sky130hd" / "gcd" / self.variant
            d.mkdir(parents=True)
        _results = self.flow_dir / "results" / "sky130hd" / "gcd" / self.variant
        for fname in ("2_floorplan.odb", "2_floorplan.sdc",
                       "3_place.odb", "3_place.sdc",
                       "4_cts.odb", "4_cts.sdc"):
            (_results / fname).write_text(f"dummy content for {fname}")

        self.tm = TrialManager(self.runs_dir)
        self.cm = CheckpointManager(self.flow_dir)

        self.base_params = {
            "FP": {"CORE_UTILIZATION": 38, "CORE_ASPECT_RATIO": 1.0},
            "PL": {"CELL_PAD_IN_SITES_GLOBAL_PLACEMENT": 0},
            "CTS": {},
            "RT": {"FASTROUTE_LAYER_ADJUSTMENT": 0.2, "GRT_CONGESTION_ITERATIONS": 30},
        }

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir)

    # ------------------------------------------------------------------
    # Helpers: build a tree with checkpoints at FP/PL/CTS
    # ------------------------------------------------------------------
    def _build_tree_with_checkpoints(self):
        """Build tree: root→FP→PL→CTS, with checkpoints at each stage.

        Returns (tree, cts_node_id, fp_cp, pl_cp, cts_cp).
        """
        tree = OptimizationTree()
        tm, cm = self.tm, self.cm
        variant = self.variant

        # FP node
        fp_trial = TrialRecord(
            trial_id=_new_trial_id(), experiment_id="test",
            status="ok", artifact_dir=str(self.runs_dir / f"iter-0-{_new_trial_id()}"),
            params=self.base_params,
        )
        fp_trial.artifact_dir = str(self.runs_dir / fp_trial.trial_id)
        (self.runs_dir / fp_trial.trial_id).mkdir(parents=True)
        tm._write_trial(fp_trial)
        fp_ph = CheckpointManager.param_hash(self.base_params)
        fp_cp = cm.create(fp_trial, "FP", "sky130hd", "gcd", variant, fp_ph,
                          runs_dir=self.runs_dir)
        fp_node_id = tree.add_path(
            0, ROOT_ID,
            [("FP", variant, self.base_params["FP"], {"fp_ws_ps": -1154.0})],
            source_trial_id=fp_trial.trial_id,
        )[0]

        # PL node
        pl_trial = TrialRecord(
            trial_id=_new_trial_id(), experiment_id="test",
            status="ok", artifact_dir=str(self.runs_dir / f"iter-0-{_new_trial_id()}"),
            params=self.base_params,
        )
        pl_trial.artifact_dir = str(self.runs_dir / pl_trial.trial_id)
        (self.runs_dir / pl_trial.trial_id).mkdir(parents=True)
        tm._write_trial(pl_trial)
        pl_cp = cm.create(pl_trial, "PL", "sky130hd", "gcd", variant, fp_ph,
                          runs_dir=self.runs_dir)
        pl_node_id = tree.add_path(
            0, fp_node_id,
            [("PL", variant, self.base_params["PL"], {"pl_ws_ps": -1200.0})],
            source_trial_id=pl_trial.trial_id,
        )[0]

        # CTS node (branch origin for most tests)
        cts_trial = TrialRecord(
            trial_id=_new_trial_id(), experiment_id="test",
            status="ok", artifact_dir=str(self.runs_dir / f"iter-0-{_new_trial_id()}"),
            params=self.base_params,
        )
        cts_trial.artifact_dir = str(self.runs_dir / cts_trial.trial_id)
        (self.runs_dir / cts_trial.trial_id).mkdir(parents=True)
        tm._write_trial(cts_trial)
        cts_cp = cm.create(cts_trial, "CTS", "sky130hd", "gcd", variant, fp_ph,
                           runs_dir=self.runs_dir)
        cts_node_id = tree.add_path(
            0, pl_node_id,
            [("CTS", variant, self.base_params["CTS"], {"cts_ws_ps": -1180.0})],
            source_trial_id=cts_trial.trial_id,
        )[0]

        return tree, cts_node_id, fp_cp, pl_cp, cts_cp

    # ==================================================================
    # Scenario: CTS request + RT-only params → reuse CTS, effective=RT
    # ==================================================================
    def test_cts_request_rt_only_params_reuses_cts(self):
        """GRT_CONGESTION_ITERATIONS affects RT only → CTS checkpoint still valid."""
        tree, cts_node_id, fp_cp, pl_cp, cts_cp = self._build_tree_with_checkpoints()

        candidate = dict(self.base_params)
        candidate["RT"]["GRT_CONGESTION_ITERATIONS"] = 50  # RT-only change

        res = resolve_checkpoint(
            cts_node_id, "RT",
            candidate_params=candidate,
            inherited_params=self.base_params,
            tree=tree, trial_mgr=self.tm, checkpoint_mgr=self.cm,
        )

        self.assertEqual(res.execution_mode, "checkpoint_fork")
        self.assertEqual(res.effective_start_stage, "RT")
        self.assertEqual(res.requested_parent_node_id, cts_node_id)
        self.assertEqual(res.requested_start_stage, "RT")
        self.assertIsNotNone(res.consumed_checkpoint)
        self.assertIsNotNone(res.consumed_node_id,
                            "consumed_node_id must be set when checkpoint is consumed")
        self.assertIsNotNone(res.consumed_variant,
                            "consumed_variant must be set when checkpoint is consumed")
        self.assertTrue(res.manifest_verified)
        self.assertTrue(res.is_compatible)
        self.assertEqual(res.invalidating_parameters, [])

    # ==================================================================
    # Scenario: CTS request + CTS params → fallback PL, effective=CTS
    # ==================================================================
    def test_cts_request_cts_params_falls_back_to_pl(self):
        """CTS_CLUSTER_SIZE changes → CTS checkpoint incompatible,
        but PL checkpoint still valid → effective=CTS."""
        tree, cts_node_id, fp_cp, pl_cp, cts_cp = self._build_tree_with_checkpoints()

        candidate = dict(self.base_params)
        candidate["CTS"]["CTS_CLUSTER_SIZE"] = 50  # invalidates CTS only

        res = resolve_checkpoint(
            cts_node_id, "RT",
            candidate_params=candidate,
            inherited_params=self.base_params,
            tree=tree, trial_mgr=self.tm, checkpoint_mgr=self.cm,
        )

        self.assertEqual(res.execution_mode, "checkpoint_fork")
        self.assertEqual(res.effective_start_stage, "CTS",
                         "CTS checkpoint invalid, PL should be used → effective=CTS")
        # consumed_checkpoint should be the PL checkpoint
        self.assertIsNotNone(res.consumed_checkpoint)
        self.assertIn("PL", res.consumed_checkpoint or "",
                      f"Expected PL checkpoint, got {res.consumed_checkpoint}")
        # consumed variant must be from PL checkpoint source, not CTS
        self.assertIsNotNone(res.consumed_variant)
        self.assertIsNotNone(res.consumed_node_id)
        self.assertTrue(res.compatibility_checked)
        # PL checkpoint IS compatible (CTS_CLUSTER_SIZE only affects CTS+RT,
        # not PL), so invalidating_parameters may be empty for the PL
        # checkpoint that was ultimately consumed.

    # ==================================================================
    # Scenario: CTS request + PL params → fallback FP, effective=PL
    # ==================================================================
    def test_cts_request_pl_params_falls_back_to_fp(self):
        """CELL_PAD_IN_SITES_GLOBAL_PLACEMENT changes → PL+CTS checkpoints
        incompatible, but FP checkpoint still valid → effective=PL."""
        tree, cts_node_id, fp_cp, pl_cp, cts_cp = self._build_tree_with_checkpoints()

        candidate = dict(self.base_params)
        candidate["PL"]["CELL_PAD_IN_SITES_GLOBAL_PLACEMENT"] = 2

        res = resolve_checkpoint(
            cts_node_id, "RT",
            candidate_params=candidate,
            inherited_params=self.base_params,
            tree=tree, trial_mgr=self.tm, checkpoint_mgr=self.cm,
        )

        self.assertEqual(res.execution_mode, "checkpoint_fork")
        self.assertEqual(res.effective_start_stage, "PL",
                         "PL+CTS checkpoints invalid, FP should be used → effective=PL")
        self.assertIsNotNone(res.consumed_checkpoint)
        self.assertIn("FP", res.consumed_checkpoint or "",
                      f"Expected FP checkpoint, got {res.consumed_checkpoint}")
        # consumed variant must be from FP checkpoint source
        self.assertIsNotNone(res.consumed_variant)
        self.assertIsNotNone(res.consumed_node_id)

    # ==================================================================
    # Scenario: CTS request + FP params → full restart, effective=FP
    # ==================================================================
    def test_cts_request_fp_params_full_restart(self):
        """CORE_UTILIZATION changes → all checkpoints incompatible → full restart."""
        tree, cts_node_id, fp_cp, pl_cp, cts_cp = self._build_tree_with_checkpoints()

        candidate = dict(self.base_params)
        candidate["FP"]["CORE_UTILIZATION"] = 50  # affects all stages

        res = resolve_checkpoint(
            cts_node_id, "RT",
            candidate_params=candidate,
            inherited_params=self.base_params,
            tree=tree, trial_mgr=self.tm, checkpoint_mgr=self.cm,
        )

        self.assertEqual(res.execution_mode, "full_restart")
        self.assertEqual(res.effective_start_stage, "FP")
        self.assertIsNone(res.consumed_checkpoint)
        self.assertIsNone(res.consumed_node_id,
                         "consumed_node_id must be None for full_restart")
        self.assertIsNone(res.consumed_variant,
                         "consumed_variant must be None for full_restart")
        self.assertIsNotNone(res.fallback_reason)
        self.assertIn("no compatible", (res.fallback_reason or ""))

    # ==================================================================
    # Scenario: manifest missing → skip that checkpoint
    # ==================================================================
    def test_manifest_missing_file_skips_checkpoint(self):
        """When an artifact file is missing, that checkpoint is skipped
        and an earlier compatible one is tried."""
        tree, cts_node_id, fp_cp, pl_cp, cts_cp = self._build_tree_with_checkpoints()

        # Delete CTS artifact → CTS checkpoint manifest fails.
        # PL checkpoint is still valid → should fall back to PL.
        cts_odb = (self.flow_dir / "results" / "sky130hd" / "gcd"
                   / self.variant / "4_cts.odb")
        cts_odb.unlink()

        candidate = dict(self.base_params)
        candidate["RT"]["GRT_CONGESTION_ITERATIONS"] = 50  # RT-only change

        res = resolve_checkpoint(
            cts_node_id, "RT",
            candidate_params=candidate,
            inherited_params=self.base_params,
            tree=tree, trial_mgr=self.tm, checkpoint_mgr=self.cm,
        )

        # CTS checkpoint manifest fails → PL checkpoint is used.
        self.assertEqual(res.execution_mode, "checkpoint_fork",
                         "CTS manifest failed, PL checkpoint should be used")
        self.assertEqual(res.effective_start_stage, "CTS",
                         "PL checkpoint compatible → effective=CTS")

    def test_all_manifests_fail_full_restart(self):
        """When all artifact files are missing, full restart is the result."""
        tree, cts_node_id, fp_cp, pl_cp, cts_cp = self._build_tree_with_checkpoints()

        # Delete ALL artifact files
        for fname in ("2_floorplan.odb", "2_floorplan.sdc",
                       "3_place.odb", "3_place.sdc",
                       "4_cts.odb", "4_cts.sdc"):
            p = (self.flow_dir / "results" / "sky130hd" / "gcd"
                 / self.variant / fname)
            if p.exists():
                p.unlink()

        candidate = dict(self.base_params)
        candidate["RT"]["GRT_CONGESTION_ITERATIONS"] = 50

        res = resolve_checkpoint(
            cts_node_id, "RT",
            candidate_params=candidate,
            inherited_params=self.base_params,
            tree=tree, trial_mgr=self.tm, checkpoint_mgr=self.cm,
        )

        self.assertEqual(res.execution_mode, "full_restart")
        self.assertEqual(res.effective_start_stage, "FP")
        self.assertIsNotNone(res.fallback_reason)

    # ==================================================================
    # Scenario: hash mismatch → skip that checkpoint
    # ==================================================================
    def test_hash_mismatch_skips_checkpoint(self):
        """A tampered artifact file causes hash mismatch → that checkpoint
        is skipped; the resolver falls back to an earlier checkpoint."""
        tree, cts_node_id, fp_cp, pl_cp, cts_cp = self._build_tree_with_checkpoints()

        # Tamper with CTS artifact → CTS checkpoint fails hash check.
        cts_odb = (self.flow_dir / "results" / "sky130hd" / "gcd"
                   / self.variant / "4_cts.odb")
        cts_odb.write_text("TAMPERED CTS — different from original hash")

        candidate = dict(self.base_params)
        candidate["RT"]["GRT_CONGESTION_ITERATIONS"] = 50  # RT-only

        res = resolve_checkpoint(
            cts_node_id, "RT",
            candidate_params=candidate,
            inherited_params=self.base_params,
            tree=tree, trial_mgr=self.tm, checkpoint_mgr=self.cm,
        )

        # CTS checkpoint hash mismatch → skipped; PL is still valid.
        self.assertEqual(res.execution_mode, "checkpoint_fork",
                         "CTS hash mismatch → should fall back to PL checkpoint")
        self.assertEqual(res.effective_start_stage, "CTS")

    # ==================================================================
    # Scenario: unknown parameter → conservative incompatible
    # ==================================================================
    def test_unknown_parameter_conservative_incompatible(self):
        """Unknown parameters are conservatively treated as incompatible."""
        tree, cts_node_id, fp_cp, pl_cp, cts_cp = self._build_tree_with_checkpoints()

        candidate = dict(self.base_params)
        candidate["FP"]["UNKNOWN_PARAM_XYZ"] = 999

        res = resolve_checkpoint(
            cts_node_id, "RT",
            candidate_params=candidate,
            inherited_params=self.base_params,
            tree=tree, trial_mgr=self.tm, checkpoint_mgr=self.cm,
        )

        self.assertEqual(res.execution_mode, "full_restart")
        self.assertTrue(res.compatibility_checked)
        self.assertFalse(res.is_compatible)
        unknown_params = [p for p in res.invalidating_parameters if "unknown" in p.lower()]
        self.assertGreater(len(unknown_params), 0,
                          f"Expected '(unknown)' suffix in: {res.invalidating_parameters}")

    # ==================================================================
    # Scenario: old Trial JSON loads with execution_resolution=null
    # ==================================================================
    def test_old_trial_json_loads_with_null_execution_resolution(self):
        """Backward compat: Trial JSON without execution_resolution key
        deserialises with execution_resolution=None."""
        old_dict = {
            "trial_id": "old001",
            "experiment_id": "old-test",
            "status": "ok",
            "params": {"FP": {}, "PL": {}, "CTS": {}, "RT": {}},
            "final_qor": {"wns_ps": -100.0, "tns_ps": -200.0, "area_um2": 500.0, "power_w": 0.01},
            "stage_results": [],
        }
        trial = TrialRecord.from_dict(old_dict)
        self.assertIsNone(trial.execution_resolution,
                          "Old trials without execution_resolution key should load with null")

        # Round-trip: old dict → TrialRecord → dict → TrialRecord
        roundtrip = TrialRecord.from_dict(trial.to_dict())
        self.assertIsNone(roundtrip.execution_resolution)

    # ==================================================================
    # Scenario: resolution fully persisted when requested ≠ effective
    # ==================================================================
    def test_resolution_persisted_when_requested_differs_from_effective(self):
        """When the effective start stage differs from the requested,
        the full ExecutionResolution is persisted in the TrialRecord."""
        tree, cts_node_id, fp_cp, pl_cp, cts_cp = self._build_tree_with_checkpoints()

        candidate = dict(self.base_params)
        candidate["FP"]["CORE_UTILIZATION"] = 50  # forces full restart

        res = resolve_checkpoint(
            cts_node_id, "RT",  # requested: RT
            candidate_params=candidate,
            inherited_params=self.base_params,
            tree=tree, trial_mgr=self.tm, checkpoint_mgr=self.cm,
        )

        self.assertNotEqual(res.requested_start_stage, res.effective_start_stage,
                            "This test requires requested ≠ effective")
        self.assertEqual(res.requested_start_stage, "RT")
        self.assertEqual(res.effective_start_stage, "FP")
        self.assertEqual(res.execution_mode, "full_restart")

        # Serialize to trial and verify roundtrip
        trial = TrialRecord(
            trial_id=_new_trial_id(),
            experiment_id="test-resolution",
            status="ok",
            execution_resolution=res,
            params=candidate,
        )
        d = trial.to_dict()
        self.assertIsNotNone(d.get("execution_resolution"))

        trial2 = TrialRecord.from_dict(d)
        self.assertIsNotNone(trial2.execution_resolution)
        self.assertEqual(trial2.execution_resolution.requested_start_stage, "RT")
        self.assertEqual(trial2.execution_resolution.effective_start_stage, "FP")
        self.assertEqual(trial2.execution_resolution.execution_mode, "full_restart")
        self.assertEqual(trial2.execution_resolution.requested_parent_node_id, cts_node_id)
        self.assertIsNotNone(trial2.execution_resolution.fallback_reason)

    # ==================================================================
    # Scenario: source trial association from tree node, not last trial
    # ==================================================================
    def test_source_trial_from_tree_node_not_last_trial(self):
        """The parent trial for a new iteration comes from the tree node's
        source_trial_id, NOT from self._current_trial (which could be stale)."""
        tree, cts_node_id, fp_cp, pl_cp, cts_cp = self._build_tree_with_checkpoints()

        # The CTS node should have a source_trial_id
        cts_node = tree.find_node(cts_node_id)
        self.assertIsNotNone(cts_node)
        self.assertIsNotNone(cts_node.source_trial_id,
                             "Tree node must have source_trial_id for proper parent association")

        # Verify that the source trial actually exists in TrialManager
        src_trial = self.tm.get(cts_node.source_trial_id)
        self.assertIsNotNone(src_trial,
                            f"source trial {cts_node.source_trial_id} should be loadable")

        # Also verify FP and PL nodes have source_trial_ids
        fp_node = tree.find_node(f"iter0_FP")
        self.assertIsNotNone(fp_node.source_trial_id)
        pl_node = tree.find_node(f"iter0_PL")
        self.assertIsNotNone(pl_node.source_trial_id)

    # ==================================================================
    # Additional: root-only tree → full restart
    # ==================================================================
    def test_root_only_tree_full_restart(self):
        """When the tree has only a root node, resolution returns full_restart."""
        tree = OptimizationTree()
        candidate = dict(self.base_params)

        res = resolve_checkpoint(
            ROOT_ID, "FP",
            candidate_params=candidate,
            inherited_params={"FP": {}, "PL": {}, "CTS": {}},
            tree=tree, trial_mgr=self.tm, checkpoint_mgr=self.cm,
        )

        self.assertEqual(res.execution_mode, "full_restart")
        self.assertEqual(res.effective_start_stage, "FP")
        self.assertIsNotNone(res.fallback_reason)

    # ==================================================================
    # Backward compat: OptimNode from_dict without source_trial_id
    # ==================================================================
    def test_optim_node_backward_compat_no_source_trial_id(self):
        """Old tree.json nodes without source_trial_id load with None."""
        from optimization_tree import OptimNode
        old_dict = {
            "node_id": "iter0_FP", "iteration": 0, "stage": "FP",
            "variant": "base", "params": {}, "stage_qor": None,
            "parent_id": "root", "children_ids": [], "branch_count": 0,
        }
        node = OptimNode.from_dict(old_dict)
        self.assertIsNone(node.source_trial_id)

    # ==================================================================
    # ExecutionResolution serialization edge cases
    # ==================================================================
    def test_execution_resolution_to_dict_all_fields(self):
        """Full ExecutionResolution round-trips through dict."""
        er = ExecutionResolution(
            requested_parent_node_id="iter1_CTS",
            requested_start_stage="RT",
            effective_start_stage="PL",
            execution_mode="checkpoint_fork",
            consumed_checkpoint="cp-test001-FP",
            consumed_node_id="iter0_FP",
            consumed_variant="agenticpd_iter0",
            manifest_verified=True,
            manifest_errors=[],
            compatibility_checked=True,
            is_compatible=True,
            invalidating_parameters=["CTS_CLUSTER_SIZE"],
            fallback_reason="CTS checkpoint incompatible, fell back to PL",
        )
        d = er.to_dict()
        er2 = ExecutionResolution.from_dict(d)
        self.assertIsNotNone(er2)
        self.assertEqual(er2.requested_parent_node_id, "iter1_CTS")
        self.assertEqual(er2.effective_start_stage, "PL")
        self.assertEqual(er2.consumed_checkpoint, "cp-test001-FP")
        self.assertEqual(er2.consumed_node_id, "iter0_FP")
        self.assertEqual(er2.consumed_variant, "agenticpd_iter0")
        self.assertEqual(er2.invalidating_parameters, ["CTS_CLUSTER_SIZE"])

    def test_execution_resolution_from_none_returns_none(self):
        """from_dict(None) → None (backward compat)."""
        self.assertIsNone(ExecutionResolution.from_dict(None))

    def test_execution_resolution_from_empty_returns_none(self):
        """from_dict({}) → None (empty dict treated as no resolution)."""
        self.assertIsNone(ExecutionResolution.from_dict({}))

    # ==================================================================
    # Stage D fix: audit trail completeness
    # ==================================================================

    def test_audit_trail_single_entry_when_deepest_consumed(self):
        """When the deepest (CTS) checkpoint is compatible, the audit trail
        has exactly one entry (consumed, no rejection_reason)."""
        tree, cts_node_id, fp_cp, pl_cp, cts_cp = self._build_tree_with_checkpoints()

        candidate = dict(self.base_params)
        candidate["RT"]["GRT_CONGESTION_ITERATIONS"] = 50  # RT-only

        res = resolve_checkpoint(
            cts_node_id, "RT",
            candidate_params=candidate,
            inherited_params=self.base_params,
            tree=tree, trial_mgr=self.tm, checkpoint_mgr=self.cm,
        )

        self.assertEqual(res.execution_mode, "checkpoint_fork")
        self.assertEqual(len(res.checkpoint_audit_trail), 1,
                         "Deepest consumed → one audit entry")
        entry = res.checkpoint_audit_trail[0]
        self.assertEqual(entry.checkpoint_id, cts_cp.checkpoint_id)
        self.assertEqual(entry.stage, "CTS")
        self.assertTrue(entry.manifest_verified)
        self.assertTrue(entry.is_compatible)
        self.assertIsNone(entry.rejection_reason,
                          "Consumed entry must have null rejection_reason")
        # Flat fields match consumed entry
        self.assertTrue(res.manifest_verified)
        self.assertTrue(res.is_compatible)
        self.assertEqual(res.invalidating_parameters, [])
        # No fallback occurred
        self.assertIsNone(res.fallback_reason,
                          "fallback_reason must be None when deepest is consumed")

    def test_audit_trail_preserves_rejected_checkpoints(self):
        """When CTS is rejected (CTS param change) and PL is consumed,
        the audit trail has both entries: CTS(rejected) + PL(consumed)."""
        tree, cts_node_id, fp_cp, pl_cp, cts_cp = self._build_tree_with_checkpoints()

        candidate = dict(self.base_params)
        candidate["CTS"]["CTS_CLUSTER_SIZE"] = 50  # invalidates CTS only

        res = resolve_checkpoint(
            cts_node_id, "RT",
            candidate_params=candidate,
            inherited_params=self.base_params,
            tree=tree, trial_mgr=self.tm, checkpoint_mgr=self.cm,
        )

        self.assertEqual(res.execution_mode, "checkpoint_fork")
        self.assertEqual(len(res.checkpoint_audit_trail), 2,
                         "CTS rejected + PL consumed → two entries")

        # First entry: CTS (rejected — compatibility failure)
        cts_entry = res.checkpoint_audit_trail[0]
        self.assertEqual(cts_entry.checkpoint_id, cts_cp.checkpoint_id)
        self.assertEqual(cts_entry.stage, "CTS")
        self.assertTrue(cts_entry.manifest_verified,
                        "CTS manifest should pass (files exist)")
        self.assertTrue(cts_entry.compatibility_checked)
        self.assertFalse(cts_entry.is_compatible)
        self.assertIsNotNone(cts_entry.rejection_reason)
        self.assertIn("incompat", cts_entry.rejection_reason.lower())
        self.assertIn("CTS_CLUSTER_SIZE", cts_entry.invalidating_parameters)

        # Second entry: PL (consumed)
        pl_entry = res.checkpoint_audit_trail[1]
        self.assertEqual(pl_entry.checkpoint_id, pl_cp.checkpoint_id)
        self.assertEqual(pl_entry.stage, "PL")
        self.assertTrue(pl_entry.manifest_verified)
        self.assertTrue(pl_entry.is_compatible)
        self.assertIsNone(pl_entry.rejection_reason,
                          "Consumed entry must have null rejection_reason")

        # Flat fields reflect the CONSUMED (PL) checkpoint
        self.assertTrue(res.is_compatible)
        self.assertEqual(res.consumed_checkpoint, pl_cp.checkpoint_id)

        # fallback_reason must be populated since deepest was rejected
        self.assertIsNotNone(res.fallback_reason)
        self.assertIn("fell back", (res.fallback_reason or "").lower())
        self.assertIn(cts_cp.checkpoint_id, res.fallback_reason or "")

    def test_audit_trail_full_restart_all_rejected(self):
        """When all checkpoints are rejected (FP param change), audit trail
        has three entries, all with rejection_reason populated."""
        tree, cts_node_id, fp_cp, pl_cp, cts_cp = self._build_tree_with_checkpoints()

        candidate = dict(self.base_params)
        candidate["FP"]["CORE_UTILIZATION"] = 50  # invalidates all

        res = resolve_checkpoint(
            cts_node_id, "RT",
            candidate_params=candidate,
            inherited_params=self.base_params,
            tree=tree, trial_mgr=self.tm, checkpoint_mgr=self.cm,
        )

        self.assertEqual(res.execution_mode, "full_restart")
        self.assertEqual(len(res.checkpoint_audit_trail), 3,
                         "All three checkpoints examined")

        # All entries are rejected
        for entry in res.checkpoint_audit_trail:
            self.assertFalse(entry.is_compatible)
            self.assertIsNotNone(entry.rejection_reason,
                                f"Entry {entry.checkpoint_id} must have rejection_reason")

        # Flat fields describe the deepest (first) audit entry.
        # All manifests pass → manifest_verified is True.
        self.assertTrue(res.manifest_verified,
                        "Deepest (CTS) manifest passed → manifest_verified=True")
        self.assertEqual(res.manifest_errors, [],
                         "Deepest (CTS) manifest passed → manifest_errors=[]")
        self.assertFalse(res.is_compatible)
        self.assertTrue(res.compatibility_checked,
                        "compatibility was checked on at least one checkpoint")
        self.assertGreater(len(res.invalidating_parameters), 0,
                           "invalidating_parameters from deepest entry populated")
        # consumed fields must all be None
        self.assertIsNone(res.consumed_checkpoint)
        self.assertIsNone(res.consumed_node_id)
        self.assertIsNone(res.consumed_variant)

        # fallback_reason summarises all rejections
        self.assertIsNotNone(res.fallback_reason)
        self.assertIn("all rejected", (res.fallback_reason or ""))

    def test_audit_trail_manifest_failure_recorded(self):
        """Manifest failure produces an audit entry with manifest_verified=False
        and no compatibility check (compatibility_checked=False)."""
        tree, cts_node_id, fp_cp, pl_cp, cts_cp = self._build_tree_with_checkpoints()

        # Delete CTS artifact → manifest failure
        cts_odb = (self.flow_dir / "results" / "sky130hd" / "gcd"
                   / self.variant / "4_cts.odb")
        cts_odb.unlink()

        candidate = dict(self.base_params)
        candidate["RT"]["GRT_CONGESTION_ITERATIONS"] = 50

        res = resolve_checkpoint(
            cts_node_id, "RT",
            candidate_params=candidate,
            inherited_params=self.base_params,
            tree=tree, trial_mgr=self.tm, checkpoint_mgr=self.cm,
        )

        self.assertEqual(res.execution_mode, "checkpoint_fork")

        # First entry: CTS (manifest failure)
        cts_entry = res.checkpoint_audit_trail[0]
        self.assertEqual(cts_entry.checkpoint_id, cts_cp.checkpoint_id)
        self.assertFalse(cts_entry.manifest_verified)
        self.assertGreater(len(cts_entry.manifest_errors), 0,
                           "Manifest errors must be recorded")
        self.assertFalse(cts_entry.compatibility_checked,
                         "Compatibility not checked when manifest fails")
        self.assertIsNotNone(cts_entry.rejection_reason)
        self.assertIn("manifest", cts_entry.rejection_reason.lower())

        # Second entry: PL (consumed)
        pl_entry = res.checkpoint_audit_trail[1]
        self.assertTrue(pl_entry.manifest_verified)
        self.assertTrue(pl_entry.is_compatible)
        self.assertIsNone(pl_entry.rejection_reason)

    # ==================================================================
    # Stage D fix: flat fields describe deepest entry on full_restart
    # ==================================================================

    def test_full_restart_all_manifest_ok_only_param_incompat(self):
        """All manifests pass (files exist), only parameter incompatibility
        causes full_restart. Flat fields must reflect the deepest (CTS)
        entry: manifest_verified=True, manifest_errors=[], is_compatible=False,
        invalidating_parameters populated from deepest entry."""
        tree, cts_node_id, fp_cp, pl_cp, cts_cp = self._build_tree_with_checkpoints()

        candidate = dict(self.base_params)
        candidate["FP"]["CORE_UTILIZATION"] = 50  # invalidates all checkpoints

        res = resolve_checkpoint(
            cts_node_id, "RT",
            candidate_params=candidate,
            inherited_params=self.base_params,
            tree=tree, trial_mgr=self.tm, checkpoint_mgr=self.cm,
        )

        self.assertEqual(res.execution_mode, "full_restart")

        # Deepest (CTS) entry: manifest passed, compatibility failed
        deepest = res.checkpoint_audit_trail[0]
        self.assertEqual(deepest.stage, "CTS")
        self.assertTrue(deepest.manifest_verified,
                        "CTS manifest should pass (files exist)")
        self.assertFalse(deepest.is_compatible)

        # Flat fields must match deepest entry's values
        self.assertTrue(res.manifest_verified,
                        "Deepest manifest passed → manifest_verified=True")
        self.assertEqual(res.manifest_errors, [],
                         "Deepest manifest passed → manifest_errors=[]")
        self.assertTrue(res.compatibility_checked)
        self.assertFalse(res.is_compatible)
        self.assertGreater(len(res.invalidating_parameters), 0,
                           "invalidating_parameters from deepest entry")
        for p in res.invalidating_parameters:
            self.assertIn("CORE_UTILIZATION", p)
        # consumed_* all None
        self.assertIsNone(res.consumed_checkpoint)
        self.assertIsNone(res.consumed_node_id)
        self.assertIsNone(res.consumed_variant)

    def test_full_restart_deepest_manifest_failed_flat_fields_reflect_it(self):
        """Deepest (CTS) checkpoint has manifest failure. Even if a shallower
        checkpoint later passes manifest, the flat fields must still reflect
        the DEEPEST (first-attempted) entry: manifest_verified=False,
        manifest_errors populated from CTS."""
        tree, cts_node_id, fp_cp, pl_cp, cts_cp = self._build_tree_with_checkpoints()

        # Delete CTS artifacts → CTS manifest fails.
        # Also change FP params so PL checkpoint isn't suddenly consumed.
        cts_odb = (self.flow_dir / "results" / "sky130hd" / "gcd"
                   / self.variant / "4_cts.odb")
        cts_odb.unlink()

        candidate = dict(self.base_params)
        candidate["FP"]["CORE_UTILIZATION"] = 50  # invalidates remaining checkpoints

        res = resolve_checkpoint(
            cts_node_id, "RT",
            candidate_params=candidate,
            inherited_params=self.base_params,
            tree=tree, trial_mgr=self.tm, checkpoint_mgr=self.cm,
        )

        self.assertEqual(res.execution_mode, "full_restart")

        # Verify CTS manifest failure in audit trail
        deepest = res.checkpoint_audit_trail[0]
        self.assertEqual(deepest.stage, "CTS")
        self.assertFalse(deepest.manifest_verified,
                         "CTS manifest should fail (file deleted)")
        self.assertGreater(len(deepest.manifest_errors), 0)

        # All five flat fields must be direct copies of the deepest
        # (first) audit entry.
        self.assertEqual(res.manifest_verified, deepest.manifest_verified,
                         "manifest_verified must match deepest entry")
        self.assertEqual(res.manifest_errors, deepest.manifest_errors,
                         "manifest_errors must match deepest entry")
        self.assertEqual(res.compatibility_checked, deepest.compatibility_checked,
                         "compatibility_checked must match deepest entry")
        self.assertEqual(res.is_compatible, deepest.is_compatible,
                         "is_compatible must match deepest entry")
        self.assertEqual(res.invalidating_parameters, deepest.invalidating_parameters,
                         "invalidating_parameters must match deepest entry")
        # consumed_* all None
        self.assertIsNone(res.consumed_checkpoint)
        self.assertIsNone(res.consumed_node_id)
        self.assertIsNone(res.consumed_variant)

    def test_consumed_checkpoint_flat_fields_describe_consumed_entry(self):
        """When a checkpoint IS consumed, flat fields describe the consumed
        entry — manifest_verified=True, is_compatible=True."""
        tree, cts_node_id, fp_cp, pl_cp, cts_cp = self._build_tree_with_checkpoints()

        candidate = dict(self.base_params)
        candidate["RT"]["GRT_CONGESTION_ITERATIONS"] = 50  # RT-only change

        res = resolve_checkpoint(
            cts_node_id, "RT",
            candidate_params=candidate,
            inherited_params=self.base_params,
            tree=tree, trial_mgr=self.tm, checkpoint_mgr=self.cm,
        )

        self.assertEqual(res.execution_mode, "checkpoint_fork")
        self.assertEqual(res.effective_start_stage, "RT")

        # Audit trail: one consumed entry
        self.assertEqual(len(res.checkpoint_audit_trail), 1)
        consumed = res.checkpoint_audit_trail[0]
        self.assertTrue(consumed.is_compatible)
        self.assertIsNone(consumed.rejection_reason)

        # Flat fields describe the consumed entry
        self.assertTrue(res.manifest_verified)
        self.assertEqual(res.manifest_errors, [])
        self.assertTrue(res.compatibility_checked)
        self.assertTrue(res.is_compatible)
        self.assertEqual(res.invalidating_parameters, [])
        self.assertIsNotNone(res.consumed_checkpoint)
        self.assertIsNotNone(res.consumed_node_id)
        self.assertIsNotNone(res.consumed_variant)
        self.assertIsNone(res.fallback_reason,
                          "No fallback when deepest is consumed")

    # ==================================================================
    # Audit trail serialization round-trip
    # ==================================================================

    def test_execution_resolution_audit_trail_roundtrip(self):
        """ExecutionResolution with audit trail survives to_dict →
        from_dict → to_dict round-trip."""
        er = ExecutionResolution(
            requested_parent_node_id="iter1_CTS",
            requested_start_stage="RT",
            effective_start_stage="CTS",
            execution_mode="checkpoint_fork",
            consumed_checkpoint="cp-test001-PL",
            consumed_node_id="iter0_PL",
            consumed_variant="agenticpd_iter0",
            manifest_verified=True,
            compatibility_checked=True,
            is_compatible=True,
            fallback_reason="fell back from deeper checkpoint(s): cp-x-CTS(CTS): manifest failed",
            checkpoint_audit_trail=[
                CheckpointAuditEntry(
                    checkpoint_id="cp-x-CTS", stage="CTS",
                    source_trial_id="trial_cts",
                    manifest_verified=False,
                    manifest_errors=["MISSING: results/.../4_cts.odb"],
                    compatibility_checked=False, is_compatible=False,
                    rejection_reason="manifest verification failed: MISSING: results/.../4_cts.odb",
                ),
                CheckpointAuditEntry(
                    checkpoint_id="cp-test001-PL", stage="PL",
                    source_trial_id="test001",
                    manifest_verified=True,
                    compatibility_checked=True, is_compatible=True,
                    rejection_reason=None,
                ),
            ],
        )

        d = er.to_dict()
        er2 = ExecutionResolution.from_dict(d)
        self.assertIsNotNone(er2)

        # Audit trail survived
        self.assertEqual(len(er2.checkpoint_audit_trail), 2)
        self.assertEqual(er2.checkpoint_audit_trail[0].checkpoint_id, "cp-x-CTS")
        self.assertFalse(er2.checkpoint_audit_trail[0].manifest_verified)
        self.assertEqual(er2.checkpoint_audit_trail[1].checkpoint_id, "cp-test001-PL")
        self.assertTrue(er2.checkpoint_audit_trail[1].is_compatible)
        self.assertIsNone(er2.checkpoint_audit_trail[1].rejection_reason)

        # Flat fields also survived
        self.assertTrue(er2.is_compatible)
        self.assertEqual(er2.consumed_checkpoint, "cp-test001-PL")

    def test_execution_resolution_audit_trail_backward_compat(self):
        """Old ExecutionResolution JSON without checkpoint_audit_trail
        deserialises with an empty trail (backward compat)."""
        old_dict = {
            "requested_parent_node_id": "iter0_CTS",
            "requested_start_stage": "RT",
            "effective_start_stage": "RT",
            "execution_mode": "checkpoint_fork",
            "consumed_checkpoint": "cp-old-CTS",
            "consumed_node_id": "iter0_CTS",
            "consumed_variant": "base",
            "manifest_verified": True,
            "manifest_errors": [],
            "compatibility_checked": True,
            "is_compatible": True,
            "invalidating_parameters": [],
            "fallback_reason": None,
            # No checkpoint_audit_trail key
        }
        er = ExecutionResolution.from_dict(old_dict)
        self.assertIsNotNone(er)
        self.assertEqual(er.consumed_checkpoint, "cp-old-CTS")
        self.assertEqual(er.checkpoint_audit_trail, [],
                         "Old JSON without audit_trail → empty list")

        # Round-trip: old-style → dict → new-style (now with audit_trail=[])
        d = er.to_dict()
        self.assertIn("checkpoint_audit_trail", d)
        self.assertEqual(d["checkpoint_audit_trail"], [])

        er2 = ExecutionResolution.from_dict(d)
        self.assertEqual(er2.checkpoint_audit_trail, [])

    # ==================================================================
    # inherited_params interface preservation
    # ==================================================================
    def test_inherited_params_accepted_and_ignored(self):
        """inherited_params is accepted by resolve_checkpoint() but does
        not affect the decision — it is reserved for Optimizer integration."""
        import copy
        tree, cts_node_id, fp_cp, pl_cp, cts_cp = self._build_tree_with_checkpoints()

        # Deep copy to avoid shared inner-dict references between candidate
        # and inherited_params (shallow dict() copies would cross-mutate).
        candidate = copy.deepcopy(self.base_params)
        candidate["RT"]["GRT_CONGESTION_ITERATIONS"] = 50

        # Pass inherited_params that differ from candidate_params for Bef
        # stages.  The resolver should ignore the discrepancy because
        # compatibility is only checked against candidate_params.
        inherited = copy.deepcopy(self.base_params)
        inherited["FP"]["CORE_UTILIZATION"] = 999  # nonsense value

        res = resolve_checkpoint(
            cts_node_id, "RT",
            candidate_params=candidate,
            inherited_params=inherited,
            tree=tree, trial_mgr=self.tm, checkpoint_mgr=self.cm,
        )

        # Decision based on candidate_params (which matches base), not
        # inherited_params (which has a nonsense FP value).
        self.assertEqual(res.execution_mode, "checkpoint_fork",
                         "inherited_params is ignored; candidate_params determines outcome")
        self.assertEqual(res.effective_start_stage, "RT")



# =============================================================================
# Stage D fix 2.3+2.4: Fake agents + TrackingRunner for real
# Optimizer.run_iteration() integration tests
# =============================================================================

class FakeJudge:
    """Judge that returns a fixed decision (no LLM call)."""
    def __init__(self, branch_node_id: str, branch_stage: str,
                 hints: dict = None):
        self.branch_node_id = branch_node_id
        self.branch_stage = branch_stage
        self.hints = hints or {}

    def act(self, ctx: dict) -> dict:
        return {
            "branch_node": self.branch_node_id,
            "branch_stage": self.branch_stage,
            "hints": dict(self.hints),
        }


class FakeStageAgent:
    """StageAgent that returns fixed params (no LLM call)."""
    def __init__(self, params: dict = None, reason: str = "fake agent decision"):
        self.params = params or {}
        self.reason = reason

    def act(self, ctx: dict) -> dict:
        return {"params": dict(self.params), "reason": self.reason}


class TrackingMockRunner:
    """Wraps MockORFSRunner with call tracking for copy/clean/wipe.

    Delegates run_stage/run_finish to MockORFSRunner so tests get
    deterministic synthetic QoR without EDA tools.
    """

    def __init__(self, cfg):
        from orfs_interface import MockORFSRunner
        self._mock = MockORFSRunner(cfg)
        self.cfg = cfg
        self.copy_calls: list = []
        self.clean_downstream_calls: list = []
        self.wipe_variant_calls: list = []

    # --- delegated ORFS methods ---
    def run_flow(self, *args, **kwargs):
        return self._mock.run_flow(*args, **kwargs)

    def run_stage(self, *args, **kwargs):
        return self._mock.run_stage(*args, **kwargs)

    def run_finish(self, *args, **kwargs):
        return self._mock.run_finish(*args, **kwargs)

    def export_best(self, *args, **kwargs):
        return self._mock.export_best(*args, **kwargs)

    # --- tracked methods ---
    def copy_parent_results(self, parent_variant: str, new_variant: str) -> None:
        self.copy_calls.append((parent_variant, new_variant))

    def _wipe_variant(self, variant: str) -> None:
        self.wipe_variant_calls.append(variant)

    def _clean_downstream_stages(self, variant: str,
                                  from_stage: str) -> list:
        self.clean_downstream_calls.append((variant, from_stage))
        from orfs.parser import downstream_clean_targets
        return downstream_clean_targets(from_stage)

    def wipe_all_variants(self) -> int:
        return 0


class OptimizerIntegrationTest(unittest.TestCase):
    """Integration tests using real Optimizer.run_iteration() with
    FakeJudge, FakeStageAgents, and TrackingMockRunner.

    These tests directly assert:
      - Actual copy_parent_results source variant
      - Actual _clean_downstream_stages calls with correct from_stage
      - Tree parent node ID after run_iteration
      - Trial parent ID (lineage)
      - Full restart never copies
    """

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.flow_dir = self.tmpdir / "flow"
        self.session_dir = self.tmpdir / "runs" / "sky130hd_gcd" / "session"
        self.session_dir.mkdir(parents=True)

        # FrameworkConfig for the test session
        self.cfg = FrameworkConfig(
            platform="sky130hd", design="gcd", iterations=3,
            flow_dir=self.flow_dir,
            run_dir=self.session_dir,
        )

        from config import BASELINE_PARAMS, STAGES
        self.base_params = {s: dict(BASELINE_PARAMS.get(s, {}))
                            for s in STAGES}

        # Create minimal ORFS-like artifact dirs + dummy files for manifests
        self._setup_artifact_dirs("agenticpd_baseline")
        self._setup_artifact_dirs("variant_fp_baseline")
        self._setup_artifact_dirs("variant_pl_baseline")

        self.runner = TrackingMockRunner(self.cfg)
        from managers import TrialManager, CheckpointManager
        self.tm = TrialManager(self.session_dir)
        self.cm = CheckpointManager(self.flow_dir)

    def _setup_artifact_dirs(self, variant: str):
        for cat in ("results", "logs", "reports"):
            d = self.flow_dir / cat / "sky130hd" / "gcd" / variant
            d.mkdir(parents=True)
        r = self.flow_dir / "results" / "sky130hd" / "gcd" / variant
        for fname in ("2_floorplan.odb", "2_floorplan.sdc",
                       "3_place.odb", "3_place.sdc",
                       "4_cts.odb", "4_cts.sdc"):
            (r / fname).write_text(f"dummy {variant}/{fname}")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir)

    def _build_optimizer_with_tree(self, cts_node_id, judge_branch_stage="RT"):
        """Create an Optimizer with pre-built tree + checkpoints, then
        replace Judge/StageAgents with fakes."""
        from optimizer import Optimizer
        opt = Optimizer(self.cfg, llm=None, runner=self.runner)
        # Replace trial/checkpoint managers with our test instances
        opt.trial_mgr = self.tm
        opt.checkpoint_mgr = self.cm
        # Fake Judge: request from cts_node, branch_stage=RT
        opt.judge = FakeJudge(cts_node_id, judge_branch_stage)
        # Fake StageAgents: return baseline params (unchanged)
        opt.stage_agents = {
            s: FakeStageAgent(params=self.base_params.get(s, {}),
                              reason="fake: keep baseline")
            for s in config.STAGES
        }
        return opt

    def _build_baseline_tree_with_checkpoints(self):
        """Build tree root->FP->PL->CTS with distinct variants and checkpoints.

        Returns (tree, fp_node_id, pl_node_id, cts_node_id,
                 variant_fp, variant_pl, fp_trial, pl_trial, cts_trial).
        """
        tree = OptimizationTree()
        var_fp = "variant_fp_baseline"
        var_pl = "variant_pl_baseline"
        var_cts = "agenticpd_baseline"

        ph = CheckpointManager.param_hash(self.base_params)

        # FP
        fp_trial = TrialRecord(
            trial_id=_new_trial_id(), experiment_id="test-opt",
            status="ok",
            artifact_dir=str(self.session_dir / _new_trial_id()),
            params=self.base_params,
        )
        fp_trial.artifact_dir = str(self.session_dir / fp_trial.trial_id)
        (self.session_dir / fp_trial.trial_id).mkdir(parents=True)
        self.tm._write_trial(fp_trial)
        self.cm.create(fp_trial, "FP", "sky130hd", "gcd", var_fp, ph,
                       runs_dir=self.session_dir)
        fp_node_id = tree.add_path(
            0, ROOT_ID,
            [("FP", var_fp, self.base_params["FP"], {"fp_ws_ps": -1154.0})],
            source_trial_id=fp_trial.trial_id,
        )[0]

        # PL
        pl_trial = TrialRecord(
            trial_id=_new_trial_id(), experiment_id="test-opt",
            status="ok",
            artifact_dir=str(self.session_dir / _new_trial_id()),
            params=self.base_params,
        )
        pl_trial.artifact_dir = str(self.session_dir / pl_trial.trial_id)
        (self.session_dir / pl_trial.trial_id).mkdir(parents=True)
        self.tm._write_trial(pl_trial)
        self.cm.create(pl_trial, "PL", "sky130hd", "gcd", var_pl, ph,
                       runs_dir=self.session_dir)
        pl_node_id = tree.add_path(
            0, fp_node_id,
            [("PL", var_pl, self.base_params["PL"], {"pl_ws_ps": -1200.0})],
            source_trial_id=pl_trial.trial_id,
        )[0]

        # CTS
        cts_trial = TrialRecord(
            trial_id=_new_trial_id(), experiment_id="test-opt",
            status="ok",
            artifact_dir=str(self.session_dir / _new_trial_id()),
            params=self.base_params,
        )
        cts_trial.artifact_dir = str(self.session_dir / cts_trial.trial_id)
        (self.session_dir / cts_trial.trial_id).mkdir(parents=True)
        self.tm._write_trial(cts_trial)
        self.cm.create(cts_trial, "CTS", "sky130hd", "gcd", var_cts, ph,
                       runs_dir=self.session_dir)
        cts_node_id = tree.add_path(
            0, pl_node_id,
            [("CTS", var_cts, self.base_params["CTS"], {"cts_ws_ps": -1180.0})],
            source_trial_id=cts_trial.trial_id,
        )[0]

        return (tree, fp_node_id, pl_node_id, cts_node_id,
                var_fp, var_pl, var_cts,
                fp_trial, pl_trial, cts_trial)

    # ==================================================================
    # PL->CTS checkpoint fork: copy from PL, execute CTS+RT
    # ==================================================================
    def test_pl_to_cts_checkpoint_fork_real_run_iteration(self):
        """PL checkpoint fork with CTS_CLUSTER_SIZE change in downstream.
        Judge requests from PL node (branch_stage=CTS) so the CTS
        StageAgent generates new params.  The CTS checkpoint (which lives
        on a CTS node with a different variant) is incompatible due to the
        CTS param change, so the resolver consumes the PL checkpoint.
        run_iteration() must copy from PL variant, mount new nodes under
        the PL node, and set parent trial to the PL trial."""
        (tree, fp_node_id, pl_node_id, cts_node_id,
         var_fp, var_pl, var_cts,
         fp_trial, pl_trial, cts_trial) = self._build_baseline_tree_with_checkpoints()

        # CTS_CLUSTER_SIZE change: invalidates the CTS checkpoint (the PL
        # checkpoint remains compatible since this param only affects
        # CTS+RT).  Judge must request from PL node so branch_stage=CTS
        # and the CTS StageAgent is called (not inherited).
        self.base_params["CTS"]["CTS_CLUSTER_SIZE"] = 50

        opt = self._build_optimizer_with_tree(pl_node_id, judge_branch_stage="CTS")
        opt.tree = tree
        # Need a minimal history entry for observation summary
        opt.history = [{"iteration": 0, "status": "ok", "params": self.base_params,
                        "qor": {"wns_ps": -1150, "tns_ps": -2000, "area_um2": 5000, "power_w": 0.01},
                        "stage_qor": {}, "judge_decision": None}]
        opt._recompute_best()

        result = opt.run_iteration(1)

        self.assertTrue(result.ok, f"run_iteration should succeed, got: {result.error}")

        # ---- Assert copy source ----
        self.assertEqual(len(self.runner.copy_calls), 1,
                         "checkpoint_fork must call copy_parent_results once")
        copy_src, new_variant = self.runner.copy_calls[0]
        self.assertEqual(copy_src, var_pl,
                         f"Must copy from PL variant ({var_pl}), got {copy_src}")

        # ---- Assert cleanup ----
        self.assertEqual(len(self.runner.clean_downstream_calls), 1,
                         "checkpoint_fork must call _clean_downstream_stages")
        self.assertEqual(self.runner.clean_downstream_calls[0][1], "CTS",
                         "Must clean downstream from effective_start_stage=CTS")

        # ---- Assert tree parent ----
        new_cts_node = tree.find_node("iter1_CTS")
        self.assertIsNotNone(new_cts_node, "New CTS node must be in tree")
        self.assertEqual(new_cts_node.parent_id, pl_node_id,
                         f"New CTS parent must be PL ({pl_node_id}), "
                         f"not CTS ({cts_node_id})")

        # ---- Assert trial parent ----
        trial = self.tm.get(opt._current_trial.trial_id)
        self.assertIsNotNone(trial, "Trial must be persisted")
        self.assertEqual(trial.parent_trial_id, pl_trial.trial_id,
                         f"Trial parent must be PL trial ({pl_trial.trial_id})")

    # ==================================================================
    # Full restart: run_iteration verifies wipe, no copy, root parent
    # ==================================================================
    def test_full_restart_real_run_iteration(self):
        """Tree with NO checkpoints -> full_restart.
        run_iteration() must wipe variant, NOT copy, mount under ROOT,
        set parent trial to None, persist execution_resolution."""
        # Build tree WITHOUT creating checkpoints (no trials persisted).
        # The resolver finds zero ancestor checkpoints -> full_restart.
        tree = OptimizationTree()
        var_base = "agenticpd_baseline"
        fp_node_id = tree.add_path(
            0, ROOT_ID,
            [("FP", var_base, self.base_params["FP"], {"fp_ws_ps": -1154.0})],
            source_trial_id=None,
        )[0]
        pl_node_id = tree.add_path(
            0, fp_node_id,
            [("PL", var_base, self.base_params["PL"], {"pl_ws_ps": -1200.0})],
            source_trial_id=None,
        )[0]
        cts_node_id = tree.add_path(
            0, pl_node_id,
            [("CTS", var_base, self.base_params["CTS"], {"cts_ws_ps": -1180.0})],
            source_trial_id=None,
        )[0]

        opt = self._build_optimizer_with_tree(cts_node_id)
        opt.tree = tree
        opt.history = [{"iteration": 0, "status": "ok", "params": self.base_params,
                        "qor": {"wns_ps": -1150, "tns_ps": -2000, "area_um2": 5000, "power_w": 0.01},
                        "stage_qor": {}, "judge_decision": None}]
        opt._recompute_best()

        result = opt.run_iteration(1)

        self.assertTrue(result.ok, f"run_iteration should succeed, got: {result.error}")

        # ---- Assert NO copy ----
        self.assertEqual(len(self.runner.copy_calls), 0,
                         "full_restart must NEVER call copy_parent_results")

        # ---- Assert NO clean_downstream ----
        self.assertEqual(len(self.runner.clean_downstream_calls), 0,
                         "full_restart must NOT call _clean_downstream_stages")

        # ---- Assert wipe ----
        self.assertEqual(len(self.runner.wipe_variant_calls), 1,
                         "full_restart must call _wipe_variant")
        self.assertTrue(self.runner.wipe_variant_calls[0].startswith("agenticpd_iter"),
                        f"Must wipe new variant, got {self.runner.wipe_variant_calls}")

        # ---- Assert tree parent is ROOT ----
        new_fp_node = tree.find_node("iter1_FP")
        self.assertIsNotNone(new_fp_node, "New FP node must be in tree")
        self.assertEqual(new_fp_node.parent_id, ROOT_ID,
                         "Full restart must mount under ROOT")

        # ---- Assert trial parent is None ----
        trial = self.tm.get(opt._current_trial.trial_id)
        self.assertIsNotNone(trial, "Trial must be persisted")
        self.assertIsNone(trial.parent_trial_id,
                          "Full restart trial must have no parent")

        # ---- Assert execution_resolution is persisted ----
        self.assertIsNotNone(trial.execution_resolution)
        self.assertEqual(trial.execution_resolution.execution_mode, "full_restart")


# =============================================================================
# Stage D fix 2.2: downstream clean target coverage + file-level regression
# =============================================================================

class DownstreamCleanTargetTest(unittest.TestCase):
    """Verify downstream_clean_targets() covers all stages including
    unprefixed files like route.guide, output_guide.mod, updated_clks.sdc.

    The ORFS make targets are the authoritative source for what gets
    cleaned; these tests verify our mapping is correct and that the
    targets cover the file types Codex identified.
    """

    def test_clean_targets_from_cts(self):
        """CTS fork cleans CTS + RT + finish.
        clean_route handles route.guide, output_guide.mod, updated_clks.sdc.
        clean_finish handles 6_* artifacts."""
        from orfs.parser import downstream_clean_targets
        targets = downstream_clean_targets("CTS")
        self.assertEqual(targets, ["clean_cts", "clean_route", "clean_finish"])

    def test_clean_targets_from_rt(self):
        """RT fork cleans RT + finish."""
        from orfs.parser import downstream_clean_targets
        targets = downstream_clean_targets("RT")
        self.assertEqual(targets, ["clean_route", "clean_finish"])

    def test_clean_targets_from_pl(self):
        """PL fork cleans PL + CTS + RT + finish."""
        from orfs.parser import downstream_clean_targets
        targets = downstream_clean_targets("PL")
        self.assertEqual(targets, ["clean_place", "clean_cts", "clean_route", "clean_finish"])

    def test_clean_targets_from_fp(self):
        """FP fork cleans all stages."""
        from orfs.parser import downstream_clean_targets
        targets = downstream_clean_targets("FP")
        self.assertEqual(targets, ["clean_floorplan", "clean_place",
                                   "clean_cts", "clean_route", "clean_finish"])

    def test_finish_clean_target_exists(self):
        """clean_finish is registered (guards 6_* artifacts)."""
        from orfs.parser import CLEAN_TARGETS
        self.assertIn("finish", CLEAN_TARGETS)
        self.assertEqual(CLEAN_TARGETS["finish"], "clean_finish")

    def test_unprefixed_files_covered_by_clean_targets(self):
        """Create route.guide, output_guide.mod, updated_clks.sdc in a
        temp variant directory and verify they would be deleted by the
        relevant ORFS clean targets.

        Note: this test does NOT invoke make -- it validates that the file
        names are known and that downstream_clean_targets() includes the
        stages whose ORFS clean targets handle them:
          - route.guide, output_guide.mod, updated_clks.sdc -> clean_route
          - 6_* artifacts                                   -> clean_finish
        """
        import tempfile, shutil
        tmpdir = Path(tempfile.mkdtemp())
        try:
            # Create a fake variant with the unprefixed files
            variant = "test_variant"
            for cat in ("results", "logs", "reports", "objects"):
                d = tmpdir / cat / "sky130hd" / "gcd" / variant
                d.mkdir(parents=True)
            # Write unprefixed files in results/
            res = tmpdir / "results" / "sky130hd" / "gcd" / variant
            (res / "route.guide").write_text("fake route guide")
            (res / "output_guide.mod").write_text("fake output guide")
            (res / "updated_clks.sdc").write_text("fake updated clocks")

            # Verify they exist
            self.assertTrue((res / "route.guide").exists())
            self.assertTrue((res / "output_guide.mod").exists())
            self.assertTrue((res / "updated_clks.sdc").exists())

            # Verify downstream_clean_targets includes clean_route +
            # clean_finish for any start stage <= RT
            from orfs.parser import downstream_clean_targets
            for start in ("FP", "PL", "CTS", "RT"):
                targets = downstream_clean_targets(start)
                self.assertIn("clean_route", targets,
                              f"{start}-> must include clean_route (handles route.guide)")
                self.assertIn("clean_finish", targets,
                              f"{start}-> must include clean_finish "
                              f"(handles output_guide.mod, updated_clks.sdc)")

            # Simulate file deletion to confirm they can be removed
            for f in (res / "route.guide", res / "output_guide.mod",
                      res / "updated_clks.sdc"):
                f.unlink()
            self.assertFalse((res / "route.guide").exists())
            self.assertFalse((res / "output_guide.mod").exists())
            self.assertFalse((res / "updated_clks.sdc").exists())
        finally:
            shutil.rmtree(tmpdir)
# =============================================================================
# Stage D fix 2.1+2.3: additional CheckpointResolverIntegrationTest cases
# (moved back from OptimizerIntegrationTest)
# =============================================================================

class CheckpointResolverMoreTests(unittest.TestCase):
    """Additional resolver tests that were accidentally placed in the wrong class."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.flow_dir = self.tmpdir / "flow"
        self.runs_dir = self.tmpdir / "runs"
        self.runs_dir.mkdir(parents=True)
        self.variant = "agenticpd_iter0"
        for cat in ("results", "logs", "reports"):
            d = self.flow_dir / cat / "sky130hd" / "gcd" / self.variant
            d.mkdir(parents=True)
        _results = self.flow_dir / "results" / "sky130hd" / "gcd" / self.variant
        for fname in ("2_floorplan.odb", "2_floorplan.sdc",
                       "3_place.odb", "3_place.sdc",
                       "4_cts.odb", "4_cts.sdc"):
            (_results / fname).write_text(f"dummy content for {fname}")
        self.tm = TrialManager(self.runs_dir)
        self.cm = CheckpointManager(self.flow_dir)
        self.base_params = {
            "FP": {"CORE_UTILIZATION": 38, "CORE_ASPECT_RATIO": 1.0},
            "PL": {"CELL_PAD_IN_SITES_GLOBAL_PLACEMENT": 0},
            "CTS": {},
            "RT": {"FASTROUTE_LAYER_ADJUSTMENT": 0.2, "GRT_CONGESTION_ITERATIONS": 30},
        }

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir)

    def _build_tree_with_checkpoints(self):
        """Same helper as CheckpointResolverIntegrationTest."""
        tree = OptimizationTree()
        variant = self.variant
        fp_trial = TrialRecord(
            trial_id=_new_trial_id(), experiment_id="test",
            status="ok", artifact_dir=str(self.runs_dir / _new_trial_id()),
            params=self.base_params,
        )
        fp_trial.artifact_dir = str(self.runs_dir / fp_trial.trial_id)
        (self.runs_dir / fp_trial.trial_id).mkdir(parents=True)
        self.tm._write_trial(fp_trial)
        fp_ph = CheckpointManager.param_hash(self.base_params)
        fp_cp = self.cm.create(fp_trial, "FP", "sky130hd", "gcd", variant, fp_ph,
                              runs_dir=self.runs_dir)
        fp_node_id = tree.add_path(
            0, ROOT_ID,
            [("FP", variant, self.base_params["FP"], {"fp_ws_ps": -1154.0})],
            source_trial_id=fp_trial.trial_id,
        )[0]
        pl_trial = TrialRecord(
            trial_id=_new_trial_id(), experiment_id="test",
            status="ok", artifact_dir=str(self.runs_dir / _new_trial_id()),
            params=self.base_params,
        )
        pl_trial.artifact_dir = str(self.runs_dir / pl_trial.trial_id)
        (self.runs_dir / pl_trial.trial_id).mkdir(parents=True)
        self.tm._write_trial(pl_trial)
        pl_cp = self.cm.create(pl_trial, "PL", "sky130hd", "gcd", variant, fp_ph,
                              runs_dir=self.runs_dir)
        pl_node_id = tree.add_path(
            0, fp_node_id,
            [("PL", variant, self.base_params["PL"], {"pl_ws_ps": -1200.0})],
            source_trial_id=pl_trial.trial_id,
        )[0]
        cts_trial = TrialRecord(
            trial_id=_new_trial_id(), experiment_id="test",
            status="ok", artifact_dir=str(self.runs_dir / _new_trial_id()),
            params=self.base_params,
        )
        cts_trial.artifact_dir = str(self.runs_dir / cts_trial.trial_id)
        (self.runs_dir / cts_trial.trial_id).mkdir(parents=True)
        self.tm._write_trial(cts_trial)
        cts_cp = self.cm.create(cts_trial, "CTS", "sky130hd", "gcd", variant, fp_ph,
                               runs_dir=self.runs_dir)
        cts_node_id = tree.add_path(
            0, pl_node_id,
            [("CTS", variant, self.base_params["CTS"], {"cts_ws_ps": -1180.0})],
            source_trial_id=cts_trial.trial_id,
        )[0]
        return tree, cts_node_id, fp_cp, pl_cp, cts_cp

    def test_same_trial_multiple_checkpoints_independently_loadable(self):
        """FP, PL, CTS checkpoints from the SAME trial can all be loaded
        independently (no overwrite)."""
        # Create ONE trial with checkpoints at all three stages.
        tid = _new_trial_id()
        trial_dir = self.runs_dir / tid
        trial_dir.mkdir(parents=True)
        trial = TrialRecord(
            trial_id=tid, experiment_id="test-multi",
            status="ok", artifact_dir=str(trial_dir),
            params=self.base_params,
        )
        self.tm._write_trial(trial)

        ph = CheckpointManager.param_hash(self.base_params)
        fp_cp = self.cm.create(trial, "FP", "sky130hd", "gcd", self.variant, ph,
                               runs_dir=self.runs_dir)
        pl_cp = self.cm.create(trial, "PL", "sky130hd", "gcd", self.variant, ph,
                               runs_dir=self.runs_dir)
        cts_cp = self.cm.create(trial, "CTS", "sky130hd", "gcd", self.variant, ph,
                                runs_dir=self.runs_dir)

        # Verify per-stage files exist (not a single overwritten file)
        self.assertTrue((trial_dir / "checkpoints" / "FP.json").is_file(),
                        "FP checkpoint file exists")
        self.assertTrue((trial_dir / "checkpoints" / "PL.json").is_file(),
                        "PL checkpoint file exists")
        self.assertTrue((trial_dir / "checkpoints" / "CTS.json").is_file(),
                        "CTS checkpoint file exists")

        # Load each independently via CheckpointManager
        fp_loaded = self.cm.load(trial, "FP", runs_dir=self.runs_dir)
        self.assertIsNotNone(fp_loaded, "FP checkpoint loadable")
        self.assertEqual(fp_loaded.checkpoint_id, fp_cp.checkpoint_id)

        pl_loaded = self.cm.load(trial, "PL", runs_dir=self.runs_dir)
        self.assertIsNotNone(pl_loaded, "PL checkpoint loadable")
        self.assertEqual(pl_loaded.checkpoint_id, pl_cp.checkpoint_id)

        cts_loaded = self.cm.load(trial, "CTS", runs_dir=self.runs_dir)
        self.assertIsNotNone(cts_loaded, "CTS checkpoint loadable")
        self.assertEqual(cts_loaded.checkpoint_id, cts_cp.checkpoint_id)

    # ==================================================================
    # Stage D fix 2.2: consumed_variant is source checkpoint variant,
    # NOT the Judge-requested node's variant
    # ==================================================================
    def test_consumed_variant_is_source_not_judge_request(self):
        """When resolver falls back to an earlier checkpoint with a different
        variant, consumed_variant is from the consumed checkpoint's source,
        not the Judge-requested node."""
        tree, cts_node_id, fp_cp, pl_cp, cts_cp = self._build_tree_with_checkpoints()

        # Give each tree node a distinct variant to verify correct tracking
        fp_node = tree.find_node("iter0_FP")
        pl_node = tree.find_node("iter0_PL")
        cts_node = tree.find_node("iter0_CTS")
        fp_node.variant = "variant_fp"
        pl_node.variant = "variant_pl"
        cts_node.variant = "variant_cts"

        # CTS_CLUSTER_SIZE change → CTS checkpoint incompatible → PL consumed
        candidate = dict(self.base_params)
        candidate["CTS"]["CTS_CLUSTER_SIZE"] = 50

        res = resolve_checkpoint(
            cts_node_id, "RT",
            candidate_params=candidate,
            inherited_params=self.base_params,
            tree=tree, trial_mgr=self.tm, checkpoint_mgr=self.cm,
        )

        self.assertEqual(res.execution_mode, "checkpoint_fork")
        # The consumed variant must be PL's variant, NOT CTS's
        self.assertEqual(res.consumed_variant, "variant_pl",
                         f"consumed_variant should be PL's variant ('variant_pl'), "
                         f"not Judge-requested CTS variant. Got: {res.consumed_variant}")
        self.assertNotEqual(res.consumed_variant, "variant_cts",
                           "consumed_variant must NOT be Judge-requested node's variant")
        self.assertEqual(res.consumed_node_id, pl_node.node_id)

    # ==================================================================
    # Stage D fix 2.2: full restart consumed fields are all None
    # ==================================================================
    def test_full_restart_consumed_fields_all_none(self):
        """Full restart must not have any consumed checkpoint/variant/node set."""
        tree, cts_node_id, fp_cp, pl_cp, cts_cp = self._build_tree_with_checkpoints()

        candidate = dict(self.base_params)
        candidate["FP"]["CORE_UTILIZATION"] = 50  # invalidates all checkpoints

        res = resolve_checkpoint(
            cts_node_id, "RT",
            candidate_params=candidate,
            inherited_params=self.base_params,
            tree=tree, trial_mgr=self.tm, checkpoint_mgr=self.cm,
        )

        self.assertEqual(res.execution_mode, "full_restart")
        self.assertIsNone(res.consumed_checkpoint)
        self.assertIsNone(res.consumed_node_id)
        self.assertIsNone(res.consumed_variant)
        self.assertIsNotNone(res.fallback_reason)

    # ==================================================================
    # Stage D fix 2.3: baseline checkpoint cache hit participates in resolver
    # ==================================================================
    def test_baseline_checkpoint_cache_hit_resolver_consumes(self):
        """A baseline trial persisted to the current session with checkpoints
        can be found and consumed by the resolver."""
        # Simulate what run_baseline() does: create a trial, persist to
        # session, create FP/PL/CTS checkpoints.
        bl_trial_id = _new_trial_id()
        bl_trial = TrialRecord(
            trial_id=bl_trial_id,
            experiment_id="test-baseline",
            status="ok",
            artifact_dir=f"iter-0-{bl_trial_id}",
            params=self.base_params,
        )
        # Persist to session (same as _persist_baseline_trial)
        self.tm._write_trial(bl_trial)
        self.tm._append_index(bl_trial)

        ph = CheckpointManager.param_hash(self.base_params)
        self.cm.create(bl_trial, "FP", "sky130hd", "gcd", self.variant, ph,
                       runs_dir=self.runs_dir)
        self.cm.create(bl_trial, "PL", "sky130hd", "gcd", self.variant, ph,
                       runs_dir=self.runs_dir)
        self.cm.create(bl_trial, "CTS", "sky130hd", "gcd", self.variant, ph,
                       runs_dir=self.runs_dir)

        # Build a tree with baseline nodes
        tree = OptimizationTree()
        fp_node_id = tree.add_path(
            0, ROOT_ID,
            [("FP", self.variant, self.base_params["FP"], {"fp_ws_ps": -1154.0})],
            source_trial_id=bl_trial_id,
        )[0]
        pl_node_id = tree.add_path(
            0, fp_node_id,
            [("PL", self.variant, self.base_params["PL"], {"pl_ws_ps": -1200.0})],
            source_trial_id=bl_trial_id,
        )[0]
        cts_node_id = tree.add_path(
            0, pl_node_id,
            [("CTS", self.variant, self.base_params["CTS"], {"cts_ws_ps": -1180.0})],
            source_trial_id=bl_trial_id,
        )[0]

        # Now resolve: RT-only param change → CTS checkpoint should be compatible
        candidate = dict(self.base_params)
        candidate["RT"]["GRT_CONGESTION_ITERATIONS"] = 50

        res = resolve_checkpoint(
            cts_node_id, "RT",
            candidate_params=candidate,
            inherited_params=self.base_params,
            tree=tree, trial_mgr=self.tm, checkpoint_mgr=self.cm,
            runs_dir=self.runs_dir,
        )

        self.assertEqual(res.execution_mode, "checkpoint_fork",
                         "Baseline cache-hit: resolver should find and consume CTS checkpoint")
        self.assertEqual(res.effective_start_stage, "RT")
        self.assertIsNotNone(res.consumed_checkpoint)
        self.assertIn("CTS", res.consumed_checkpoint or "")
        # Verify TrialManager.get finds the baseline trial
        loaded_trial = self.tm.get(bl_trial_id)
        self.assertIsNotNone(loaded_trial,
                            "Baseline trial should be findable by TrialManager.get()")
        # Verify checkpoints are loadable from the baseline trial
        for stage in ("FP", "PL", "CTS"):
            cp = self.cm.load(loaded_trial, stage, runs_dir=self.runs_dir)
            self.assertIsNotNone(cp,
                                f"Baseline {stage} checkpoint should be loadable")


if __name__ == "__main__":
    unittest.main()
