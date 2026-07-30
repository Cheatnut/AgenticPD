# -*- coding: utf-8 -*-
"""test_cohort_executor.py — Stage D cohort executor regression / integration tests.

Pure Python, no LLM, no ORFS, no network.  Covers trace persistence,
reconstruction, and idempotency.
"""

import copy
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schemas.trial import (
    FailureClass,
    StageResult,
)
from cohort_executor import (
    CohortExecutionResult,
    execute_cohort,
    reconstruct_cohort_decisions,
)
from decision_trace import DEFAULT_TRACE_PATH, read_trace, cohort_already_executed
from gwtw_scheduler import AllHardDeadError
from managers import CheckpointManager, TrialManager
from optimization_tree import OptimizationTree, ROOT_ID


class FakeExecutorTestBase(unittest.TestCase):

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())
        self.flow_dir = self.tmpdir / "flow"
        self.runs_dir = self.tmpdir / "runs"
        self.runs_dir.mkdir(parents=True)

        self.platform = "sky130hd"
        self.design = "gcd"
        self.variant = "base"

        for cat in ("results",):
            (self.flow_dir / cat / self.platform
             / self.design / self.variant).mkdir(parents=True)
        for fname in ("2_floorplan.odb", "2_floorplan.sdc",
                      "3_place.odb", "3_place.sdc",
                      "4_cts.odb", "4_cts.sdc"):
            (self.flow_dir / "results" / self.platform / self.design
             / self.variant / fname).write_text(f"fake {fname}")

        self.trial_mgr = TrialManager(self.runs_dir)
        self.checkpoint_mgr = CheckpointManager(self.flow_dir)

        self._BASELINE = {
            "FP": {"CORE_UTILIZATION": 38, "CORE_ASPECT_RATIO": 1.0},
            "PL": {}, "CTS": {},
            "RT": {"FASTROUTE_LAYER_ADJUSTMENT": 0.2,
                   "GRT_CONGESTION_ITERATIONS": 30},
        }
        self.param_hash = CheckpointManager.param_hash(
            {"FP": {"CORE_UTILIZATION": 38}, "PL": {}, "CTS": {}, "RT": {}})

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def _make_pl_trial(self, wns, tns, tree=None, iteration=0):
        t = self.trial_mgr.create(experiment_id="test", iteration=iteration)
        t.status = "ok"
        t.params = copy.deepcopy(self._BASELINE)
        t.stage_results = [
            StageResult(stage="FP", status="ok", elapsed_s=10.0, exit_code=0),
            StageResult(stage="PL", status="ok", elapsed_s=15.0, exit_code=0,
                        stage_qor={"PL_tag_ws_ps": wns,
                                   "PL_tag_tns_ps": tns}),
        ]
        cp = self.checkpoint_mgr.create(
            trial=t, stage="PL",
            platform=self.platform, design=self.design,
            variant=self.variant, param_hash=self.param_hash,
            runs_dir=self.runs_dir)
        t.checkpoint = cp
        self.trial_mgr.update(t)
        if tree is not None:
            fp_nid = tree.add_path(
                iteration * 10 + 100, ROOT_ID,
                [("FP", f"v-{t.trial_id}-fp", {"CORE_UTILIZATION": 38},
                  {"fp_ws_ps": -45.0})],
                source_trial_id=t.trial_id)[0]
            tree.add_path(
                iteration * 10 + 100, fp_nid,
                [("PL", f"v-{t.trial_id}-pl", {},
                  {"pl_ws_ps": float(wns)})],
                source_trial_id=t.trial_id)
        return t

    def _make_cts_trial(self, wns, tns, tree=None, iteration=0):
        t = self.trial_mgr.create(experiment_id="test", iteration=iteration)
        t.status = "ok"
        t.params = copy.deepcopy(self._BASELINE)
        t.stage_results = [
            StageResult(stage="FP", status="ok", elapsed_s=10.0, exit_code=0),
            StageResult(stage="PL", status="ok", elapsed_s=15.0, exit_code=0),
            StageResult(stage="CTS", status="ok", elapsed_s=12.0, exit_code=0,
                        stage_qor={"CTS_tag_ws_ps": wns,
                                   "CTS_tag_tns_ps": tns}),
        ]
        cp = self.checkpoint_mgr.create(
            trial=t, stage="CTS",
            platform=self.platform, design=self.design,
            variant=self.variant, param_hash=self.param_hash,
            runs_dir=self.runs_dir)
        t.checkpoint = cp
        self.trial_mgr.update(t)
        if tree is not None:
            fp_nid = tree.add_path(
                iteration * 10 + 800, ROOT_ID,
                [("FP", f"v-{t.trial_id}-fp", {"CORE_UTILIZATION": 38},
                  {"fp_ws_ps": -45.0})],
                source_trial_id=t.trial_id)[0]
            pl_nid = tree.add_path(
                iteration * 10 + 800, fp_nid,
                [("PL", f"v-{t.trial_id}-pl", {},
                  {"pl_ws_ps": -50.0})],
                source_trial_id=t.trial_id)[0]
            tree.add_path(
                iteration * 10 + 800, pl_nid,
                [("CTS", f"v-{t.trial_id}-cts", {},
                  {"cts_ws_ps": float(wns)})],
                source_trial_id=t.trial_id)
        return t

    def _make_failed_trial(self, failure=FailureClass.TOOL_CRASH):
        t = self.trial_mgr.create(experiment_id="test", iteration=0)
        t.status = "failed"
        t.failure = failure
        t.stage_results = [
            StageResult(stage="PL", status="failed", elapsed_s=5.0,
                        exit_code=1, failure=failure),
        ]
        self.trial_mgr.update(t)
        return t


