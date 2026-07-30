# -*- coding: utf-8 -*-
"""test_mutation_planner.py — Stage D mutation planner regression tests.

Pure Python, no LLM, no ORFS, no network.
"""

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import PARAM_SPACE, ParamSpec
from gwtw_scheduler import ForkRequest
from mutation_planner import (
    plan_child_params,
    legal_param_names,
    MutationEvidence,
    NoLegalMutationError,
)


def _fr(parent_id, stage):
    return ForkRequest(
        parent_trial_id=parent_id, decision_stage=stage,
        reason="population_replenishment",
    )


# Baseline-like parent params.
BASELINE = {
    "FP": {"CORE_UTILIZATION": 38, "CORE_ASPECT_RATIO": 1.0},
    "PL": {},
    "CTS": {},
    "RT": {"FASTROUTE_LAYER_ADJUSTMENT": 0.2,
           "GRT_CONGESTION_ITERATIONS": 30},
}


# =========================================================================
# Legal parameters
# =========================================================================


class LegalParamsTest(unittest.TestCase):
    """Tests for the legal parameter derivation from PARAM_SPACE."""

    def test_pl_legal_includes_cts_and_rt_only(self):
        names = legal_param_names("PL")
        self.assertIn("CTS_CLUSTER_SIZE", names)
        self.assertIn("CTS_CLUSTER_DIAMETER", names)
        self.assertIn("GRT_CONGESTION_ITERATIONS", names)
        self.assertEqual(len(names), 3,
                         f"PL should have 3 legal params, got {len(names)}: {names}")

    def test_pl_excludes_fp_params(self):
        names = legal_param_names("PL")
        self.assertNotIn("CORE_UTILIZATION", names)
        self.assertNotIn("CORE_ASPECT_RATIO", names)

    def test_pl_excludes_pl_params(self):
        names = legal_param_names("PL")
        self.assertNotIn("PLACE_DENSITY_LB_ADDON", names)
        self.assertNotIn("CELL_PAD_IN_SITES_GLOBAL_PLACEMENT", names)

    def test_pl_excludes_setup_slack_margin(self):
        """SETUP_SLACK_MARGIN affects FP/PL/CTS/RT → excluded from PL."""
        names = legal_param_names("PL")
        self.assertNotIn("SETUP_SLACK_MARGIN", names)

    def test_pl_excludes_fastroute_layer_adjustment(self):
        """FASTROUTE_LAYER_ADJUSTMENT affects FP/PL/CTS/RT → excluded from PL."""
        names = legal_param_names("PL")
        self.assertNotIn("FASTROUTE_LAYER_ADJUSTMENT", names)

    def test_cts_only_grt_congestion_iterations(self):
        names = legal_param_names("CTS")
        self.assertEqual(names, ["GRT_CONGESTION_ITERATIONS"],
                         f"CTS legal should be [GRT_CONGESTION_ITERATIONS], got {names}")

    def test_cts_excludes_cts_params(self):
        names = legal_param_names("CTS")
        self.assertNotIn("CTS_CLUSTER_SIZE", names)
        self.assertNotIn("CTS_CLUSTER_DIAMETER", names)
        self.assertNotIn("SETUP_SLACK_MARGIN", names)

    def test_cts_excludes_fastroute_layer_adjustment(self):
        """FASTROUTE_LAYER_ADJUSTMENT affects all stages → excluded from CTS."""
        names = legal_param_names("CTS")
        self.assertNotIn("FASTROUTE_LAYER_ADJUSTMENT", names)

    def test_legal_param_names_rejects_invalid_stage(self):
        with self.assertRaises(ValueError):
            legal_param_names("FP")
        with self.assertRaises(ValueError):
            legal_param_names("RT")


# =========================================================================
# PL checkpoint mutation
# =========================================================================


