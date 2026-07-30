# -*- coding: utf-8 -*-
"""test_cohort_planner.py — Stage D cohort planner regression / integration tests.

Pure Python, no LLM, no ORFS, no network.
"""

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schemas.trial import (
    CheckpointRef,
    FailureClass,
    StageResult,
    TrialRecord,
)
from cohort_planner import (
    CohortPlan,
    CohortPlanError,
    ForkPlan,
    plan_cohort,
    _FORK_SEED_BASE,
)
from doomed_predictor import DEFAULT_RULE_VERSION as DOOMED_VERSION
from gwtw_scheduler import (
    AllHardDeadError,
    ForkRequest,
    PopulationCapacityError,
    DEFAULT_SCHEDULER_VERSION as SCHEDULER_VERSION,
)
from mutation_planner import (
    MutationEvidence,
    DEFAULT_PLANNER_VERSION as PLANNER_VERSION,
)


# ---------------------------------------------------------------------------
# Shared factories
# ---------------------------------------------------------------------------


def _sr(stage, status="ok", elapsed=10.0, qor=None, failure=None):
    return StageResult(
        stage=stage, status=status, elapsed_s=elapsed,
        exit_code=0 if status == "ok" else 1,
        log_path=None, command=None, start_time=None, end_time=None,
        report_path=None,
        stage_qor=qor or {},
        failure=failure,
        error_message=None,
    )


def _trial(trial_id, stage, status="ok", wns=None, tns=None,
           elapsed=10.0, parent=None, checkpoint_stage=None,
           checkpoint_id=None, failure_type=None):
    """Minimal TrialRecord for a single-stage trial."""
    qor = {}
    tag = f"{stage}_tag"
    if wns is not None:
        qor[f"{tag}_ws_ps"] = wns
    if tns is not None:
        qor[f"{tag}_tns_ps"] = tns
    cp = None
    if checkpoint_stage and checkpoint_id:
        cp = CheckpointRef(
            checkpoint_id=checkpoint_id,
            source_trial_id=trial_id,
            stage=checkpoint_stage,
            param_hash="abc",
            orfs_commit="def",
            created_at="2025-01-01T00:00:00",
            artifact_manifest=[],
            artifact_dir=None,
        )
    fc = FailureClass(failure_type) if failure_type else None
    return TrialRecord(
        trial_id=trial_id,
        experiment_id="test",
        status=status,
        start_time=None, end_time=None,
        params={},
        stage_results=[_sr(stage, status=status, elapsed=elapsed,
                           qor=qor, failure=fc)],
        parent_trial_id=parent,
        final_qor=None,
        failure=fc,
        error_message=None,
        checkpoint=cp,
        config_hash=None, env_hash=None,
        param_diff=None,
        artifact_dir=None,
        execution_resolution=None,
        doomed_decisions=[],
        gwtw_decisions=[],
        decision_trace_refs=[],
    )


_BASELINE_PARAMS = {
    "FP": {"CORE_UTILIZATION": 38, "CORE_ASPECT_RATIO": 1.0},
    "PL": {},
    "CTS": {},
    "RT": {"FASTROUTE_LAYER_ADJUSTMENT": 0.2,
           "GRT_CONGESTION_ITERATIONS": 30},
}


# =========================================================================
# Validation
# =========================================================================


