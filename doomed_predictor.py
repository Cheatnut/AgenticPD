# -*- coding: utf-8 -*-
"""doomed_predictor.py — Stage D: rule-based Doomed Run predictor.

Pure Python, no I/O, no ORFS, no LLM, no side effects.

Input:  a cohort (list of MinimalObservation) at a single decision stage
        (PL or CTS), plus a survivor_count.

Output: a list of DoomedDecision, one per observation, with deterministic
        risk_class, risk_score, and reason_codes.

Rules (applied in order):
  1. hard_dead — any of:
       - status == "failed"
       - failure_type == "timeout"
       - stage_wns_ps is None AND stage_tns_ps is None  (necessary timing missing)
       - checkpoint_id is None                           (no resumable artifact)
  2. Sort remaining (non-hard-dead) by:
       WNS descending → TNS descending → trial_id ascending (stable).
  3. Top survivor_count → "survivor"; rest → "soft_bad".
  4. risk_score is a deterministic, linearly-scaled relative rank within
     the non-hard-dead cohort (1.0 = best, 0.0 = worst / hard_dead).
"""

from __future__ import annotations

import logging
from typing import List

from schemas.trial import MinimalObservation, DoomedDecision

log = logging.getLogger(__name__)

# Default rule version — bump when the classification or ranking logic changes.
DEFAULT_RULE_VERSION = "1.0.0"


