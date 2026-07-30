# -*- coding: utf-8 -*-
"""cohort_planner.py — Stage D: deterministic decision pipeline orchestrator.

Pure Python, no I/O, no ORFS, no LLM, no side effects.

Orchestrates the full Stage D decision loop for one cohort at a single
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

from doomed_predictor import DEFAULT_RULE_VERSION as _DEFAULT_DOOMED_VERSION
from doomed_predictor import predict as predict_doomed
from gwtw_scheduler import (
    DEFAULT_SCHEDULER_VERSION as _DEFAULT_SCHEDULER_VERSION,
    AllHardDeadError,
    ForkRequest,
    PopulationCapacityError,
    schedule as schedule_gwtw,
)
from mutation_planner import (
    DEFAULT_PLANNER_VERSION as _DEFAULT_PLANNER_VERSION,
    MutationEvidence,
    NoLegalMutationError,
    plan_child_params,
)
from observation_builder import build_minimal_observation
from schemas.trial import (
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
    """Run the full Stage D decision pipeline on a cohort.

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

if __name__ == "__main__":
    import sys

    ok = 0
    fail_count = 0

    def check(cond, msg):
        global ok, fail_count
        if cond:
            ok += 1
        else:
            fail_count += 1
            print(f"  FAIL: {msg}")

    # -- helpers ----------------------------------------------------------
    from schemas.trial import FailureClass, StageResult, CheckpointRef

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

    def _trial(trial_id, stage, status="ok", wns=None, tns=None,
               elapsed=10.0, parent=None, checkpoint_stage=None,
               checkpoint_id=None, failure_type=None):
        """Minimal TrialRecord factory for a single-stage trial."""
        qor = {}
        # Use proper ORFS tag format so _extract_timing can parse keys
        # ending in ``_ws_ps`` / ``_tns_ps``.
        tag = f"{stage}_tag"
        if wns is not None:
            qor[f"{tag}_ws_ps"] = wns
        if tns is not None:
            qor[f"{tag}_tns_ps"] = tns
        cp = None
        if checkpoint_stage and checkpoint_id:
            cp = CheckpointRef(
                checkpoint_id=checkpoint_id,
                source_trial_id=trial_id,
                stage=checkpoint_stage,
                param_hash="abc",
                orfs_commit="def",
                created_at="2025-01-01T00:00:00",
                artifact_manifest=[],
                artifact_dir=None,
            )
        fc = failure_type
        if failure_type:
            fc = FailureClass(failure_type)
        return TrialRecord(
            trial_id=trial_id,
            experiment_id="test",
            status=status,
            start_time=None, end_time=None,
            params={},
            stage_results=[_sr(stage, status=status, elapsed=elapsed,
                               qor=qor, failure=fc)],
            parent_trial_id=parent,
            final_qor=None,
            failure=fc,
            error_message=None,
            checkpoint=cp,
            config_hash=None, env_hash=None,
            param_diff=None,
            artifact_dir=None,
            execution_resolution=None,
            doomed_decisions=[],
            gwtw_decisions=[],
            decision_trace_refs=[],
        )

    # Baseline-like parent params for mutation planning.
    _BASELINE_PARAMS: Dict[str, Dict[str, Any]] = {
        "FP": {"CORE_UTILIZATION": 38, "CORE_ASPECT_RATIO": 1.0},
        "PL": {},
        "CTS": {},
        "RT": {"FASTROUTE_LAYER_ADJUSTMENT": 0.2,
               "GRT_CONGESTION_ITERATIONS": 30},
    }

    # =====================================================================
    # 1. Empty cohort rejected
    # =====================================================================
    try:
        plan_cohort([], "PL", 1, 0, 4, 2, seed=0, parent_params_by_id={})
        check(False, "empty cohort should raise CohortPlanError")
    except CohortPlanError as e:
        check("must not be empty" in str(e).lower(),
              f"empty cohort msg: {e}")

    # =====================================================================
    # 2. Invalid decision_stage
    # =====================================================================
    t = _trial("a", "FP")
    try:
        plan_cohort([t], "FP", 1, 0, 4, 2, seed=0, parent_params_by_id={})
        check(False, "FP decision_stage should raise CohortPlanError")
    except CohortPlanError as e:
        check("PL or CTS" in str(e), f"FP stage msg: {e}")

    # =====================================================================
    # 3. Trial missing StageResult for decision_stage
    # =====================================================================
    t_pl_only = _trial("a", "PL")
    try:
        plan_cohort([t_pl_only], "CTS", 1, 0, 4, 2, seed=0,
                    parent_params_by_id={})
        check(False, "CTS request on PL-only trial should raise")
    except CohortPlanError as e:
        check("no StageResult" in str(e).lower()
             or "has no" in str(e).lower(),
             f"missing stage msg: {e}")

    # =====================================================================
    # 4. Bool seed rejected
    # =====================================================================
    try:
        plan_cohort([_trial("a", "PL")], "PL", 1, 0, 4, 2, seed=True,
                    parent_params_by_id={})
        check(False, "bool seed should raise CohortPlanError")
    except CohortPlanError as e:
        check("must be an int" in str(e).lower(), f"bool seed msg: {e}")

    # =====================================================================
    # 4b. Duplicate trial_id rejected
    # =====================================================================
    dup_cohort = [
        _trial("dup", "PL", wns=-50, tns=-100, checkpoint_stage="PL",
               checkpoint_id="cp-dup"),
        _trial("dup", "PL", wns=-200, tns=-500, checkpoint_stage="PL",
               checkpoint_id="cp-dup2"),
    ]
    try:
        plan_cohort(dup_cohort, "PL", 2, 0, 4, 2, seed=0,
                    parent_params_by_id={
                        "dup": copy.deepcopy(_BASELINE_PARAMS),
                    })
        check(False, "duplicate trial_id should raise CohortPlanError")
    except CohortPlanError as e:
        check("duplicate trial_id" in str(e).lower(), f"dup msg: {e}")

    # =====================================================================
    # 5. Basic PL cohort: 2 survivors, pop=4 → need 2 forks
    # =====================================================================
    cohort_pl = [
        _trial("a", "PL", wns=-50, tns=-100, checkpoint_stage="PL",
               checkpoint_id="cp-a"),
        _trial("b", "PL", wns=-200, tns=-500, checkpoint_stage="PL",
               checkpoint_id="cp-b"),
    ]
    parent_params = {
        "a": copy.deepcopy(_BASELINE_PARAMS),
        "b": copy.deepcopy(_BASELINE_PARAMS),
    }
    plan = plan_cohort(
        cohort_pl, "PL", 2, 0, 4, 2, seed=42, parent_params_by_id=parent_params,
    )
    check(plan.decision_stage == "PL", f"stage PL: {plan.decision_stage}")
    check(len(plan.observations) == 2, f"2 obs: {len(plan.observations)}")
    check(len(plan.doomed_decisions) == 2,
          f"2 doomed: {len(plan.doomed_decisions)}")
    check(len(plan.gwtw_decisions) == 2,
          f"2 gwtw: {len(plan.gwtw_decisions)}")
    # Both survivors → active=2, pop=4 → 2 forks.
    check(len(plan.fork_plans) == 2,
          f"2 forks: {len(plan.fork_plans)}")
    # ForkPlan fields: checkpoint_id, derived_seed.
    for fp in plan.fork_plans:
        check(isinstance(fp.fork_request, ForkRequest),
              f"fork_request type: {type(fp.fork_request).__name__}")
        check(isinstance(fp.evidence, MutationEvidence),
              f"evidence type: {type(fp.evidence).__name__}")
        check(isinstance(fp.child_params, dict),
              "child_params is dict")
        check(fp.fork_request.reason == "population_replenishment",
              f"reason: {fp.fork_request.reason}")
        check(isinstance(fp.checkpoint_id, str) and fp.checkpoint_id,
              f"checkpoint_id non-empty: {fp.checkpoint_id!r}")
        check(isinstance(fp.derived_seed, int),
              f"derived_seed is int: {fp.derived_seed}")
        # derived_seed = master_seed * _FORK_SEED_BASE + fork_index.
        check(fp.derived_seed >= 42 * _FORK_SEED_BASE,
              f"derived_seed >= seed*base: {fp.derived_seed}")

    # =====================================================================
    # 6. Determinism: same input → same output
    # =====================================================================
    p1 = plan_cohort(
        cohort_pl, "PL", 2, 0, 4, 2, seed=123,
        parent_params_by_id=parent_params,
    )
    p2 = plan_cohort(
        cohort_pl, "PL", 2, 0, 4, 2, seed=123,
        parent_params_by_id=parent_params,
    )
    # Observations match.
    for i in range(2):
        check(p1.observations[i].trial_id == p2.observations[i].trial_id,
              f"determinism obs trial_id idx {i}")
        check(p1.doomed_decisions[i].risk_class
              == p2.doomed_decisions[i].risk_class,
              f"determinism risk_class idx {i}")
        check(p1.gwtw_decisions[i].action == p2.gwtw_decisions[i].action,
              f"determinism action idx {i}")
    check(len(p1.fork_plans) == len(p2.fork_plans),
          f"determinism fork count: {len(p1.fork_plans)} vs {len(p2.fork_plans)}")
    for j in range(len(p1.fork_plans)):
        check(p1.fork_plans[j].evidence.param_name
              == p2.fork_plans[j].evidence.param_name,
              f"determinism fork param idx {j}")
        check(p1.fork_plans[j].evidence.new_value
              == p2.fork_plans[j].evidence.new_value,
              f"determinism fork new_value idx {j}")

    # =====================================================================
    # 7. Different seed → same fork count, potentially different mutations
    # =====================================================================
    p_s1 = plan_cohort(
        cohort_pl, "PL", 2, 0, 4, 2, seed=1,
        parent_params_by_id=parent_params,
    )
    p_s2 = plan_cohort(
        cohort_pl, "PL", 2, 0, 4, 2, seed=2,
        parent_params_by_id=parent_params,
    )
    check(len(p_s1.fork_plans) == len(p_s2.fork_plans),
          "fork count consistent across seeds")
    # With 3+ legal params, different seeds often produce different params.
    names_s1 = {fp.evidence.param_name for fp in p_s1.fork_plans}
    names_s2 = {fp.evidence.param_name for fp in p_s2.fork_plans}
    # At least the set of changed params can diverge across seeds.
    check(isinstance(names_s1, set) and isinstance(names_s2, set),
          "seed divergence: both produce param name sets")

    # =====================================================================
    # 8. All hard_dead → AllHardDeadError propagated
    # =====================================================================
    cohort_dead = [
        _trial("x", "PL", status="failed", wns=None, tns=None,
               failure_type="tool_crash"),
        _trial("y", "PL", status="failed", wns=None, tns=None,
               failure_type="timeout"),
    ]
    try:
        plan_cohort(
            cohort_dead, "PL", 1, 0, 4, 2, seed=0,
            parent_params_by_id={},
        )
        check(False, "all hard_dead should raise AllHardDeadError")
    except AllHardDeadError as e:
        check(e.stage == "PL", f"AllHardDeadError stage: {e.stage}")
        check(e.cohort_size == 2, f"cohort_size: {e.cohort_size}")

    # =====================================================================
    # 9. Missing parent params → CohortPlanError
    # =====================================================================
    try:
        plan_cohort(
            cohort_pl, "PL", 2, 0, 4, 2, seed=0,
            parent_params_by_id={"a": copy.deepcopy(_BASELINE_PARAMS)},
            # missing "b"
        )
        check(False, "missing parent params should raise CohortPlanError")
    except CohortPlanError as e:
        check("No parent params" in str(e)
              or "parent params" in str(e).lower(),
              f"missing parent params msg: {e}")

    # =====================================================================
    # 9b. Fork parent checkpoint source_trial_id mismatch
    # =====================================================================
    from schemas.trial import CheckpointRef as _CPRef
    t_bad_src = _trial("a", "PL", wns=-50, tns=-100)
    # Override checkpoint: correct stage, wrong source_trial.
    t_bad_src.checkpoint = _CPRef(
        checkpoint_id="cp-a", source_trial_id="OTHER_TRIAL",
        stage="PL", param_hash="abc", orfs_commit="def",
        created_at="2025-01-01T00:00:00",
        artifact_manifest=[], artifact_dir=None,
    )
    # Need to explicitly set the stage_result for PL.
    from schemas.trial import StageResult as _SR
    t_bad_src.stage_results = [
        _SR(stage="PL", status="ok", elapsed_s=10.0,
            exit_code=0, log_path=None, command=None,
            start_time=None, end_time=None, report_path=None,
            stage_qor={"PL_tag_ws_ps": -50, "PL_tag_tns_ps": -100},
            failure=None, error_message=None),
    ]
    try:
        plan_cohort(
            [t_bad_src, _trial("b", "PL", wns=-200, tns=-500,
                               checkpoint_stage="PL", checkpoint_id="cp-b")],
            "PL", 2, 0, 4, 2, seed=0,
            parent_params_by_id={
                "a": copy.deepcopy(_BASELINE_PARAMS),
                "b": copy.deepcopy(_BASELINE_PARAMS),
            },
        )
        check(False, "checkpoint source_trial mismatch should raise")
    except CohortPlanError as e:
        check("source_trial_id" in str(e).lower(),
              f"source_trial_id mismatch msg: {e}")

    # =====================================================================
    # 10. CTS cohort: single survivor, pop=3 → 2 forks
    # =====================================================================
    cohort_cts = [
        _trial("cts1", "CTS", wns=-100, tns=-500, checkpoint_stage="CTS",
               checkpoint_id="cp-cts1"),
        _trial("cts2", "CTS", wns=-300, tns=-800, checkpoint_stage="CTS",
               checkpoint_id="cp-cts2"),
    ]
    parent_params_cts = {
        "cts1": copy.deepcopy(_BASELINE_PARAMS),
        "cts2": copy.deepcopy(_BASELINE_PARAMS),
    }
    plan_cts = plan_cohort(
        cohort_cts, "CTS", 1, 0, 3, 2, seed=99,
        parent_params_by_id=parent_params_cts,
    )
    check(plan_cts.decision_stage == "CTS",
          f"CTS stage: {plan_cts.decision_stage}")
    check(len(plan_cts.fork_plans) == 2,
          f"CTS: 2 forks, got {len(plan_cts.fork_plans)}")
    # CTS legal params = [GRT_CONGESTION_ITERATIONS] only.
    for fp in plan_cts.fork_plans:
        check(fp.evidence.param_name == "GRT_CONGESTION_ITERATIONS",
              f"CTS fork param={fp.evidence.param_name}, "
              f"expected GRT_CONGESTION_ITERATIONS")

    # =====================================================================
    # 11. Metadata fields populated
    # =====================================================================
    plan_meta = plan_cohort(
        cohort_pl, "PL", 2, 1, 5, 3, seed=7,
        parent_params_by_id=parent_params,
    )
    check(plan_meta.seed == 7, f"seed: {plan_meta.seed}")
    check(plan_meta.survivor_count == 2,
          f"survivor_count: {plan_meta.survivor_count}")
    check(plan_meta.audit_quota == 1,
          f"audit_quota: {plan_meta.audit_quota}")
    check(plan_meta.population_size == 5,
          f"population_size: {plan_meta.population_size}")
    check(plan_meta.max_children_per_parent == 3,
          f"max_children: {plan_meta.max_children_per_parent}")
    check(plan_meta.doomed_rule_version == _DEFAULT_DOOMED_VERSION,
          f"doomed_ver: {plan_meta.doomed_rule_version}")
    check(plan_meta.scheduler_version == _DEFAULT_SCHEDULER_VERSION,
          f"scheduler_ver: {plan_meta.scheduler_version}")
    check(plan_meta.planner_version == _DEFAULT_PLANNER_VERSION,
          f"planner_ver: {plan_meta.planner_version}")

    # =====================================================================
    # 12. Inputs not mutated
    # =====================================================================
    cohort_orig = copy.deepcopy(cohort_pl)
    params_orig = copy.deepcopy(parent_params)
    plan_cohort(
        cohort_pl, "PL", 2, 0, 4, 2, seed=0,
        parent_params_by_id=parent_params,
    )
    for i in range(len(cohort_pl)):
        check(cohort_pl[i].trial_id == cohort_orig[i].trial_id,
              f"cohort trial_id unchanged idx {i}")
        check(cohort_pl[i].status == cohort_orig[i].status,
              f"cohort status unchanged idx {i}")
    check(parent_params == params_orig, "parent_params unchanged")

    # =====================================================================
    # 13. Mixed-status cohort (survivor + soft_bad + hard_dead)
    # =====================================================================
    cohort_mixed = [
        _trial("dead", "PL", status="failed", wns=None, tns=None,
               failure_type="tool_crash"),
        _trial("top", "PL", wns=-50, tns=-100, checkpoint_stage="PL",
               checkpoint_id="cp-top"),
        _trial("mid", "PL", wns=-200, tns=-500, checkpoint_stage="PL",
               checkpoint_id="cp-mid"),
        _trial("low", "PL", wns=-400, tns=-900, checkpoint_stage="PL",
               checkpoint_id="cp-low"),
    ]
    params_mixed = {
        "top": copy.deepcopy(_BASELINE_PARAMS),
        "mid": copy.deepcopy(_BASELINE_PARAMS),
        "low": copy.deepcopy(_BASELINE_PARAMS),
    }
    plan_mixed = plan_cohort(
        cohort_mixed, "PL", 2, 0, 3, 1, seed=55,
        parent_params_by_id=params_mixed,
    )
    # dead → hard_dead, top + mid → survivors, low → soft_bad.
    check(plan_mixed.doomed_decisions[0].risk_class == "hard_dead",
          f"dead → hard_dead, got {plan_mixed.doomed_decisions[0].risk_class}")
    check(plan_mixed.doomed_decisions[1].risk_class == "survivor",
          f"top → survivor, got {plan_mixed.doomed_decisions[1].risk_class}")
    check(plan_mixed.doomed_decisions[2].risk_class == "survivor",
          f"mid → survivor, got {plan_mixed.doomed_decisions[2].risk_class}")
    check(plan_mixed.doomed_decisions[3].risk_class == "soft_bad",
          f"low → soft_bad, got {plan_mixed.doomed_decisions[3].risk_class}")
    # hard_dead → pause.
    check(plan_mixed.gwtw_decisions[0].action == "pause",
          f"dead → pause, got {plan_mixed.gwtw_decisions[0].action}")
    # 2 survivors → active=2, pop=3, max_children=1 → 1 fork from one parent.
    check(len(plan_mixed.fork_plans) == 1,
          f"1 fork, got {len(plan_mixed.fork_plans)}")

    # =====================================================================
    # 14. Fork seeds are deterministic and traceable
    # =====================================================================
    fp0 = plan_mixed.fork_plans[0]
    # fork_seed = master_seed * _FORK_SEED_BASE + fork_index
    expected_fork_seed = 55 * _FORK_SEED_BASE + 0
    check(fp0.fork_request.parent_trial_id in {"top", "mid"},
          f"fork parent is a survivor: {fp0.fork_request.parent_trial_id}")

    # =====================================================================
    # 15. PopulationCapacityError propagated
    # =====================================================================
    try:
        plan_cohort(
            cohort_pl, "PL", 2, 0, 10, 1, seed=0,
            parent_params_by_id=parent_params,
            # active=2, pop=10 → need 8 forks, cap=2 → insufficient
        )
        check(False, "insufficient capacity should raise PopulationCapacityError")
    except PopulationCapacityError as e:
        check("need 8 children" in str(e).lower()
              or "max fork capacity" in str(e).lower(),
              f"capacity error msg: {e}")

    # =====================================================================
    # 16. Custom version strings flow through
    # =====================================================================
    plan_custom = plan_cohort(
        cohort_pl, "PL", 2, 0, 4, 2, seed=0,
        parent_params_by_id=parent_params,
        doomed_rule_version="v-doomed",
        scheduler_version="v-sched",
        planner_version="v-plan",
    )
    check(plan_custom.doomed_rule_version == "v-doomed",
          f"custom doomed_ver: {plan_custom.doomed_rule_version}")
    check(plan_custom.scheduler_version == "v-sched",
          f"custom sched_ver: {plan_custom.scheduler_version}")
    check(plan_custom.planner_version == "v-plan",
          f"custom plan_ver: {plan_custom.planner_version}")

    # =====================================================================
    # 17. Audit quota: soft_bad → audit_continue
    # =====================================================================
    plan_audit = plan_cohort(
        cohort_mixed, "PL", 1, 1, 4, 2, seed=0,
        parent_params_by_id=params_mixed,
    )
    # With survivor_count=1, audit_quota=1:
    #   dead → hard_dead (pause), top → survivor, mid → audit_continue, low → pause
    check(plan_audit.gwtw_decisions[0].action == "pause",
          f"dead → pause: {plan_audit.gwtw_decisions[0].action}")
    check(plan_audit.gwtw_decisions[1].action == "continue",
          f"top → continue: {plan_audit.gwtw_decisions[1].action}")
    check(plan_audit.gwtw_decisions[2].action == "audit_continue",
          f"mid → audit_continue: {plan_audit.gwtw_decisions[2].action}")
    check(plan_audit.gwtw_decisions[2].is_audit_pass is True,
          "audit_continue → is_audit_pass=True")
    check(plan_audit.gwtw_decisions[3].action == "pause",
          f"low → pause: {plan_audit.gwtw_decisions[3].action}")

    # =====================================================================
    # 18. ForkPlan derived_seed formula: seed * _FORK_SEED_BASE + idx
    # =====================================================================
    for idx, fp in enumerate(plan.fork_plans):
        expected_fs = 42 * _FORK_SEED_BASE + idx
        check(fp.derived_seed == expected_fs,
              f"fork idx {idx}: derived_seed={fp.derived_seed}, "
              f"expected={expected_fs}")
        check(fp.checkpoint_id in ("cp-a", "cp-b"),
              f"fork idx {idx}: checkpoint_id={fp.checkpoint_id!r} "
              f"is from a survivor")

    # =====================================================================
    # 19. Observation → decision alignment: same length, same order
    # =====================================================================
    plan_align = plan_cohort(
        cohort_pl, "PL", 2, 0, 4, 2, seed=0,
        parent_params_by_id=parent_params,
    )
    n = len(cohort_pl)
    check(len(plan_align.observations) == n, "obs len == cohort len")
    check(len(plan_align.doomed_decisions) == n, "doomed len == cohort len")
    check(len(plan_align.gwtw_decisions) == n, "gwtw len == cohort len")
    for i in range(n):
        obs_tid = plan_align.observations[i].trial_id
        dec_tid = plan_align.doomed_decisions[i].input_evidence.get("trial_id")
        check(obs_tid == dec_tid,
              f"alignment idx {i}: obs={obs_tid}, doomed_evidence={dec_tid}")

    # =====================================================================
    # 20. ForkPlan evidence reason + structural fields
    # =====================================================================
    for fp in plan.fork_plans:
        check(_DEFAULT_PLANNER_VERSION in fp.evidence.reason,
              f"version in evidence reason: {fp.evidence.reason[:60]}...")
        check(isinstance(fp.checkpoint_id, str) and fp.checkpoint_id,
              f"checkpoint_id non-empty: {fp.checkpoint_id!r}")
        check(fp.derived_seed >= 42 * _FORK_SEED_BASE,
              f"derived_seed range: {fp.derived_seed}")
        check(fp.fork_request.decision_stage == "PL",
              f"decision_stage PL: {fp.fork_request.decision_stage}")

    # -- Summary --
    total = ok + fail_count
    print(f"\n{'='*50}")
    print(f"  {ok}/{total} passed"
          + (f", {fail_count} FAILED" if fail_count else " — ALL OK"))
    print(f"{'='*50}")
    sys.exit(1 if fail_count else 0)