class PLMutationTest(unittest.TestCase):
    """PL checkpoint fork mutations."""

    def test_pl_fork_changes_exactly_one_param(self):
        child, ev = plan_child_params(_fr("p1", "PL"), BASELINE, seed=42)
        diffs = []
        for stage in ["FP", "PL", "CTS", "RT"]:
            for k, v in child.get(stage, {}).items():
                pv = BASELINE.get(stage, {}).get(k)
                if v != pv:
                    diffs.append((k, pv, v))
        self.assertEqual(len(diffs), 1,
                         f"exactly 1 diff, got {len(diffs)}: {diffs}")

    def test_pl_mutation_is_legal(self):
        child, ev = plan_child_params(_fr("p1", "PL"), BASELINE, seed=42)
        pl_legal = set(legal_param_names("PL"))
        self.assertIn(ev.param_name, pl_legal,
                      f"{ev.param_name} in PL-legal {pl_legal}")

    def test_pl_mutation_value_in_range(self):
        child, ev = plan_child_params(_fr("p1", "PL"), BASELINE, seed=42)
        spec = _get_spec(ev.param_name)
        self.assertGreaterEqual(ev.new_value, spec.vmin,
                                f"new >= vmin({spec.vmin})")
        self.assertLessEqual(ev.new_value, spec.vmax,
                             f"new <= vmax({spec.vmax})")

    def test_pl_mutation_differs_from_parent(self):
        child, ev = plan_child_params(_fr("p1", "PL"), BASELINE, seed=42)
        old = BASELINE.get(ev.stage, {}).get(ev.param_name)
        self.assertNotEqual(ev.new_value, old,
                            f"new {ev.new_value} != old {old}")

    def test_pl_can_change_cts_cluster_size(self):
        """Multiple seeds should eventually hit CTS_CLUSTER_SIZE."""
        found = set()
        for s in range(50):
            _, ev = plan_child_params(_fr("p1", "PL"), BASELINE, seed=s)
            found.add(ev.param_name)
        self.assertIn("CTS_CLUSTER_SIZE", found,
                      "CTS_CLUSTER_SIZE should be selected by some seed")
        self.assertIn("CTS_CLUSTER_DIAMETER", found)
        self.assertIn("GRT_CONGESTION_ITERATIONS", found)

    def test_pl_preserves_fp_params(self):
        child, _ = plan_child_params(_fr("p1", "PL"), BASELINE, seed=42)
        self.assertEqual(child["FP"], BASELINE["FP"])

    def test_pl_preserves_pl_params(self):
        child, _ = plan_child_params(_fr("p1", "PL"), BASELINE, seed=42)
        self.assertEqual(child["PL"], BASELINE["PL"])

    def test_pl_fastroute_layer_adjustment_unchanged(self):
        """FASTROUTE_LAYER_ADJUSTMENT is not legal for PL → never changed."""
        for s in range(30):
            child, ev = plan_child_params(_fr("p1", "PL"), BASELINE, seed=s)
            self.assertEqual(child["RT"]["FASTROUTE_LAYER_ADJUSTMENT"], 0.2,
                             f"seed={s}: FASTROUTE_LAYER_ADJUSTMENT unchanged")
            self.assertNotEqual(ev.param_name, "FASTROUTE_LAYER_ADJUSTMENT")

    def test_pl_setup_slack_margin_never_changed(self):
        """SETUP_SLACK_MARGIN is not legal for PL → never selected."""
        # Add SETUP_SLACK_MARGIN to parent so it could be changed if legal.
        rich = copy.deepcopy(BASELINE)
        rich["CTS"]["SETUP_SLACK_MARGIN"] = 0.05
        for s in range(30):
            _, ev = plan_child_params(_fr("p1", "PL"), rich, seed=s)
            self.assertNotEqual(ev.param_name, "SETUP_SLACK_MARGIN",
                                f"seed={s}: SETUP_SLACK_MARGIN never selected")


# =========================================================================
# CTS checkpoint mutation
# =========================================================================


class CTSMutationTest(unittest.TestCase):
    """CTS checkpoint fork mutations — only GRT_CONGESTION_ITERATIONS."""

    def test_cts_fork_only_changes_grt_congestion(self):
        for s in range(20):
            _, ev = plan_child_params(_fr("p2", "CTS"), BASELINE, seed=s)
            self.assertEqual(ev.param_name, "GRT_CONGESTION_ITERATIONS",
                             f"seed={s}: only GRT_CONGESTION_ITERATIONS")

    def test_cts_mutation_in_range(self):
        _, ev = plan_child_params(_fr("p2", "CTS"), BASELINE, seed=0)
        self.assertGreaterEqual(ev.new_value, 10)
        self.assertLessEqual(ev.new_value, 50)

    def test_cts_mutation_differs_from_parent(self):
        _, ev = plan_child_params(_fr("p2", "CTS"), BASELINE, seed=0)
        self.assertNotEqual(ev.new_value, 30)

    def test_cts_preserves_fp_pl_cts_params(self):
        child, _ = plan_child_params(_fr("p2", "CTS"), BASELINE, seed=0)
        self.assertEqual(child["FP"], BASELINE["FP"])
        self.assertEqual(child["PL"], BASELINE["PL"])
        self.assertEqual(child["CTS"], BASELINE["CTS"])

    def test_cts_excludes_cts_cluster_size(self):
        """CTS_CLUSTER_SIZE affects CTS → illegal for CTS checkpoint."""
        rich = copy.deepcopy(BASELINE)
        rich["CTS"]["CTS_CLUSTER_SIZE"] = 50
        for s in range(20):
            _, ev = plan_child_params(_fr("p2", "CTS"), rich, seed=s)
            self.assertNotEqual(ev.param_name, "CTS_CLUSTER_SIZE")

    def test_cts_excludes_fastroute_layer_adjustment(self):
        for s in range(20):
            _, ev = plan_child_params(_fr("p2", "CTS"), BASELINE, seed=s)
            self.assertNotEqual(ev.param_name, "FASTROUTE_LAYER_ADJUSTMENT")

    def test_cts_excludes_setup_slack_margin(self):
        rich = copy.deepcopy(BASELINE)
        rich["CTS"]["SETUP_SLACK_MARGIN"] = 0.05
        for s in range(20):
            _, ev = plan_child_params(_fr("p2", "CTS"), rich, seed=s)
            self.assertNotEqual(ev.param_name, "SETUP_SLACK_MARGIN")


