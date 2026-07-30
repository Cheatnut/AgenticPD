# -*- coding: utf-8 -*-
"""test_doomed_predictor.py — Stage D rule-based DoomedPredictor regression tests.

Pure Python, no LLM, no ORFS, no network.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from schemas.trial import MinimalObservation, DoomedDecision
from doomed_predictor import predict


class DoomedPredictorTest(unittest.TestCase):
    """Unit tests for the rule-based DoomedPredictor."""

    # ------------------------------------------------------------------
    # hard_dead classification
    # ------------------------------------------------------------------
    def test_failed_status_is_hard_dead(self):
        obs = MinimalObservation(
            trial_id="t1", stage="PL", status="failed",
            failure_type="tool_crash",
            stage_wns_ps=-100.0, checkpoint_id="cp-1",
        )
        results = predict([obs], survivor_count=1)
        self.assertEqual(results[0].risk_class, "hard_dead")
        self.assertIn("stage_failed", results[0].reason_codes)

    def test_timeout_is_hard_dead(self):
        obs = MinimalObservation(
            trial_id="t1", stage="PL", status="failed",
            failure_type="timeout",
            stage_wns_ps=-100.0, checkpoint_id="cp-1",
        )
        results = predict([obs], survivor_count=1)
        self.assertEqual(results[0].risk_class, "hard_dead")
        self.assertIn("timeout", results[0].reason_codes)

    def test_timing_both_none_is_hard_dead(self):
        obs = MinimalObservation(
            trial_id="t1", stage="PL", status="ok",
            stage_wns_ps=None, stage_tns_ps=None,
            checkpoint_id="cp-1",
        )
        results = predict([obs], survivor_count=1)
        self.assertEqual(results[0].risk_class, "hard_dead")
        self.assertIn("timing_missing", results[0].reason_codes)

    def test_checkpoint_missing_is_hard_dead(self):
        obs = MinimalObservation(
            trial_id="t1", stage="PL", status="ok",
            stage_wns_ps=-50.0, stage_tns_ps=-100.0,
            checkpoint_id=None,
        )
        results = predict([obs], survivor_count=1)
        self.assertEqual(results[0].risk_class, "hard_dead")
        self.assertIn("checkpoint_missing", results[0].reason_codes)

    def test_multiple_hard_dead_reasons(self):
        obs = MinimalObservation(
            trial_id="t1", stage="PL", status="failed",
            failure_type="tool_crash",
            stage_wns_ps=None, stage_tns_ps=None,
            checkpoint_id=None,
        )
        results = predict([obs], survivor_count=1)
        reasons = results[0].reason_codes
        self.assertIn("stage_failed", reasons)
        self.assertIn("timing_missing", reasons)
        self.assertIn("checkpoint_missing", reasons)

    # ------------------------------------------------------------------
    # WNS-only or TNS-only is valid (not hard_dead)
    # ------------------------------------------------------------------
    def test_wns_only_not_hard_dead(self):
        obs = MinimalObservation(
            trial_id="t1", stage="PL", status="ok",
            stage_wns_ps=-50.0, stage_tns_ps=None,
            checkpoint_id="cp-1",
        )
        results = predict([obs], survivor_count=1)
        self.assertEqual(results[0].risk_class, "survivor")

    def test_tns_only_not_hard_dead(self):
        obs = MinimalObservation(
            trial_id="t1", stage="PL", status="ok",
            stage_wns_ps=None, stage_tns_ps=-100.0,
            checkpoint_id="cp-1",
        )
        results = predict([obs], survivor_count=1)
        self.assertEqual(results[0].risk_class, "survivor")

    # ------------------------------------------------------------------
    # Sorting: WNS → TNS → trial_id
    # ------------------------------------------------------------------
    def test_sort_by_wns_descending(self):
        cohort = [
            MinimalObservation(trial_id="a", stage="PL", status="ok",
                              stage_wns_ps=-200.0, stage_tns_ps=0.0,
                              checkpoint_id="cp-a"),
            MinimalObservation(trial_id="b", stage="PL", status="ok",
                              stage_wns_ps=-50.0, stage_tns_ps=0.0,
                              checkpoint_id="cp-b"),
        ]
        results = predict(cohort, survivor_count=1)
        # b (WNS=-50) better than a (WNS=-200) → b is survivor
        self.assertEqual(results[1].risk_class, "survivor")
        self.assertEqual(results[0].risk_class, "soft_bad")

    def test_sort_by_tns_when_wns_tied(self):
        cohort = [
            MinimalObservation(trial_id="a", stage="PL", status="ok",
                              stage_wns_ps=-100.0, stage_tns_ps=-500.0,
                              checkpoint_id="cp-a"),
            MinimalObservation(trial_id="b", stage="PL", status="ok",
                              stage_wns_ps=-100.0, stage_tns_ps=-100.0,
                              checkpoint_id="cp-b"),
        ]
        results = predict(cohort, survivor_count=1)
        # b has better TNS (-100 > -500) → b is survivor
        self.assertEqual(results[1].risk_class, "survivor")
        self.assertEqual(results[0].risk_class, "soft_bad")

    def test_sort_by_trial_id_when_wns_and_tns_tied(self):
        cohort = [
            MinimalObservation(trial_id="z", stage="PL", status="ok",
                              stage_wns_ps=-100.0, stage_tns_ps=-200.0,
                              checkpoint_id="cp-z"),
            MinimalObservation(trial_id="a", stage="PL", status="ok",
                              stage_wns_ps=-100.0, stage_tns_ps=-200.0,
                              checkpoint_id="cp-a"),
            MinimalObservation(trial_id="m", stage="PL", status="ok",
                              stage_wns_ps=-100.0, stage_tns_ps=-200.0,
                              checkpoint_id="cp-m"),
        ]
        results = predict(cohort, survivor_count=1)
        # Sorted by trial_id: a < m < z → "a" (original idx 1) is survivor
        self.assertEqual(results[1].risk_class, "survivor",
                         "trial_id 'a' should be best among ties")
        self.assertEqual(results[2].risk_class, "soft_bad")
        self.assertEqual(results[0].risk_class, "soft_bad")

    # ------------------------------------------------------------------
    # survivor_count behavior
    # ------------------------------------------------------------------
    def test_survivor_count_zero_all_soft_bad(self):
        cohort = [
            MinimalObservation(trial_id="a", stage="PL", status="ok",
                              stage_wns_ps=-50.0, stage_tns_ps=-100.0,
                              checkpoint_id="cp-a"),
            MinimalObservation(trial_id="b", stage="PL", status="ok",
                              stage_wns_ps=-100.0, stage_tns_ps=-200.0,
                              checkpoint_id="cp-b"),
        ]
        results = predict(cohort, survivor_count=0)
        for r in results:
            self.assertEqual(r.risk_class, "soft_bad")

    def test_survivor_count_exceeds_candidates_all_survivor(self):
        cohort = [
            MinimalObservation(trial_id="a", stage="PL", status="ok",
                              stage_wns_ps=-50.0, stage_tns_ps=-100.0,
                              checkpoint_id="cp-a"),
            MinimalObservation(trial_id="b", stage="PL", status="ok",
                              stage_wns_ps=-100.0, stage_tns_ps=-200.0,
                              checkpoint_id="cp-b"),
        ]
        results = predict(cohort, survivor_count=10)
        for r in results:
            self.assertEqual(r.risk_class, "survivor")

    def test_mixed_hard_dead_and_soft_bad(self):
        """hard_dead excluded from ranking; survivor_count applied to remainder."""
        cohort = [
            MinimalObservation(trial_id="dead", stage="PL", status="failed",
                              failure_type="tool_crash",
                              checkpoint_id=None),
            MinimalObservation(trial_id="good", stage="PL", status="ok",
                              stage_wns_ps=-50.0, stage_tns_ps=-100.0,
                              checkpoint_id="cp-good"),
            MinimalObservation(trial_id="bad", stage="PL", status="ok",
                              stage_wns_ps=-200.0, stage_tns_ps=-500.0,
                              checkpoint_id="cp-bad"),
        ]
        results = predict(cohort, survivor_count=1)
        self.assertEqual(results[0].risk_class, "hard_dead")
        self.assertEqual(results[1].risk_class, "survivor",
                         "good (WNS=-50) should be survivor")
        self.assertEqual(results[2].risk_class, "soft_bad")

    # ------------------------------------------------------------------
    # risk_score
    # ------------------------------------------------------------------
    def test_hard_dead_risk_score_zero(self):
        obs = MinimalObservation(
            trial_id="x", stage="PL", status="failed",
            checkpoint_id=None,
        )
        results = predict([obs], survivor_count=1)
        self.assertEqual(results[0].risk_score, 0.0)

    def test_best_survivor_risk_score_one(self):
        cohort = [
            MinimalObservation(trial_id="a", stage="PL", status="ok",
                              stage_wns_ps=-50.0, checkpoint_id="cp-a"),
            MinimalObservation(trial_id="b", stage="PL", status="ok",
                              stage_wns_ps=-100.0, checkpoint_id="cp-b"),
        ]
        results = predict(cohort, survivor_count=2)
        self.assertEqual(results[0].risk_score, 1.0,
                         "best (a, WNS=-50) → risk_score=1.0")
        self.assertEqual(results[1].risk_score, 0.0,
                         "worst (b, WNS=-100) → risk_score=0.0")

    def test_single_candidate_risk_score_one(self):
        obs = MinimalObservation(
            trial_id="x", stage="PL", status="ok",
            stage_wns_ps=-50.0, checkpoint_id="cp-x",
        )
        results = predict([obs], survivor_count=1)
        self.assertEqual(results[0].risk_score, 1.0)

    def test_soft_bad_risk_score_less_than_survivor(self):
        cohort = [
            MinimalObservation(trial_id="a", stage="PL", status="ok",
                              stage_wns_ps=-50.0, checkpoint_id="cp-a"),
            MinimalObservation(trial_id="b", stage="PL", status="ok",
                              stage_wns_ps=-100.0, checkpoint_id="cp-b"),
            MinimalObservation(trial_id="c", stage="PL", status="ok",
                              stage_wns_ps=-200.0, checkpoint_id="cp-c"),
        ]
        results = predict(cohort, survivor_count=1)
        # Sorted: a(WNS=-50, rank 0) → risk=1.0 survivor
        #          b(WNS=-100, rank 1) → risk=0.5 soft_bad
        #          c(WNS=-200, rank 2) → risk=0.0 soft_bad
        self.assertEqual(results[0].risk_class, "survivor")
        self.assertEqual(results[0].risk_score, 1.0)
        self.assertEqual(results[1].risk_class, "soft_bad")
        self.assertEqual(results[1].risk_score, 0.5)
        self.assertEqual(results[2].risk_class, "soft_bad")
        self.assertEqual(results[2].risk_score, 0.0)

    # ------------------------------------------------------------------
    # Determinism
    # ------------------------------------------------------------------
    def test_same_input_same_output(self):
        cohort = [
            MinimalObservation(trial_id="a", stage="PL", status="ok",
                              stage_wns_ps=-50.0, checkpoint_id="cp-a"),
            MinimalObservation(trial_id="b", stage="PL", status="ok",
                              stage_wns_ps=-100.0, checkpoint_id="cp-b"),
            MinimalObservation(trial_id="dead", stage="PL", status="failed",
                              checkpoint_id=None),
        ]
        r1 = predict(cohort, survivor_count=1)
        r2 = predict(cohort, survivor_count=1)
        for i in range(len(cohort)):
            self.assertEqual(r1[i].risk_class, r2[i].risk_class)
            self.assertEqual(r1[i].risk_score, r2[i].risk_score)
            self.assertEqual(r1[i].reason_codes, r2[i].reason_codes)
            self.assertEqual(r1[i].rule_version, r2[i].rule_version)

    # ------------------------------------------------------------------
    # Edge cases
    # ------------------------------------------------------------------
    def test_empty_cohort(self):
        results = predict([], survivor_count=1)
        self.assertEqual(results, [])

    # ------------------------------------------------------------------
    # Input validation
    # ------------------------------------------------------------------
    def test_rejects_negative_survivor_count(self):
        obs = MinimalObservation(trial_id="a", stage="PL", status="ok",
                                stage_wns_ps=-50.0, checkpoint_id="cp-a")
        with self.assertRaises(ValueError):
            predict([obs], survivor_count=-1)

    def test_rejects_mixed_stages(self):
        cohort = [
            MinimalObservation(trial_id="a", stage="PL", status="ok",
                              stage_wns_ps=-50.0, checkpoint_id="cp-a"),
            MinimalObservation(trial_id="b", stage="CTS", status="ok",
                              stage_wns_ps=-100.0, checkpoint_id="cp-b"),
        ]
        with self.assertRaises(ValueError):
            predict(cohort, survivor_count=1)

    def test_rejects_invalid_stage_rt(self):
        obs = MinimalObservation(trial_id="a", stage="RT", status="ok",
                                stage_wns_ps=-50.0, checkpoint_id="cp-a")
        with self.assertRaises(ValueError):
            predict([obs], survivor_count=1)

    def test_rejects_invalid_stage_fp(self):
        obs = MinimalObservation(trial_id="a", stage="FP", status="ok",
                                stage_wns_ps=-50.0, checkpoint_id="cp-a")
        with self.assertRaises(ValueError):
            predict([obs], survivor_count=1)

    def test_rejects_float_survivor_count(self):
        obs = MinimalObservation(trial_id="a", stage="PL", status="ok",
                                stage_wns_ps=-50.0, checkpoint_id="cp-a")
        with self.assertRaises(ValueError):
            predict([obs], survivor_count=1.5)

    def test_rejects_string_survivor_count(self):
        obs = MinimalObservation(trial_id="a", stage="PL", status="ok",
                                stage_wns_ps=-50.0, checkpoint_id="cp-a")
        with self.assertRaises(ValueError):
            predict([obs], survivor_count="1")

    def test_rejects_bool_survivor_count(self):
        obs = MinimalObservation(trial_id="a", stage="PL", status="ok",
                                stage_wns_ps=-50.0, checkpoint_id="cp-a")
        with self.assertRaises(ValueError):
            predict([obs], survivor_count=True)

    def test_all_hard_dead(self):
        cohort = [
            MinimalObservation(trial_id="a", stage="PL", status="failed",
                              checkpoint_id=None),
            MinimalObservation(trial_id="b", stage="PL", status="failed",
                              checkpoint_id=None),
        ]
        results = predict(cohort, survivor_count=1)
        for r in results:
            self.assertEqual(r.risk_class, "hard_dead")
            self.assertEqual(r.risk_score, 0.0)

    def test_input_evidence_recorded(self):
        obs = MinimalObservation(
            trial_id="t1", stage="CTS", status="ok",
            stage_wns_ps=-50.0, stage_tns_ps=-100.0,
            checkpoint_id="cp-1",
        )
        results = predict([obs], survivor_count=1)
        evidence = results[0].input_evidence
        self.assertEqual(evidence["trial_id"], "t1")
        self.assertEqual(evidence["stage"], "CTS")
        self.assertEqual(evidence["cohort_size"], 1)
        self.assertEqual(evidence["survivor_count"], 1)

    def test_rule_version_recorded(self):
        obs = MinimalObservation(
            trial_id="t1", stage="PL", status="ok",
            stage_wns_ps=-50.0, checkpoint_id="cp-1",
        )
        results = predict([obs], survivor_count=1, rule_version="2.3.1")
        self.assertEqual(results[0].rule_version, "2.3.1")

    def test_result_order_matches_input_order(self):
        cohort = [
            MinimalObservation(trial_id="c", stage="PL", status="ok",
                              stage_wns_ps=-200.0, checkpoint_id="cp-c"),
            MinimalObservation(trial_id="a", stage="PL", status="ok",
                              stage_wns_ps=-50.0, checkpoint_id="cp-a"),
            MinimalObservation(trial_id="b", stage="PL", status="failed",
                              checkpoint_id=None),
        ]
        results = predict(cohort, survivor_count=1)
        self.assertEqual(results[0].input_evidence["trial_id"], "c")
        self.assertEqual(results[1].input_evidence["trial_id"], "a")
        self.assertEqual(results[2].input_evidence["trial_id"], "b")


if __name__ == "__main__":
    unittest.main()
