# -*- coding: utf-8 -*-
"""gwtw_scheduler.py — Stage D: rule-based GWTW (Go-With-The-Winners) Scheduler.

Pure Python, no I/O, no ORFS, no LLM, no side effects.

Input:  a cohort (list of MinimalObservation), the corresponding
        DoomedDecision list, quotas, population_size, parent descendant
        limit, and a seed.

Output: deterministic (List[GWTWDecision], List[ForkRequest]) for the
        cohort at a single decision stage (PL or CTS).

Rules (applied in order):
  1. hard_dead → action="pause"; cannot continue or be a fork parent.
  2. survivor → action="continue" (always); eligible as fork parent.
     Fork children are represented solely via ForkRequest — the
     survivor's own GWTWDecision.action never changes to "fork".
  3. soft_bad → top *audit_quota* by rank → "audit_continue";
     rest → "pause".
  4. Fork parents selected from survivors (not audit_continue).
     Children distributed round-robin up to *max_children_per_parent*
     each, using *seed* for deterministic tie-breaking.
  5. On success: active_count + fork_count MUST equal population_size.
     If active_count > population_size or parent capacity is
     insufficient → PopulationCapacityError.
  6. All hard_dead → AllHardDeadError raised (explicit experiment
     failure).
  7. Same input + same seed → identical output every call.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from typing import List, Tuple

from schemas.trial import MinimalObservation, DoomedDecision, GWTWDecision

log = logging.getLogger(__name__)

DEFAULT_SCHEDULER_VERSION = "1.0.0"


# =============================================================================
# Data classes / exceptions
# =============================================================================


@dataclass
class ForkRequest:
    """Minimal fork intent — the orchestrator creates the child Trial.

    Attributes:
        parent_trial_id: survivor trial to fork from.
        decision_stage:  PL or CTS — the stage at which this fork is
                         requested.
        reason:          machine-readable reason slug (e.g.
                         ``"population_replenishment"``).
    """

    parent_trial_id: str
    decision_stage: str
    reason: str


class AllHardDeadError(Exception):
    """Raised when every candidate in a cohort is hard_dead.

    The experiment cannot continue — no survivors to advance or fork from.
    """

    def __init__(self, stage: str, cohort_size: int) -> None:
        self.stage = stage
        self.cohort_size = cohort_size
        super().__init__(
            f"All {cohort_size} candidates at stage {stage} are hard_dead — "
            f"experiment failed"
        )


class PopulationCapacityError(Exception):
    """Raised when the scheduler cannot satisfy the population constraint.

    Two cases:
      - ``active_count > population_size`` — more active trials than the
        target can accommodate.
      - insufficient fork capacity — not enough parent slots to spawn the
        required number of children to reach *population_size*.
    """

    def __init__(
        self,
        reason: str,
        active_count: int,
        population_size: int,
        max_fork_capacity: int,
    ) -> None:
        self.reason = reason
        self.active_count = active_count
        self.population_size = population_size
        self.max_fork_capacity = max_fork_capacity
        super().__init__(
            f"Population capacity error: {reason} "
            f"(active={active_count}, target={population_size}, "
            f"max_fork_capacity={max_fork_capacity})"
        )


# =============================================================================
# Core scheduler
# =============================================================================


def schedule(
    cohort: List[MinimalObservation],
    doomed_decisions: List[DoomedDecision],
    survivor_count: int,
    audit_quota: int,
    population_size: int,
    max_children_per_parent: int,
    seed: int,
    scheduler_version: str = DEFAULT_SCHEDULER_VERSION,
) -> Tuple[List[GWTWDecision], List[ForkRequest]]:
    """Produce deterministic GWTW decisions and fork requests for a cohort.

    Args:
        cohort:                  observations for a single decision stage
                                 (PL or CTS).  All entries must share the
                                 same ``.stage``.
        doomed_decisions:        DoomedPredictor output, one per cohort
                                 entry, in the **same order** as *cohort*.
                                 Each entry's ``input_evidence`` MUST
                                 contain a ``trial_id`` key matching the
                                 corresponding observation.
        survivor_count:          how many top-ranked non-hard-dead entries
                                 the DoomedPredictor classified as survivor.
        audit_quota:             maximum number of soft_bad entries to
                                 promote to ``audit_continue``.
        population_size:         target number of active (non-paused)
                                 trials after scheduling.  Must be strictly
                                 positive.
        max_children_per_parent: maximum fork children a single survivor
                                 parent may spawn.
        seed:                    integer seed for deterministic
                                 tie-breaking and parent selection.
        scheduler_version:       version string recorded in every output
                                 decision.

    Returns:
        ``(decisions, fork_requests)`` where *decisions* has one
        ``GWTWDecision`` per cohort entry (same order) and *fork_requests*
        lists the children to spawn.

    Guarantee:
        ``active_count + len(fork_requests) == population_size`` on every
        successful return.

    Raises:
        ValueError:               input validation failure (length mismatch,
                                  trial_id mismatch, mixed/invalid stage,
                                  bad quota/seed types, missing trial_id in
                                  input_evidence).
        AllHardDeadError:         every candidate is hard_dead — experiment
                                  cannot proceed.
        PopulationCapacityError:  active_count exceeds population_size, or
                                  parent fork capacity is insufficient to
                                  reach population_size.
    """
    # ------------------------------------------------------------------
    # Input validation
    # ------------------------------------------------------------------
    _validate_inputs(
        cohort, doomed_decisions, survivor_count, audit_quota,
        population_size, max_children_per_parent, seed,
    )

    stage = cohort[0].stage
    n = len(cohort)

    # ------------------------------------------------------------------
    # Phase 1 — rank all entries by WNS ↘, TNS ↘, trial_id ↗
    # ------------------------------------------------------------------
    def _sort_key(idx: int) -> tuple:
        obs = cohort[idx]
        wns = obs.stage_wns_ps if obs.stage_wns_ps is not None else float("-inf")
        tns = obs.stage_tns_ps if obs.stage_tns_ps is not None else float("-inf")
        return (-wns, -tns, obs.trial_id)

    sorted_indices = sorted(range(n), key=_sort_key)
    rank_of: dict[int, int] = {idx: rank for rank, idx in enumerate(sorted_indices)}

    # ------------------------------------------------------------------
    # Phase 2 — count categories
    # ------------------------------------------------------------------
    hard_dead_indices: List[int] = []
    survivor_indices: List[int] = []
    soft_bad_indices: List[int] = []

    for i, dec in enumerate(doomed_decisions):
        if dec.risk_class == "hard_dead":
            hard_dead_indices.append(i)
        elif dec.risk_class == "survivor":
            survivor_indices.append(i)
        else:
            soft_bad_indices.append(i)

    # --- all hard_dead → explicit failure ---
    if len(hard_dead_indices) == n:
        raise AllHardDeadError(stage=stage, cohort_size=n)

    # ------------------------------------------------------------------
    # Phase 3 — assign soft_bad: audit_quota → audit_continue, rest → pause
    # ------------------------------------------------------------------
    soft_bad_indices.sort(key=lambda i: rank_of[i])
    audit_count = min(audit_quota, len(soft_bad_indices))
    audit_indices = set(soft_bad_indices[:audit_count])
    pause_indices = set(soft_bad_indices[audit_count:])
    # hard_dead also get "pause".
    all_pause_indices = pause_indices | set(hard_dead_indices)

    # ------------------------------------------------------------------
    # Phase 4 — compute how many children are needed
    # ------------------------------------------------------------------
    active_count = len(survivor_indices) + audit_count

    # --- active exceeds target ---
    if active_count > population_size:
        raise PopulationCapacityError(
            reason=f"active_count ({active_count}) exceeds "
                   f"population_size ({population_size})",
            active_count=active_count,
            population_size=population_size,
            max_fork_capacity=len(survivor_indices) * max_children_per_parent,
        )

    children_needed = population_size - active_count

    # ------------------------------------------------------------------
    # Phase 5 — select fork parents from survivors, distribute children
    # ------------------------------------------------------------------
    survivor_indices.sort(key=lambda i: (rank_of[i], cohort[i].trial_id))
    rng = random.Random(seed)

    parent_allocations: List[Tuple[int, int]] = []
    remaining = children_needed
    max_fork_capacity = len(survivor_indices) * max_children_per_parent

    if remaining > 0:
        if not survivor_indices:
            raise PopulationCapacityError(
                reason="no survivors available to fork from",
                active_count=active_count,
                population_size=population_size,
                max_fork_capacity=0,
            )
        if max_children_per_parent == 0:
            raise PopulationCapacityError(
                reason="max_children_per_parent is 0, cannot fork",
                active_count=active_count,
                population_size=population_size,
                max_fork_capacity=0,
            )
        if remaining > max_fork_capacity:
            raise PopulationCapacityError(
                reason=f"need {remaining} children but max fork capacity "
                       f"is {max_fork_capacity} "
                       f"({len(survivor_indices)} parents × "
                       f"{max_children_per_parent})",
                active_count=active_count,
                population_size=population_size,
                max_fork_capacity=max_fork_capacity,
            )

        # Round-robin distribution across eligible parents.
        eligible = list(survivor_indices)
        _seed_shuffle(eligible, rng)

        allocations: dict[int, int] = {i: 0 for i in eligible}
        while remaining > 0:
            assigned_this_round = False
            for p in eligible:
                if allocations[p] >= max_children_per_parent:
                    continue
                allocations[p] += 1
                remaining -= 1
                assigned_this_round = True
                if remaining == 0:
                    break
            if not assigned_this_round:
                # All parents at capacity — should not reach here due to
                # the capacity check above, but guard anyway.
                raise PopulationCapacityError(
                    reason="all parents at capacity during round-robin",
                    active_count=active_count,
                    population_size=population_size,
                    max_fork_capacity=max_fork_capacity,
                )

        parent_allocations = [
            (p, cnt) for p, cnt in allocations.items() if cnt > 0
        ]
        parent_allocations.sort(key=lambda pa: rank_of[pa[0]])

    # ------------------------------------------------------------------
    # Phase 6 — build GWTWDecision for each cohort entry
    # ------------------------------------------------------------------
    decisions: List[GWTWDecision] = []

    for i in range(n):
        rank = rank_of[i]

        if i in hard_dead_indices:
            action = "pause"
            is_audit_pass = False
        elif i in audit_indices:
            action = "audit_continue"
            is_audit_pass = True
        elif i in pause_indices:
            action = "pause"
            is_audit_pass = False
        else:
            # Survivor — always "continue".  Fork children are in
            # ForkRequest only; the parent's action does not change.
            action = "continue"
            is_audit_pass = False

        decisions.append(GWTWDecision(
            action=action,
            decision_stage=stage,
            rank=rank,
            parent_trial_id=None,
            child_trial_id=None,
            is_audit_pass=is_audit_pass,
            scheduler_version=scheduler_version,
        ))

    # ------------------------------------------------------------------
    # Phase 7 — build ForkRequest list
    # ------------------------------------------------------------------
    fork_requests: List[ForkRequest] = []
    for parent_idx, child_count in parent_allocations:
        parent_obs = cohort[parent_idx]
        for _ in range(child_count):
            fork_requests.append(ForkRequest(
                parent_trial_id=parent_obs.trial_id,
                decision_stage=stage,
                reason="population_replenishment",
            ))

    # ------------------------------------------------------------------
    # Phase 8 — population constraint invariant
    # ------------------------------------------------------------------
    actual_fork_count = len(fork_requests)
    if active_count + actual_fork_count != population_size:
        raise PopulationCapacityError(
            reason=f"invariant violated: active ({active_count}) + "
                   f"forks ({actual_fork_count}) != "
                   f"population_size ({population_size})",
            active_count=active_count,
            population_size=population_size,
            max_fork_capacity=max_fork_capacity,
        )

    return decisions, fork_requests


# =============================================================================
# Helpers
# =============================================================================


def _validate_inputs(
    cohort: List[MinimalObservation],
    doomed_decisions: List[DoomedDecision],
    survivor_count: int,
    audit_quota: int,
    population_size: int,
    max_children_per_parent: int,
    seed: int,
) -> None:
    """Validate all scheduler inputs; raises ValueError on failure."""

    # -- length match --
    if len(cohort) != len(doomed_decisions):
        raise ValueError(
            f"cohort length ({len(cohort)}) must equal "
            f"doomed_decisions length ({len(doomed_decisions)})"
        )

    # -- cohort non-empty --
    if not cohort:
        raise ValueError("cohort must not be empty")

    # -- stage homogeneity and validity --
    _VALID_STAGES = frozenset({"PL", "CTS"})
    stages = {obs.stage for obs in cohort}
    if len(stages) != 1:
        raise ValueError(
            f"All observations must share the same stage, got {sorted(stages)}"
        )
    stage = stages.pop()
    if stage not in _VALID_STAGES:
        raise ValueError(f"Stage must be PL or CTS, got {stage!r}")

    # -- trial_id correspondence (mandatory in input_evidence) --
    for i, (obs, dec) in enumerate(zip(cohort, doomed_decisions)):
        ev_trial_id = dec.input_evidence.get("trial_id")
        if ev_trial_id is None:
            raise ValueError(
                f"doomed_decision at index {i} is missing 'trial_id' "
                f"in input_evidence"
            )
        if ev_trial_id != obs.trial_id:
            raise ValueError(
                f"trial_id mismatch at index {i}: "
                f"observation has {obs.trial_id!r}, "
                f"doomed_decision evidence has {ev_trial_id!r}"
            )

    # -- non-negative integer quotas (reject bool) --
    for name, value in [
        ("survivor_count", survivor_count),
        ("audit_quota", audit_quota),
        ("population_size", population_size),
        ("max_children_per_parent", max_children_per_parent),
    ]:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(
                f"{name} must be an int, got {type(value).__name__} {value!r}"
            )
        if value < 0:
            raise ValueError(
                f"{name} must be non-negative, got {value}"
            )

    if population_size == 0:
        raise ValueError("population_size must be positive, got 0")

    # -- seed must be int, not bool --
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ValueError(
            f"seed must be an int, got {type(seed).__name__} {seed!r}"
        )


def _seed_shuffle(items: list, rng: random.Random) -> None:
    """Deterministic in-place shuffle using *rng*."""
    rng.shuffle(items)


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

    # =====================================================================
    # 1. Empty cohort rejected
    # =====================================================================
    try:
        schedule([], [], 1, 0, 4, 2, seed=0)
        check(False, "empty cohort should raise ValueError")
    except ValueError as e:
        check("must not be empty" in str(e).lower(), f"empty cohort msg: {e}")

    # =====================================================================
    # 2. Length mismatch
    # =====================================================================
    try:
        schedule(
            [_obs("a", "PL", -50, -100)],
            [], 1, 0, 4, 2, seed=0,
        )
        check(False, "length mismatch should raise ValueError")
    except ValueError as e:
        check("length" in str(e).lower(), f"length mismatch msg: {e}")

    # =====================================================================
    # 3. trial_id mismatch
    # =====================================================================
    try:
        schedule(
            [_obs("a", "PL", -50, -100)],
            [_dec("survivor", 1.0, "b")],
            1, 0, 4, 2, seed=0,
        )
        check(False, "trial_id mismatch should raise ValueError")
    except ValueError as e:
        check("trial_id mismatch" in str(e).lower(),
              f"trial_id mismatch msg: {e}")

    # =====================================================================
    # 3b. Missing trial_id in input_evidence
    # =====================================================================
    dec_no_trial = DoomedDecision(
        risk_class="survivor", risk_score=1.0,
        reason_codes=["survivor"], rule_version="1.0.0",
        input_evidence={"cohort_size": 1},  # no trial_id
    )
    try:
        schedule([_obs("a", "PL", -50, -100)], [dec_no_trial],
                 1, 0, 4, 2, seed=0)
        check(False, "missing trial_id in evidence should raise ValueError")
    except ValueError as e:
        check("missing 'trial_id'" in str(e).lower(),
              f"missing trial_id msg: {e}")

    # =====================================================================
    # 4. Mixed stages rejected
    # =====================================================================
    try:
        schedule(
            [_obs("a", "PL", -50, -100), _obs("b", "CTS", -100, -200)],
            [_dec("survivor", 1.0, "a"), _dec("survivor", 0.5, "b")],
            2, 0, 4, 2, seed=0,
        )
        check(False, "mixed stages should raise ValueError")
    except ValueError as e:
        check("same stage" in str(e).lower(), f"mixed stage msg: {e}")

    # =====================================================================
    # 5. Invalid stage rejected
    # =====================================================================
    try:
        schedule(
            [_obs("a", "RT", -50, -100)],
            [_dec("survivor", 1.0, "a")],
            1, 0, 4, 2, seed=0,
        )
        check(False, "invalid stage RT should raise ValueError")
    except ValueError as e:
        check("PL or CTS" in str(e), f"invalid stage msg: {e}")

    # =====================================================================
    # 6. Negative quotas rejected
    # =====================================================================
    for bad_name, bad_val in [
        ("survivor_count", -1),
        ("audit_quota", -1),
        ("population_size", -1),
        ("max_children_per_parent", -1),
    ]:
        kwargs = {
            "survivor_count": 1, "audit_quota": 0,
            "population_size": 4, "max_children_per_parent": 2, "seed": 0,
        }
        kwargs[bad_name] = bad_val
        try:
            schedule(
                [_obs("a", "PL", -50, -100)],
                [_dec("survivor", 1.0, "a")],
                **kwargs,
            )
            check(False, f"negative {bad_name} should raise ValueError")
        except ValueError as e:
            check("non-negative" in str(e).lower(),
                  f"negative {bad_name} msg: {e}")

    # =====================================================================
    # 7. population_size == 0 rejected
    # =====================================================================
    try:
        schedule(
            [_obs("a", "PL", -50, -100)],
            [_dec("survivor", 1.0, "a")],
            1, 0, 0, 2, seed=0,
        )
        check(False, "population_size=0 should raise ValueError")
    except ValueError as e:
        check("positive" in str(e).lower(),
              f"population_size=0 msg: {e}")

    # =====================================================================
    # 8. Bool quotas rejected
    # =====================================================================
    for bad_name in ["survivor_count", "audit_quota", "population_size",
                      "max_children_per_parent"]:
        kwargs = {
            "survivor_count": 1, "audit_quota": 0,
            "population_size": 4, "max_children_per_parent": 2, "seed": 0,
        }
        kwargs[bad_name] = True
        try:
            schedule(
                [_obs("a", "PL", -50, -100)],
                [_dec("survivor", 1.0, "a")],
                **kwargs,
            )
            check(False, f"bool {bad_name} should raise ValueError")
        except ValueError as e:
            check("must be an int" in str(e).lower(),
                  f"bool {bad_name} msg: {e}")

    # =====================================================================
    # 9. Bool seed rejected
    # =====================================================================
    try:
        schedule(
            [_obs("a", "PL", -50, -100)],
            [_dec("survivor", 1.0, "a")],
            1, 0, 4, 2, seed=True,
        )
        check(False, "bool seed should raise ValueError")
    except ValueError as e:
        check("must be an int" in str(e).lower(), f"bool seed msg: {e}")

    # =====================================================================
    # 10. All hard_dead → AllHardDeadError
    # =====================================================================
    cohort_all_dead = [
        _obs("a", "PL", None, None, status="failed", failure_type="tool_crash"),
        _obs("b", "PL", None, None, status="failed", failure_type="timeout"),
    ]
    decs_all_dead = [
        _dec("hard_dead", 0.0, "a", ["stage_failed", "timing_missing"]),
        _dec("hard_dead", 0.0, "b", ["timeout"]),
    ]
    try:
        schedule(cohort_all_dead, decs_all_dead, 0, 0, 4, 2, seed=0)
        check(False, "all hard_dead should raise AllHardDeadError")
    except AllHardDeadError as e:
        check(e.stage == "PL", f"AllHardDeadError.stage: {e.stage}")
        check(e.cohort_size == 2, f"AllHardDeadError.cohort_size: {e.cohort_size}")
        check("experiment failed" in str(e).lower(),
              f"AllHardDeadError msg: {e}")

    # =====================================================================
    # 11. Survivor → continue; hard_dead → pause; pop constraint met
    # =====================================================================
    cohort_simple = [
        _obs("a", "PL", -50, -100),
        _obs("b", "PL", -200, -500),
    ]
    decs_simple = [
        _dec("survivor", 1.0, "a", ["survivor"]),
        _dec("soft_bad", 0.0, "b", ["rank_low"]),
    ]
    decisions, forks = schedule(
        cohort_simple, decs_simple, 1, 0, 3, 2, seed=42,
    )
    check(len(decisions) == 2, f"2 decisions, got {len(decisions)}")
    # Survivor always "continue"; children only in ForkRequest.
    check(decisions[0].action == "continue",
          f"survivor → continue, got {decisions[0].action}")
    check(decisions[0].rank == 0, f"best rank=0, got {decisions[0].rank}")
    check(decisions[0].parent_trial_id is None,
          "survivor parent_trial_id is None")
    check(decisions[1].action == "pause",
          f"soft_bad no audit → pause, got {decisions[1].action}")
    check(decisions[1].is_audit_pass is False,
          "pause → is_audit_pass=False")
    # active=1, pop=3 → need 2 forks, cap=2 → 2 forks.
    check(len(forks) == 2,
          f"active=1, pop=3 → 2 forks, got {len(forks)}")
    # Population invariant: active + forks == pop.
    check(1 + len(forks) == 3, "active(1) + forks == pop(3)")

    # =====================================================================
    # 12. soft_bad → audit_continue with quota; survivor → continue
    # =====================================================================
    cohort_audit = [
        _obs("a", "PL", -50, -100),
        _obs("b", "PL", -200, -500),
        _obs("c", "PL", -300, -600),
    ]
    decs_audit = [
        _dec("survivor", 1.0, "a", ["survivor"]),
        _dec("soft_bad", 0.5, "b", ["rank_low"]),
        _dec("soft_bad", 0.0, "c", ["rank_low"]),
    ]
    # active=1(a)+1(b)=2, pop=4 → need 2 forks, cap=2 → 2 forks.
    decisions, forks = schedule(
        cohort_audit, decs_audit, 1, 1, 4, 2, seed=0,
    )
    check(decisions[0].action == "continue",
          f"survivor → continue, got {decisions[0].action}")
    check(decisions[1].action == "audit_continue",
          f"soft_bad top-1 → audit_continue, got {decisions[1].action}")
    check(decisions[1].is_audit_pass is True,
          "audit_continue → is_audit_pass=True")
    check(decisions[2].action == "pause",
          f"soft_bad rest → pause, got {decisions[2].action}")
    check(len(forks) == 2,
          f"active=2, pop=4 → 2 forks, got {len(forks)}")
    check(2 + len(forks) == 4, "active(2) + forks == pop(4)")

    # =====================================================================
    # 13. hard_dead → pause (not finish)
    # =====================================================================
    cohort_mixed = [
        _obs("dead", "PL", None, None, status="failed",
             failure_type="tool_crash", checkpoint_id=None),
        _obs("surv", "PL", -50, -100),
        _obs("soft", "PL", -300, -600),
    ]
    decs_mixed = [
        _dec("hard_dead", 0.0, "dead", ["stage_failed", "timing_missing",
                                        "checkpoint_missing"]),
        _dec("survivor", 1.0, "surv", ["survivor"]),
        _dec("soft_bad", 0.0, "soft", ["rank_low"]),
    ]
    # active=1(surv)+0(audit)=1, pop=3 → need 2 forks.
    decisions, forks = schedule(
        cohort_mixed, decs_mixed, 1, 0, 3, 2, seed=0,
    )
    check(decisions[0].action == "pause",
          f"hard_dead → pause, got {decisions[0].action}")
    check(decisions[0].rank == 2,
          f"hard_dead worst rank, got {decisions[0].rank}")
    check(decisions[0].parent_trial_id is None,
          "hard_dead parent_trial_id=None")
    check(decisions[0].is_audit_pass is False,
          "hard_dead is_audit_pass=False")
    check(decisions[1].action == "continue",
          f"survivor → continue, got {decisions[1].action}")
    check(1 + len(forks) == 3, "active(1) + forks == pop(3)")

    # =====================================================================
    # 14. Determinism: same input + same seed → same output
    # =====================================================================
    cohort_det = [
        _obs("a", "PL", -50, -100),
        _obs("b", "PL", -200, -500),
        _obs("c", "PL", -300, -600),
    ]
    decs_det = [
        _dec("survivor", 1.0, "a", ["survivor"]),
        _dec("soft_bad", 0.5, "b", ["rank_low"]),
        _dec("soft_bad", 0.0, "c", ["rank_low"]),
    ]
    r1, f1 = schedule(cohort_det, decs_det, 1, 1, 4, 2, seed=99)
    r2, f2 = schedule(cohort_det, decs_det, 1, 1, 4, 2, seed=99)
    for i in range(3):
        check(r1[i].action == r2[i].action, f"determinism action idx {i}")
        check(r1[i].rank == r2[i].rank, f"determinism rank idx {i}")
        check(r1[i].is_audit_pass == r2[i].is_audit_pass,
              f"determinism is_audit_pass idx {i}")
        check(r1[i].parent_trial_id == r2[i].parent_trial_id,
              f"determinism parent_trial_id idx {i}")
    check(len(f1) == len(f2),
          f"determinism fork count {len(f1)} vs {len(f2)}")
    for j in range(len(f1)):
        check(f1[j].parent_trial_id == f2[j].parent_trial_id,
              f"determinism fork parent idx {j}")

    # =====================================================================
    # 15. Different seed same parent pool → same fork count
    # =====================================================================
    r_seed_a, f_a = schedule(cohort_det, decs_det, 1, 0, 3, 2, seed=1)
    r_seed_b, f_b = schedule(cohort_det, decs_det, 1, 0, 3, 2, seed=2)
    check(len(f_a) == len(f_b),
          f"single-parent: same fork count across seeds "
          f"({len(f_a)} vs {len(f_b)})")

    # =====================================================================
    # 16. PopulationCapacityError: active exceeds target
    # =====================================================================
    try:
        schedule(
            [_obs("a", "PL", -50, -100), _obs("b", "PL", -55, -110)],
            [_dec("survivor", 1.0, "a"), _dec("survivor", 0.9, "b")],
            2, 0, 1, 2, seed=0,
        )
        check(False, "active > pop should raise PopulationCapacityError")
    except PopulationCapacityError as e:
        check("exceeds" in str(e).lower(),
              f"active > pop msg: {e}")
        check(e.active_count == 2, f"active_count=2, got {e.active_count}")
        check(e.population_size == 1,
              f"population_size=1, got {e.population_size}")

    # =====================================================================
    # 17. PopulationCapacityError: no survivors to fork
    # =====================================================================
    try:
        schedule(
            [_obs("a", "PL", -50, -100), _obs("b", "PL", -200, -500)],
            [_dec("soft_bad", 0.5, "a"), _dec("soft_bad", 0.0, "b")],
            0, 2, 4, 2, seed=0,  # 2 audit_continue, 0 survivors
        )
        check(False, "no survivors for fork should raise PopulationCapacityError")
    except PopulationCapacityError as e:
        check("no survivors" in str(e).lower(),
              f"no survivors msg: {e}")

    # =====================================================================
    # 18. PopulationCapacityError: max_children_per_parent=0
    # =====================================================================
    try:
        schedule(
            [_obs("a", "PL", -50, -100), _obs("b", "PL", -200, -500)],
            [_dec("survivor", 1.0, "a"), _dec("soft_bad", 0.0, "b")],
            1, 0, 4, 0, seed=0,
        )
        check(False, "max_children=0 with pop>active should raise")
    except PopulationCapacityError as e:
        check("max_children_per_parent is 0" in str(e).lower(),
              f"max_children=0 msg: {e}")

    # =====================================================================
    # 19. PopulationCapacityError: insufficient fork capacity
    # =====================================================================
    try:
        schedule(
            [_obs("a", "PL", -50, -100), _obs("b", "PL", -200, -500)],
            [_dec("survivor", 1.0, "a"), _dec("soft_bad", 0.0, "b")],
            1, 0, 7, 2, seed=0,  # active=1, need 6, cap=2 → insufficient
        )
        check(False, "insufficient fork capacity should raise")
    except PopulationCapacityError as e:
        check("need 6 children" in str(e).lower() and "2" in str(e),
              f"insufficient cap msg: {e}")

    # =====================================================================
    # 20. max_children_per_parent respected (when capacity sufficient)
    # =====================================================================
    cohort_cap = [
        _obs("a", "PL", -50, -100),
        _obs("b", "PL", -200, -500),
    ]
    decs_cap = [
        _dec("survivor", 1.0, "a", ["survivor"]),
        _dec("soft_bad", 0.0, "b", ["rank_low"]),
    ]
    _, forks = schedule(cohort_cap, decs_cap, 1, 0, 4, 3, seed=0)
    # active=1, pop=4 → need 3 forks, cap=3 → 3 forks.
    check(len(forks) == 3,
          f"active=1, pop=4, cap=3 → 3 forks, got {len(forks)}")
    for f in forks:
        check(f.parent_trial_id == "a",
              f"all children from survivor 'a', got {f.parent_trial_id}")
    check(1 + len(forks) == 4, "active(1) + forks(3) == pop(4)")

    # =====================================================================
    # 21. Zero max_children when pop already met → ok
    # =====================================================================
    _, forks_zero = schedule(cohort_simple, decs_simple, 1, 0, 1, 0, seed=0)
    # active=1, pop=1 → 0 forks needed, max_children=0 is fine.
    check(len(forks_zero) == 0,
          f"pop already met → 0 forks, got {len(forks_zero)}")

    # =====================================================================
    # 22. Children distributed across multiple parents
    # =====================================================================
    cohort_multi = [
        _obs("a", "PL", -50, -100),
        _obs("b", "PL", -55, -110),
        _obs("c", "PL", -300, -600),
    ]
    decs_multi = [
        _dec("survivor", 1.0, "a", ["survivor"]),
        _dec("survivor", 0.9, "b", ["survivor"]),
        _dec("soft_bad", 0.0, "c", ["rank_low"]),
    ]
    # active=2, pop=6 → need 4 forks, cap=2 → 4 forks (2 each).
    _, forks_multi = schedule(
        cohort_multi, decs_multi, 2, 0, 6, 2, seed=42,
    )
    check(len(forks_multi) == 4,
          f"2 parents × cap 2 = 4 forks, got {len(forks_multi)}")
    parents = {f.parent_trial_id for f in forks_multi}
    check(parents == {"a", "b"}, f"both parents used, got {parents}")
    a_count = sum(1 for f in forks_multi if f.parent_trial_id == "a")
    b_count = sum(1 for f in forks_multi if f.parent_trial_id == "b")
    check(a_count == 2, f"parent a → 2 children, got {a_count}")
    check(b_count == 2, f"parent b → 2 children, got {b_count}")
    check(2 + len(forks_multi) == 6, "active(2) + forks(4) == pop(6)")

    # =====================================================================
    # 23. Rank ordering correct in decisions
    # =====================================================================
    decisions_rank, _ = schedule(cohort_audit, decs_audit, 1, 1, 4, 2, seed=0)
    check(decisions_rank[0].rank == 0, "best WNS rank=0")
    check(decisions_rank[1].rank == 1, "middle WNS rank=1")
    check(decisions_rank[2].rank == 2, "worst WNS rank=2")

    # =====================================================================
    # 24. ForkRequest fields
    # =====================================================================
    _, forks_fields = schedule(cohort_cap, decs_cap, 1, 0, 4, 3, seed=0)
    for f in forks_fields:
        check(f.decision_stage == "PL",
              f"fork decision_stage=PL, got {f.decision_stage}")
        check(f.reason == "population_replenishment",
              f"fork reason, got {f.reason}")
        check(isinstance(f.parent_trial_id, str) and f.parent_trial_id,
              f"fork parent_trial_id non-empty, got {f.parent_trial_id!r}")

    # =====================================================================
    # 25. scheduler_version recorded
    # =====================================================================
    decisions_ver, _ = schedule(
        cohort_simple, decs_simple, 1, 0, 3, 2, seed=0,
        scheduler_version="2.0.0-test",
    )
    for d in decisions_ver:
        check(d.scheduler_version == "2.0.0-test",
              f"version recorded: {d.scheduler_version}")

    # =====================================================================
    # 26. Output length matches input length
    # =====================================================================
    decisions_len, _ = schedule(cohort_multi, decs_multi, 2, 0, 6, 2, seed=42)
    check(len(decisions_len) == 3, "output length == input length")

    # =====================================================================
    # 27. active_count + fork_count == population_size (simple)
    # =====================================================================
    _, forks_none = schedule(cohort_audit, decs_audit, 1, 1, 3, 2, seed=0)
    check(len(forks_none) == 1,
          f"active=2, pop=3 → 1 fork, got {len(forks_none)}")
    check(2 + len(forks_none) == 3, "active(2) + forks(1) == pop(3)")

    # =====================================================================
    # 28. GWTWDecision fields: no "fork" action, no "finish" action
    # =====================================================================
    all_decisions = decisions + decisions_rank + decisions_len
    for d in all_decisions:
        check(d.action != "fork",
              f"no action='fork' in scheduler output, got {d.action}")
        check(d.action != "finish",
              f"no action='finish' in scheduler output, got {d.action}")
        check(d.parent_trial_id is None,
              "parent_trial_id always None from scheduler")
        check(d.child_trial_id is None,
              "child_trial_id always None from scheduler")

    # -- Summary --
    total = ok + fail
    print(f"\n{'='*50}")
    print(f"  {ok}/{total} passed"
          + (f", {fail} FAILED" if fail else " — ALL OK"))
    print(f"{'='*50}")
    sys.exit(1 if fail else 0)