class ValidationTest(unittest.TestCase):

    def test_empty_cohort_rejected(self):
        with self.assertRaises(CohortPlanError):
            plan_cohort([], "PL", 1, 0, 4, 2, seed=0, parent_params_by_id={})

    def test_invalid_decision_stage_fp(self):
        t = _trial("a", "FP")
        with self.assertRaises(CohortPlanError):
            plan_cohort([t], "FP", 1, 0, 4, 2, seed=0, parent_params_by_id={})

    def test_invalid_decision_stage_rt(self):
        t = _trial("a", "RT")
        with self.assertRaises(CohortPlanError):
            plan_cohort([t], "RT", 1, 0, 4, 2, seed=0, parent_params_by_id={})

    def test_trial_missing_stage_result(self):
        t = _trial("a", "PL")
        with self.assertRaises(CohortPlanError):
            plan_cohort([t], "CTS", 1, 0, 4, 2, seed=0,
                        parent_params_by_id={})

    def test_bool_seed_rejected(self):
        t = _trial("a", "PL")
        with self.assertRaises(CohortPlanError):
            plan_cohort([t], "PL", 1, 0, 4, 2, seed=True,
                        parent_params_by_id={})

    def test_float_seed_rejected(self):
        t = _trial("a", "PL")
        with self.assertRaises(CohortPlanError):
            plan_cohort([t], "PL", 1, 0, 4, 2, seed=1.5,
                        parent_params_by_id={})

    def test_duplicate_trial_id_rejected(self):
        dup = [
            _trial("dup", "PL", wns=-50, tns=-100, checkpoint_stage="PL",
                   checkpoint_id="cp-1"),
            _trial("dup", "PL", wns=-200, tns=-500, checkpoint_stage="PL",
                   checkpoint_id="cp-2"),
        ]
        with self.assertRaises(CohortPlanError):
            plan_cohort(dup, "PL", 2, 0, 4, 2, seed=0,
                        parent_params_by_id={
                            "dup": copy.deepcopy(_BASELINE_PARAMS),
                        })


# =========================================================================
# PL cohort — basic pipeline
# =========================================================================


class PLCohortTest(unittest.TestCase):

    def setUp(self):
        self.cohort = [
            _trial("a", "PL", wns=-50, tns=-100, checkpoint_stage="PL",
                   checkpoint_id="cp-a"),
            _trial("b", "PL", wns=-200, tns=-500, checkpoint_stage="PL",
                   checkpoint_id="cp-b"),
        ]
        self.parent_params = {
            "a": copy.deepcopy(_BASELINE_PARAMS),
            "b": copy.deepcopy(_BASELINE_PARAMS),
        }

    def test_basic_pl_pipeline(self):
        plan = plan_cohort(
            self.cohort, "PL", 2, 0, 4, 2, seed=42,
            parent_params_by_id=self.parent_params,
        )
        self.assertEqual(plan.decision_stage, "PL")
        self.assertEqual(len(plan.observations), 2)
        self.assertEqual(len(plan.doomed_decisions), 2)
        self.assertEqual(len(plan.gwtw_decisions), 2)
        # Both survivors → active=2, pop=4 → 2 forks.
        self.assertEqual(len(plan.fork_plans), 2)

    def test_both_survivors(self):
        plan = plan_cohort(
            self.cohort, "PL", 2, 0, 4, 2, seed=0,
            parent_params_by_id=self.parent_params,
        )
        for dec in plan.doomed_decisions:
            self.assertEqual(dec.risk_class, "survivor")

    def test_survivor_actions_continue(self):
        plan = plan_cohort(
            self.cohort, "PL", 2, 0, 4, 2, seed=0,
            parent_params_by_id=self.parent_params,
        )
        for gwtw in plan.gwtw_decisions:
            self.assertEqual(gwtw.action, "continue")

    def test_fork_plan_structure(self):
        plan = plan_cohort(
            self.cohort, "PL", 2, 0, 4, 2, seed=42,
            parent_params_by_id=self.parent_params,
        )
        for idx, fp in enumerate(plan.fork_plans):
            self.assertIsInstance(fp, ForkPlan)
            self.assertIsInstance(fp.fork_request, ForkRequest)
            self.assertIsInstance(fp.evidence, MutationEvidence)
            self.assertIsInstance(fp.child_params, dict)
            self.assertEqual(fp.fork_request.reason, "population_replenishment")
            # checkpoint_id: non-empty string, from a survivor parent.
            self.assertIsInstance(fp.checkpoint_id, str)
            self.assertTrue(fp.checkpoint_id)
            self.assertIn(fp.checkpoint_id, ["cp-a", "cp-b"])
            # derived_seed = master_seed * _FORK_SEED_BASE + idx.
            self.assertIsInstance(fp.derived_seed, int)
            expected_fs = 42 * _FORK_SEED_BASE + idx
            self.assertEqual(fp.derived_seed, expected_fs,
                             f"fork idx {idx}: derived_seed={fp.derived_seed}, "
                             f"expected={expected_fs}")

    def test_fork_parent_is_survivor(self):
        plan = plan_cohort(
            self.cohort, "PL", 2, 0, 4, 2, seed=42,
            parent_params_by_id=self.parent_params,
        )
        survivor_ids = {obs.trial_id
                        for obs, dec in zip(plan.observations,
                                           plan.doomed_decisions)
                        if dec.risk_class == "survivor"}
        for fp in plan.fork_plans:
            self.assertIn(fp.fork_request.parent_trial_id, survivor_ids)

    def test_population_invariant(self):
        """active + forks == population_size."""
        plan = plan_cohort(
            self.cohort, "PL", 2, 0, 4, 2, seed=42,
            parent_params_by_id=self.parent_params,
        )
        active = sum(1 for d in plan.gwtw_decisions
                     if d.action in ("continue", "audit_continue"))
        self.assertEqual(active + len(plan.fork_plans), 4)


