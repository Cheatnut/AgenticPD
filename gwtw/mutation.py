# -*- coding: utf-8 -*-
"""pure downstream mutation planner.

Pure Python, no I/O, no ORFS, no LLM, no side effects.

Input:  a ForkRequest (from GWTW Scheduler), the parent's resolved
        params, and a seed.

Output: child params (deep copy of parent with exactly ONE parameter
        changed) plus MutationEvidence recording what was changed.

Rules:
  1. Legal parameters are derived solely from ``config.PARAM_SPACE`` and
     ``ParamSpec.affects`` — no second parameter table.
  2. A parameter is legal for a checkpoint at stage S iff its
     ``affects`` earliest stage is strictly after S (i.e., changing it
     does not invalidate the checkpoint at S or any earlier stage).
  3. Each child changes exactly ONE parameter, to a value within
     [vmin, vmax] and different from the parent's value.
  4. Same input + seed → deterministic output.  Parent params are never
     modified.
  5. No legal mutation possible → ``NoLegalMutationError``.
"""

from __future__ import annotations

import copy
import logging
import random
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from config import PARAM_SPACE, ParamSpec
from gwtw.scheduler import ForkRequest

log = logging.getLogger(__name__)

DEFAULT_PLANNER_VERSION = "1.0.0"

# Stage → index for ordering; used to determine whether a param's
# *affects* are strictly downstream of a checkpoint.
_STAGE_ORDER: Dict[str, int] = {"FP": 0, "PL": 1, "CTS": 2, "RT": 3}
# Decision stages where a checkpoint can be consumed for forking.
_VALID_CHECKPOINT_STAGES = frozenset({"PL", "CTS"})


# =============================================================================
# Data classes / exceptions
# =============================================================================


@dataclass
class MutationEvidence:
    """What was changed and why — audit trail for a single child mutation.

    Attributes:
        param_name:  name of the parameter that was changed.
        old_value:   parent's value (``None`` if the parameter was not
                     explicitly set in the parent).
        new_value:   new value assigned to the child.
        stage:       owning stage of the parameter (from ParamSpec).
        affects:     stages invalidated by this change.
        reason:      human-readable explanation of the mutation.
    """

    param_name: str
    old_value: Any
    new_value: Any
    stage: str
    affects: tuple
    reason: str


class NoLegalMutationError(Exception):
    """Raised when no legal single-parameter change is possible.

    This means every parameter that is legal for the checkpoint stage
    is either at a range boundary (cannot choose a different value) or
    there are simply no legal parameters at all.
    """

    def __init__(self, parent_trial_id: str, decision_stage: str,
                 detail: str) -> None:
        self.parent_trial_id = parent_trial_id
        self.decision_stage = decision_stage
        self.detail = detail
        super().__init__(
            f"No legal mutation for fork from {parent_trial_id!r} "
            f"at {decision_stage}: {detail}"
        )


# =============================================================================
# Core planner
# =============================================================================


