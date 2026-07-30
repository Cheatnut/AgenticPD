# -*- coding: utf-8 -*-
"""mutation_planner.py — Stage D: pure downstream mutation planner.

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
from gwtw_scheduler import ForkRequest

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
    def _fr(parent_id, stage):
        return ForkRequest(
            parent_trial_id=parent_id, decision_stage=stage,
            reason="population_replenishment",
        )

    # Baseline-like parent params (matches BASELINE_PARAMS).
    BASELINE = {
        "FP": {"CORE_UTILIZATION": 38, "CORE_ASPECT_RATIO": 1.0},
        "PL": {},
        "CTS": {},
        "RT": {"FASTROUTE_LAYER_ADJUSTMENT": 0.2,
               "GRT_CONGESTION_ITERATIONS": 30},
    }

    # =====================================================================
    # Validation
    # =====================================================================
    try:
        plan_child_params(_fr("p1", "RT"), BASELINE, seed=0)
        check(False, "RT decision_stage should raise ValueError")
    except ValueError as e:
        check("PL or CTS" in str(e), f"RT stage msg: {e}")

    try:
        plan_child_params(_fr("p1", "FP"), BASELINE, seed=0)
        check(False, "FP decision_stage should raise ValueError")
    except ValueError as e:
        check("PL or CTS" in str(e), f"FP stage msg: {e}")

    try:
        plan_child_params(_fr("p1", "PL"), BASELINE, seed=True)
        check(False, "bool seed should raise ValueError")
    except ValueError as e:
        check("must be an int" in str(e).lower(), f"bool seed msg: {e}")

    # =====================================================================
    # PL checkpoint: legal params
    # =====================================================================
    pl_legal = legal_param_names("PL")
    check("CTS_CLUSTER_SIZE" in pl_legal,
          f"PL legal includes CTS_CLUSTER_SIZE, got {pl_legal}")
    check("CTS_CLUSTER_DIAMETER" in pl_legal,
          f"PL legal includes CTS_CLUSTER_DIAMETER, got {pl_legal}")
    check("GRT_CONGESTION_ITERATIONS" in pl_legal,
          f"PL legal includes GRT_CONGESTION_ITERATIONS, got {pl_legal}")
    # Excluded: affects FP/PL.
    check("CORE_UTILIZATION" not in pl_legal,
          "CORE_UTILIZATION excluded from PL (affects FP)")
    check("PLACE_DENSITY_LB_ADDON" not in pl_legal,
          "PLACE_DENSITY_LB_ADDON excluded from PL (affects PL)")
    check("SETUP_SLACK_MARGIN" not in pl_legal,
          "SETUP_SLACK_MARGIN excluded from PL (affects FP/PL/CTS/RT)")
    check("FASTROUTE_LAYER_ADJUSTMENT" not in pl_legal,
          "FASTROUTE_LAYER_ADJUSTMENT excluded from PL (affects FP/PL/CTS/RT)")

    # =====================================================================
    # CTS checkpoint: only GRT_CONGESTION_ITERATIONS
    # =====================================================================
    cts_legal = legal_param_names("CTS")
    check(cts_legal == ["GRT_CONGESTION_ITERATIONS"],
          f"CTS legal = [GRT_CONGESTION_ITERATIONS], got {cts_legal}")
    check("CTS_CLUSTER_SIZE" not in cts_legal,
          "CTS_CLUSTER_SIZE excluded from CTS (affects CTS)")
    check("SETUP_SLACK_MARGIN" not in cts_legal,
          "SETUP_SLACK_MARGIN excluded from CTS")
    check("FASTROUTE_LAYER_ADJUSTMENT" not in cts_legal,
          "FASTROUTE_LAYER_ADJUSTMENT excluded from CTS")

    # =====================================================================
    # PL checkpoint: generate child
    # =====================================================================
    child, ev = plan_child_params(_fr("p1", "PL"), BASELINE, seed=42)
    # Child must differ from parent in exactly one param.
    diffs = []
    for stage in ["FP", "PL", "CTS", "RT"]:
        for k, v in child.get(stage, {}).items():
            pv = BASELINE.get(stage, {}).get(k)
            if v != pv:
                diffs.append((k, pv, v))
    check(len(diffs) == 1, f"exactly 1 change, got {len(diffs)}: {diffs}")
    changed_name, old, new = diffs[0]
    check(changed_name == ev.param_name,
          f"evidence param {ev.param_name} matches changed {changed_name}")
    check(old == ev.old_value,
          f"evidence old {ev.old_value} matches {old}")
    check(new == ev.new_value,
          f"evidence new {ev.new_value} matches {new}")
    check(changed_name in pl_legal,
          f"changed param {changed_name} is PL-legal")
    # New value within range.
    spec = [s for specs in PARAM_SPACE.values() for s in specs
            if s.name == changed_name][0]
    check(spec.vmin <= new <= spec.vmax,
          f"new value {new} in [{spec.vmin}, {spec.vmax}]")
    # New value differs from old.
    check(new != old, f"new {new} != old {old}")

    # =====================================================================
    # CTS checkpoint: generate child — only GRT_CONGESTION_ITERATIONS
    # =====================================================================
    child, ev = plan_child_params(_fr("p2", "CTS"), BASELINE, seed=99)
    check(ev.param_name == "GRT_CONGESTION_ITERATIONS",
          f"CTS fork changes GRT_CONGESTION_ITERATIONS, got {ev.param_name}")
    check(ev.old_value == 30,
          f"old GRT_CONGESTION_ITERATIONS = 30, got {ev.old_value}")
    check(ev.new_value != 30,
          f"new GRT_CONGESTION_ITERATIONS != 30, got {ev.new_value}")
    check(10 <= ev.new_value <= 50,
          f"new in [10, 50], got {ev.new_value}")
    # Only RT changed, other stages untouched.
    for stage in ["FP", "PL", "CTS"]:
        check(child[stage] == BASELINE[stage],
              f"stage {stage} unchanged")
    check(child["RT"]["FASTROUTE_LAYER_ADJUSTMENT"] == 0.2,
          "FASTROUTE_LAYER_ADJUSTMENT unchanged")
    check(child["RT"]["GRT_CONGESTION_ITERATIONS"] != 30,
          "GRT_CONGESTION_ITERATIONS changed")

    # =====================================================================
    # Determinism: same input + same seed → same output
    # =====================================================================
    c1, e1 = plan_child_params(_fr("p1", "PL"), BASELINE, seed=123)
    c2, e2 = plan_child_params(_fr("p1", "PL"), BASELINE, seed=123)
    check(c1 == c2, "same child params")
    check(e1.param_name == e2.param_name, "same evidence param_name")
    check(e1.old_value == e2.old_value, "same evidence old_value")
    check(e1.new_value == e2.new_value, "same evidence new_value")

    # =====================================================================
    # Different seed → possibly different param/value (at least sometimes)
    # =====================================================================
    # With 3 legal params, different seeds should diverge often.
    results = set()
    for s in range(20):
        child, ev = plan_child_params(_fr("p1", "PL"), BASELINE, seed=s)
        results.add((ev.param_name, ev.new_value))
    check(len(results) > 1,
          f"different seeds → different results, got {len(results)} unique")

    # =====================================================================
    # Parent params not mutated
    # =====================================================================
    parent_copy = copy.deepcopy(BASELINE)
    plan_child_params(_fr("p1", "PL"), BASELINE, seed=0)
    check(BASELINE == parent_copy, "parent params unchanged after call")

    # =====================================================================
    # NoLegalMutationError: CTS checkpoint with all RT params at limits
    # =====================================================================
    # GRT_CONGESTION_ITERATIONS is the only legal param for CTS.
    # If it's the only value in range, NoLegalMutationError.
    single_val_parent = copy.deepcopy(BASELINE)
    single_val_parent["RT"]["GRT_CONGESTION_ITERATIONS"] = 30
    # Normal call still works: range 10-50 has many alternatives.
    child, ev = plan_child_params(
        _fr("p3", "CTS"), single_val_parent, seed=0,
    )
    check(ev.param_name == "GRT_CONGESTION_ITERATIONS",
          "CTS fork with normal range works")

    # =====================================================================
    # PL checkpoint with non-baseline parent (all PL-legal params set)
    # =====================================================================
    rich_parent = copy.deepcopy(BASELINE)
    rich_parent["CTS"]["CTS_CLUSTER_SIZE"] = 100
    rich_parent["CTS"]["CTS_CLUSTER_DIAMETER"] = 200
    rich_parent["RT"]["GRT_CONGESTION_ITERATIONS"] = 30
    child, ev = plan_child_params(_fr("p4", "PL"), rich_parent, seed=7)
    check(ev.param_name in pl_legal,
          f"changed {ev.param_name} is PL-legal")
    # Verify the changed param actually differs.
    old_pv = rich_parent.get(ev.stage, {}).get(ev.param_name)
    new_pv = child[ev.stage][ev.param_name]
    check(new_pv != old_pv,
          f"new value {new_pv} != old value {old_pv}")

    # =====================================================================
    # Evidence fields populated
    # =====================================================================
    child, ev = plan_child_params(_fr("p1", "PL"), BASELINE, seed=0)
    check(isinstance(ev.param_name, str) and ev.param_name,
          f"evidence param_name: {ev.param_name!r}")
    check(ev.stage in ["FP", "PL", "CTS", "RT"],
          f"evidence stage valid: {ev.stage}")
    check(isinstance(ev.affects, tuple), "evidence affects is tuple")
    check(isinstance(ev.reason, str) and len(ev.reason) > 0,
          "evidence reason non-empty")
    check(ev.new_value is not None, "new_value not None")
    check("1.0.0" in ev.reason, "planner version in reason")

    # =====================================================================
    # Int param: new value is int (not float)
    # =====================================================================
    child, ev = plan_child_params(_fr("p1", "PL"), BASELINE, seed=0)
    spec = [s for specs in PARAM_SPACE.values() for s in specs
            if s.name == ev.param_name][0]
    if spec.ptype == "int":
        check(isinstance(ev.new_value, int),
              f"int param {ev.param_name} → int value, got {type(ev.new_value).__name__}")
    elif spec.ptype == "float":
        check(isinstance(ev.new_value, float),
              f"float param {ev.param_name} → float value, got {type(ev.new_value).__name__}")

    # =====================================================================
    # Deep copy: child shares no inner dicts with parent
    # =====================================================================
    child, ev = plan_child_params(_fr("p1", "PL"), BASELINE, seed=0)
    child["FP"]["CORE_UTILIZATION"] = 99
    check(BASELINE["FP"]["CORE_UTILIZATION"] == 38,
          "parent not affected by child mutation")

    # =====================================================================
    # legal_param_names: invalid stage
    # =====================================================================
    try:
        legal_param_names("FP")
        check(False, "legal_param_names FP should raise")
    except ValueError as e:
        check("PL or CTS" in str(e), f"legal_param_names FP msg: {e}")

    # -- Summary --
    total = ok + fail
    print(f"\n{'='*50}")
    print(f"  {ok}/{total} passed"
          + (f", {fail} FAILED" if fail else " — ALL OK"))
    print(f"{'='*50}")
    sys.exit(1 if fail else 0)