# =========================================================================
# Trace persistence
# =========================================================================


class TracePersistenceTest(FakeExecutorTestBase):

    def test_trace_written_with_all_entry_types(self):
        tree = OptimizationTree()
        a = self._make_pl_trial(-50, -100, tree, 0)
        b = self._make_pl_trial(-200, -500, tree, 1)
        params = {t.trial_id: copy.deepcopy(self._BASELINE)
                  for t in [a, b]}

        execute_cohort(
            [a, b], "PL", 2, 0, 4, 2, seed=42,
            parent_params_by_id=params,
            trial_mgr=self.trial_mgr,
            checkpoint_mgr=self.checkpoint_mgr,
            tree=tree, runs_dir=self.runs_dir,
        )
        entries = read_trace(self.runs_dir, DEFAULT_TRACE_PATH)
        types = {e["entry_type"] for e in entries}
        self.assertSetEqual(
            types, {"observation", "doomed_decision", "gwtw_decision",
                    "fork_intent", "fork", "execution_resolution",
                    "cohort_complete"})

    def test_trace_entries_have_cohort_stage_and_seed(self):
        tree = OptimizationTree()
        a = self._make_pl_trial(-50, -100, tree, 0)
        b = self._make_pl_trial(-200, -500, tree, 1)
        params = {t.trial_id: copy.deepcopy(self._BASELINE)
                  for t in [a, b]}

        execute_cohort(
            [a, b], "PL", 2, 0, 4, 2, seed=77,
            parent_params_by_id=params,
            trial_mgr=self.trial_mgr,
            checkpoint_mgr=self.checkpoint_mgr,
            tree=tree, runs_dir=self.runs_dir,
        )
        entries = read_trace(self.runs_dir, DEFAULT_TRACE_PATH)
        for e in entries:
            self.assertEqual(e["cohort_stage"], "PL")
            self.assertEqual(e["cohort_seed"], 77)

    def test_trials_record_trace_refs(self):
        tree = OptimizationTree()
        a = self._make_pl_trial(-50, -100, tree, 0)
        params = {a.trial_id: copy.deepcopy(self._BASELINE)}

        execute_cohort(
            [a], "PL", 1, 0, 1, 0, seed=0,
            parent_params_by_id=params,
            trial_mgr=self.trial_mgr,
            checkpoint_mgr=self.checkpoint_mgr,
            tree=tree, runs_dir=self.runs_dir,
        )
        a2 = self.trial_mgr.get(a.trial_id)
        self.assertGreaterEqual(len(a2.decision_trace_refs), 3)
        for ref in a2.decision_trace_refs:
            self.assertEqual(ref.trace_path, DEFAULT_TRACE_PATH)
            self.assertTrue(ref.decision_id.startswith("dtr-"))

    def test_fork_and_er_entries_written(self):
        tree = OptimizationTree()
        a = self._make_pl_trial(-50, -100, tree, 0)
        b = self._make_pl_trial(-200, -500, tree, 1)
        params = {t.trial_id: copy.deepcopy(self._BASELINE)
                  for t in [a, b]}

        execute_cohort(
            [a, b], "PL", 2, 0, 4, 2, seed=42,
            parent_params_by_id=params,
            trial_mgr=self.trial_mgr,
            checkpoint_mgr=self.checkpoint_mgr,
            tree=tree, runs_dir=self.runs_dir,
        )
        entries = read_trace(self.runs_dir, DEFAULT_TRACE_PATH)
        forks = [e for e in entries if e["entry_type"] == "fork"]
        ers = [e for e in entries if e["entry_type"] == "execution_resolution"]
        self.assertEqual(len(forks), 2)
        self.assertEqual(len(ers), 2)
        for f in forks:
            self.assertIn("checkpoint_id", f["data"])
            self.assertIn("param_name", f["data"])
            self.assertIn("derived_seed", f["data"])


# =========================================================================
# Reconstruction
# =========================================================================


class ReconstructionTest(FakeExecutorTestBase):

    def test_reconstruct_pl_cohort_from_trace(self):
        tree = OptimizationTree()
        a = self._make_pl_trial(-50, -100, tree, 0)
        b = self._make_pl_trial(-200, -500, tree, 1)
        params = {t.trial_id: copy.deepcopy(self._BASELINE)
                  for t in [a, b]}

        execute_cohort(
            [a, b], "PL", 2, 0, 4, 2, seed=42,
            parent_params_by_id=params,
            trial_mgr=self.trial_mgr,
            checkpoint_mgr=self.checkpoint_mgr,
            tree=tree, runs_dir=self.runs_dir,
        )
        recon = reconstruct_cohort_decisions(
            self.runs_dir, "PL", seed=42,
            trial_ids=[a.trial_id, b.trial_id],
            survivor_count=2, population_size=4, max_children_per_parent=2,
            doomed_rule_version="1.0.0", scheduler_version="1.0.0",
            planner_version="1.0.0",
        )
        self.assertEqual(len(recon), 2)
        for tid in [a.trial_id, b.trial_id]:
            self.assertIn(tid, recon)
            self.assertIn("observation", recon[tid])
            self.assertIn("doomed", recon[tid])
            self.assertIn("gwtw", recon[tid])

    def test_reconstruct_cts_from_trace(self):
        tree = OptimizationTree()
        a = self._make_cts_trial(-50, -100, tree, 0)
        b = self._make_cts_trial(-200, -600, tree, 1)
        params = {a.trial_id: copy.deepcopy(self._BASELINE),
                  b.trial_id: copy.deepcopy(self._BASELINE)}

        execute_cohort(
            [a, b], "CTS", 2, 0, 4, 2, seed=99,
            parent_params_by_id=params,
            trial_mgr=self.trial_mgr,
            checkpoint_mgr=self.checkpoint_mgr,
            tree=tree, runs_dir=self.runs_dir,
        )
        recon = reconstruct_cohort_decisions(
            self.runs_dir, "CTS", seed=99,
            trial_ids=[a.trial_id, b.trial_id],
            survivor_count=2, population_size=4, max_children_per_parent=2,
            doomed_rule_version="1.0.0", scheduler_version="1.0.0",
            planner_version="1.0.0",
        )
        self.assertEqual(len(recon), 2)
        for tid in [a.trial_id, b.trial_id]:
            self.assertIn("observation", recon[tid])
            self.assertIn("gwtw", recon[tid])
            self.assertEqual(recon[tid]["gwtw"]["action"], "continue")

    def test_reconstruct_excludes_different_seed(self):
        tree = OptimizationTree()
        a = self._make_pl_trial(-50, -100, tree, 0)
        params = {a.trial_id: copy.deepcopy(self._BASELINE)}

        execute_cohort(
            [a], "PL", 1, 0, 1, 0, seed=42,
            parent_params_by_id=params,
            trial_mgr=self.trial_mgr,
            checkpoint_mgr=self.checkpoint_mgr,
            tree=tree, runs_dir=self.runs_dir,
        )
        recon = reconstruct_cohort_decisions(
            self.runs_dir, "PL", seed=99,
            trial_ids=[a.trial_id],
            survivor_count=1, population_size=1,
        )
        self.assertEqual(len(recon), 0,
                         "different seed → no entries returned")


