# -*- coding: utf-8 -*-
"""cohort_planner.py — deterministic decision pipeline orchestrator.

Pure Python, no I/O, no ORFS, no LLM, no side effects.

Orchestrates the full cohort decision loop for one cohort at a single
decision stage (PL or CTS):

    1. observation_builder.build_minimal_observation   (TrialRecord → MinimalObservation)
    2. doomed_predictor.predict                        (cohort → DoomedDecision[])
    3. gwtw_scheduler.schedule                         (cohort + decisions → GWTWDecisions + ForkRequests)
    4. mutation_planner.plan_child_params              (each ForkRequest → child params + evidence)

Inputs are never mutated.  All outputs are collected into an immutable-looking
CohortPlan.  Same seed + same inputs → identical CohortPlan every call.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from gwtw.doom import DEFAULT_RULE_VERSION as _DEFAULT_DOOMED_VERSION
from gwtw.doom import predict as predict_doomed
from gwtw.scheduler import (
    DEFAULT_SCHEDULER_VERSION as _DEFAULT_SCHEDULER_VERSION,
    AllHardDeadError,
    ForkRequest,
    PopulationCapacityError,
    schedule as schedule_gwtw,
)
from gwtw.mutation import (
    DEFAULT_PLANNER_VERSION as _DEFAULT_PLANNER_VERSION,
    MutationEvidence,
    NoLegalMutationError,
    plan_child_params,
)
from gwtw.observation import build_minimal_observation
from core.models import (
    DoomedDecision,
    GWTWDecision,
    MinimalObservation,
    TrialRecord,
)

log = logging.getLogger(__name__)

# =============================================================================
# Data classes
# =============================================================================


@dataclass(frozen=True)
class ForkPlan:
    """One fork child plan — immutable after construction.

    Attributes:
        fork_request:   the original ForkRequest from the scheduler.
        checkpoint_id:  the consumed checkpoint from the parent trial
                        (matches the parent's observation).
        child_params:   ``{stage: {name: value}}`` child parameters with
                        exactly one parameter changed from parent.
        evidence:       what was changed and why.
        derived_seed:   the deterministic fork seed derived from
                        ``master_seed * _FORK_SEED_BASE + fork_index``.
    """

    fork_request: ForkRequest
    checkpoint_id: str
    child_params: Dict[str, Dict[str, Any]]
    evidence: MutationEvidence
    derived_seed: int


@dataclass
class CohortPlan:
    """Complete plan for one cohort at a single decision stage.

    Attributes:
        decision_stage:           PL or CTS.
        observations:             one per cohort entry (same order).
        doomed_decisions:         one per observation (same order).
        gwtw_decisions:           one per observation (same order).
        fork_plans:               child param plans for every ForkRequest.
        seed:                     master seed for this cohort.
        survivor_count:           DoomedPredictor survivor quota.
        audit_quota:              soft_bad → audit_continue quota.
        population_size:          target active population.
        max_children_per_parent:  max forks per survivor parent.
        doomed_rule_version:      version of doomed prediction rules used.
        scheduler_version:        version of GWTW scheduler rules used.
        planner_version:          version of mutation planner used.
    """

    decision_stage: str
    observations: List[MinimalObservation] = field(default_factory=list)
    doomed_decisions: List[DoomedDecision] = field(default_factory=list)
    gwtw_decisions: List[GWTWDecision] = field(default_factory=list)
    fork_plans: List[ForkPlan] = field(default_factory=list)
    # Metadata
    seed: int = 0
    survivor_count: int = 0
    audit_quota: int = 0
    population_size: int = 0
    max_children_per_parent: int = 0
    doomed_rule_version: str = ""
    scheduler_version: str = ""
    planner_version: str = ""


# =============================================================================
# Custom exceptions
# =============================================================================


class CohortPlanError(Exception):
    """Raised when cohort planning cannot proceed.

    Covers: empty cohort, mixed stages, missing parent params for a fork,
    forks from non-survivors (defensive invariant check).
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Cohort plan error: {reason}")


# =============================================================================
# Core planner
# =============================================================================

# Fork seeds are derived from the master seed plus the fork's position in
# the ForkRequest list.  Multiplier chosen to keep seeds deterministic and
# easily traceable while avoiding collisions with the master seed space.
_FORK_SEED_BASE: int = 10000


def plan_cohort(
    cohort: List[TrialRecord],
    decision_stage: str,
    survivor_count: int,
    audit_quota: int,
    population_size: int,
    max_children_per_parent: int,
    seed: int,
    parent_params_by_id: Dict[str, Dict[str, Dict[str, Any]]],
    *,
    doomed_rule_version: str = _DEFAULT_DOOMED_VERSION,
    scheduler_version: str = _DEFAULT_SCHEDULER_VERSION,
    planner_version: str = _DEFAULT_PLANNER_VERSION,
) -> CohortPlan:
    """Run the full cohort decision pipeline.

    Args:
        cohort:                  completed TrialRecords at the **same**
                                 decision_stage (PL or CTS).
        decision_stage:          ``"PL"`` or ``"CTS"``.
        survivor_count:          top N non-hard-dead → survivor.
        audit_quota:             soft_bad → audit_continue count.
        population_size:         target active trial count.
        max_children_per_parent: max fork children each survivor can spawn.
        seed:                    integer master seed for deterministic
                                 tie-breaking and mutation.
        parent_params_by_id:     ``{trial_id: {stage: {name: value}}}`` —
                                 resolved parameters for every trial that
                                 could be a fork parent.  **Not mutated.**
        doomed_rule_version:     passed through to :func:`doomed_predictor.predict`.
        scheduler_version:       passed through to :func:`gwtw_scheduler.schedule`.
        planner_version:         passed through to :func:`mutation_planner.plan_child_params`.

    Returns:
        ``CohortPlan`` with all decisions, fork plans, and metadata.

    Raises:
        CohortPlanError:         empty cohort, mixed stages, missing parent
                                 params, or internal invariant violation.
        AllHardDeadError:        every candidate is hard_dead (relayed from
                                 GWTW scheduler).
        PopulationCapacityError: active count / fork capacity cannot satisfy
                                 population_size (relayed from scheduler).
        NoLegalMutationError:    a fork parent has no legal single-parameter
                                 mutation (relayed from mutation planner).
    """
    # ------------------------------------------------------------------
    # Phase 0 — deep-copy inputs (defensive; callers may reuse)
    # ------------------------------------------------------------------
    cohort = copy.deepcopy(list(cohort))
    parent_params_by_id = copy.deepcopy(parent_params_by_id)

    # ------------------------------------------------------------------
    # Phase 0.5 — input validation
    # ------------------------------------------------------------------
    _validate_cohort(cohort, decision_stage)
    _validate_seed(seed)

    # ------------------------------------------------------------------
    # Phase 1 — build observations
    # ------------------------------------------------------------------
    observations: List[MinimalObservation] = []
    for trial in cohort:
        obs = build_minimal_observation(trial, decision_stage)
        observations.append(obs)

    # ------------------------------------------------------------------
    # Phase 2 — doomed prediction
    # ------------------------------------------------------------------
    doomed_decisions = predict_doomed(
        observations,
        survivor_count=survivor_count,
        rule_version=doomed_rule_version,
    )

    # ------------------------------------------------------------------
    # Phase 3 — GWTW scheduling
    # ------------------------------------------------------------------
    gwtw_decisions, fork_requests = schedule_gwtw(
        observations,
        doomed_decisions,
        survivor_count=survivor_count,
        audit_quota=audit_quota,
        population_size=population_size,
        max_children_per_parent=max_children_per_parent,
        seed=seed,
        scheduler_version=scheduler_version,
    )

    # ------------------------------------------------------------------
    # Phase 3.5 — defensive invariants + checkpoint validation
    # ------------------------------------------------------------------
    # Build lookup: trial_id → TrialRecord (for checkpoint validation).
    trial_by_id: Dict[str, TrialRecord] = {t.trial_id: t for t in cohort}
    # Build lookup: trial_id → MinimalObservation (for checkpoint_id).
    obs_by_id: Dict[str, MinimalObservation] = {
        obs.trial_id: obs for obs in observations
    }

    # Invariant: every fork parent is classified as survivor.
    survivor_trial_ids: set = set()
    for obs, dec in zip(observations, doomed_decisions):
        if dec.risk_class == "survivor":
            survivor_trial_ids.add(obs.trial_id)

    for fr in fork_requests:
        parent_id = fr.parent_trial_id

        # a) parent must be a survivor.
        if parent_id not in survivor_trial_ids:
            raise CohortPlanError(
                f"ForkRequest parent {parent_id!r} is not a "
                f"survivor — internal invariant violated"
            )

        # b) parent observation must carry a checkpoint_id.
        parent_obs = obs_by_id[parent_id]
        if parent_obs.checkpoint_id is None:
            raise CohortPlanError(
                f"ForkRequest parent {parent_id!r} has no checkpoint "
                f"(checkpoint_id is None in observation)"
            )

        # c) parent's trial checkpoint must exist, match stage and source_trial.
        parent_trial = trial_by_id[parent_id]
        cp = parent_trial.checkpoint
        if cp is None:
            raise CohortPlanError(
                f"ForkRequest parent {parent_id!r} has no CheckpointRef "
                f"in its TrialRecord"
            )
        if cp.stage != decision_stage:
            raise CohortPlanError(
                f"ForkRequest parent {parent_id!r} checkpoint stage "
                f"is {cp.stage!r}, expected {decision_stage!r}"
            )
        if cp.source_trial_id != parent_id:
            raise CohortPlanError(
                f"ForkRequest parent {parent_id!r} checkpoint "
                f"source_trial_id is {cp.source_trial_id!r}, expected "
                f"{parent_id!r}"
            )

    # ------------------------------------------------------------------
    # Phase 4 — mutation planning for each fork request
    # ------------------------------------------------------------------
    fork_plans: List[ForkPlan] = []
    for idx, fr in enumerate(fork_requests):
        parent_id = fr.parent_trial_id

        # Look up parent params.
        parent_params = parent_params_by_id.get(parent_id)
        if parent_params is None:
            raise CohortPlanError(
                f"No parent params found for fork parent "
                f"{parent_id!r} (required by ForkRequest "
                f"#{idx} at stage {fr.decision_stage})"
            )

        # Derive deterministic fork seed from master seed + fork index.
        # This guarantees that same (seed, fork_order) → same mutation
        # every run, while giving each fork a different change.
        fork_seed = seed * _FORK_SEED_BASE + idx

        child_params, evidence = plan_child_params(
            fr,
            parent_params=parent_params,
            seed=fork_seed,
            planner_version=planner_version,
        )

        # Parent checkpoint_id comes from the observation (already validated
        # in Phase 3.5 that it is not None and the stage/source match).
        parent_cp_id: str = obs_by_id[parent_id].checkpoint_id  # type: ignore[assignment]

        fork_plans.append(ForkPlan(
            fork_request=fr,
            checkpoint_id=parent_cp_id,
            child_params=child_params,
            evidence=evidence,
            derived_seed=fork_seed,
        ))

    # ------------------------------------------------------------------
    # Phase 5 — assemble CohortPlan
    # ------------------------------------------------------------------
    return CohortPlan(
        decision_stage=decision_stage,
        observations=observations,
        doomed_decisions=doomed_decisions,
        gwtw_decisions=gwtw_decisions,
        fork_plans=fork_plans,
        seed=seed,
        survivor_count=survivor_count,
        audit_quota=audit_quota,
        population_size=population_size,
        max_children_per_parent=max_children_per_parent,
        doomed_rule_version=doomed_rule_version,
        scheduler_version=scheduler_version,
        planner_version=planner_version,
    )


# =============================================================================
# Helpers
# =============================================================================


def _validate_cohort(
    cohort: List[TrialRecord],
    decision_stage: str,
) -> None:
    """Validate cohort structure before running the pipeline."""
    if not cohort:
        raise CohortPlanError("cohort must not be empty")

    _VALID_STAGES = frozenset({"PL", "CTS"})
    if decision_stage not in _VALID_STAGES:
        raise CohortPlanError(
            f"decision_stage must be PL or CTS, got {decision_stage!r}"
        )

    # Detect duplicate trial IDs — each entry must be unique.
    seen_ids: set = set()
    for trial in cohort:
        if trial.trial_id in seen_ids:
            raise CohortPlanError(
                f"Duplicate trial_id {trial.trial_id!r} in cohort"
            )
        seen_ids.add(trial.trial_id)

    # Every trial must have a StageResult at decision_stage.
    for trial in cohort:
        stage_names = {sr.stage for sr in trial.stage_results}
        if decision_stage not in stage_names:
            raise CohortPlanError(
                f"Trial {trial.trial_id!r} has no StageResult for "
                f"{decision_stage!r} (has: {sorted(stage_names)})"
            )


def _validate_seed(seed: int) -> None:
    """Reject bool and non-int seeds."""
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise CohortPlanError(
            f"seed must be an int, got {type(seed).__name__} {seed!r}"
        )


# =============================================================================
# Self-test
# =============================================================================