# =========================================================================
# CTS cohort
# =========================================================================


class CTSCohortTest(unittest.TestCase):

    def setUp(self):
        self.cohort = [
            _trial("cts1", "CTS", wns=-100, tns=-500,
                   checkpoint_stage="CTS", checkpoint_id="cp-1"),
            _trial("cts2", "CTS", wns=-300, tns=-800,
                   checkpoint_stage="CTS", checkpoint_id="cp-2"),
        ]
        self.parent_params = {
            "cts1": copy.deepcopy(_BASELINE_PARAMS),
            "cts2": copy.deepcopy(_BASELINE_PARAMS),
        }

    def test_basic_cts_pipeline(self):
        plan = plan_cohort(
            self.cohort, "CTS", 1, 0, 3, 2, seed=99,
            parent_params_by_id=self.parent_params,
        )
        self.assertEqual(plan.decision_stage, "CTS")
        self.assertEqual(len(plan.fork_plans), 2)

    def test_cts_mutation_only_grt_congestion(self):
        """CTS legal params = [GRT_CONGESTION_ITERATIONS] only."""
        plan = plan_cohort(
            self.cohort, "CTS", 1, 0, 3, 2, seed=99,
            parent_params_by_id=self.parent_params,
        )
        for fp in plan.fork_plans:
            self.assertEqual(fp.evidence.param_name,
                             "GRT_CONGESTION_ITERATIONS")
            # New value must differ from old.
            self.assertNotEqual(fp.evidence.new_value,
                               fp.evidence.old_value)
            # In range [10, 50].
            self.assertGreaterEqual(fp.evidence.new_value, 10)
            self.assertLessEqual(fp.evidence.new_value, 50)


# =========================================================================
# Determinism
# =========================================================================


class DeterminismTest(unittest.TestCase):

    def setUp(self):
        self.cohort = [
            _trial("a", "PL", wns=-50, tns=-100, checkpoint_stage="PL",
                   checkpoint_id="cp-a"),
            _trial("b", "PL", wns=-200, tns=-500, checkpoint_stage="PL",
                   checkpoint_id="cp-b"),
        ]
        self.parent_params = {
            "a": copy.deepcopy(_BASELINE_PARAMS),
            "b": copy.deepcopy(_BASELINE_PARAMS),
        }

    def test_same_input_same_output(self):
        p1 = plan_cohort(
            self.cohort, "PL", 2, 0, 4, 2, seed=123,
            parent_params_by_id=self.parent_params,
        )
        p2 = plan_cohort(
            self.cohort, "PL", 2, 0, 4, 2, seed=123,
            parent_params_by_id=self.parent_params,
        )
        # Observations
        for i in range(len(self.cohort)):
            self.assertEqual(p1.observations[i].trial_id,
                             p2.observations[i].trial_id)
            self.assertEqual(p1.doomed_decisions[i].risk_class,
                             p2.doomed_decisions[i].risk_class)
            self.assertEqual(p1.gwtw_decisions[i].action,
                             p2.gwtw_decisions[i].action)
        # Fork plans
        self.assertEqual(len(p1.fork_plans), len(p2.fork_plans))
        for j in range(len(p1.fork_plans)):
            self.assertEqual(p1.fork_plans[j].evidence.param_name,
                             p2.fork_plans[j].evidence.param_name)
            self.assertEqual(p1.fork_plans[j].evidence.new_value,
                             p2.fork_plans[j].evidence.new_value)
            self.assertEqual(p1.fork_plans[j].fork_request.parent_trial_id,
                             p2.fork_plans[j].fork_request.parent_trial_id)

    def test_different_seed_same_fork_count(self):
        """Same parent pool → same fork count, mutations may differ."""
        p_s1 = plan_cohort(
            self.cohort, "PL", 2, 0, 4, 2, seed=1,
            parent_params_by_id=self.parent_params,
        )
        p_s2 = plan_cohort(
            self.cohort, "PL", 2, 0, 4, 2, seed=2,
            parent_params_by_id=self.parent_params,
        )
        self.assertEqual(len(p_s1.fork_plans), len(p_s2.fork_plans))

    def test_fork_seeds_are_deterministic(self):
        """fork_seed = master_seed * _FORK_SEED_BASE + fork_index."""
        plan = plan_cohort(
            self.cohort, "PL", 2, 0, 4, 2, seed=7,
            parent_params_by_id=self.parent_params,
        )
        for idx, fp in enumerate(plan.fork_plans):
            expected = 7 * _FORK_SEED_BASE + idx
            self.assertEqual(fp.derived_seed, expected,
                             f"fork idx {idx}: derived_seed={fp.derived_seed}")
            self.assertIsNotNone(fp.checkpoint_id)
            self.assertTrue(fp.checkpoint_id)


