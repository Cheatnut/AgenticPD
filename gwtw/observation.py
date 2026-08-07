# -*- coding: utf-8 -*-
"""observation_builder.py — pure MinimalObservation builder.

Pure Python, no I/O, no ORFS, no LLM, no side effects.

Input:  a TrialRecord and a decision_stage (PL or CTS).

Output: a MinimalObservation populated from the TrialRecord's
        corresponding StageResult.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Set, Tuple

from core.models import (
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