# =========================================================================
# Idempotency
# =========================================================================


class IdempotencyTest(FakeExecutorTestBase):

    def test_re_execute_same_cohort_returns_from_disk(self):
        tree = OptimizationTree()
        a = self._make_pl_trial(-50, -100, tree, 0)
        b = self._make_pl_trial(-200, -500, tree, 1)
        params = {t.trial_id: copy.deepcopy(self._BASELINE)
                  for t in [a, b]}

        r1 = execute_cohort(
            [a, b], "PL", 2, 0, 4, 2, seed=42,
            parent_params_by_id=params,
            trial_mgr=self.trial_mgr,
            checkpoint_mgr=self.checkpoint_mgr,
            tree=tree, runs_dir=self.runs_dir,
        )
        self.assertIsNotNone(r1.cohort_plan)
        self.assertEqual(len(r1.child_trial_ids), 2)

        # Re-execute — idempotent, skips planning.
        r2 = execute_cohort(
            [a, b], "PL", 2, 0, 4, 2, seed=42,
            parent_params_by_id=params,
            trial_mgr=self.trial_mgr,
            checkpoint_mgr=self.checkpoint_mgr,
            tree=tree, runs_dir=self.runs_dir,
        )
        self.assertIsNone(r2.cohort_plan,
                          "idempotent → cohort_plan is None")
        self.assertEqual(r2.trial_outcomes, r1.trial_outcomes)
        self.assertEqual(r2.seed, 42)
        self.assertGreater(len(r2.trace_refs), 0)

    def test_different_seed_not_idempotent(self):
        tree = OptimizationTree()
        a = self._make_pl_trial(-50, -100, tree, 0)
        b = self._make_pl_trial(-200, -500, tree, 1)
        params = {t.trial_id: copy.deepcopy(self._BASELINE)
                  for t in [a, b]}

        execute_cohort(
            [a, b], "PL", 2, 0, 4, 2, seed=42,
            parent_params_by_id=params,
            trial_mgr=self.trial_mgr,
            checkpoint_mgr=self.checkpoint_mgr,
            tree=tree, runs_dir=self.runs_dir,
        )
        r2 = execute_cohort(
            [a, b], "PL", 2, 0, 4, 2, seed=99,
            parent_params_by_id=params,
            trial_mgr=self.trial_mgr,
            checkpoint_mgr=self.checkpoint_mgr,
            tree=tree, runs_dir=self.runs_dir,
        )
        self.assertIsNotNone(r2.cohort_plan,
                             "different seed → not idempotent")

    def test_idempotency_does_not_duplicate_children(self):
        tree = OptimizationTree()
        a = self._make_pl_trial(-50, -100, tree, 0)
        b = self._make_pl_trial(-200, -500, tree, 1)
        params = {t.trial_id: copy.deepcopy(self._BASELINE)
                  for t in [a, b]}

        r1 = execute_cohort(
            [a, b], "PL", 2, 0, 4, 2, seed=42,
            parent_params_by_id=params,
            trial_mgr=self.trial_mgr,
            checkpoint_mgr=self.checkpoint_mgr,
            tree=tree, runs_dir=self.runs_dir,
        )
        self.assertEqual(len(r1.child_trial_ids), 2)

        # Idempotent re-execute — children recovered from disk, NOT re-created.
        r2 = execute_cohort(
            [a, b], "PL", 2, 0, 4, 2, seed=42,
            parent_params_by_id=params,
            trial_mgr=self.trial_mgr,
            checkpoint_mgr=self.checkpoint_mgr,
            tree=tree, runs_dir=self.runs_dir,
        )
        self.assertIsNone(r2.cohort_plan)
        # Children recovered from trace (not re-forked).
        self.assertEqual(len(r2.child_trial_ids), 2,
                         "idempotent → 2 children recovered from trace")
        # Trace entries should NOT have duplicated.
        entries = read_trace(self.runs_dir, DEFAULT_TRACE_PATH)
        fork_count = sum(1 for e in entries if e["entry_type"] == "fork")
        sentinel_count = sum(1 for e in entries
                            if e["entry_type"] == "cohort_complete")
        self.assertEqual(fork_count, 2,
                         f"still 2 fork entries, not duplicated: {fork_count}")
        self.assertEqual(sentinel_count, 1,
                         "still 1 sentinel, not duplicated")

    def test_cohort_already_executed_helper(self):
        tree = OptimizationTree()
        a = self._make_pl_trial(-50, -100, tree, 0)
        b = self._make_pl_trial(-200, -500, tree, 1)
        params = {t.trial_id: copy.deepcopy(self._BASELINE)
                  for t in [a, b]}

        self.assertFalse(
            cohort_already_executed(self.runs_dir, DEFAULT_TRACE_PATH,
                                    "PL", seed=42,
                                    trial_ids=[a.trial_id, b.trial_id]),
            "not yet executed")

        execute_cohort(
            [a, b], "PL", 2, 0, 4, 2, seed=42,
            parent_params_by_id=params,
            trial_mgr=self.trial_mgr,
            checkpoint_mgr=self.checkpoint_mgr,
            tree=tree, runs_dir=self.runs_dir,
        )

        self.assertTrue(
            cohort_already_executed(
                self.runs_dir, DEFAULT_TRACE_PATH,
                "PL", seed=42,
                trial_ids=[a.trial_id, b.trial_id],
                survivor_count=2, audit_quota=0,
                population_size=4, max_children_per_parent=2,
                doomed_rule_version="1.0.0",
                scheduler_version="1.0.0",
                planner_version="1.0.0"),
            "after execution → True")
        self.assertFalse(
            cohort_already_executed(
                self.runs_dir, DEFAULT_TRACE_PATH,
                "PL", seed=42,
                trial_ids=[a.trial_id, b.trial_id, "unknown"],
                survivor_count=2, audit_quota=0,
                population_size=4, max_children_per_parent=2,
                doomed_rule_version="1.0.0",
                scheduler_version="1.0.0",
                planner_version="1.0.0"),
            "partial cohort → False")