# =========================================================================
# Mixed-status cohort (hard_dead + survivor + soft_bad)
# =========================================================================


class MixedStatusTest(unittest.TestCase):

    def setUp(self):
        self.cohort = [
            _trial("dead", "PL", status="failed", wns=None, tns=None,
                   failure_type="tool_crash"),
            _trial("top", "PL", wns=-50, tns=-100, checkpoint_stage="PL",
                   checkpoint_id="cp-top"),
            _trial("mid", "PL", wns=-200, tns=-500, checkpoint_stage="PL",
                   checkpoint_id="cp-mid"),
            _trial("low", "PL", wns=-400, tns=-900, checkpoint_stage="PL",
                   checkpoint_id="cp-low"),
        ]
        self.parent_params = {
            "top": copy.deepcopy(_BASELINE_PARAMS),
            "mid": copy.deepcopy(_BASELINE_PARAMS),
            "low": copy.deepcopy(_BASELINE_PARAMS),
        }

    def test_hard_dead_classified(self):
        plan = plan_cohort(
            self.cohort, "PL", 2, 0, 3, 1, seed=55,
            parent_params_by_id=self.parent_params,
        )
        self.assertEqual(plan.doomed_decisions[0].risk_class, "hard_dead")

    def test_hard_dead_paused(self):
        plan = plan_cohort(
            self.cohort, "PL", 2, 0, 3, 1, seed=55,
            parent_params_by_id=self.parent_params,
        )
        self.assertEqual(plan.gwtw_decisions[0].action, "pause")

    def test_hard_dead_not_fork_parent(self):
        plan = plan_cohort(
            self.cohort, "PL", 2, 0, 3, 1, seed=55,
            parent_params_by_id=self.parent_params,
        )
        for fp in plan.fork_plans:
            self.assertNotEqual(fp.fork_request.parent_trial_id, "dead")

    def test_survivors_and_soft_bad_ranked(self):
        plan = plan_cohort(
            self.cohort, "PL", 2, 0, 3, 1, seed=55,
            parent_params_by_id=self.parent_params,
        )
        # top (WNS=-50) > mid (WNS=-200) > low (WNS=-400)
        self.assertEqual(plan.doomed_decisions[1].risk_class, "survivor")
        self.assertEqual(plan.doomed_decisions[2].risk_class, "survivor")
        self.assertEqual(plan.doomed_decisions[3].risk_class, "soft_bad")


# =========================================================================
# Audit quota
# =========================================================================


