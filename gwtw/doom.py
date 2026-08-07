# -*- coding: utf-8 -*-
"""doomed_predictor.py — rule-based Doomed Run predictor.

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

from core.models import MinimalObservation, DoomedDecision

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