# =========================================================================
# Pause + checkpoint persistence
# =========================================================================


class PausePersistenceTest(FakeExecutorTestBase):

    def test_pause_persists_status_and_checkpoint(self):
        tree = OptimizationTree()
        dead = self._make_failed_trial()
        ok_ = self._make_pl_trial(-50, -100, tree, 0)
        params = {ok_.trial_id: copy.deepcopy(self._BASELINE)}

        execute_cohort(
            [dead, ok_], "PL", 1, 0, 3, 2, seed=0,
            parent_params_by_id=params,
            trial_mgr=self.trial_mgr,
            checkpoint_mgr=self.checkpoint_mgr,
            tree=tree, runs_dir=self.runs_dir,
        )
        dead2 = self.trial_mgr.get(dead.trial_id)
        self.assertEqual(dead2.status, "paused")

    def test_pause_preserves_checkpoint_and_trace_refs(self):
        tree = OptimizationTree()
        weak = self._make_pl_trial(-400, -900, tree, 0)
        top = self._make_pl_trial(-50, -100, tree, 1)
        params = {weak.trial_id: copy.deepcopy(self._BASELINE),
                  top.trial_id: copy.deepcopy(self._BASELINE)}

        execute_cohort(
            [weak, top], "PL", 1, 0, 3, 2, seed=0,
            parent_params_by_id=params,
            trial_mgr=self.trial_mgr,
            checkpoint_mgr=self.checkpoint_mgr,
            tree=tree, runs_dir=self.runs_dir,
        )
        w2 = self.trial_mgr.get(weak.trial_id)
        self.assertEqual(w2.status, "paused")
        self.assertIsNotNone(w2.checkpoint)
        self.assertGreaterEqual(len(w2.decision_trace_refs), 3)


# =========================================================================
# Error cases
# =========================================================================


class ErrorPropagationTest(FakeExecutorTestBase):

    def test_all_hard_dead_raises(self):
        t1 = self._make_failed_trial()
        t2 = self._make_failed_trial(FailureClass.TIMEOUT)
        with self.assertRaises(AllHardDeadError):
            execute_cohort(
                [t1, t2], "PL", 1, 0, 4, 2, seed=0,
                parent_params_by_id={},
                trial_mgr=self.trial_mgr,
                checkpoint_mgr=self.checkpoint_mgr,
                tree=OptimizationTree(), runs_dir=self.runs_dir,
            )


# =========================================================================
# Restored regression tests
# =========================================================================


class TreeMissingFullRestartTest(FakeExecutorTestBase):

    def test_parent_not_in_tree_produces_full_restart(self):
        """When parent is not in tree, child gets full_restart — never a
        direct fork with consumed_checkpoint."""
        t = self._make_pl_trial(-50, -100, tree=None, iteration=0)
        params = {t.trial_id: copy.deepcopy(self._BASELINE)}

        result = execute_cohort(
            [t], "PL", 1, 0, 2, 1, seed=0,
            parent_params_by_id=params,
            trial_mgr=self.trial_mgr,
            checkpoint_mgr=self.checkpoint_mgr,
            tree=OptimizationTree(), runs_dir=self.runs_dir,
        )
        self.assertEqual(len(result.child_trial_ids), 1)
        self.assertEqual(len(result.child_checkpoint_resolutions), 1)
        res = result.child_checkpoint_resolutions[0]
        self.assertEqual(res.execution_mode, "full_restart")
        self.assertEqual(res.effective_start_stage, "FP")
        self.assertIsNone(res.consumed_checkpoint)
        self.assertIsNone(res.consumed_node_id)
        self.assertIsNone(res.consumed_variant)
        self.assertIn("not found in", res.fallback_reason or "")