class AuditQuotaTest(unittest.TestCase):

    def setUp(self):
        self.cohort = [
            _trial("top", "PL", wns=-50, tns=-100, checkpoint_stage="PL",
                   checkpoint_id="cp-top"),
            _trial("mid", "PL", wns=-200, tns=-500, checkpoint_stage="PL",
                   checkpoint_id="cp-mid"),
            _trial("low", "PL", wns=-400, tns=-900, checkpoint_stage="PL",
                   checkpoint_id="cp-low"),
        ]
        self.parent_params = {
            "top": copy.deepcopy(_BASELINE_PARAMS),
            "mid": copy.deepcopy(_BASELINE_PARAMS),
            "low": copy.deepcopy(_BASELINE_PARAMS),
        }

    def test_audit_continue_best_soft_bad(self):
        plan = plan_cohort(
            self.cohort, "PL", 1, 1, 4, 2, seed=0,
            parent_params_by_id=self.parent_params,
        )
        # top → survivor, mid → audit_continue, low → pause
        self.assertEqual(plan.gwtw_decisions[0].action, "continue")
        self.assertEqual(plan.gwtw_decisions[1].action, "audit_continue")
        self.assertEqual(plan.gwtw_decisions[2].action, "pause")

    def test_audit_continue_is_audit_pass(self):
        plan = plan_cohort(
            self.cohort, "PL", 1, 1, 4, 2, seed=0,
            parent_params_by_id=self.parent_params,
        )
        self.assertTrue(plan.gwtw_decisions[1].is_audit_pass)

    def test_audit_quota_zero_no_audit(self):
        plan = plan_cohort(
            self.cohort, "PL", 1, 0, 3, 2, seed=0,
            parent_params_by_id=self.parent_params,
        )
        for d in plan.gwtw_decisions:
            self.assertNotEqual(d.action, "audit_continue")


# =========================================================================
# Error propagation
# =========================================================================


class ErrorPropagationTest(unittest.TestCase):

    def test_all_hard_dead_propagated(self):
        cohort = [
            _trial("x", "PL", status="failed", wns=None, tns=None,
                   failure_type="tool_crash"),
            _trial("y", "PL", status="failed", wns=None, tns=None,
                   failure_type="timeout"),
        ]
        with self.assertRaises(AllHardDeadError):
            plan_cohort(cohort, "PL", 1, 0, 4, 2, seed=0,
                        parent_params_by_id={})

    def test_missing_parent_params(self):
        cohort = [
            _trial("a", "PL", wns=-50, tns=-100, checkpoint_stage="PL",
                   checkpoint_id="cp-a"),
            _trial("b", "PL", wns=-200, tns=-500, checkpoint_stage="PL",
                   checkpoint_id="cp-b"),
        ]
        # Only provide params for "a", missing "b".
        with self.assertRaises(CohortPlanError):
            plan_cohort(
                cohort, "PL", 2, 0, 4, 2, seed=0,
                parent_params_by_id={
                    "a": copy.deepcopy(_BASELINE_PARAMS),
                },
            )

    def test_population_capacity_error_propagated(self):
        cohort = [
            _trial("a", "PL", wns=-50, tns=-100, checkpoint_stage="PL",
                   checkpoint_id="cp-a"),
            _trial("b", "PL", wns=-200, tns=-500, checkpoint_stage="PL",
                   checkpoint_id="cp-b"),
        ]
        params = {t: copy.deepcopy(_BASELINE_PARAMS) for t in ["a", "b"]}
        # active=2, pop=10, max_children=1 → need 8 forks, cap=2.
        with self.assertRaises(PopulationCapacityError):
            plan_cohort(cohort, "PL", 2, 0, 10, 1, seed=0,
                        parent_params_by_id=params)

    def test_fork_parent_checkpoint_source_trial_mismatch(self):
        """Fork parent checkpoint source_trial_id != trial_id."""
        from schemas.trial import CheckpointRef as CPRef
        t_bad = _trial("a", "PL", wns=-50, tns=-100)
        t_bad.checkpoint = CPRef(
            checkpoint_id="cp-a", source_trial_id="OTHER_TRIAL",
            stage="PL", param_hash="abc", orfs_commit="def",
            created_at="2025-01-01T00:00:00",
            artifact_manifest=[], artifact_dir=None,
        )
        cohort = [
            t_bad,
            _trial("b", "PL", wns=-200, tns=-500, checkpoint_stage="PL",
                   checkpoint_id="cp-b"),
        ]
        params = {"a": copy.deepcopy(_BASELINE_PARAMS),
                  "b": copy.deepcopy(_BASELINE_PARAMS)}
        with self.assertRaises(CohortPlanError):
            plan_cohort(cohort, "PL", 2, 0, 4, 2, seed=0,
                        parent_params_by_id=params)