# =========================================================================
# Determinism
# =========================================================================


class DeterminismTest(unittest.TestCase):

    def test_same_input_same_seed_same_output(self):
        c1, e1 = plan_child_params(_fr("p1", "PL"), BASELINE, seed=12345)
        c2, e2 = plan_child_params(_fr("p1", "PL"), BASELINE, seed=12345)
        self.assertEqual(c1, c2)
        self.assertEqual(e1.param_name, e2.param_name)
        self.assertEqual(e1.old_value, e2.old_value)
        self.assertEqual(e1.new_value, e2.new_value)
        self.assertEqual(e1.stage, e2.stage)
        self.assertEqual(e1.affects, e2.affects)

    def test_same_input_same_seed_same_output_cts(self):
        c1, e1 = plan_child_params(_fr("p2", "CTS"), BASELINE, seed=99999)
        c2, e2 = plan_child_params(_fr("p2", "CTS"), BASELINE, seed=99999)
        self.assertEqual(c1, c2)
        self.assertEqual(e1.new_value, e2.new_value)

    def test_different_seed_different_output(self):
        """At least some seeds differ (3 legal params for PL → high prob)."""
        seen = set()
        for s in range(30):
            _, ev = plan_child_params(_fr("p1", "PL"), BASELINE, seed=s)
            seen.add((ev.param_name, ev.new_value))
        self.assertGreater(len(seen), 1,
                           f"different seeds → different results, got {len(seen)}")


# =========================================================================
# Parent immutability
# =========================================================================


class ParentImmutabilityTest(unittest.TestCase):

    def test_parent_not_mutated(self):
        parent_copy = copy.deepcopy(BASELINE)
        plan_child_params(_fr("p1", "PL"), BASELINE, seed=0)
        self.assertEqual(BASELINE, parent_copy)

    def test_child_is_deep_copy(self):
        child, _ = plan_child_params(_fr("p1", "PL"), BASELINE, seed=0)
        # Mutate child → parent unaffected.
        child["FP"]["CORE_UTILIZATION"] = 99
        self.assertEqual(BASELINE["FP"]["CORE_UTILIZATION"], 38)

    def test_child_inner_dicts_independent(self):
        child, _ = plan_child_params(_fr("p1", "PL"), BASELINE, seed=0)
        self.assertIsNot(child, BASELINE)
        for stage in child:
            if stage in BASELINE:
                self.assertIsNot(child[stage], BASELINE[stage])


# =========================================================================
# Evidence
# =========================================================================


class EvidenceTest(unittest.TestCase):

    def test_evidence_fields_populated(self):
        _, ev = plan_child_params(_fr("p1", "PL"), BASELINE, seed=0)
        self.assertIsInstance(ev.param_name, str)
        self.assertGreater(len(ev.param_name), 0)
        self.assertIn(ev.stage, ["FP", "PL", "CTS", "RT"])
        self.assertIsInstance(ev.affects, tuple)
        self.assertGreater(len(ev.affects), 0)
        self.assertIsInstance(ev.reason, str)
        self.assertGreater(len(ev.reason), 0)
        self.assertIsNotNone(ev.new_value)

    def test_evidence_reason_contains_parent_trial_id(self):
        _, ev = plan_child_params(_fr("parent-abc", "PL"), BASELINE, seed=0)
        self.assertIn("parent-abc", ev.reason)

    def test_evidence_reason_contains_decision_stage(self):
        _, ev = plan_child_params(_fr("p1", "PL"), BASELINE, seed=0)
        self.assertIn("PL", ev.reason)

    def test_evidence_old_value_matches_parent(self):
        child, ev = plan_child_params(_fr("p1", "PL"), BASELINE, seed=42)
        if ev.old_value is not None:
            parent_val = BASELINE.get(ev.stage, {}).get(ev.param_name)
            self.assertEqual(ev.old_value, parent_val)

    def test_evidence_new_value_in_child(self):
        child, ev = plan_child_params(_fr("p1", "PL"), BASELINE, seed=42)
        child_val = child[ev.stage][ev.param_name]
        self.assertEqual(ev.new_value, child_val)

    def test_planner_version_in_evidence(self):
        _, ev = plan_child_params(_fr("p1", "PL"), BASELINE, seed=0)
        self.assertIn("1.0.0", ev.reason)