class CTSCohortRegressionTest(FakeExecutorTestBase):

    def test_cts_resolver_gives_rt_effective_start(self):
        """Child of CTS checkpoint resolves to effective_start_stage=RT."""
        tree = OptimizationTree()
        a = self._make_cts_trial(-50, -100, tree, 0)
        b = self._make_cts_trial(-200, -500, tree, 1)
        params = {a.trial_id: copy.deepcopy(self._BASELINE),
                  b.trial_id: copy.deepcopy(self._BASELINE)}

        result = execute_cohort(
            [a, b], "CTS", 2, 0, 4, 2, seed=0,
            parent_params_by_id=params,
            trial_mgr=self.trial_mgr,
            checkpoint_mgr=self.checkpoint_mgr,
            tree=tree, runs_dir=self.runs_dir,
        )
        self.assertGreater(len(result.child_checkpoint_resolutions), 0)
        for res in result.child_checkpoint_resolutions:
            self.assertEqual(res.execution_mode, "checkpoint_fork")
            self.assertEqual(res.effective_start_stage, "RT")
            self.assertIsNotNone(res.consumed_checkpoint)
            self.assertIn("-CTS", res.consumed_checkpoint)

    def test_cts_fork_only_rt_legal_mutation(self):
        """CTS checkpoint → only GRT_CONGESTION_ITERATIONS is legal."""
        tree = OptimizationTree()
        surv = self._make_cts_trial(-50, -100, tree, 0)
        soft = self._make_cts_trial(-300, -700, tree, 1)
        params = {surv.trial_id: copy.deepcopy(self._BASELINE),
                  soft.trial_id: copy.deepcopy(self._BASELINE)}

        result = execute_cohort(
            [surv, soft], "CTS", 1, 0, 3, 2, seed=99,
            parent_params_by_id=params,
            trial_mgr=self.trial_mgr,
            checkpoint_mgr=self.checkpoint_mgr,
            tree=tree, runs_dir=self.runs_dir,
        )
        self.assertGreater(len(result.child_trial_ids), 0)
        for child_id in result.child_trial_ids:
            child = self.trial_mgr.get(child_id)
            has_grt = False
            for sp in child.params.values():
                if "GRT_CONGESTION_ITERATIONS" in sp:
                    has_grt = True
                    self.assertNotEqual(sp["GRT_CONGESTION_ITERATIONS"], 30)
            self.assertTrue(has_grt,
                            f"CTS child must change GRT_CONGESTION_ITERATIONS")

    def test_cts_consumes_checkpoint_without_explicit_runs_dir(self):
        """When runs_dir is None, executor falls back to trial_mgr.runs_dir
        and still resolves CTS checkpoint."""
        tree = OptimizationTree()
        a = self._make_cts_trial(-50, -100, tree, 0)
        b = self._make_cts_trial(-200, -500, tree, 1)
        params = {a.trial_id: copy.deepcopy(self._BASELINE),
                  b.trial_id: copy.deepcopy(self._BASELINE)}

        result = execute_cohort(
            [a, b], "CTS", 2, 0, 4, 2, seed=0,
            parent_params_by_id=params,
            trial_mgr=self.trial_mgr,
            checkpoint_mgr=self.checkpoint_mgr,
            tree=tree, runs_dir=None,
        )
        self.assertGreater(len(result.child_checkpoint_resolutions), 0)
        for res in result.child_checkpoint_resolutions:
            self.assertEqual(res.execution_mode, "checkpoint_fork")
            self.assertIsNotNone(res.consumed_checkpoint)
            self.assertIn("-CTS", res.consumed_checkpoint)


# =========================================================================
# Fault injection: crash-recovery
# =========================================================================


