# -*- coding: utf-8 -*-
"""observation_builder.py — Stage D: pure MinimalObservation builder.

Pure Python, no I/O, no ORFS, no LLM, no side effects.

Input:  a TrialRecord and a decision_stage (PL or CTS).

Output: a MinimalObservation populated from the TrialRecord's
        corresponding StageResult.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Set, Tuple

from schemas.trial import (
    FailureClass,
    MinimalObservation,
    StageResult,
    TrialRecord,
)

log = logging.getLogger(__name__)

_VALID_STAGES: frozenset = frozenset({"PL", "CTS"})

# Suffixes that identify WNS / TNS in stage_qor keys.
_WS_SUFFIX = "_ws_ps"
_TNS_SUFFIX = "_tns_ps"


# =============================================================================
# Tag sort key — deterministic across PYTHONHASHSEED
# =============================================================================


def _numeric_tag_sort_key(tag: str) -> Tuple[Tuple[int, ...], str]:
    """Return a deterministic sort key for an ORFS stage_qor tag.

    The key is ``(leading_numeric_prefix, full_tag_string)``.

    *leading_numeric_prefix* is the tuple of leading underscore-separated
    integer segments.  For example ``"3_5_place_dp"`` yields ``(3, 5)``.
    A tag with no leading digits yields ``(0,)``.

    *full_tag_string* is the unchanged *tag* argument — it serves as
    the tie-break when two tags share the same leading numeric prefix,
    guaranteeing deterministic ordering regardless of ``PYTHONHASHSEED``
    or CPython set iteration order.

    Returns:
        ``(numeric_tuple, tag)`` suitable for use with :func:`max`,
        :func:`sorted`, or :func:`min`.
    """
    parts = tag.split("_")
    nums: List[int] = []
    for p in parts:
        if p.isdigit():
            nums.append(int(p))
        else:
            break
    numeric = tuple(nums) if nums else (0,)
    return (numeric, tag)


# =============================================================================
# Core builder
# =============================================================================


def build_minimal_observation(
    trial: TrialRecord,
    decision_stage: str,
) -> MinimalObservation:
    """Build a MinimalObservation from *trial* at *decision_stage*.

    Args:
        trial:          completed TrialRecord (may be ok, failed, or paused).
        decision_stage: ``"PL"`` or ``"CTS"`` — which stage to observe.

    Returns:
        MinimalObservation with WNS/TNS extracted from the stage's QoR,
        status/elapsed/failure/checkpoint mapped from the TrialRecord.

    Raises:
        ValueError: *decision_stage* is not PL/CTS, or the TrialRecord
                    has no StageResult for that stage.
    """
    if decision_stage not in _VALID_STAGES:
        raise ValueError(
            f"decision_stage must be PL or CTS, got {decision_stage!r}"
        )

    # -- find the matching StageResult --
    stage_result = _find_stage_result(trial, decision_stage)

    # -- extract WNS / TNS from stage_qor (same-tag, numerically latest) --
    wns_ps, tns_ps = _extract_timing(stage_result.stage_qor)

    # -- map failure_type --
    failure_type = _map_failure_type(stage_result)

    # -- map checkpoint_id (None if checkpoint stage != decision_stage) --
    checkpoint_id = None
    if trial.checkpoint is not None:
        if trial.checkpoint.stage == decision_stage:
            checkpoint_id = trial.checkpoint.checkpoint_id

    return MinimalObservation(
        trial_id=trial.trial_id,
        stage=decision_stage,
        status=stage_result.status,
        stage_wns_ps=wns_ps,
        stage_tns_ps=tns_ps,
        stage_elapsed_s=stage_result.elapsed_s,
        failure_type=failure_type,
        checkpoint_id=checkpoint_id,
        parent_trial_id=trial.parent_trial_id,
    )


# =============================================================================
# Helpers
# =============================================================================


def _find_stage_result(
    trial: TrialRecord,
    decision_stage: str,
) -> StageResult:
    """Return the StageResult for *decision_stage*, raising if missing."""
    for sr in trial.stage_results:
        if sr.stage == decision_stage:
            return sr
    raise ValueError(
        f"Trial {trial.trial_id!r} has no StageResult for stage "
        f"{decision_stage!r}"
    )


def _extract_timing(
    stage_qor: Dict[str, float],
) -> Tuple[Optional[float], Optional[float]]:
    """Extract (wns_ps, tns_ps) from *stage_qor*.

    Rules:
      - WNS and TNS MUST come from the same ORFS tag (the key prefix
        before ``_ws_ps`` / ``_tns_ps``).
      - When multiple tags carry a valid WNS+TNS pair, the one with the
        highest numeric stage prefix wins (latest numeric prefix).
      - Numeric tie-break: when two tags share the same numeric prefix,
        the full tag string is compared to guarantee deterministic
        selection (no dependency on set iteration order).
      - If no valid pair is found, returns ``(None, None)``.
    """
    # 1. Collect all tags that have at least one timing suffix.
    tags_with_ws: Set[str] = set()
    tags_with_tns: Set[str] = set()

    for key in stage_qor:
        if key.endswith(_WS_SUFFIX):
            tag = key[:-len(_WS_SUFFIX)]
            tags_with_ws.add(tag)
        elif key.endswith(_TNS_SUFFIX):
            tag = key[:-len(_TNS_SUFFIX)]
            tags_with_tns.add(tag)

    # 2. Keep only tags that have BOTH WNS and TNS.
    complete_tags = tags_with_ws & tags_with_tns
    if not complete_tags:
        return None, None

    # 3. Pick the tag with the highest numeric prefix.
    #    Tie-break on the full tag string so the result is deterministic
    #    even when two tags share the same numeric stage prefix
    #    (e.g. "3_1_z" > "3_1_a").
    #    _numeric_tag_sort_key returns (numeric_prefix_tuple, tag_string)
    #    which gives a total order — no dependency on set iteration order.
    best_tag = max(complete_tags, key=_numeric_tag_sort_key)

    wns = stage_qor.get(f"{best_tag}{_WS_SUFFIX}")
    tns = stage_qor.get(f"{best_tag}{_TNS_SUFFIX}")
    return wns, tns


def _map_failure_type(sr: StageResult) -> Optional[str]:
    """Map StageResult failure info to a MinimalObservation failure_type string.

    Returns None for ``NONE`` / ``none``, otherwise the enum's string value
    (``"tool_crash"``, ``"timeout"``, etc.).
    """
    if sr.failure is None or sr.failure == FailureClass.NONE:
        return None
    return sr.failure.value


# =============================================================================
# Self-test
# =============================================================================

if __name__ == "__main__":
    import sys

    ok = 0
    fail = 0

    def check(cond, msg):
        global ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  FAIL: {msg}")

    # -- helpers --
    def _sr(stage, status="ok", elapsed=10.0, qor=None, failure=None):
        """Minimal StageResult factory."""
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
        """Minimal TrialRecord factory."""
        from schemas.trial import CheckpointRef
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
        from schemas.trial import CheckpointRef
        return CheckpointRef(
            checkpoint_id=cp_id, source_trial_id=source_trial,
            stage=stage, param_hash="abc", orfs_commit="def",
            created_at="2025-01-01T00:00:00",
            artifact_manifest=[], artifact_dir=None,
        )

    # =====================================================================
    # 1. Invalid stage rejected
    # =====================================================================
    t = _trial("t1", [_sr("PL")])
    try:
        build_minimal_observation(t, "FP")
        check(False, "FP should raise ValueError")
    except ValueError as e:
        check("PL or CTS" in str(e), f"FP stage msg: {e}")
    try:
        build_minimal_observation(t, "RT")
        check(False, "RT should raise ValueError")
    except ValueError as e:
        check("PL or CTS" in str(e), f"RT stage msg: {e}")

    # =====================================================================
    # 2. Missing StageResult
    # =====================================================================
    t_no_pl = _trial("t2", [_sr("FP"), _sr("synth")])
    try:
        build_minimal_observation(t_no_pl, "PL")
        check(False, "missing PL StageResult should raise ValueError")
    except ValueError as e:
        check("no StageResult" in str(e).lower()
             or "has no" in str(e).lower(),
             f"missing stage msg: {e}")

    # =====================================================================
    # 3. Basic PL observation with timing
    # =====================================================================
    t_pl = _trial("t3", [
        _sr("PL", status="ok", elapsed=45.2, qor={
            "3_5_place_dp_ws_ps": -1460.3,
            "3_5_place_dp_tns_ps": -61747.6,
        }),
    ], parent="parent1")
    obs = build_minimal_observation(t_pl, "PL")
    check(obs.trial_id == "t3", f"trial_id: {obs.trial_id}")
    check(obs.stage == "PL", f"stage: {obs.stage}")
    check(obs.status == "ok", f"status: {obs.status}")
    check(obs.stage_wns_ps == -1460.3, f"wns: {obs.stage_wns_ps}")
    check(obs.stage_tns_ps == -61747.6, f"tns: {obs.stage_tns_ps}")
    check(obs.stage_elapsed_s == 45.2, f"elapsed: {obs.stage_elapsed_s}")
    check(obs.failure_type is None, f"failure_type: {obs.failure_type}")
    check(obs.parent_trial_id == "parent1",
          f"parent: {obs.parent_trial_id}")

    # =====================================================================
    # 4. CTS observation
    # =====================================================================
    t_cts = _trial("t4", [
        _sr("CTS", status="ok", elapsed=30.0, qor={
            "4_1_cts_ws_ps": -800.0,
            "4_1_cts_tns_ps": -12000.0,
        }),
    ])
    obs = build_minimal_observation(t_cts, "CTS")
    check(obs.stage == "CTS", f"stage: {obs.stage}")
    check(obs.stage_wns_ps == -800.0, f"wns: {obs.stage_wns_ps}")
    check(obs.stage_tns_ps == -12000.0, f"tns: {obs.stage_tns_ps}")

    # =====================================================================
    # 5. Timing missing: no stage_qor
    # =====================================================================
    t_no_qor = _trial("t5", [_sr("PL", status="ok", qor={})])
    obs = build_minimal_observation(t_no_qor, "PL")
    check(obs.stage_wns_ps is None, f"wns None: {obs.stage_wns_ps}")
    check(obs.stage_tns_ps is None, f"tns None: {obs.stage_tns_ps}")

    # =====================================================================
    # 6. Timing missing: only WS, no TNS
    # =====================================================================
    t_ws_only = _trial("t6", [_sr("PL", qor={
        "3_5_place_dp_ws_ps": -100.0,
    })])
    obs = build_minimal_observation(t_ws_only, "PL")
    check(obs.stage_wns_ps is None,
          f"ws-only → both None, wns={obs.stage_wns_ps}")
    check(obs.stage_tns_ps is None,
          f"ws-only → both None, tns={obs.stage_tns_ps}")

    # =====================================================================
    # 7. Timing missing: only TNS, no WS
    # =====================================================================
    t_tns_only = _trial("t7", [_sr("PL", qor={
        "3_5_place_dp_tns_ps": -500.0,
    })])
    obs = build_minimal_observation(t_tns_only, "PL")
    check(obs.stage_wns_ps is None,
          f"tns-only → both None, wns={obs.stage_wns_ps}")
    check(obs.stage_tns_ps is None,
          f"tns-only → both None, tns={obs.stage_tns_ps}")

    # =====================================================================
    # 8. Multiple tags: pick numerically latest
    # =====================================================================
    t_multi = _trial("t8", [_sr("RT", qor={
        "5_1_grt_ws_ps": -200.0,
        "5_1_grt_tns_ps": -5000.0,
        "5_2_route_ws_ps": -180.0,
        "5_2_route_tns_ps": -4500.0,
    })])
    # RT is not a valid decision_stage, but the timing extraction logic
    # should still pick 5_2_route (latest numeric tag).
    # Test extraction directly:
    wns, tns = _extract_timing({
        "5_1_grt_ws_ps": -200.0,
        "5_1_grt_tns_ps": -5000.0,
        "5_2_route_ws_ps": -180.0,
        "5_2_route_tns_ps": -4500.0,
    })
    check(wns == -180.0,
          f"latest tag ws: expected -180.0, got {wns}")
    check(tns == -4500.0,
          f"latest tag tns: expected -4500.0, got {tns}")

    # =====================================================================
    # 9. Multiple tags: don't mix different tags
    # =====================================================================
    # If one tag has WS and another has TNS, they must not be paired.
    wns, tns = _extract_timing({
        "5_1_grt_ws_ps": -200.0,
        "5_2_route_tns_ps": -4500.0,
    })
    check(wns is None and tns is None,
          f"cross-tag mixing → None, got wns={wns}, tns={tns}")

    # =====================================================================
    # 10. Failed stage
    # =====================================================================
    t_failed = _trial("t9", [
        _sr("PL", status="failed", elapsed=12.0, qor={},
            failure=FailureClass.TOOL_CRASH),
    ])
    obs = build_minimal_observation(t_failed, "PL")
    check(obs.status == "failed", f"failed status: {obs.status}")
    check(obs.failure_type == "tool_crash",
          f"failure_type: {obs.failure_type}")
    check(obs.stage_wns_ps is None, "failed + no qor → wns None")
    check(obs.stage_tns_ps is None, "failed + no qor → tns None")

    # =====================================================================
    # 11. Timeout failure
    # =====================================================================
    t_timeout = _trial("t10", [
        _sr("CTS", status="failed", elapsed=3600.0, qor={},
            failure=FailureClass.TIMEOUT),
    ])
    obs = build_minimal_observation(t_timeout, "CTS")
    check(obs.failure_type == "timeout",
          f"timeout failure_type: {obs.failure_type}")

    # =====================================================================
    # 12. Checkpoint matches decision_stage
    # =====================================================================
    cp_pl = _cp("cp-abc-PL", "PL")
    t_cp = _trial("t11", [_sr("PL")], checkpoint=cp_pl)
    obs = build_minimal_observation(t_cp, "PL")
    check(obs.checkpoint_id == "cp-abc-PL",
          f"checkpoint matched: {obs.checkpoint_id}")

    # =====================================================================
    # 13. Checkpoint stage mismatch → checkpoint_id=None
    # =====================================================================
    cp_cts = _cp("cp-abc-CTS", "CTS")
    t_cp_mismatch = _trial("t12", [_sr("PL")], checkpoint=cp_cts)
    obs = build_minimal_observation(t_cp_mismatch, "PL")
    check(obs.checkpoint_id is None,
          f"checkpoint mismatch → None, got {obs.checkpoint_id}")

    # =====================================================================
    # 14. No checkpoint
    # =====================================================================
    t_no_cp = _trial("t13", [_sr("PL")])
    obs = build_minimal_observation(t_no_cp, "PL")
    check(obs.checkpoint_id is None,
          f"no checkpoint → None, got {obs.checkpoint_id}")

    # =====================================================================
    # 15. FailureClass.NONE → failure_type=None
    # =====================================================================
    t_none_fail = _trial("t14", [
        _sr("PL", failure=FailureClass.NONE),
    ])
    obs = build_minimal_observation(t_none_fail, "PL")
    check(obs.failure_type is None,
          f"NONE → None, got {obs.failure_type!r}")

    # =====================================================================
    # 16. QOR_INCOMPLETE failure
    # =====================================================================
    t_qor_inc = _trial("t15", [
        _sr("PL", status="failed", failure=FailureClass.QOR_INCOMPLETE),
    ])
    obs = build_minimal_observation(t_qor_inc, "PL")
    check(obs.failure_type == "qor_incomplete",
          f"qor_incomplete: {obs.failure_type}")

    # =====================================================================
    # 17. Skipped stage still produces observation
    # =====================================================================
    t_skip = _trial("t16", [
        _sr("PL", status="skipped", elapsed=0.0),
    ])
    obs = build_minimal_observation(t_skip, "PL")
    check(obs.status == "skipped", f"skipped status: {obs.status}")

    # =====================================================================
    # 18. parent_trial_id None
    # =====================================================================
    t_no_parent = _trial("t17", [_sr("PL")], parent=None)
    obs = build_minimal_observation(t_no_parent, "PL")
    check(obs.parent_trial_id is None,
          f"no parent → None, got {obs.parent_trial_id}")

    # =====================================================================
    # 19. Numeric tag sorting: 10_1 > 2_1; tie-break by full tag string
    # =====================================================================
    wns, tns = _extract_timing({
        "2_1_floorplan_ws_ps": -500.0,
        "2_1_floorplan_tns_ps": -10000.0,
        "10_1_cts_ws_ps": -100.0,
        "10_1_cts_tns_ps": -2000.0,
    })
    check(wns == -100.0,
          f"numeric sort: 10_1 > 2_1, expected ws=-100.0, got {wns}")
    check(tns == -2000.0,
          f"numeric sort: expected tns=-2000.0, got {tns}")
    # Same numeric prefix → tag string tie-break.
    wns, tns = _extract_timing({
        "3_1_a_ws_ps": -100.0,
        "3_1_a_tns_ps": -500.0,
        "3_1_z_ws_ps": -200.0,
        "3_1_z_tns_ps": -1000.0,
    })
    check(wns == -200.0,
          f"string tie-break: 3_1_z > 3_1_a, expected ws=-200.0, got {wns}")
    check(tns == -1000.0,
          f"string tie-break: expected tns=-1000.0, got {tns}")

    # =====================================================================
    # 20. TrialRecord not modified
    # =====================================================================
    import copy
    t_orig = _trial("t18", [
        _sr("PL", qor={"3_5_place_dp_ws_ps": -100.0,
                        "3_5_place_dp_tns_ps": -500.0}),
    ])
    t_copy = copy.deepcopy(t_orig)
    build_minimal_observation(t_orig, "PL")
    # Compare via repr (simplified).
    check(t_orig.trial_id == t_copy.trial_id, "trial_id unchanged")
    check(t_orig.stage_results[0].stage_qor
          == t_copy.stage_results[0].stage_qor,
          "stage_qor unchanged")

    # -- Summary --
    total = ok + fail
    print(f"\n{'='*50}")
    print(f"  {ok}/{total} passed"
          + (f", {fail} FAILED" if fail else " — ALL OK"))
    print(f"{'='*50}")
    sys.exit(1 if fail else 0)