def predict(
    cohort: List[MinimalObservation],
    survivor_count: int,
    rule_version: str = DEFAULT_RULE_VERSION,
) -> List[DoomedDecision]:
    """Classify every observation in *cohort* as hard_dead, soft_bad, or survivor.

    Args:
        cohort:         observations for a single decision stage (PL or CTS).
                        All entries must share the same ``.stage``.
        survivor_count: how many of the top-ranked non-hard-dead trials to
                        classify as ``survivor``.  If survivor_count ≥ number
                        of non-hard-dead trials, all survivors are promoted.
        rule_version:   version string recorded in every output decision.

    Returns:
        One DoomedDecision per input observation, in the same order as
        *cohort*.  The caller is responsible for pairing results back to
        trials.  The returned list is always the same length as *cohort*.

    Determinism:
        Same cohort + same survivor_count + same rule_version produces
        identical results every call.  There is no randomness.
    """
    if not cohort:
        return []

    # ------------------------------------------------------------------
    # Input validation
    # ------------------------------------------------------------------
    _VALID_STAGES = frozenset({"PL", "CTS"})

    # bool is a subclass of int — reject it explicitly.
    if not isinstance(survivor_count, int) or isinstance(survivor_count, bool):
        raise ValueError(
            f"survivor_count must be an int, got {type(survivor_count).__name__} "
            f"{survivor_count!r}")
    if survivor_count < 0:
        raise ValueError(
            f"survivor_count must be non-negative, got {survivor_count}")

    stages = {obs.stage for obs in cohort}
    if len(stages) != 1:
        raise ValueError(
            f"All observations must share the same stage, got {sorted(stages)}")
    stage = stages.pop()
    if stage not in _VALID_STAGES:
        raise ValueError(
            f"Stage must be PL or CTS, got {stage!r}")

    # ------------------------------------------------------------------
    # Phase 1 — classify hard_dead and collect reason codes
    # ------------------------------------------------------------------
    hard_dead: List[tuple] = []       # (index, observation, reason_codes)
    candidates: List[tuple] = []       # (index, observation)

    for i, obs in enumerate(cohort):
        reasons: List[str] = []

        if obs.status == "failed":
            reasons.append("stage_failed")
        if obs.failure_type == "timeout":
            reasons.append("timeout")
        # Necessary timing: at least one of WNS/TNS must be present.
        if obs.stage_wns_ps is None and obs.stage_tns_ps is None:
            reasons.append("timing_missing")
        if obs.checkpoint_id is None:
            reasons.append("checkpoint_missing")

        if reasons:
            hard_dead.append((i, obs, reasons))
        else:
            candidates.append((i, obs))

    # ------------------------------------------------------------------
    # Phase 2 — sort non-hard-dead by WNS ↘, TNS ↘, trial_id ↗
    # ------------------------------------------------------------------
    def _sort_key(item: tuple) -> tuple:
        _i, obs = item
        # Sort descending: negate so larger WNS/TNS come first.
        # None handling is unnecessary here — candidates already passed
        # the timing_missing check, but guard anyway.
        wns = obs.stage_wns_ps if obs.stage_wns_ps is not None else float("-inf")
        tns = obs.stage_tns_ps if obs.stage_tns_ps is not None else float("-inf")
        return (-wns, -tns, obs.trial_id)

    candidates.sort(key=_sort_key)

    # ------------------------------------------------------------------
    # Phase 3 — assign survivor / soft_bad and compute risk_score
    # ------------------------------------------------------------------
    n = len(candidates)
    survivor_count = min(survivor_count, n)  # clamp upper bound only

    # Build a lookup: original_index → DoomedDecision
    results_map: dict = {}

    # Hard-dead entries
    for i, obs, reasons in hard_dead:
        results_map[i] = DoomedDecision(
            risk_class="hard_dead",
            risk_score=0.0,
            reason_codes=sorted(reasons),
            rule_version=rule_version,
            input_evidence={
                "trial_id": obs.trial_id,
                "stage": obs.stage,
                "status": obs.status,
                "stage_wns_ps": obs.stage_wns_ps,
                "stage_tns_ps": obs.stage_tns_ps,
                "checkpoint_id": obs.checkpoint_id,
                "cohort_size": len(cohort),
                "survivor_count": survivor_count,
            },
        )

    # Non-hard-dead entries
    for rank, (i, obs) in enumerate(candidates):
        is_survivor = rank < survivor_count
        risk_class = "survivor" if is_survivor else "soft_bad"

        # Linear risk_score: 1.0 (best) → 0.0 (worst among candidates).
        # Hard-dead always get 0.0.
        if n <= 1:
            risk_score = 1.0
        else:
            risk_score = round(1.0 - (rank / (n - 1)), 4)

        reasons = []
        if is_survivor:
            reasons.append("survivor")
        if not is_survivor:
            reasons.append("rank_low")

        results_map[i] = DoomedDecision(
            risk_class=risk_class,
            risk_score=risk_score,
            reason_codes=sorted(reasons),
            rule_version=rule_version,
            input_evidence={
                "trial_id": obs.trial_id,
                "stage": obs.stage,
                "status": obs.status,
                "stage_wns_ps": obs.stage_wns_ps,
                "stage_tns_ps": obs.stage_tns_ps,
                "cohort_size": len(cohort),
                "survivor_count": survivor_count,
                "rank": rank,
            },
        )

    # ------------------------------------------------------------------
    # Return in original cohort order
    # ------------------------------------------------------------------
    return [results_map[i] for i in range(len(cohort))]


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

    # -- Empty cohort --
    results = predict([], survivor_count=2)
    check(len(results) == 0, "empty cohort → empty results")

    obs_ok = MinimalObservation(trial_id="a", stage="PL", status="ok",
                               stage_wns_ps=-50.0, checkpoint_id="cp-a")

    # -- Input validation: negative survivor_count --
    try:
        predict([obs_ok], survivor_count=-1)
        check(False, "negative survivor_count should raise ValueError")
    except ValueError as e:
        check("non-negative" in str(e).lower(), f"negative survivor_count message: {e}")

    # -- Input validation: float survivor_count --
    try:
        predict([obs_ok], survivor_count=1.5)
        check(False, "float survivor_count should raise ValueError")
    except ValueError as e:
        check("must be an int" in str(e).lower(), f"float survivor_count message: {e}")

    # -- Input validation: string survivor_count --
    try:
        predict([obs_ok], survivor_count="1")
        check(False, "string survivor_count should raise ValueError")
    except ValueError as e:
        check("must be an int" in str(e).lower(), f"string survivor_count message: {e}")

    # -- Input validation: bool survivor_count --
    try:
        predict([obs_ok], survivor_count=True)
        check(False, "bool survivor_count should raise ValueError")
    except ValueError as e:
        check("must be an int" in str(e).lower(), f"bool survivor_count message: {e}")

    # -- Input validation: mixed stages --
    cohort_mixed_stage = [
        MinimalObservation(trial_id="a", stage="PL", status="ok",
                          stage_wns_ps=-50.0, checkpoint_id="cp-a"),
        MinimalObservation(trial_id="b", stage="CTS", status="ok",
                          stage_wns_ps=-100.0, checkpoint_id="cp-b"),
    ]
    try:
        predict(cohort_mixed_stage, survivor_count=1)
        check(False, "mixed stages should raise ValueError")
    except ValueError as e:
        check("same stage" in str(e).lower(), f"mixed stage message: {e}")

    # -- Input validation: invalid stage --
    obs_invalid_stage = MinimalObservation(trial_id="a", stage="RT", status="ok",
                                          stage_wns_ps=-50.0, checkpoint_id="cp-a")
    try:
        predict([obs_invalid_stage], survivor_count=1)
        check(False, "invalid stage RT should raise ValueError")
    except ValueError as e:
        check("PL or CTS" in str(e), f"invalid stage message: {e}")

    # -- All hard_dead (stage failed) --
    cohort_failed = [
        MinimalObservation(trial_id="a", stage="PL", status="failed",
                          failure_type="tool_crash"),
        MinimalObservation(trial_id="b", stage="PL", status="failed",
                          failure_type="timeout"),
    ]
    results = predict(cohort_failed, survivor_count=1)
    check(len(results) == 2, "all failed cohort → 2 results")
    for r in results:
        check(r.risk_class == "hard_dead", f"failed trial {r.input_evidence['trial_id']} → hard_dead")
        check(r.risk_score == 0.0, "hard_dead risk_score = 0.0")

    # -- All survivors when survivor_count >= N --
    cohort_ok = [
        MinimalObservation(trial_id="a", stage="PL", status="ok",
                          stage_wns_ps=-100.0, stage_tns_ps=-200.0,
                          checkpoint_id="cp-a-PL"),
        MinimalObservation(trial_id="b", stage="PL", status="ok",
                          stage_wns_ps=-50.0, stage_tns_ps=-100.0,
                          checkpoint_id="cp-b-PL"),
    ]
    results = predict(cohort_ok, survivor_count=5)
    check(len(results) == 2, "all ok → 2 results")
    for r in results:
        check(r.risk_class == "survivor",
              f"survivor_count=5 with 2 candidates → {r.risk_class}")
    # Best (b, WNS=-50) should have higher risk_score
    check(results[1].risk_score > results[0].risk_score,
          f"b (WNS=-50) risk {results[1].risk_score} > a (WNS=-100) risk {results[0].risk_score}")

    # -- Mixed: soft_bad when survivor_count < N --
    cohort_mixed = [
        MinimalObservation(trial_id="a", stage="PL", status="ok",
                          stage_wns_ps=-100.0, stage_tns_ps=-200.0,
                          checkpoint_id="cp-a-PL"),
        MinimalObservation(trial_id="b", stage="PL", status="ok",
                          stage_wns_ps=-50.0, stage_tns_ps=-100.0,
                          checkpoint_id="cp-b-PL"),
        MinimalObservation(trial_id="c", stage="PL", status="ok",
                          stage_wns_ps=-200.0, stage_tns_ps=-500.0,
                          checkpoint_id="cp-c-PL"),
    ]
    results = predict(cohort_mixed, survivor_count=1)
    check(len(results) == 3, "mixed cohort → 3 results")
    # Sort order: b(-50) > a(-100) > c(-200). Only b is survivor.
    check(results[1].risk_class == "survivor",
          f"b (best WNS) → survivor, got {results[1].risk_class}")
    check(results[1].risk_score == 1.0, "best candidate risk_score = 1.0")
    check(results[0].risk_class == "soft_bad",
          f"a (middle) → soft_bad, got {results[0].risk_class}")
    check(results[2].risk_class == "soft_bad",
          f"c (worst) → soft_bad, got {results[2].risk_class}")
    check(results[2].risk_score == 0.0, "worst candidate risk_score = 0.0")

    # -- Timing missing → hard_dead --
    obs_no_timing = MinimalObservation(
        trial_id="x", stage="PL", status="ok",
        stage_wns_ps=None, stage_tns_ps=None,
        checkpoint_id="cp-x-PL",
    )
    results = predict([obs_no_timing], survivor_count=1)
    check(results[0].risk_class == "hard_dead", "timing both None → hard_dead")
    check("timing_missing" in results[0].reason_codes, "reason_codes includes timing_missing")

    # -- Checkpoint missing → hard_dead --
    obs_no_cp = MinimalObservation(
        trial_id="x", stage="PL", status="ok",
        stage_wns_ps=-50.0, stage_tns_ps=-100.0,
        checkpoint_id=None,
    )
    results = predict([obs_no_cp], survivor_count=1)
    check(results[0].risk_class == "hard_dead", "no checkpoint → hard_dead")
    check("checkpoint_missing" in results[0].reason_codes, "reason_codes includes checkpoint_missing")

    # -- Determinism: same input → same output --
    results_a = predict(cohort_mixed, survivor_count=1)
    results_b = predict(cohort_mixed, survivor_count=1)
    for i in range(len(cohort_mixed)):
        check(results_a[i].risk_class == results_b[i].risk_class,
              f"determinism: same risk_class for idx {i}")
        check(results_a[i].risk_score == results_b[i].risk_score,
              f"determinism: same risk_score for idx {i}")
        check(results_a[i].reason_codes == results_b[i].reason_codes,
              f"determinism: same reason_codes for idx {i}")

    # -- Tie-breaking: same WNS/TNS → trial_id order --
    cohort_tie = [
        MinimalObservation(trial_id="c", stage="PL", status="ok",
                          stage_wns_ps=-100.0, stage_tns_ps=-200.0,
                          checkpoint_id="cp-c-PL"),
        MinimalObservation(trial_id="a", stage="PL", status="ok",
                          stage_wns_ps=-100.0, stage_tns_ps=-200.0,
                          checkpoint_id="cp-a-PL"),
        MinimalObservation(trial_id="b", stage="PL", status="ok",
                          stage_wns_ps=-100.0, stage_tns_ps=-200.0,
                          checkpoint_id="cp-b-PL"),
    ]
    results = predict(cohort_tie, survivor_count=1)
    # Sorted: a < b < c (alphabetical trial_id). "a" (idx 1) should be survivor.
    check(results[1].risk_class == "survivor",
          f"tie-break: trial_id 'a' (idx 1) should be survivor, got {results[1].risk_class}")
    check(results[2].risk_class == "soft_bad", "trial_id 'b' → soft_bad")
    check(results[0].risk_class == "soft_bad", "trial_id 'c' → soft_bad")

    # -- Multiple reasons: failed + no checkpoint --
    obs_multi = MinimalObservation(
        trial_id="x", stage="PL", status="failed",
        failure_type="tool_crash",
        stage_wns_ps=None, stage_tns_ps=None,
        checkpoint_id=None,
    )
    results = predict([obs_multi], survivor_count=1)
    reasons = results[0].reason_codes
    check("stage_failed" in reasons, "multiple: stage_failed")
    check("timing_missing" in reasons, "multiple: timing_missing")
    check("checkpoint_missing" in reasons, "multiple: checkpoint_missing")

    # -- survivor_count=0 → all non-hard-dead become soft_bad --
    results = predict(cohort_ok, survivor_count=0)
    for r in results:
        check(r.risk_class == "soft_bad",
              f"survivor_count=0 → all soft_bad, got {r.risk_class}")

    # -- WNS present but TNS None → still valid (not hard_dead) --
    obs_wns_only = MinimalObservation(
        trial_id="x", stage="PL", status="ok",
        stage_wns_ps=-50.0, stage_tns_ps=None,
        checkpoint_id="cp-x-PL",
    )
    results = predict([obs_wns_only], survivor_count=1)
    check(results[0].risk_class == "survivor",
          f"WNS only → valid, got {results[0].risk_class}")

    # -- TNS present but WNS None → still valid --
    obs_tns_only = MinimalObservation(
        trial_id="x", stage="PL", status="ok",
        stage_wns_ps=None, stage_tns_ps=-100.0,
        checkpoint_id="cp-x-PL",
    )
    results = predict([obs_tns_only], survivor_count=1)
    check(results[0].risk_class == "survivor",
          f"TNS only → valid, got {results[0].risk_class}")

    # -- Summary --
    total = ok + fail
    print(f"\n{'='*50}")
    print(f"  {ok}/{total} passed" + (f", {fail} FAILED" if fail else " — ALL OK"))
    print(f"{'='*50}")
    sys.exit(1 if fail else 0)