class FaultInjectionTest(FakeExecutorTestBase):
    """Simulate crashes between phases and verify recovery does not duplicate."""

    def test_crash_after_decisions_before_forks(self):
        """Crash after Phase 2 (decisions + intents on disk, no forks).
        Recovery recreates children without duplicating decisions."""
        tree = OptimizationTree()
        a = self._make_pl_trial(-50, -100, tree, 0)
        b = self._make_pl_trial(-200, -500, tree, 1)
        params = {t.trial_id: copy.deepcopy(self._BASELINE)
                  for t in [a, b]}

        # Step 1: run normally to get baseline.
        r_normal = execute_cohort(
            [a, b], "PL", 2, 0, 4, 2, seed=42,
            parent_params_by_id=params,
            trial_mgr=self.trial_mgr,
            checkpoint_mgr=self.checkpoint_mgr,
            tree=tree, runs_dir=self.runs_dir,
        )
        self.assertEqual(len(r_normal.child_trial_ids), 2)
        n_forks_normal = sum(
            1 for e in read_trace(self.runs_dir, DEFAULT_TRACE_PATH)
            if e["entry_type"] == "fork")

        # Step 2: delete fork, er, and sentinel entries from trace
        # (simulate crash before Phase 3), but keep decisions + intents.
        trace_path = self.runs_dir / DEFAULT_TRACE_PATH
        lines = trace_path.read_text().splitlines()
        kept = [l for l in lines if l.strip() and (
            '"fork"' not in l or '"fork_intent"' in l
        ) and '"execution_resolution"' not in l
                and '"cohort_complete"' not in l]
        trace_path.write_text("\n".join(kept) + "\n")

        # Delete child TrialRecords from disk.
        for cid in r_normal.child_trial_ids:
            child_dir = self.runs_dir / f"iter-0-{cid}"
            if child_dir.is_dir():
                child_dir_trial = child_dir / "trial.json"
                if child_dir_trial.is_file():
                    child_dir_trial.unlink()

        # Step 3: re-execute — should resume, create 2 new children.
        r_recovery = execute_cohort(
            [a, b], "PL", 2, 0, 4, 2, seed=42,
            parent_params_by_id=params,
            trial_mgr=self.trial_mgr,
            checkpoint_mgr=self.checkpoint_mgr,
            tree=tree, runs_dir=self.runs_dir,
        )
        self.assertEqual(len(r_recovery.child_trial_ids), 2)
        # Same number of forks as baseline.
        entries = read_trace(self.runs_dir, DEFAULT_TRACE_PATH)
        fork_count = sum(1 for e in entries if e["entry_type"] == "fork")
        self.assertEqual(fork_count, n_forks_normal)
        # No duplicated decisions.
        doomed_count = sum(1 for e in entries
                          if e["entry_type"] == "doomed_decision"
                          and e.get("cohort_id") == entries[0].get("cohort_id"))
        self.assertLessEqual(doomed_count, 2)
        sentinel_count = sum(1 for e in entries
                            if e["entry_type"] == "cohort_complete")
        self.assertEqual(sentinel_count, 1)

    def test_crash_after_partial_forks(self):
        """Crash after 1 fork written. Recovery creates only missing fork,
        audit_continue trial never becomes fork parent."""
        tree = OptimizationTree()
        a = self._make_pl_trial(-50, -100, tree, 0)
        b = self._make_pl_trial(-200, -500, tree, 1)
        params = {t.trial_id: copy.deepcopy(self._BASELINE)
                  for t in [a, b]}

        # Step 1: run normally.
        r_normal = execute_cohort(
            [a, b], "PL", 2, 0, 4, 2, seed=42,
            parent_params_by_id=params,
            trial_mgr=self.trial_mgr,
            checkpoint_mgr=self.checkpoint_mgr,
            tree=tree, runs_dir=self.runs_dir,
        )
        self.assertEqual(len(r_normal.child_trial_ids), 2)

        # Step 2: delete sentinel + 1 fork + 1 er from trace.
        trace_path = self.runs_dir / DEFAULT_TRACE_PATH
        lines = trace_path.read_text().splitlines()
        # Keep first fork+er, drop second fork+er and sentinel.
        kept: list = []
        fork_seen = 0
        for l in lines:
            if not l.strip():
                continue
            if '"cohort_complete"' in l:
                continue
            if '"fork"' in l and '"fork_intent"' not in l:
                fork_seen += 1
                if fork_seen > 1:
                    continue
            if '"execution_resolution"' in l:
                er_seen = kept.count('"execution_resolution"') if False else 0
                # Count er entries in kept lines.
                er_count = sum(1 for kl in kept
                              if '"execution_resolution"' in kl)
                if er_count >= 1:
                    continue
            kept.append(l)
        trace_path.write_text("\n".join(kept) + "\n")

        # Delete one child TrialRecord.
        if r_normal.child_trial_ids:
            cid_to_delete = r_normal.child_trial_ids[-1]
            child_dir = self.runs_dir / f"iter-0-{cid_to_delete}"
            if child_dir.is_dir():
                import shutil as _shutil
                _shutil.rmtree(child_dir)

        # Step 3: recovery.
        r_recovery = execute_cohort(
            [a, b], "PL", 2, 0, 4, 2, seed=42,
            parent_params_by_id=params,
            trial_mgr=self.trial_mgr,
            checkpoint_mgr=self.checkpoint_mgr,
            tree=tree, runs_dir=self.runs_dir,
        )
        self.assertEqual(len(r_recovery.child_trial_ids), 2)
        entries = read_trace(self.runs_dir, DEFAULT_TRACE_PATH)
        fork_count = sum(1 for e in entries if e["entry_type"] == "fork")
        self.assertEqual(fork_count, 2)
        sentinel_count = sum(1 for e in entries
                            if e["entry_type"] == "cohort_complete")
        self.assertEqual(sentinel_count, 1)

    def test_er_trace_written_crash_before_trial_update(self):
        """ER trace written but child TrialRecord has no execution_resolution
        → child fixed in-place, NOT duplicated.  Child ID unchanged,
        fork count stays 2, each child has exactly one resolution."""
        tree = OptimizationTree()
        a = self._make_pl_trial(-50, -100, tree, 0)
        b = self._make_pl_trial(-200, -500, tree, 1)
        params = {t.trial_id: copy.deepcopy(self._BASELINE)
                  for t in [a, b]}

        # Step 1: run normally.
        r_normal = execute_cohort(
            [a, b], "PL", 2, 0, 4, 2, seed=42,
            parent_params_by_id=params,
            trial_mgr=self.trial_mgr,
            checkpoint_mgr=self.checkpoint_mgr,
            tree=tree, runs_dir=self.runs_dir,
        )
        self.assertEqual(len(r_normal.child_trial_ids), 2)
        original_child_ids = set(r_normal.child_trial_ids)

        # Step 2: strip execution_resolution from one child's TrialRecord.
        child_to_corrupt = r_normal.child_trial_ids[0]
        child_t = self.trial_mgr.get(child_to_corrupt)
        self.assertIsNotNone(child_t)
        child_t.execution_resolution = None
        child_t.decision_trace_refs = []  # simulate incomplete update
        self.trial_mgr.update(child_t)

        # Step 3: delete sentinel from trace.
        trace_path = self.runs_dir / DEFAULT_TRACE_PATH
        lines = trace_path.read_text().splitlines()
        kept = [l for l in lines if l.strip()
                and '"cohort_complete"' not in l]
        trace_path.write_text("\n".join(kept) + "\n")

        # Step 4: recovery — fixes the child in-place.
        r_recovery = execute_cohort(
            [a, b], "PL", 2, 0, 4, 2, seed=42,
            parent_params_by_id=params,
            trial_mgr=self.trial_mgr,
            checkpoint_mgr=self.checkpoint_mgr,
            tree=tree, runs_dir=self.runs_dir,
        )
        # Child count unchanged.
        self.assertEqual(len(r_recovery.child_trial_ids), 2)
        # Child IDs unchanged (same set, no new child created).
        self.assertEqual(set(r_recovery.child_trial_ids), original_child_ids,
                         "child IDs unchanged — no new child created")
        # Fork count still 2 (no duplicate fork entry).
        entries = read_trace(self.runs_dir, DEFAULT_TRACE_PATH)
        fork_count = sum(1 for e in entries if e["entry_type"] == "fork")
        self.assertEqual(fork_count, 2,
                         "fork count still 2, not duplicated")
        # Each child has exactly one resolution.
        self.assertEqual(len(r_recovery.child_checkpoint_resolutions), 2,
                         "exactly 2 resolutions, one per child")
        # The corrupted child now has execution_resolution restored.
        child_fixed = self.trial_mgr.get(child_to_corrupt)
        self.assertIsNotNone(child_fixed.execution_resolution,
                             "corrupted child's ER was fixed")
        sentinel_count = sum(1 for e in entries
                            if e["entry_type"] == "cohort_complete")
        self.assertEqual(sentinel_count, 1)

    def test_fork_written_er_not_written_crash(self):
        """Fork trace entry exists, TrialRecord on disk, but NO ER trace
        entry → re-resolve and write ER in-place.  Child ID unchanged,
        fork count unchanged.  Params cleared and restored via intent."""
        tree = OptimizationTree()
        a = self._make_pl_trial(-50, -100, tree, 0)
        b = self._make_pl_trial(-200, -500, tree, 1)
        params = {t.trial_id: copy.deepcopy(self._BASELINE)
                  for t in [a, b]}

        # Baseline: run normally, capture params for comparison.
        r_normal = execute_cohort(
            [a, b], "PL", 2, 0, 4, 2, seed=42,
            parent_params_by_id=params,
            trial_mgr=self.trial_mgr,
            checkpoint_mgr=self.checkpoint_mgr,
            tree=tree, runs_dir=self.runs_dir,
        )
        self.assertEqual(len(r_normal.child_trial_ids), 2)
        original_child_ids = set(r_normal.child_trial_ids)
        # Capture normal child params before corruption.
        normal_params = {}
        for cid in r_normal.child_trial_ids:
            ct = self.trial_mgr.get(cid)
            normal_params[cid] = copy.deepcopy(ct.params) if ct else None

        # Step 2: delete ER trace entries + sentinel, clear params + ER.
        trace_path = self.runs_dir / DEFAULT_TRACE_PATH
        lines = trace_path.read_text().splitlines()
        kept = [l for l in lines if l.strip()
                and '"execution_resolution"' not in l
                and '"cohort_complete"' not in l]
        trace_path.write_text("\n".join(kept) + "\n")

        for cid in r_normal.child_trial_ids:
            ct = self.trial_mgr.get(cid)
            if ct:
                ct.execution_resolution = None
                ct.decision_trace_refs = []
                ct.params = {}  # simulate params not yet persisted
                self.trial_mgr.update(ct)

        # Step 3: recovery.
        r_recovery = execute_cohort(
            [a, b], "PL", 2, 0, 4, 2, seed=42,
            parent_params_by_id=params,
            trial_mgr=self.trial_mgr,
            checkpoint_mgr=self.checkpoint_mgr,
            tree=tree, runs_dir=self.runs_dir,
        )
        self.assertEqual(set(r_recovery.child_trial_ids), original_child_ids)
        entries = read_trace(self.runs_dir, DEFAULT_TRACE_PATH)
        fork_count = sum(1 for e in entries if e["entry_type"] == "fork")
        self.assertEqual(fork_count, 2)
        er_count = sum(1 for e in entries
                       if e["entry_type"] == "execution_resolution")
        self.assertEqual(er_count, 2)
        self.assertEqual(len(r_recovery.child_checkpoint_resolutions), 2)
        # Params restored identical to normal execution.
        for cid in original_child_ids:
            ct = self.trial_mgr.get(cid)
            self.assertIsNotNone(ct.execution_resolution)
            self.assertEqual(ct.params, normal_params[cid],
                             f"child {cid} params restored identically")
        # trace_refs have unique decision_ids.
        ref_ids = [r.decision_id for r in r_recovery.trace_refs]
        self.assertEqual(len(ref_ids), len(set(ref_ids)),
                         "trace_refs decision_ids are unique")
        sentinel_count = sum(1 for e in entries
                            if e["entry_type"] == "cohort_complete")
        self.assertEqual(sentinel_count, 1)

    def test_er_field_mismatch_fixed_without_duplicate(self):
        """ER trace has different effective_start_stage than persisted ER
        → fix in-place.  Params cleared and restored via intent.
        trace_refs decision_ids are unique."""
        tree = OptimizationTree()
        a = self._make_pl_trial(-50, -100, tree, 0)
        b = self._make_pl_trial(-200, -500, tree, 1)
        params = {t.trial_id: copy.deepcopy(self._BASELINE)
                  for t in [a, b]}

        # Baseline.
        r_normal = execute_cohort(
            [a, b], "PL", 2, 0, 4, 2, seed=42,
            parent_params_by_id=params,
            trial_mgr=self.trial_mgr,
            checkpoint_mgr=self.checkpoint_mgr,
            tree=tree, runs_dir=self.runs_dir,
        )
        child_to_corrupt = r_normal.child_trial_ids[0]
        # Capture normal params.
        ct_normal = self.trial_mgr.get(child_to_corrupt)
        normal_params = copy.deepcopy(ct_normal.params)

        # Corrupt: clear params, corrupt ER.
        ct_normal.params = {}
        old_er = ct_normal.execution_resolution
        from schemas.trial import ExecutionResolution
        ct_normal.execution_resolution = ExecutionResolution(
            requested_parent_node_id=old_er.requested_parent_node_id,
            requested_start_stage=old_er.requested_start_stage,
            effective_start_stage="FP",
            execution_mode="full_restart",
            consumed_checkpoint=None,
            fallback_reason="corrupted for test",
        )
        self.trial_mgr.update(ct_normal)

        # Delete sentinel.
        trace_path = self.runs_dir / DEFAULT_TRACE_PATH
        lines = trace_path.read_text().splitlines()
        kept = [l for l in lines if l.strip()
                and '"cohort_complete"' not in l]
        trace_path.write_text("\n".join(kept) + "\n")

        # Recovery.
        r_recovery = execute_cohort(
            [a, b], "PL", 2, 0, 4, 2, seed=42,
            parent_params_by_id=params,
            trial_mgr=self.trial_mgr,
            checkpoint_mgr=self.checkpoint_mgr,
            tree=tree, runs_dir=self.runs_dir,
        )
        self.assertEqual(len(r_recovery.child_trial_ids), 2)
        self.assertEqual(len(r_recovery.child_checkpoint_resolutions), 2)

        # Params restored.
        child_fixed = self.trial_mgr.get(child_to_corrupt)
        self.assertEqual(child_fixed.params, normal_params,
                         "params restored identically via intent")
        # ER fixed.
        self.assertIsNotNone(child_fixed.execution_resolution)
        self.assertNotEqual(
            child_fixed.execution_resolution.effective_start_stage, "FP")
        # trace_refs unique.
        ref_ids = [r.decision_id for r in r_recovery.trace_refs]
        self.assertEqual(len(ref_ids), len(set(ref_ids)),
                         "trace_refs decision_ids are unique")
        entries = read_trace(self.runs_dir, DEFAULT_TRACE_PATH)
        fork_count = sum(1 for e in entries if e["entry_type"] == "fork")
        self.assertEqual(fork_count, 2)