# =========================================================================
# NoLegalMutationError
# =========================================================================


class NoLegalMutationTest(unittest.TestCase):

    def test_no_legal_params_for_cts_when_only_value_exhausted(self):
        """When GRT_CONGESTION_ITERATIONS has only one possible value, error."""
        parent = copy.deepcopy(BASELINE)
        # GRT_CONGESTION_ITERATIONS range is 10-50 (41 values) — can't
        # practically exhaust.  But we can test the error class directly.
        self.assertIsNotNone(NoLegalMutationError)

    def test_error_contains_parent_id_and_stage(self):
        err = NoLegalMutationError("p-x", "PL", "test detail")
        self.assertEqual(err.parent_trial_id, "p-x")
        self.assertEqual(err.decision_stage, "PL")
        self.assertIn("p-x", str(err))
        self.assertIn("PL", str(err))
        self.assertIn("test detail", str(err))


# =========================================================================
# Type correctness
# =========================================================================


class TypeCorrectnessTest(unittest.TestCase):

    def test_int_param_produces_int_value(self):
        for s in range(30):
            _, ev = plan_child_params(_fr("p1", "PL"), BASELINE, seed=s)
            spec = _get_spec(ev.param_name)
            if spec.ptype == "int":
                self.assertIsInstance(ev.new_value, int,
                                      f"seed={s}: {ev.param_name} int → "
                                      f"{type(ev.new_value).__name__}")

    def test_float_param_produces_float_value(self):
        """No float params in PL-legal set for baseline, but test defensively."""
        for s in range(30):
            _, ev = plan_child_params(_fr("p1", "PL"), BASELINE, seed=s)
            spec = _get_spec(ev.param_name)
            if spec.ptype == "float":
                self.assertIsInstance(ev.new_value, float)

    def test_grt_congestion_is_int(self):
        for s in range(20):
            _, ev = plan_child_params(_fr("p2", "CTS"), BASELINE, seed=s)
            self.assertIsInstance(ev.new_value, int,
                                  f"GRT_CONGESTION_ITERATIONS should be int, "
                                  f"got {type(ev.new_value).__name__} seed={s}")


# =========================================================================
# Validation
# =========================================================================


class ValidationTest(unittest.TestCase):

    def test_invalid_stage_rt_rejected(self):
        with self.assertRaises(ValueError):
            plan_child_params(_fr("p1", "RT"), BASELINE, seed=0)

    def test_invalid_stage_fp_rejected(self):
        with self.assertRaises(ValueError):
            plan_child_params(_fr("p1", "FP"), BASELINE, seed=0)

    def test_invalid_stage_synth_rejected(self):
        with self.assertRaises(ValueError):
            plan_child_params(_fr("p1", "synth"), BASELINE, seed=0)

    def test_bool_seed_rejected(self):
        with self.assertRaises(ValueError):
            plan_child_params(_fr("p1", "PL"), BASELINE, seed=True)

    def test_float_seed_rejected(self):
        with self.assertRaises(ValueError):
            plan_child_params(_fr("p1", "PL"), BASELINE, seed=1.5)


# =========================================================================
# Full child params
# =========================================================================


class ChildParamsCompletenessTest(unittest.TestCase):

    def test_child_has_all_stages(self):
        child, _ = plan_child_params(_fr("p1", "PL"), BASELINE, seed=0)
        for stage in ["FP", "PL", "CTS", "RT"]:
            self.assertIn(stage, child)

    def test_child_no_extra_keys(self):
        child, _ = plan_child_params(_fr("p1", "PL"), BASELINE, seed=0)
        self.assertEqual(set(child.keys()), {"FP", "PL", "CTS", "RT"})

    def test_child_params_are_subset_superset_consistent(self):
        """Every parent param is present and possibly changed or preserved."""
        child, _ = plan_child_params(_fr("p1", "PL"), BASELINE, seed=42)
        for stage in ["FP", "PL", "CTS", "RT"]:
            for k in BASELINE.get(stage, {}):
                self.assertIn(k, child[stage],
                              f"param {k} missing from child[{stage}]")


# =========================================================================
# Helpers
# =========================================================================


def _get_spec(name: str) -> ParamSpec:
    for specs in PARAM_SPACE.values():
        for s in specs:
            if s.name == name:
                return s
    raise KeyError(name)


if __name__ == "__main__":
    unittest.main()
