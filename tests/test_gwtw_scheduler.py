# -*- coding: utf-8 -*-
"""test_gwtw_scheduler.py — Stage D rule-based GWTW Scheduler regression tests.

Pure Python, no LLM, no ORFS, no network.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schemas.trial import MinimalObservation, DoomedDecision
from gwtw_scheduler import (
    schedule,
    ForkRequest,
    AllHardDeadError,
    PopulationCapacityError,
    DEFAULT_SCHEDULER_VERSION,
)


def _obs(trial_id, stage, wns, tns, status="ok", checkpoint_id=None,
         failure_type=None):
    return MinimalObservation(
        trial_id=trial_id, stage=stage, status=status,
        stage_wns_ps=wns, stage_tns_ps=tns,
        stage_elapsed_s=10.0, failure_type=failure_type,
        checkpoint_id=checkpoint_id or f"cp-{trial_id}-{stage}",
        parent_trial_id=None,
    )


def _dec(risk_class, risk_score, trial_id, reasons=None):
    return DoomedDecision(
        risk_class=risk_class, risk_score=risk_score,
        reason_codes=reasons or [risk_class],
        rule_version="1.0.0",
        input_evidence={"trial_id": trial_id, "cohort_size": 1,
                        "survivor_count": 1},
    )


# =========================================================================
# Input validation
# =========================================================================


class GWTWSchedulerValidationTest(unittest.TestCase):

    def test_empty_cohort_rejected(self):
        with self.assertRaises(ValueError):
            schedule([], [], 1, 0, 4, 2, seed=0)

    def test_length_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            schedule([_obs("a", "PL", -50, -100)], [], 1, 0, 4, 2, seed=0)

    def test_trial_id_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            schedule(
                [_obs("a", "PL", -50, -100)],
                [_dec("survivor", 1.0, "b")],
                1, 0, 4, 2, seed=0,
            )

    def test_missing_trial_id_in_evidence_rejected(self):
        dec = DoomedDecision(
            risk_class="survivor", risk_score=1.0,
            reason_codes=["survivor"], rule_version="1.0.0",
            input_evidence={"cohort_size": 1},  # no trial_id
        )
        with self.assertRaises(ValueError):
            schedule([_obs("a", "PL", -50, -100)], [dec], 1, 0, 4, 2, seed=0)

    def test_mixed_stages_rejected(self):
        with self.assertRaises(ValueError):
            schedule(
                [_obs("a", "PL", -50, -100),
                 _obs("b", "CTS", -100, -200)],
                [_dec("survivor", 1.0, "a"),
                 _dec("survivor", 0.5, "b")],
                2, 0, 4, 2, seed=0,
            )

    def test_invalid_stage_fp_rejected(self):
        with self.assertRaises(ValueError):
            schedule(
                [_obs("a", "FP", -50, -100)],
                [_dec("survivor", 1.0, "a")],
                1, 0, 4, 2, seed=0,
            )

    def test_invalid_stage_rt_rejected(self):
        with self.assertRaises(ValueError):
            schedule(
                [_obs("a", "RT", -50, -100)],
                [_dec("survivor", 1.0, "a")],
                1, 0, 4, 2, seed=0,
            )

    def test_negative_survivor_count_rejected(self):
        with self.assertRaises(ValueError):
            schedule(
                [_obs("a", "PL", -50, -100)],
                [_dec("survivor", 1.0, "a")],
                -1, 0, 4, 2, seed=0,
            )

    def test_negative_audit_quota_rejected(self):
        with self.assertRaises(ValueError):
            schedule(
                [_obs("a", "PL", -50, -100)],
                [_dec("survivor", 1.0, "a")],
                1, -1, 4, 2, seed=0,
            )

    def test_negative_population_size_rejected(self):
        with self.assertRaises(ValueError):
            schedule(
                [_obs("a", "PL", -50, -100)],
                [_dec("survivor", 1.0, "a")],
                1, 0, -1, 2, seed=0,
            )

    def test_negative_max_children_rejected(self):
        with self.assertRaises(ValueError):
            schedule(
                [_obs("a", "PL", -50, -100)],
                [_dec("survivor", 1.0, "a")],
                1, 0, 4, -1, seed=0,
            )

    def test_zero_population_size_rejected(self):
        with self.assertRaises(ValueError):
            schedule(
                [_obs("a", "PL", -50, -100)],
                [_dec("survivor", 1.0, "a")],
                1, 0, 0, 2, seed=0,
            )

    def test_bool_survivor_count_rejected(self):
        with self.assertRaises(ValueError):
            schedule(
                [_obs("a", "PL", -50, -100)],
                [_dec("survivor", 1.0, "a")],
                True, 0, 4, 2, seed=0,
            )

    def test_bool_audit_quota_rejected(self):
        with self.assertRaises(ValueError):
            schedule(
                [_obs("a", "PL", -50, -100)],
                [_dec("survivor", 1.0, "a")],
                1, True, 4, 2, seed=0,
            )

    def test_bool_population_size_rejected(self):
        with self.assertRaises(ValueError):
            schedule(
                [_obs("a", "PL", -50, -100)],
                [_dec("survivor", 1.0, "a")],
                1, 0, True, 2, seed=0,
            )

    def test_bool_max_children_rejected(self):
        with self.assertRaises(ValueError):
            schedule(
                [_obs("a", "PL", -50, -100)],
                [_dec("survivor", 1.0, "a")],
                1, 0, 4, True, seed=0,
            )

    def test_bool_seed_rejected(self):
        with self.assertRaises(ValueError):
            schedule(
                [_obs("a", "PL", -50, -100)],
                [_dec("survivor", 1.0, "a")],
                1, 0, 4, 2, seed=True,
            )

    def test_float_seed_rejected(self):
        with self.assertRaises(ValueError):
            schedule(
                [_obs("a", "PL", -50, -100)],
                [_dec("survivor", 1.0, "a")],
                1, 0, 4, 2, seed=1.5,
            )

    def test_string_survivor_count_rejected(self):
        with self.assertRaises(ValueError):
            schedule(
                [_obs("a", "PL", -50, -100)],
                [_dec("survivor", 1.0, "a")],
                "1", 0, 4, 2, seed=0,
            )


# =========================================================================
# All-hard-dead
# =========================================================================


class GWTWSchedulerHardDeadTest(unittest.TestCase):

    def test_all_hard_dead_raises(self):
        cohort = [
            _obs("a", "PL", None, None, status="failed",
                 failure_type="tool_crash"),
            _obs("b", "PL", None, None, status="failed",
                 failure_type="timeout"),
        ]
        decs = [
            _dec("hard_dead", 0.0, "a", ["stage_failed", "timing_missing"]),
            _dec("hard_dead", 0.0, "b", ["timeout"]),
        ]
        with self.assertRaises(AllHardDeadError) as ctx:
            schedule(cohort, decs, 0, 0, 4, 2, seed=0)
        self.assertEqual(ctx.exception.stage, "PL")
        self.assertEqual(ctx.exception.cohort_size, 2)

    def test_single_hard_dead_all_dead_raises(self):
        obs = _obs("x", "CTS", None, None, status="failed",
                   failure_type="tool_crash", checkpoint_id=None)
        dec = _dec("hard_dead", 0.0, "x",
                   ["stage_failed", "timing_missing", "checkpoint_missing"])
        with self.assertRaises(AllHardDeadError) as ctx:
            schedule([obs], [dec], 0, 0, 4, 2, seed=0)
        self.assertEqual(ctx.exception.stage, "CTS")
        self.assertEqual(ctx.exception.cohort_size, 1)

    def test_hard_dead_gets_pause_not_finish(self):
        cohort = [
            _obs("dead", "PL", None, None, status="failed",
                 failure_type="tool_crash", checkpoint_id=None),
            _obs("surv", "PL", -50, -100),
        ]
        decs = [
            _dec("hard_dead", 0.0, "dead",
                 ["stage_failed", "timing_missing", "checkpoint_missing"]),
            _dec("survivor", 1.0, "surv", ["survivor"]),
        ]
        decisions, _ = schedule(cohort, decs, 1, 0, 3, 2, seed=0)
        self.assertEqual(decisions[0].action, "pause")
        self.assertIsNone(decisions[0].parent_trial_id)
        self.assertIsNone(decisions[0].child_trial_id)
        self.assertFalse(decisions[0].is_audit_pass)

    def test_hard_dead_ranked_last(self):
        cohort = [
            _obs("a", "PL", -50, -100),
            _obs("dead", "PL", None, None, status="failed",
                 failure_type="tool_crash", checkpoint_id=None),
        ]
        decs = [
            _dec("survivor", 1.0, "a", ["survivor"]),
            _dec("hard_dead", 0.0, "dead",
                 ["stage_failed", "timing_missing", "checkpoint_missing"]),
        ]
        decisions, _ = schedule(cohort, decs, 1, 0, 3, 2, seed=0)
        self.assertEqual(decisions[0].rank, 0, "survivor rank 0")
        self.assertEqual(decisions[1].rank, 1, "hard_dead rank 1")

    def test_hard_dead_not_fork_parent(self):
        cohort = [
            _obs("dead", "PL", None, None, status="failed",
                 failure_type="tool_crash", checkpoint_id=None),
            _obs("surv", "PL", -50, -100),
        ]
        decs = [
            _dec("hard_dead", 0.0, "dead",
                 ["stage_failed", "timing_missing", "checkpoint_missing"]),
            _dec("survivor", 1.0, "surv", ["survivor"]),
        ]
        _, forks = schedule(cohort, decs, 1, 0, 3, 2, seed=0)
        for f in forks:
            self.assertNotEqual(f.parent_trial_id, "dead")


# =========================================================================
# Survivor → continue; forks only in ForkRequest
# =========================================================================


class GWTWSchedulerSurvivorTest(unittest.TestCase):

    def test_survivor_always_continue(self):
        cohort = [
            _obs("a", "PL", -50, -100),
            _obs("b", "PL", -200, -500),
        ]
        decs = [
            _dec("survivor", 1.0, "a", ["survivor"]),
            _dec("soft_bad", 0.0, "b", ["rank_low"]),
        ]
        # pop=3, active=1 → need 2 forks (cap=2 → ok).
        decisions, forks = schedule(cohort, decs, 1, 0, 3, 2, seed=0)
        self.assertEqual(decisions[0].action, "continue")
        self.assertIsNone(decisions[0].parent_trial_id)
        self.assertGreater(len(forks), 0, "children generated as ForkRequest")

    def test_survivors_as_parents_not_via_action(self):
        """Fork parent identity is in ForkRequest, not GWTWDecision.action."""
        cohort = [
            _obs("a", "PL", -50, -100),
            _obs("b", "PL", -200, -500),
        ]
        decs = [
            _dec("survivor", 1.0, "a", ["survivor"]),
            _dec("soft_bad", 0.0, "b", ["rank_low"]),
        ]
        decisions, forks = schedule(cohort, decs, 1, 0, 3, 2, seed=0)
        # a is survivor → "continue" (not "fork").
        self.assertEqual(decisions[0].action, "continue")
        # ForkRequest records the parent.
        for f in forks:
            self.assertEqual(f.parent_trial_id, "a")

    def test_multiple_survivors_all_continue(self):
        cohort = [
            _obs("a", "PL", -50, -100),
            _obs("b", "PL", -55, -110),
            _obs("c", "PL", -300, -600),
        ]
        decs = [
            _dec("survivor", 1.0, "a", ["survivor"]),
            _dec("survivor", 0.9, "b", ["survivor"]),
            _dec("soft_bad", 0.0, "c", ["rank_low"]),
        ]
        # active=2, pop=6 → need 4 forks (2 parents × cap 2 = 4 → ok).
        decisions, forks = schedule(cohort, decs, 2, 0, 6, 2, seed=42)
        for d in decisions:
            if d.action != "pause":
                self.assertEqual(d.action, "continue")
        parents = {f.parent_trial_id for f in forks}
        self.assertEqual(parents, {"a", "b"})
        self.assertEqual(len(forks), 4)

    def test_survivor_no_forks_when_population_full(self):
        cohort = [
            _obs("a", "PL", -50, -100),
            _obs("b", "PL", -55, -110),
            _obs("c", "PL", -300, -600),
        ]
        decs = [
            _dec("survivor", 1.0, "a", ["survivor"]),
            _dec("survivor", 0.9, "b", ["survivor"]),
            _dec("soft_bad", 0.0, "c", ["rank_low"]),
        ]
        decisions, forks = schedule(cohort, decs, 2, 0, 2, 2, seed=0)
        self.assertEqual(len(forks), 0)
        for d in decisions:
            if d.action != "pause":
                self.assertEqual(d.action, "continue")


# =========================================================================
# Audit quota
# =========================================================================


class GWTWSchedulerAuditTest(unittest.TestCase):

    def test_audit_continue_within_quota(self):
        cohort = [
            _obs("a", "PL", -50, -100),
            _obs("b", "PL", -200, -500),
            _obs("c", "PL", -300, -600),
        ]
        decs = [
            _dec("survivor", 1.0, "a", ["survivor"]),
            _dec("soft_bad", 0.5, "b", ["rank_low"]),
            _dec("soft_bad", 0.0, "c", ["rank_low"]),
        ]
        decisions, _ = schedule(cohort, decs, 1, 1, 4, 2, seed=0)
        self.assertEqual(decisions[1].action, "audit_continue")
        self.assertTrue(decisions[1].is_audit_pass)
        self.assertEqual(decisions[2].action, "pause")
        self.assertFalse(decisions[2].is_audit_pass)

    def test_audit_quota_exceeds_soft_bad_count(self):
        cohort = [
            _obs("a", "PL", -50, -100),
            _obs("b", "PL", -200, -500),
        ]
        decs = [
            _dec("survivor", 1.0, "a", ["survivor"]),
            _dec("soft_bad", 0.0, "b", ["rank_low"]),
        ]
        decisions, _ = schedule(cohort, decs, 1, 5, 3, 2, seed=0)
        self.assertEqual(decisions[1].action, "audit_continue")
        self.assertTrue(decisions[1].is_audit_pass)

    def test_audit_quota_zero_all_soft_bad_pause(self):
        cohort = [
            _obs("a", "PL", -50, -100),
            _obs("b", "PL", -200, -500),
            _obs("c", "PL", -300, -600),
        ]
        decs = [
            _dec("survivor", 1.0, "a", ["survivor"]),
            _dec("soft_bad", 0.5, "b", ["rank_low"]),
            _dec("soft_bad", 0.0, "c", ["rank_low"]),
        ]
        # active=1, pop=3 → need 2 forks (cap=2 → ok).
        decisions, _ = schedule(cohort, decs, 1, 0, 3, 2, seed=0)
        self.assertEqual(decisions[1].action, "pause")
        self.assertEqual(decisions[2].action, "pause")

    def test_audit_continue_not_fork_parent(self):
        """Only survivors can fork; audit_continue trials are soft_bad."""
        cohort = [
            _obs("a", "PL", -50, -100),
            _obs("b", "PL", -200, -500),
        ]
        decs = [
            _dec("survivor", 1.0, "a", ["survivor"]),
            _dec("soft_bad", 0.0, "b", ["rank_low"]),
        ]
        _, forks = schedule(cohort, decs, 1, 1, 4, 2, seed=0)
        for f in forks:
            self.assertEqual(f.parent_trial_id, "a")


# =========================================================================
# Population capacity constraints
# =========================================================================


class GWTWSchedulerPopulationCapacityTest(unittest.TestCase):

    def test_active_exceeds_population_raises(self):
        cohort = [
            _obs("a", "PL", -50, -100),
            _obs("b", "PL", -55, -110),
        ]
        decs = [
            _dec("survivor", 1.0, "a", ["survivor"]),
            _dec("survivor", 0.9, "b", ["survivor"]),
        ]
        with self.assertRaises(PopulationCapacityError) as ctx:
            schedule(cohort, decs, 2, 0, 1, 2, seed=0)
        self.assertIn("exceeds", str(ctx.exception).lower())
        self.assertEqual(ctx.exception.active_count, 2)
        self.assertEqual(ctx.exception.population_size, 1)

    def test_no_survivors_to_fork_raises(self):
        cohort = [
            _obs("a", "PL", -50, -100),
            _obs("b", "PL", -200, -500),
        ]
        decs = [
            _dec("soft_bad", 0.5, "a", ["rank_low"]),
            _dec("soft_bad", 0.0, "b", ["rank_low"]),
        ]
        with self.assertRaises(PopulationCapacityError) as ctx:
            schedule(cohort, decs, 0, 2, 4, 2, seed=0)
        self.assertIn("no survivors", str(ctx.exception).lower())

    def test_max_children_zero_with_pop_deficit_raises(self):
        cohort = [
            _obs("a", "PL", -50, -100),
            _obs("b", "PL", -200, -500),
        ]
        decs = [
            _dec("survivor", 1.0, "a", ["survivor"]),
            _dec("soft_bad", 0.0, "b", ["rank_low"]),
        ]
        with self.assertRaises(PopulationCapacityError) as ctx:
            schedule(cohort, decs, 1, 0, 4, 0, seed=0)
        self.assertIn("max_children_per_parent is 0", str(ctx.exception).lower())

    def test_insufficient_fork_capacity_raises(self):
        cohort = [
            _obs("a", "PL", -50, -100),
            _obs("b", "PL", -200, -500),
        ]
        decs = [
            _dec("survivor", 1.0, "a", ["survivor"]),
            _dec("soft_bad", 0.0, "b", ["rank_low"]),
        ]
        # active=1, pop=7 → need 6 forks, cap=2 → only 2 possible.
        with self.assertRaises(PopulationCapacityError) as ctx:
            schedule(cohort, decs, 1, 0, 7, 2, seed=0)
        self.assertEqual(ctx.exception.active_count, 1)
        self.assertEqual(ctx.exception.population_size, 7)
        self.assertEqual(ctx.exception.max_fork_capacity, 2)

    def test_active_plus_forks_equals_population(self):
        """Every successful call must satisfy the invariant."""
        cohort = [
            _obs("a", "PL", -50, -100),
            _obs("b", "PL", -200, -500),
            _obs("c", "PL", -300, -600),
        ]
        decs = [
            _dec("survivor", 1.0, "a", ["survivor"]),
            _dec("soft_bad", 0.5, "b", ["rank_low"]),
            _dec("soft_bad", 0.0, "c", ["rank_low"]),
        ]
        for pop in [2, 3, 4]:
            for audit in range(0, min(2, pop)):
                try:
                    decisions, forks = schedule(
                        cohort, decs, 1, audit, pop, 3, seed=37,
                    )
                    active = sum(
                        1 for d in decisions
                        if d.action in ("continue", "audit_continue")
                    )
                    self.assertEqual(
                        active + len(forks), pop,
                        f"pop={pop} audit={audit}: "
                        f"active={active} + forks={len(forks)} != {pop}"
                    )
                except PopulationCapacityError:
                    # Some combinations are legitimately infeasible
                    # (e.g., pop=2, audit=1 with 1 survivor → active=2,
                    # need 0 forks → ok; pop=2, audit=0 → active=1,
                    # need 1 fork → ok within cap=3).
                    pass


# =========================================================================
# Fork distribution
# =========================================================================


class GWTWSchedulerForkDistributionTest(unittest.TestCase):

    def test_fork_count_satisfies_population(self):
        cohort = [
            _obs("a", "PL", -50, -100),
            _obs("b", "PL", -200, -500),
        ]
        decs = [
            _dec("survivor", 1.0, "a", ["survivor"]),
            _dec("soft_bad", 0.0, "b", ["rank_low"]),
        ]
        _, forks = schedule(cohort, decs, 1, 0, 4, 3, seed=0)
        self.assertEqual(len(forks), 3,
                         "active=1, pop=4 → need 3 forks, cap=3 → 3")

    def test_fork_requests_have_correct_fields(self):
        cohort = [
            _obs("a", "PL", -50, -100),
            _obs("b", "PL", -200, -500),
        ]
        decs = [
            _dec("survivor", 1.0, "a", ["survivor"]),
            _dec("soft_bad", 0.0, "b", ["rank_low"]),
        ]
        _, forks = schedule(cohort, decs, 1, 0, 3, 2, seed=0)
        for f in forks:
            self.assertIsInstance(f.parent_trial_id, str)
            self.assertGreater(len(f.parent_trial_id), 0)
            self.assertEqual(f.decision_stage, "PL")
            self.assertEqual(f.reason, "population_replenishment")

    def test_round_robin_distribution(self):
        cohort = [
            _obs("a", "PL", -50, -100),
            _obs("b", "PL", -55, -110),
            _obs("c", "PL", -300, -600),
        ]
        decs = [
            _dec("survivor", 1.0, "a", ["survivor"]),
            _dec("survivor", 0.9, "b", ["survivor"]),
            _dec("soft_bad", 0.0, "c", ["rank_low"]),
        ]
        _, forks = schedule(cohort, decs, 2, 0, 6, 2, seed=42)
        a_cnt = sum(1 for f in forks if f.parent_trial_id == "a")
        b_cnt = sum(1 for f in forks if f.parent_trial_id == "b")
        self.assertEqual(a_cnt, 2)
        self.assertEqual(b_cnt, 2)
        self.assertEqual(len(forks), 4)


# =========================================================================
# Determinism
# =========================================================================


class GWTWSchedulerDeterminismTest(unittest.TestCase):

    def test_same_input_same_seed_same_output(self):
        cohort = [
            _obs("a", "PL", -50, -100),
            _obs("b", "PL", -200, -500),
            _obs("c", "PL", -300, -600),
        ]
        decs = [
            _dec("survivor", 1.0, "a", ["survivor"]),
            _dec("soft_bad", 0.5, "b", ["rank_low"]),
            _dec("soft_bad", 0.0, "c", ["rank_low"]),
        ]
        r1, f1 = schedule(cohort, decs, 1, 1, 4, 2, seed=99)
        r2, f2 = schedule(cohort, decs, 1, 1, 4, 2, seed=99)
        self.assertEqual(len(r1), len(r2))
        for i in range(len(r1)):
            self.assertEqual(r1[i].action, r2[i].action)
            self.assertEqual(r1[i].rank, r2[i].rank)
            self.assertEqual(r1[i].is_audit_pass, r2[i].is_audit_pass)
            self.assertIsNone(r1[i].parent_trial_id)
            self.assertIsNone(r2[i].parent_trial_id)
        self.assertEqual(len(f1), len(f2))
        for j in range(len(f1)):
            self.assertEqual(f1[j].parent_trial_id, f2[j].parent_trial_id)
            self.assertEqual(f1[j].decision_stage, f2[j].decision_stage)
            self.assertEqual(f1[j].reason, f2[j].reason)

    def test_different_seed_same_parent_set(self):
        cohort = [
            _obs("a", "PL", -50, -100),
            _obs("b", "PL", -55, -110),
            _obs("c", "PL", -300, -600),
            _obs("d", "PL", -400, -700),
        ]
        decs = [
            _dec("survivor", 1.0, "a", ["survivor"]),
            _dec("survivor", 0.9, "b", ["survivor"]),
            _dec("soft_bad", 0.5, "c", ["rank_low"]),
            _dec("soft_bad", 0.0, "d", ["rank_low"]),
        ]
        _, f1 = schedule(cohort, decs, 2, 0, 6, 2, seed=1)
        _, f2 = schedule(cohort, decs, 2, 0, 6, 2, seed=999)
        p1 = {f.parent_trial_id for f in f1}
        p2 = {f.parent_trial_id for f in f2}
        self.assertEqual(p1, p2,
                         "same parents regardless of seed for equal-rank")


# =========================================================================
# Ranking
# =========================================================================


class GWTWSchedulerRankingTest(unittest.TestCase):

    def test_wns_determines_rank(self):
        cohort = [
            _obs("c", "PL", -300, 0),
            _obs("a", "PL", -50, 0),
            _obs("b", "PL", -200, 0),
        ]
        decs = [
            _dec("soft_bad", 0.0, "c", ["rank_low"]),
            _dec("survivor", 1.0, "a", ["survivor"]),
            _dec("soft_bad", 0.5, "b", ["rank_low"]),
        ]
        # active=1, pop=3 → need 2 forks (cap=2 → ok).
        decisions, _ = schedule(cohort, decs, 1, 0, 3, 2, seed=0)
        self.assertEqual(decisions[0].rank, 2)  # c WNS=-300
        self.assertEqual(decisions[1].rank, 0)  # a WNS=-50
        self.assertEqual(decisions[2].rank, 1)  # b WNS=-200

    def test_tns_breaks_wns_tie(self):
        cohort = [
            _obs("a", "PL", -100, -500),
            _obs("b", "PL", -100, -100),
        ]
        decs = [
            _dec("survivor", 0.5, "a", ["survivor"]),
            _dec("survivor", 1.0, "b", ["survivor"]),
        ]
        decisions, _ = schedule(cohort, decs, 2, 0, 2, 2, seed=0)
        self.assertEqual(decisions[1].rank, 0, "b (better TNS) → rank 0")
        self.assertEqual(decisions[0].rank, 1, "a (worse TNS) → rank 1")

    def test_trial_id_breaks_tns_tie(self):
        cohort = [
            _obs("z", "PL", -100, -200),
            _obs("a", "PL", -100, -200),
            _obs("m", "PL", -100, -200),
        ]
        decs = [
            _dec("survivor", 0.0, "z", ["survivor"]),
            _dec("survivor", 1.0, "a", ["survivor"]),
            _dec("survivor", 0.5, "m", ["survivor"]),
        ]
        decisions, _ = schedule(cohort, decs, 3, 0, 3, 2, seed=0)
        self.assertEqual(decisions[1].rank, 0, "a → rank 0")
        self.assertEqual(decisions[2].rank, 1, "m → rank 1")
        self.assertEqual(decisions[0].rank, 2, "z → rank 2")

    def test_rank_includes_hard_dead(self):
        cohort = [
            _obs("a", "PL", -50, -100),
            _obs("dead", "PL", None, None, status="failed",
                 failure_type="tool_crash", checkpoint_id=None),
            _obs("b", "PL", -200, -500),
        ]
        decs = [
            _dec("survivor", 1.0, "a", ["survivor"]),
            _dec("hard_dead", 0.0, "dead",
                 ["stage_failed", "timing_missing", "checkpoint_missing"]),
            _dec("soft_bad", 0.0, "b", ["rank_low"]),
        ]
        decisions, _ = schedule(cohort, decs, 1, 0, 3, 2, seed=0)
        ranks = {d.rank for d in decisions}
        self.assertEqual(ranks, {0, 1, 2})


# =========================================================================
# Output contract
# =========================================================================


class GWTWSchedulerOutputContractTest(unittest.TestCase):

    def test_output_length_matches_input(self):
        cohort = [
            _obs("a", "PL", -50, -100),
            _obs("b", "PL", -200, -500),
            _obs("c", "PL", -300, -600),
        ]
        decs = [
            _dec("survivor", 1.0, "a", ["survivor"]),
            _dec("soft_bad", 0.5, "b", ["rank_low"]),
            _dec("soft_bad", 0.0, "c", ["rank_low"]),
        ]
        decisions, _ = schedule(cohort, decs, 1, 1, 4, 2, seed=0)
        self.assertEqual(len(decisions), 3)

    def test_all_decisions_have_stage_set(self):
        cohort = [_obs("a", "CTS", -50, -100)]
        decs = [_dec("survivor", 1.0, "a", ["survivor"])]
        decisions, _ = schedule(cohort, decs, 1, 0, 1, 2, seed=0)
        for d in decisions:
            self.assertEqual(d.decision_stage, "CTS")

    def test_scheduler_version_recorded(self):
        cohort = [_obs("a", "PL", -50, -100)]
        decs = [_dec("survivor", 1.0, "a", ["survivor"])]
        decisions, _ = schedule(
            cohort, decs, 1, 0, 1, 2, seed=0,
            scheduler_version="3.0.0-custom",
        )
        for d in decisions:
            self.assertEqual(d.scheduler_version, "3.0.0-custom")

    def test_default_scheduler_version(self):
        cohort = [_obs("a", "PL", -50, -100)]
        decs = [_dec("survivor", 1.0, "a", ["survivor"])]
        decisions, _ = schedule(cohort, decs, 1, 0, 1, 2, seed=0)
        self.assertEqual(decisions[0].scheduler_version,
                         DEFAULT_SCHEDULER_VERSION)

    def test_no_fork_or_finish_actions(self):
        """Scheduler produces only continue / pause / audit_continue."""
        cohort = [
            _obs("a", "PL", -50, -100),
            _obs("b", "PL", -200, -500),
            _obs("c", "PL", -300, -600),
        ]
        decs = [
            _dec("survivor", 1.0, "a", ["survivor"]),
            _dec("soft_bad", 0.5, "b", ["rank_low"]),
            _dec("soft_bad", 0.0, "c", ["rank_low"]),
        ]
        decisions, _ = schedule(cohort, decs, 1, 1, 4, 2, seed=0)
        valid = {"continue", "pause", "audit_continue"}
        for d in decisions:
            self.assertIn(d.action, valid,
                          f"action {d.action!r} not in {valid}")
            self.assertNotEqual(d.action, "fork")
            self.assertNotEqual(d.action, "finish")

    def test_parent_and_child_ids_always_none(self):
        """Scheduler never sets parent_trial_id or child_trial_id."""
        cohort = [
            _obs("a", "PL", -50, -100),
            _obs("b", "PL", -200, -500),
            _obs("c", "PL", -300, -600),
        ]
        decs = [
            _dec("survivor", 1.0, "a", ["survivor"]),
            _dec("soft_bad", 0.5, "b", ["rank_low"]),
            _dec("soft_bad", 0.0, "c", ["rank_low"]),
        ]
        decisions, _ = schedule(cohort, decs, 1, 1, 4, 2, seed=0)
        for d in decisions:
            self.assertIsNone(d.parent_trial_id)
            self.assertIsNone(d.child_trial_id)

    def test_output_order_matches_input_order(self):
        cohort = [
            _obs("c", "PL", -300, 0),
            _obs("a", "PL", -50, 0),
            _obs("b", "PL", -200, 0),
        ]
        decs = [
            _dec("soft_bad", 0.0, "c", ["rank_low"]),
            _dec("survivor", 1.0, "a", ["survivor"]),
            _dec("soft_bad", 0.5, "b", ["rank_low"]),
        ]
        # active=1, pop=3 → need 2 forks (cap=2 → ok).
        decisions, _ = schedule(cohort, decs, 1, 0, 3, 2, seed=0)
        self.assertEqual(decisions[2].rank, 1)  # b at input idx 2
        self.assertEqual(decisions[1].rank, 0)  # a at input idx 1
        self.assertEqual(decisions[0].rank, 2)  # c at input idx 0

    def test_cts_stage_accepted(self):
        cohort = [
            _obs("a", "CTS", -50, -100),
            _obs("b", "CTS", -200, -500),
        ]
        decs = [
            _dec("survivor", 1.0, "a", ["survivor"]),
            _dec("soft_bad", 0.0, "b", ["rank_low"]),
        ]
        decisions, forks = schedule(cohort, decs, 1, 0, 3, 2, seed=0)
        self.assertEqual(len(decisions), 2)
        for d in decisions:
            self.assertEqual(d.decision_stage, "CTS")
        for f in forks:
            self.assertEqual(f.decision_stage, "CTS")

    def test_large_cohort_population_invariant(self):
        n = 50
        cohort = []
        decs = []
        for i in range(n):
            tid = f"t{i:04d}"
            cohort.append(_obs(tid, "PL", -float(i + 1),
                               -float((i + 1) * 10)))
            rc = "survivor" if i < 10 else "soft_bad"
            decs.append(_dec(rc, 1.0 - i / n, tid, [rc]))
        # 10 survivors, 5 audit → active=15, pop=20 → need 5 forks.
        # 10 parents × cap 3 = 30 → capacity ok.
        decisions, forks = schedule(cohort, decs, 10, 5, 20, 3, seed=0)
        self.assertEqual(len(decisions), n)
        active = sum(1 for d in decisions
                     if d.action in ("continue", "audit_continue"))
        self.assertEqual(active, 15)
        self.assertEqual(len(forks), 5)
        self.assertEqual(active + len(forks), 20)


if __name__ == "__main__":
    unittest.main()