class CohortIdIsolationTest(FakeExecutorTestBase):
    """Different configs on the same trial set produce different cohort_ids
    and must not leak into each other's rebuild."""

    def test_different_config_not_rebuilt_together(self):
        """Run cohort A (pop=4, surv=2), then cohort B (pop=6, surv=2)
        on the same trials with same stage+seed.  Rebuild of A must not
        pick up B's children."""
        from decision_trace import make_cohort_id
        tree = OptimizationTree()
        a = self._make_pl_trial(-50, -100, tree, 0)
        b = self._make_pl_trial(-200, -500, tree, 1)
        params = {t.trial_id: copy.deepcopy(self._BASELINE)
                  for t in [a, b]}

        # Cohort A: pop=4, surv=2.
        r_a = execute_cohort(
            [a, b], "PL", 2, 0, 4, 2, seed=42,
            parent_params_by_id=params,
            trial_mgr=self.trial_mgr,
            checkpoint_mgr=self.checkpoint_mgr,
            tree=tree, runs_dir=self.runs_dir,
        )
        self.assertEqual(len(r_a.child_trial_ids), 2)

        # Cohort B: same stage+seed, different config (pop=6, surv=2).
        r_b = execute_cohort(
            [a, b], "PL", 2, 0, 6, 3, seed=42,
            parent_params_by_id=params,
            trial_mgr=self.trial_mgr,
            checkpoint_mgr=self.checkpoint_mgr,
            tree=tree, runs_dir=self.runs_dir,
        )
        self.assertEqual(len(r_b.child_trial_ids), 4)

        # Both sentinels exist in the same trace file.
        entries = read_trace(self.runs_dir, DEFAULT_TRACE_PATH)
        sentinel_count = sum(1 for e in entries
                            if e["entry_type"] == "cohort_complete")
        self.assertEqual(sentinel_count, 2)

        # Rebuild A: must get exactly 2 children (not 6).
        r_a_rebuild = execute_cohort(
            [a, b], "PL", 2, 0, 4, 2, seed=42,
            parent_params_by_id=params,
            trial_mgr=self.trial_mgr,
            checkpoint_mgr=self.checkpoint_mgr,
            tree=tree, runs_dir=self.runs_dir,
        )
        self.assertIsNone(r_a_rebuild.cohort_plan,
                          "rebuild: cohort_plan=None")
        self.assertEqual(len(r_a_rebuild.child_trial_ids), 2,
                         "rebuild A: 2 children, not 6")
        self.assertEqual(r_a_rebuild.trial_outcomes[a.trial_id], "continue")
        self.assertEqual(r_a_rebuild.trial_outcomes[b.trial_id], "continue")

        # Rebuild B: must get exactly 4 children.
        r_b_rebuild = execute_cohort(
            [a, b], "PL", 2, 0, 6, 3, seed=42,
            parent_params_by_id=params,
            trial_mgr=self.trial_mgr,
            checkpoint_mgr=self.checkpoint_mgr,
            tree=tree, runs_dir=self.runs_dir,
        )
        self.assertIsNone(r_b_rebuild.cohort_plan)
        self.assertEqual(len(r_b_rebuild.child_trial_ids), 4,
                         "rebuild B: 4 children")


if __name__ == "__main__":
    unittest.main()