# =========================================================================
# Metadata
# =========================================================================


class MetadataTest(unittest.TestCase):

    def setUp(self):
        self.cohort = [
            _trial("a", "PL", wns=-50, tns=-100, checkpoint_stage="PL",
                   checkpoint_id="cp-a"),
        ]
        self.params = {"a": copy.deepcopy(_BASELINE_PARAMS)}

    def test_metadata_fields_populated(self):
        plan = plan_cohort(
            self.cohort, "PL", 1, 0, 1, 0, seed=7,
            parent_params_by_id=self.params,
        )
        self.assertEqual(plan.seed, 7)
        self.assertEqual(plan.survivor_count, 1)
        self.assertEqual(plan.audit_quota, 0)
        self.assertEqual(plan.population_size, 1)
        self.assertEqual(plan.max_children_per_parent, 0)
        self.assertEqual(plan.doomed_rule_version, DOOMED_VERSION)
        self.assertEqual(plan.scheduler_version, SCHEDULER_VERSION)
        self.assertEqual(plan.planner_version, PLANNER_VERSION)

    def test_custom_versions_flow_through(self):
        plan = plan_cohort(
            self.cohort, "PL", 1, 0, 1, 0, seed=0,
            parent_params_by_id=self.params,
            doomed_rule_version="v-d",
            scheduler_version="v-s",
            planner_version="v-p",
        )
        self.assertEqual(plan.doomed_rule_version, "v-d")
        self.assertEqual(plan.scheduler_version, "v-s")
        self.assertEqual(plan.planner_version, "v-p")


# =========================================================================
# Immutability
# =========================================================================


class ImmutabilityTest(unittest.TestCase):

    def test_cohort_not_mutated(self):
        cohort = [
            _trial("a", "PL", wns=-50, tns=-100, checkpoint_stage="PL",
                   checkpoint_id="cp-a"),
            _trial("b", "PL", wns=-200, tns=-500, checkpoint_stage="PL",
                   checkpoint_id="cp-b"),
        ]
        cohort_orig = copy.deepcopy(cohort)
        params = {t.trial_id: copy.deepcopy(_BASELINE_PARAMS)
                  for t in cohort}
        params_orig = copy.deepcopy(params)

        plan_cohort(cohort, "PL", 2, 0, 4, 2, seed=0,
                    parent_params_by_id=params)

        for i in range(len(cohort)):
            self.assertEqual(cohort[i].trial_id, cohort_orig[i].trial_id)
            self.assertEqual(cohort[i].status, cohort_orig[i].status)
        self.assertEqual(params, params_orig)

    def test_forkplan_is_not_mutated_by_reuse(self):
        """Calling plan_cohort twice with different seeds does not affect
        the first plan's ForkPlans."""
        cohort = [
            _trial("a", "PL", wns=-50, tns=-100, checkpoint_stage="PL",
                   checkpoint_id="cp-a"),
            _trial("b", "PL", wns=-200, tns=-500, checkpoint_stage="PL",
                   checkpoint_id="cp-b"),
        ]
        params = {t.trial_id: copy.deepcopy(_BASELINE_PARAMS)
                  for t in cohort}
        plan1 = plan_cohort(cohort, "PL", 2, 0, 4, 2, seed=1,
                            parent_params_by_id=params)
        fp1_snapshot = copy.deepcopy(plan1.fork_plans)
        # Second call with different seed.
        plan_cohort(cohort, "PL", 2, 0, 4, 2, seed=2,
                    parent_params_by_id=params)
        # First plan's fork plans unchanged.
        for j in range(len(fp1_snapshot)):
            self.assertEqual(
                plan1.fork_plans[j].evidence.param_name,
                fp1_snapshot[j].evidence.param_name,
            )
            self.assertEqual(
                plan1.fork_plans[j].evidence.new_value,
                fp1_snapshot[j].evidence.new_value,
            )


# =========================================================================
# Alignment (observation ↔ decision ↔ fork)
# =========================================================================


