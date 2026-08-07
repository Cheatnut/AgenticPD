# -*- coding: utf-8 -*-
"""rule-based GWTW (Go-With-The-Winners) Scheduler.

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

from core.models import MinimalObservation, DoomedDecision, GWTWDecision

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

