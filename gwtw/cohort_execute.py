# -*- coding: utf-8 -*-
"""cohort_executor.py — serial cohort executor for PL/CTS single-layer loop.

Pure Python orchestration layer.  Coordinates existing managers (TrialManager,
CheckpointManager) with the cohort planner, checkpoint resolver, and
append-only decision trace.

Pipeline:
  1. Idempotency guard: skip if cohort already executed (same stage+seed+trial_ids).
  2. plan_cohort on the PL- or CTS-completed cohort.
  3. Write observations + doomed/gwtw decisions + fork evidence + execution
     resolutions to the append-only decision trace JSONL.
  4. Persist DecisionTraceRef on each TrialRecord; pause trials get
     ``status="paused"``.
  5. For each ForkPlan: create child trial, resolve checkpoint via resolver,
     record ExecutionResolution.
  6. Return CohortExecutionResult with all trace refs for audit.

Supports ``reconstruct_cohort_decisions`` to rebuild in-memory decision state
from disk (trial records + trace JSONL) — no re-execution needed.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from gwtw.cohort_plan import (
    CohortPlan,
    CohortPlanError,
    ForkPlan,
    plan_cohort,
)
from gwtw.resolver import resolve_checkpoint
from storage.decision_trace import (
    DEFAULT_TRACE_PATH,
    DecisionTraceWriter,
    make_cohort_id,
    read_trace,
)
from storage.trace_io import (
    cohort_already_executed,
    cohort_decision_written,
    read_fork_intents,
    write_cohort_complete,
    write_fork_intents,
)
from gwtw.doom import DEFAULT_RULE_VERSION as _DEFAULT_DOOMED_VERSION
from gwtw.scheduler import DEFAULT_SCHEDULER_VERSION as _DEFAULT_SCHEDULER_VERSION
from storage import CheckpointManager, TrialManager
from gwtw.mutation import DEFAULT_PLANNER_VERSION as _DEFAULT_PLANNER_VERSION
from search.tree import OptimizationTree
from core.models import (
    DecisionTraceRef,
    ExecutionResolution,
    TrialRecord,
)

log = logging.getLogger(__name__)


# =============================================================================
# Data classes
# =============================================================================


@dataclass
class CohortExecutionResult:
    """Result of executing one cohort planning cycle.

    Attributes:
        decision_stage:               PL or CTS.
        cohort_plan:                  the full plan from cohort_planner.
        trial_outcomes:               ``{trial_id: action}``.
        child_trial_ids:              trial IDs of children created from
                                      ForkPlans.
        child_checkpoint_resolutions: ExecutionResolution for each child.
        trace_refs:                   DecisionTraceRef for every trace entry
                                      written during this execution.
        seed:                         master seed.
    """

    decision_stage: str
    cohort_plan: Optional[CohortPlan] = None
    trial_outcomes: Dict[str, str] = field(default_factory=dict)
    child_trial_ids: List[str] = field(default_factory=list)
    child_checkpoint_resolutions: List[ExecutionResolution] = field(
        default_factory=list)
    trace_refs: List[DecisionTraceRef] = field(default_factory=list)
    seed: int = 0


# =============================================================================
# Core executor
# =============================================================================


def execute_cohort(
    cohort: List[TrialRecord],
    decision_stage: str,
    survivor_count: int,
    audit_quota: int,
    population_size: int,
    max_children_per_parent: int,
    seed: int,
    parent_params_by_id: Dict[str, Dict[str, Dict[str, Any]]],
    trial_mgr: TrialManager,
    checkpoint_mgr: CheckpointManager,
    tree: OptimizationTree,
    *,
    experiment_id: str = "gwtw-experiment",
    iteration: int = 0,
    runs_dir: Optional[Path] = None,
    trace_path: str = DEFAULT_TRACE_PATH,
    doomed_rule_version: str = _DEFAULT_DOOMED_VERSION,
    scheduler_version: str = _DEFAULT_SCHEDULER_VERSION,
    planner_version: str = _DEFAULT_PLANNER_VERSION,
) -> CohortExecutionResult:
    """Execute one cohort planning cycle: plan → decide → persist → fork.

    Args:
        cohort:                  PL- or CTS-completed TrialRecords (same stage).
        decision_stage:          ``"PL"`` or ``"CTS"``.
        survivor_count:          top N non-hard-dead → survivor.
        audit_quota:             soft_bad → audit_continue quota.
        population_size:         target active trial count.
        max_children_per_parent: max fork children per survivor.
        seed:                    integer master seed.
        parent_params_by_id:     ``{trial_id: {stage: {name: value}}}``.
        trial_mgr:               TrialManager for persistence.
        checkpoint_mgr:          CheckpointManager for checkpoint ops.
        tree:                    optimization tree for checkpoint resolution.
        experiment_id:           recorded in child TrialRecords.
        iteration:               recorded in child trial artifact_dir names.
        runs_dir:                session runs_dir; defaults to
                                 ``trial_mgr.runs_dir``.
        trace_path:              relative path for the decision trace JSONL.
        doomed_rule_version:     forwarded to plan_cohort.
        scheduler_version:       forwarded to plan_cohort.
        planner_version:         forwarded to plan_cohort.

    Returns:
        CohortExecutionResult with decisions applied and children created.

    Raises:
        CohortPlanError:         forwarded from plan_cohort.
        AllHardDeadError:        forwarded from GWTW scheduler.
        PopulationCapacityError: forwarded from GWTW scheduler.
        NoLegalMutationError:    forwarded from mutation planner.
    """
    _runs_dir = runs_dir or trial_mgr.runs_dir
    _trace_writer = DecisionTraceWriter(_runs_dir, trace_path)

    # ------------------------------------------------------------------
    # Phase 0 — idempotency guard (three recovery modes)
    # ------------------------------------------------------------------
    _trial_ids = [t.trial_id for t in cohort]
    _ver = (doomed_rule_version, scheduler_version, planner_version)
    _cfg = (survivor_count, audit_quota, population_size,
            max_children_per_parent, *_ver)

    # Compute precise cohort_id once, used by all three paths.
    _cohort_id = make_cohort_id(decision_stage, seed, _trial_ids, *_cfg)

    # 0a) Full cohort already complete (sentinel exists) → rebuild.
    if cohort_already_executed(
        _runs_dir, trace_path, decision_stage, seed, _trial_ids,
        *_cfg,
    ):
        log.info(
            "[EXECUTOR] cohort %s already complete — rebuilding from disk",
            _cohort_id,
        )
        return _rebuild_from_disk(
            _runs_dir, trace_path, _cohort_id, cohort, trial_mgr,
        )

    # 0b) Decisions written but sentinel missing → resume forks.
    if cohort_decision_written(
        _runs_dir, trace_path, decision_stage, seed, _trial_ids,
        *_cfg,
    ):
        log.info(
            "[EXECUTOR] cohort %s seed=%s decisions on disk, "
            "resuming fork creation",
            decision_stage, seed,
        )
        return _resume_forks_from_disk(
            cohort=cohort,
            decision_stage=decision_stage,
            seed=seed,
            survivor_count=survivor_count,
            audit_quota=audit_quota,
            population_size=population_size,
            max_children_per_parent=max_children_per_parent,
            parent_params_by_id=parent_params_by_id,
            trial_mgr=trial_mgr,
            checkpoint_mgr=checkpoint_mgr,
            tree=tree,
            experiment_id=experiment_id,
            iteration=iteration,
            runs_dir=_runs_dir,
            trace_path=trace_path,
            planner_version=planner_version,
            doomed_rule_version=doomed_rule_version,
            scheduler_version=scheduler_version,
        )

    # ------------------------------------------------------------------
    # Phase 1 — plan the cohort
    # ------------------------------------------------------------------
    plan = plan_cohort(
        cohort,
        decision_stage=decision_stage,
        survivor_count=survivor_count,
        audit_quota=audit_quota,
        population_size=population_size,
        max_children_per_parent=max_children_per_parent,
        seed=seed,
        parent_params_by_id=parent_params_by_id,
        doomed_rule_version=doomed_rule_version,
        scheduler_version=scheduler_version,
        planner_version=planner_version,
    )

    trace_refs: List[DecisionTraceRef] = []

    # Persist fork intents so recovery can replay them deterministically.
    intent_refs = write_fork_intents(
        _trace_writer, _cohort_id, decision_stage, seed,
        plan.fork_plans,
    )
    trace_refs.extend(intent_refs)

    # ------------------------------------------------------------------
    # Phase 2 — persist decisions + write trace entries
    # ------------------------------------------------------------------
    trial_outcomes: Dict[str, str] = {}
    for obs, doomed, gwtw in zip(plan.observations,
                                 plan.doomed_decisions,
                                 plan.gwtw_decisions):
        trial = _find_trial_in_cohort(cohort, obs.trial_id)
        trial_outcomes[obs.trial_id] = gwtw.action

        # 2a) Write observation snapshot to trace.
        obs_ref = _trace_writer.append({
            "entry_type": "observation",
            "cohort_id": _cohort_id,
            "trial_id": obs.trial_id,
            "cohort_stage": decision_stage,
            "cohort_seed": seed,
            "data": obs.to_dict(),
        })
        trace_refs.append(obs_ref)

        # 2b) Write doomed decision to trace.
        doomed_ref = _trace_writer.append({
            "entry_type": "doomed_decision",
            "cohort_id": _cohort_id,
            "trial_id": obs.trial_id,
            "cohort_stage": decision_stage,
            "cohort_seed": seed,
            "data": doomed.to_dict(),
            "rule_version": doomed_rule_version,
        })
        trace_refs.append(doomed_ref)

        # 2c) Write GWTW decision to trace.
        gwtw_ref = _trace_writer.append({
            "entry_type": "gwtw_decision",
            "cohort_id": _cohort_id,
            "trial_id": obs.trial_id,
            "cohort_stage": decision_stage,
            "cohort_seed": seed,
            "data": gwtw.to_dict(),
            "scheduler_version": scheduler_version,
        })
        trace_refs.append(gwtw_ref)

        # 2d) Record refs on the trial.
        if not trial.doomed_decisions:
            trial.doomed_decisions = []
        trial.doomed_decisions.append(doomed)
        if not trial.gwtw_decisions:
            trial.gwtw_decisions = []
        trial.gwtw_decisions.append(gwtw)
        if not trial.decision_trace_refs:
            trial.decision_trace_refs = []
        trial.decision_trace_refs.extend([obs_ref, doomed_ref, gwtw_ref])

        # 2e) Pause status.
        if gwtw.action == "pause":
            trial.status = "paused"

        trial_mgr.update(trial)

    # ------------------------------------------------------------------
    # Phase 3 — create child trials from ForkPlans
    # ------------------------------------------------------------------
    child_ids: List[str] = []
    child_resolutions: List[ExecutionResolution] = []

    for fp in plan.fork_plans:
        parent_trial = _find_trial_in_cohort(
            cohort, fp.fork_request.parent_trial_id)

        # 3a) Create the child trial.
        child = trial_mgr.create(
            experiment_id=experiment_id,
            parent_trial_id=parent_trial.trial_id,
            branch_stage=decision_stage,
            iteration=iteration,
        )
        child.params = copy.deepcopy(fp.child_params)
        child.doomed_decisions = []
        child.gwtw_decisions = []
        child.decision_trace_refs = []

        # 3b) Write fork evidence to trace.
        fork_ref = _trace_writer.append({
            "entry_type": "fork",
            "cohort_id": _cohort_id,
            "trial_id": child.trial_id,
            "parent_trial_id": parent_trial.trial_id,
            "cohort_stage": decision_stage,
            "cohort_seed": seed,
            "data": {
                "checkpoint_id": fp.checkpoint_id,
                "param_name": fp.evidence.param_name,
                "old_value": fp.evidence.old_value,
                "new_value": fp.evidence.new_value,
                "derived_seed": fp.derived_seed,
                "planner_version": planner_version,
            },
        })
        trace_refs.append(fork_ref)
        child.decision_trace_refs.append(fork_ref)

        # 3c) Resolve child's checkpoint.
        inherited_params = copy.deepcopy(
            parent_params_by_id[parent_trial.trial_id])

        resolution = _resolve_child_checkpoint(
            parent_trial=parent_trial,
            child=child,
            child_params=fp.child_params,
            inherited_params=inherited_params,
            checkpoint_id=fp.checkpoint_id,
            tree=tree,
            trial_mgr=trial_mgr,
            checkpoint_mgr=checkpoint_mgr,
            runs_dir=_runs_dir,
        )
        child.execution_resolution = resolution

        # 3d) Write ExecutionResolution to trace.
        er_ref = _trace_writer.append({
            "entry_type": "execution_resolution",
            "cohort_id": _cohort_id,
            "trial_id": child.trial_id,
            "parent_trial_id": parent_trial.trial_id,
            "cohort_stage": decision_stage,
            "cohort_seed": seed,
            "data": resolution.to_dict(),
        })
        trace_refs.append(er_ref)
        child.decision_trace_refs.append(er_ref)

        # 3e) Persist the child.
        trial_mgr.update(child)

        child_ids.append(child.trial_id)
        child_resolutions.append(resolution)

    # ------------------------------------------------------------------
    # Phase 3.6 — write cohort_complete sentinel
    # ------------------------------------------------------------------
    sentinel_ref = write_cohort_complete(
        _trace_writer, decision_stage, seed,
        trial_ids=[t.trial_id for t in cohort],
        survivor_count=survivor_count, audit_quota=audit_quota,
        population_size=population_size,
        max_children_per_parent=max_children_per_parent,
        doomed_rule_version=doomed_rule_version,
        scheduler_version=scheduler_version,
        planner_version=planner_version,
    )
    trace_refs.append(sentinel_ref)

    # ------------------------------------------------------------------
    # Phase 4 — assemble result
    # ------------------------------------------------------------------
    return CohortExecutionResult(
        decision_stage=decision_stage,
        cohort_plan=plan,
        trial_outcomes=trial_outcomes,
        child_trial_ids=child_ids,
        child_checkpoint_resolutions=child_resolutions,
        trace_refs=trace_refs,
        seed=seed,
    )


# =============================================================================
# Reconstruction
# =============================================================================

from gwtw.cohort_common import (
    _rebuild_from_disk,
    _resume_forks_from_disk,
    _find_trial_in_cohort,
    _resolve_child_checkpoint,
    reconstruct_cohort_decisions,
)