def plan_child_params(
    fork_request: ForkRequest,
    parent_params: Dict[str, Dict[str, Any]],
    seed: int,
    planner_version: str = DEFAULT_PLANNER_VERSION,
) -> Tuple[Dict[str, Dict[str, Any]], MutationEvidence]:
    """Generate child params with exactly one parameter changed.

    Args:
        fork_request:    from GWTW Scheduler; carries ``parent_trial_id``
                         and ``decision_stage`` (PL or CTS).
        parent_params:   resolved parameters of the parent trial as
                         ``{stage: {param_name: value}}``.  **Not mutated.**
        seed:            integer seed for deterministic parameter and
                         value selection.
        planner_version: version string recorded in evidence reason.

    Returns:
        ``(child_params, evidence)``.  *child_params* is a deep copy of
        *parent_params* with exactly one parameter value changed.
        *evidence* records the mutation for the audit trail.

    Raises:
        ValueError:           invalid decision_stage in fork_request.
        NoLegalMutationError: no legal parameter can be changed without
                              invalidating the checkpoint.
    """
    decision_stage = fork_request.decision_stage  # "PL" or "CTS"

    # -- validate decision_stage --
    if decision_stage not in _VALID_CHECKPOINT_STAGES:
        raise ValueError(
            f"fork_request.decision_stage must be PL or CTS, "
            f"got {decision_stage!r}"
        )

    # -- validate seed --
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ValueError(
            f"seed must be an int, got {type(seed).__name__} {seed!r}"
        )

    # ------------------------------------------------------------------
    # 1. Collect legal parameters for this checkpoint stage
    # ------------------------------------------------------------------
    checkpoint_idx = _STAGE_ORDER[decision_stage]
    legal_specs: List[ParamSpec] = []

    for specs in PARAM_SPACE.values():
        for spec in specs:
            earliest_affected = min(
                (_STAGE_ORDER[s] for s in spec.affects),
                default=_STAGE_ORDER["RT"],
            )
            if earliest_affected > checkpoint_idx:
                legal_specs.append(spec)

    if not legal_specs:
        raise NoLegalMutationError(
            fork_request.parent_trial_id,
            decision_stage,
            f"no parameters are legal after {decision_stage} checkpoint",
        )

    # ------------------------------------------------------------------
    # 2. Flatten parent params, filter to params with viable alternatives
    # ------------------------------------------------------------------
    parent_flat: Dict[str, Any] = {}
    for stage_params in parent_params.values():
        parent_flat.update(stage_params)

    rng = random.Random(seed)
    candidates: List[Tuple[ParamSpec, Any]] = []  # (spec, old_value)

    for spec in legal_specs:
        old_val = parent_flat.get(spec.name)
        if _has_alternative(spec, old_val):
            candidates.append((spec, old_val))

    if not candidates:
        raise NoLegalMutationError(
            fork_request.parent_trial_id,
            decision_stage,
            f"none of the {len(legal_specs)} legal parameters have an "
            f"alternative value within range",
        )

    # ------------------------------------------------------------------
    # 3. Deterministic selection: sort by name → seed-pick param → seed-pick value
    # ------------------------------------------------------------------
    candidates.sort(key=lambda c: c[0].name)
    chosen_idx = rng.randint(0, len(candidates) - 1)
    chosen_spec, old_val = candidates[chosen_idx]

    new_val = _pick_new_value(chosen_spec, old_val, rng)

    # ------------------------------------------------------------------
    # 4. Build child params (deep copy + one change)
    # ------------------------------------------------------------------
    child_params = copy.deepcopy(parent_params)
    child_params.setdefault(chosen_spec.stage, {})[chosen_spec.name] = new_val

    # ------------------------------------------------------------------
    # 5. Build evidence
    # ------------------------------------------------------------------
    evidence = MutationEvidence(
        param_name=chosen_spec.name,
        old_value=old_val,
        new_value=new_val,
        stage=chosen_spec.stage,
        affects=chosen_spec.affects,
        reason=(
            f"[{planner_version}] {decision_stage} checkpoint fork from "
            f"{fork_request.parent_trial_id}: changed {chosen_spec.name} "
            f"({old_val!r} → {new_val!r}); "
            f"affects {list(chosen_spec.affects)}"
        ),
    )

    return child_params, evidence


# =============================================================================
# Helpers
# =============================================================================


def _has_alternative(spec: ParamSpec, old_val: Any) -> bool:
    """Return True if *spec* can be set to a value different from *old_val*."""
    if spec.ptype == "int":
        lo, hi = int(spec.vmin), int(spec.vmax)
        if old_val is None:
            return hi >= lo  # at least one valid integer exists
        if lo <= old_val <= hi:
            return (hi - lo) >= 1  # at least one other integer exists
        return True  # old_val outside [lo, hi] → any value in range differs

    if spec.ptype == "float":
        if spec.vmin >= spec.vmax:
            return False  # degenerate range
        if old_val is None:
            return True  # any float in range is a change
        return True  # infinite alternatives in [vmin, vmax]

    return False


def _pick_new_value(spec: ParamSpec, old_val: Any,
                    rng: random.Random) -> Any:
    """Generate a new value different from *old_val* using *rng*.

    Precondition: ``_has_alternative(spec, old_val)`` is True.
    """
    if spec.ptype == "int":
        lo, hi = int(spec.vmin), int(spec.vmax)
        options = [v for v in range(lo, hi + 1) if v != old_val]
        # Should never be empty — caller checked _has_alternative.
        return rng.choice(options)

    # Float: uniform sample in [vmin, vmax]; guard against exact match.
    for _ in range(100):
        v = round(rng.uniform(spec.vmin, spec.vmax), 4)
        if v != old_val:
            return v
    # Extremely unlikely fallback — offset from boundary.
    v = spec.vmin if old_val != spec.vmin else spec.vmax
    return round(v, 4)


# =============================================================================
# Public helpers (for test introspection)
# =============================================================================


def legal_param_names(decision_stage: str) -> List[str]:
    """Return sorted names of legal params for *decision_stage*.

    Convenience for tests: see exactly which parameters a checkpoint
    stage allows without calling ``plan_child_params``.
    """
    if decision_stage not in _VALID_CHECKPOINT_STAGES:
        raise ValueError(
            f"decision_stage must be PL or CTS, got {decision_stage!r}"
        )
    checkpoint_idx = _STAGE_ORDER[decision_stage]
    names: List[str] = []
    for specs in PARAM_SPACE.values():
        for spec in specs:
            earliest = min(
                (_STAGE_ORDER[s] for s in spec.affects),
                default=_STAGE_ORDER["RT"],
            )
            if earliest > checkpoint_idx:
                names.append(spec.name)
    names.sort()
    return names


# =============================================================================
# Self-test
# =============================================================================