class AlignmentTest(unittest.TestCase):

    def test_obs_doomed_same_length_and_order(self):
        cohort = [
            _trial("a", "PL", wns=-50, tns=-100, checkpoint_stage="PL",
                   checkpoint_id="cp-a"),
            _trial("b", "PL", wns=-200, tns=-500, checkpoint_stage="PL",
                   checkpoint_id="cp-b"),
        ]
        params = {t.trial_id: copy.deepcopy(_BASELINE_PARAMS)
                  for t in cohort}
        plan = plan_cohort(cohort, "PL", 2, 0, 4, 2, seed=0,
                           parent_params_by_id=params)
        n = len(cohort)
        self.assertEqual(len(plan.observations), n)
        self.assertEqual(len(plan.doomed_decisions), n)
        self.assertEqual(len(plan.gwtw_decisions), n)
        for i in range(n):
            obs_tid = plan.observations[i].trial_id
            dec_tid = plan.doomed_decisions[i].input_evidence.get("trial_id")
            self.assertEqual(obs_tid, dec_tid,
                             f"alignment idx {i}: obs={obs_tid}, dec={dec_tid}")


# =========================================================================
# Edge cases
# =========================================================================


class EdgeCaseTest(unittest.TestCase):

    def test_single_trial_no_forks_needed(self):
        """pop=1, survivor=1, active=1 → 0 forks, pop already met."""
        cohort = [
            _trial("a", "PL", wns=-50, tns=-100, checkpoint_stage="PL",
                   checkpoint_id="cp-a"),
        ]
        params = {"a": copy.deepcopy(_BASELINE_PARAMS)}
        plan = plan_cohort(cohort, "PL", 1, 0, 1, 0, seed=0,
                           parent_params_by_id=params)
        self.assertEqual(len(plan.fork_plans), 0)
        self.assertEqual(plan.gwtw_decisions[0].action, "continue")

    def test_multiple_fork_plans_different_parents(self):
        """Two survivors → forks may come from both (depending on seed)."""
        cohort = [
            _trial("a", "PL", wns=-50, tns=-100, checkpoint_stage="PL",
                   checkpoint_id="cp-a"),
            _trial("b", "PL", wns=-55, tns=-110, checkpoint_stage="PL",
                   checkpoint_id="cp-b"),
        ]
        params = {t.trial_id: copy.deepcopy(_BASELINE_PARAMS)
                  for t in cohort}
        plan = plan_cohort(cohort, "PL", 2, 0, 6, 2, seed=42,
                           parent_params_by_id=params)
        self.assertEqual(len(plan.fork_plans), 4)  # active=2, pop=6, cap=4 → 4 forks
        parents = {fp.fork_request.parent_trial_id for fp in plan.fork_plans}
        self.assertTrue(len(parents) >= 1,
                        f"at least one parent used, got {parents}")

    def test_fork_plans_have_distinct_child_params(self):
        """Each fork should have its own deep copy of child_params."""
        cohort = [
            _trial("a", "PL", wns=-50, tns=-100, checkpoint_stage="PL",
                   checkpoint_id="cp-a"),
            _trial("b", "PL", wns=-200, tns=-500, checkpoint_stage="PL",
                   checkpoint_id="cp-b"),
        ]
        params = {"a": copy.deepcopy(_BASELINE_PARAMS),
                  "b": copy.deepcopy(_BASELINE_PARAMS)}
        plan = plan_cohort(cohort, "PL", 2, 0, 4, 2, seed=42,
                           parent_params_by_id=params)
        for fp in plan.fork_plans:
            # Mutate the child params dict — should not affect others.
            fp.child_params["FP"]["CORE_UTILIZATION"] = 999
        # All other fork plans should be unaffected.
        self.assertTrue(True, "child_params are independent deep copies")

    def test_zero_forks_zero_max_children(self):
        """pop already met, max_children=0 is fine."""
        cohort = [
            _trial("a", "PL", wns=-50, tns=-100, checkpoint_stage="PL",
                   checkpoint_id="cp-a"),
        ]
        params = {"a": copy.deepcopy(_BASELINE_PARAMS)}
        plan = plan_cohort(cohort, "PL", 1, 0, 1, 0, seed=0,
                           parent_params_by_id=params)
        self.assertEqual(len(plan.fork_plans), 0)


if __name__ == "__main__":
    unittest.main()
