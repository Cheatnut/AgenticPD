# -*- coding: utf-8 -*-
"""test_observation_builder.py — Stage D observation builder regression tests.

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
    MinimalObservation,
    StageResult,
    TrialRecord,
)
from observation_builder import (
    _extract_timing,
    _numeric_tag_sort_key,
    build_minimal_observation,
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


def _trial(trial_id, stage_results, parent=None, checkpoint=None,
           failure=None):
    return TrialRecord(
        trial_id=trial_id,
        experiment_id="test",
        status="ok",
        start_time=None, end_time=None,
        params={},
        stage_results=stage_results,
        parent_trial_id=parent,
        final_qor=None,
        failure=failure,
        error_message=None,
        checkpoint=checkpoint,
        config_hash=None, env_hash=None,
        param_diff=None,
        artifact_dir=None,
        execution_resolution=None,
        doomed_decisions=[],
        gwtw_decisions=[],
        decision_trace_refs=[],
    )


def _cp(cp_id, stage, source_trial="parent1"):
    return CheckpointRef(
        checkpoint_id=cp_id, source_trial_id=source_trial,
        stage=stage, param_hash="abc", orfs_commit="def",
        created_at="2025-01-01T00:00:00",
        artifact_manifest=[], artifact_dir=None,
    )


# =========================================================================
# Validation
# =========================================================================


class ValidationTest(unittest.TestCase):

    def test_rejects_fp(self):
        t = _trial("t1", [_sr("FP")])
        with self.assertRaises(ValueError):
            build_minimal_observation(t, "FP")

    def test_rejects_rt(self):
        t = _trial("t1", [_sr("RT")])
        with self.assertRaises(ValueError):
            build_minimal_observation(t, "RT")

    def test_rejects_synth(self):
        t = _trial("t1", [_sr("synth")])
        with self.assertRaises(ValueError):
            build_minimal_observation(t, "synth")

    def test_rejects_missing_stage_result(self):
        t = _trial("t1", [_sr("FP"), _sr("synth")])
        with self.assertRaises(ValueError):
            build_minimal_observation(t, "PL")


# =========================================================================
# PL observation
# =========================================================================


class PLObservationTest(unittest.TestCase):

    def test_basic_pl_observation(self):
        t = _trial("t-pl", [
            _sr("PL", status="ok", elapsed=45.2, qor={
                "3_5_place_dp_ws_ps": -1460.3,
                "3_5_place_dp_tns_ps": -61747.6,
            }),
        ], parent="parent-abc")
        obs = build_minimal_observation(t, "PL")
        self.assertEqual(obs.trial_id, "t-pl")
        self.assertEqual(obs.stage, "PL")
        self.assertEqual(obs.status, "ok")
        self.assertEqual(obs.stage_wns_ps, -1460.3)
        self.assertEqual(obs.stage_tns_ps, -61747.6)
        self.assertEqual(obs.stage_elapsed_s, 45.2)
        self.assertIsNone(obs.failure_type)
        self.assertEqual(obs.parent_trial_id, "parent-abc")

    def test_pl_wns_zero(self):
        """WNS=0 (timing exactly met) is valid."""
        t = _trial("t-zero", [
            _sr("PL", qor={
                "3_5_place_dp_ws_ps": 0.0,
                "3_5_place_dp_tns_ps": 0.0,
            }),
        ])
        obs = build_minimal_observation(t, "PL")
        self.assertEqual(obs.stage_wns_ps, 0.0)
        self.assertEqual(obs.stage_tns_ps, 0.0)

    def test_pl_positive_wns(self):
        """WNS > 0 (timing met with slack) is valid."""
        t = _trial("t-pos", [
            _sr("PL", qor={
                "3_5_place_dp_ws_ps": 50.0,
                "3_5_place_dp_tns_ps": 0.0,
            }),
        ])
        obs = build_minimal_observation(t, "PL")
        self.assertEqual(obs.stage_wns_ps, 50.0)


# =========================================================================
# CTS observation
# =========================================================================


class CTSObservationTest(unittest.TestCase):

    def test_basic_cts_observation(self):
        t = _trial("t-cts", [
            _sr("CTS", status="ok", elapsed=30.0, qor={
                "4_1_cts_ws_ps": -800.0,
                "4_1_cts_tns_ps": -12000.0,
            }),
        ])
        obs = build_minimal_observation(t, "CTS")
        self.assertEqual(obs.trial_id, "t-cts")
        self.assertEqual(obs.stage, "CTS")
        self.assertEqual(obs.stage_wns_ps, -800.0)
        self.assertEqual(obs.stage_tns_ps, -12000.0)


# =========================================================================
# Timing extraction
# =========================================================================


class TimingExtractionTest(unittest.TestCase):

    def test_empty_qor_returns_none(self):
        wns, tns = _extract_timing({})
        self.assertIsNone(wns)
        self.assertIsNone(tns)

    def test_ws_only_returns_none(self):
        wns, tns = _extract_timing({"3_5_place_dp_ws_ps": -100.0})
        self.assertIsNone(wns)
        self.assertIsNone(tns)

    def test_tns_only_returns_none(self):
        wns, tns = _extract_timing({"3_5_place_dp_tns_ps": -500.0})
        self.assertIsNone(wns)
        self.assertIsNone(tns)

    def test_no_tag_has_both_returns_none(self):
        """Two different tags, each missing one side → no valid pair."""
        wns, tns = _extract_timing({
            "5_1_grt_ws_ps": -200.0,
            "5_2_route_tns_ps": -4500.0,
        })
        self.assertIsNone(wns)
        self.assertIsNone(tns)

    def test_cross_tag_mixing_prevented(self):
        """WS from one tag + TNS from another must not be paired."""
        # Even if there are multiple tags, the best complete tag wins.
        wns, tns = _extract_timing({
            "3_5_place_dp_ws_ps": -100.0,
            "3_5_place_dp_tns_ps": -500.0,
            "5_1_grt_ws_ps": -200.0,  # no TNS for this tag
        })
        # 3_5_place_dp is the only complete tag → use it.
        self.assertEqual(wns, -100.0)
        self.assertEqual(tns, -500.0)

    def test_latest_numeric_tag_wins(self):
        wns, tns = _extract_timing({
            "5_1_grt_ws_ps": -200.0,
            "5_1_grt_tns_ps": -5000.0,
            "5_2_route_ws_ps": -180.0,
            "5_2_route_tns_ps": -4500.0,
        })
        self.assertEqual(wns, -180.0, "5_2_route should win over 5_1_grt")
        self.assertEqual(tns, -4500.0)

    def test_numeric_sort_not_lexicographic(self):
        """10_1 > 2_1 when compared numerically."""
        wns, tns = _extract_timing({
            "2_1_floorplan_ws_ps": -500.0,
            "2_1_floorplan_tns_ps": -10000.0,
            "10_1_cts_ws_ps": -100.0,
            "10_1_cts_tns_ps": -2000.0,
        })
        self.assertEqual(wns, -100.0, "10_1 should sort after 2_1 numerically")
        self.assertEqual(tns, -2000.0)

    def test_single_tag_used_directly(self):
        wns, tns = _extract_timing({
            "4_1_cts_ws_ps": -800.0,
            "4_1_cts_tns_ps": -12000.0,
        })
        self.assertEqual(wns, -800.0)
        self.assertEqual(tns, -12000.0)

    def test_three_tags_pick_latest(self):
        wns, tns = _extract_timing({
            "3_5_place_dp_ws_ps": -300.0,
            "3_5_place_dp_tns_ps": -8000.0,
            "4_1_cts_ws_ps": -200.0,
            "4_1_cts_tns_ps": -5000.0,
            "5_2_route_ws_ps": -150.0,
            "5_2_route_tns_ps": -3000.0,
        })
        self.assertEqual(wns, -150.0,
                         "5_2_route should win over 4_1_cts and 3_5_place_dp")
        self.assertEqual(tns, -3000.0)

    def test_same_numeric_prefix_uses_tag_string_tiebreak(self):
        """When tags have the same numeric prefix, full tag string breaks tie.

        Sort key = (numeric_tuple, tag_string).  "3_1_z" > "3_1_a"
        because (3,1,"3_1_z") > (3,1,"3_1_a").  No dependency on set
        iteration order.
        """
        wns, tns = _extract_timing({
            "3_1_a_ws_ps": -100.0,
            "3_1_a_tns_ps": -500.0,
            "3_1_z_ws_ps": -200.0,
            "3_1_z_tns_ps": -1000.0,
        })
        # (3, 1, "3_1_z") > (3, 1, "3_1_a") → "3_1_z" wins.
        self.assertEqual(wns, -200.0, "3_1_z > 3_1_a by full tag string")
        self.assertEqual(tns, -1000.0)


# =========================================================================
# Status and failure mapping
# =========================================================================


class StatusFailureTest(unittest.TestCase):

    def test_ok_status(self):
        t = _trial("t-ok", [_sr("PL", status="ok")])
        obs = build_minimal_observation(t, "PL")
        self.assertEqual(obs.status, "ok")

    def test_failed_status_tool_crash(self):
        t = _trial("t-fail", [
            _sr("PL", status="failed", failure=FailureClass.TOOL_CRASH),
        ])
        obs = build_minimal_observation(t, "PL")
        self.assertEqual(obs.status, "failed")
        self.assertEqual(obs.failure_type, "tool_crash")

    def test_failed_status_timeout(self):
        t = _trial("t-to", [
            _sr("CTS", status="failed", elapsed=3600.0,
                failure=FailureClass.TIMEOUT),
        ])
        obs = build_minimal_observation(t, "CTS")
        self.assertEqual(obs.status, "failed")
        self.assertEqual(obs.failure_type, "timeout")

    def test_failure_none_maps_to_none(self):
        t = _trial("t-none", [_sr("PL", failure=FailureClass.NONE)])
        obs = build_minimal_observation(t, "PL")
        self.assertIsNone(obs.failure_type)

    def test_failure_none_when_failure_field_is_none(self):
        t = _trial("t-null", [_sr("PL", failure=None)])
        obs = build_minimal_observation(t, "PL")
        self.assertIsNone(obs.failure_type)

    def test_qor_incomplete_failure(self):
        t = _trial("t-qor", [
            _sr("PL", status="failed", failure=FailureClass.QOR_INCOMPLETE),
        ])
        obs = build_minimal_observation(t, "PL")
        self.assertEqual(obs.failure_type, "qor_incomplete")

    def test_parse_error_failure(self):
        t = _trial("t-pe", [
            _sr("PL", status="failed", failure=FailureClass.PARSE_ERROR),
        ])
        obs = build_minimal_observation(t, "PL")
        self.assertEqual(obs.failure_type, "parse_error")

    def test_legality_fail_failure(self):
        t = _trial("t-lf", [
            _sr("PL", status="failed", failure=FailureClass.LEGALITY_FAIL),
        ])
        obs = build_minimal_observation(t, "PL")
        self.assertEqual(obs.failure_type, "legality_fail")

    def test_skipped_status(self):
        t = _trial("t-skip", [_sr("PL", status="skipped", elapsed=0.0)])
        obs = build_minimal_observation(t, "PL")
        self.assertEqual(obs.status, "skipped")


# =========================================================================
# Checkpoint mapping
# =========================================================================


class CheckpointMappingTest(unittest.TestCase):

    def test_checkpoint_matches_decision_stage(self):
        cp = _cp("cp-abc-PL", "PL")
        t = _trial("t1", [_sr("PL")], checkpoint=cp)
        obs = build_minimal_observation(t, "PL")
        self.assertEqual(obs.checkpoint_id, "cp-abc-PL")

    def test_checkpoint_cts_matches_cts_stage(self):
        cp = _cp("cp-xyz-CTS", "CTS")
        t = _trial("t2", [_sr("CTS")], checkpoint=cp)
        obs = build_minimal_observation(t, "CTS")
        self.assertEqual(obs.checkpoint_id, "cp-xyz-CTS")

    def test_checkpoint_mismatch_returns_none(self):
        cp = _cp("cp-abc-CTS", "CTS")
        t = _trial("t3", [_sr("PL")], checkpoint=cp)
        obs = build_minimal_observation(t, "PL")
        self.assertIsNone(obs.checkpoint_id)

    def test_pl_checkpoint_mismatch_for_cts_stage(self):
        cp = _cp("cp-abc-PL", "PL")
        t = _trial("t4", [_sr("CTS")], checkpoint=cp)
        obs = build_minimal_observation(t, "CTS")
        self.assertIsNone(obs.checkpoint_id)

    def test_no_checkpoint_returns_none(self):
        t = _trial("t5", [_sr("PL")], checkpoint=None)
        obs = build_minimal_observation(t, "PL")
        self.assertIsNone(obs.checkpoint_id)

    def test_failed_trial_with_checkpoint_still_maps(self):
        cp = _cp("cp-fail-PL", "PL")
        t = _trial("t6", [
            _sr("PL", status="failed", failure=FailureClass.TOOL_CRASH),
        ], checkpoint=cp)
        obs = build_minimal_observation(t, "PL")
        self.assertEqual(obs.status, "failed")
        self.assertEqual(obs.checkpoint_id, "cp-fail-PL")


# =========================================================================
# Parent mapping
# =========================================================================


class ParentMappingTest(unittest.TestCase):

    def test_parent_trial_id_mapped(self):
        t = _trial("t1", [_sr("PL")], parent="parent-xyz")
        obs = build_minimal_observation(t, "PL")
        self.assertEqual(obs.parent_trial_id, "parent-xyz")

    def test_parent_trial_id_none(self):
        t = _trial("t2", [_sr("PL")], parent=None)
        obs = build_minimal_observation(t, "PL")
        self.assertIsNone(obs.parent_trial_id)


# =========================================================================
# Immutability
# =========================================================================


class ImmutabilityTest(unittest.TestCase):

    def test_trial_not_modified(self):
        t = _trial("t1", [
            _sr("PL", qor={"3_5_place_dp_ws_ps": -100.0,
                            "3_5_place_dp_tns_ps": -500.0}),
        ])
        original = copy.deepcopy(t)
        build_minimal_observation(t, "PL")
        self.assertEqual(t.trial_id, original.trial_id)
        self.assertEqual(t.stage_results[0].stage_qor,
                         original.stage_results[0].stage_qor)
        self.assertEqual(t.stage_results[0].status,
                         original.stage_results[0].status)

    def test_observation_is_new_object(self):
        t = _trial("t1", [_sr("PL")])
        obs = build_minimal_observation(t, "PL")
        self.assertIsInstance(obs, MinimalObservation)
        self.assertIsNot(obs.trial_id, None)


# =========================================================================
# Elapsed time
# =========================================================================


class ElapsedTest(unittest.TestCase):

    def test_elapsed_zero(self):
        t = _trial("t1", [_sr("PL", elapsed=0.0)])
        obs = build_minimal_observation(t, "PL")
        self.assertEqual(obs.stage_elapsed_s, 0.0)

    def test_elapsed_large(self):
        t = _trial("t1", [_sr("CTS", elapsed=5000.0)])
        obs = build_minimal_observation(t, "CTS")
        self.assertEqual(obs.stage_elapsed_s, 5000.0)


# =========================================================================
# Multi-stage StageResults (only relevant stage used)
# =========================================================================


class MultiStageTest(unittest.TestCase):

    def test_pl_picked_from_multi_stage_results(self):
        t = _trial("t1", [
            _sr("synth", status="ok", qor={}),
            _sr("FP", status="ok", qor={"2_1_floorplan_ws_ps": -2000.0,
                                         "2_1_floorplan_tns_ps": -50000.0}),
            _sr("PL", status="ok", qor={"3_5_place_dp_ws_ps": -1460.3,
                                         "3_5_place_dp_tns_ps": -61747.6}),
            _sr("CTS", status="ok", qor={"4_1_cts_ws_ps": -800.0,
                                          "4_1_cts_tns_ps": -12000.0}),
        ])
        obs = build_minimal_observation(t, "PL")
        self.assertEqual(obs.stage, "PL")
        self.assertEqual(obs.stage_wns_ps, -1460.3)
        self.assertEqual(obs.stage_tns_ps, -61747.6)

    def test_cts_picked_from_multi_stage_results(self):
        t = _trial("t2", [
            _sr("PL", status="ok", qor={"3_5_place_dp_ws_ps": -1460.3,
                                         "3_5_place_dp_tns_ps": -61747.6}),
            _sr("CTS", status="ok", qor={"4_1_cts_ws_ps": -800.0,
                                          "4_1_cts_tns_ps": -12000.0}),
        ])
        obs = build_minimal_observation(t, "CTS")
        self.assertEqual(obs.stage, "CTS")
        self.assertEqual(obs.stage_wns_ps, -800.0)


# =========================================================================
# Tag sort key — deterministic guarantee
# =========================================================================


class NumericTagSortKeyTest(unittest.TestCase):
    """Direct tests for _numeric_tag_sort_key — the sort key that guarantees
    deterministic max() across PYTHONHASHSEED values."""

    def test_different_numeric_prefixes(self):
        """(3, 6) > (3, 5) — later numeric prefix wins."""
        self.assertGreater(
            _numeric_tag_sort_key("3_6_cts"),
            _numeric_tag_sort_key("3_5_place_dp"),
        )

    def test_same_numeric_prefix_tiebreak_by_full_tag(self):
        """Same numeric prefix (3, 5) → full tag string breaks tie."""
        key_a = _numeric_tag_sort_key("3_5_a")
        key_b = _numeric_tag_sort_key("3_5_b")
        key_z = _numeric_tag_sort_key("3_5_z")
        # (3, 5, "3_5_z") > (3, 5, "3_5_b") > (3, 5, "3_5_a")
        self.assertGreater(key_z, key_b)
        self.assertGreater(key_b, key_a)

    def test_total_order_property_different_tags_always_different_keys(self):
        """Different tags always produce different sort keys.

        This is the property that makes max() deterministic: with a total
        order, max() does not fall back to set iteration order.
        """
        tags = [
            "3_5_place_dp",
            "3_5_other",
            "3_5_cts",
            "4_1_cts",
            "5_2_route",
            "2_1_floorplan",
        ]
        keys = [_numeric_tag_sort_key(t) for t in tags]
        # All keys must be distinct — no two different tags produce the same key.
        self.assertEqual(
            len(keys),
            len(set(keys)),
            f"Duplicate sort keys detected: {keys}",
        )

    def test_no_leading_digits_yields_zero_tuple(self):
        """Tags without leading digits get (0,) as numeric prefix."""
        key = _numeric_tag_sort_key("place_dp")
        self.assertEqual(key[0], (0,))
        # Full tag is still the second element.
        self.assertEqual(key[1], "place_dp")

    def test_leading_digits_extracted_correctly(self):
        """Leading underscore-separated integers become the numeric tuple."""
        key = _numeric_tag_sort_key("10_2_foo_bar")
        self.assertEqual(key[0], (10, 2))
        self.assertEqual(key[1], "10_2_foo_bar")

    def test_mixed_digits_and_non_digits(self):
        """Only leading contiguous digit segments are extracted."""
        # "3_5_2_z" → (3, 5, 2), full tag "3_5_2_z"
        key = _numeric_tag_sort_key("3_5_2_z")
        self.assertEqual(key[0], (3, 5, 2))
        # "3_x_5_y" → break at "x", numeric = (3,), full tag "3_x_5_y"
        key = _numeric_tag_sort_key("3_x_5_y")
        self.assertEqual(key[0], (3,))

    def test_single_digit_tag(self):
        key = _numeric_tag_sort_key("42")
        self.assertEqual(key[0], (42,))
        self.assertEqual(key[1], "42")

    def test_same_numeric_longer_tag(self):
        """(3, 5) from two competing real-world tags: string tie-break resolves."""
        # simulate real ORFS tags inside the same stage family
        k1 = _numeric_tag_sort_key("3_5_place_dp")
        k2 = _numeric_tag_sort_key("3_5_place_dp_tight")
        # Both have numeric (3, 5); strings differ.
        self.assertEqual(k1[0], (3, 5))
        self.assertEqual(k2[0], (3, 5))
        self.assertNotEqual(k1, k2,
                            "different tags must yield different keys")
        # The lexicographically later string wins under max().
        self.assertGreater(k2, k1)


if __name__ == "__main__":
    unittest.main()
