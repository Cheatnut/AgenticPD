# -*- coding: utf-8 -*-
"""cohort_executor.py — Stage D: serial cohort executor for PL/CTS single-layer loop.

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

from cohort_planner import (
    CohortPlan,
    CohortPlanError,
    ForkPlan,
    plan_cohort,
)
from checkpoint_resolver import resolve_checkpoint
from decision_trace import (
    DEFAULT_TRACE_PATH,
    DecisionTraceWriter,
    cohort_already_executed,
    cohort_decision_written,
    make_cohort_id,
    read_fork_intents,
    read_trace,
    write_cohort_complete,
    write_fork_intents,
)
from doomed_predictor import DEFAULT_RULE_VERSION as _DEFAULT_DOOMED_VERSION
from gwtw_scheduler import DEFAULT_SCHEDULER_VERSION as _DEFAULT_SCHEDULER_VERSION
from managers import CheckpointManager, TrialManager
from mutation_planner import DEFAULT_PLANNER_VERSION as _DEFAULT_PLANNER_VERSION
from optimization_tree import OptimizationTree
from schemas.trial import (
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


def reconstruct_cohort_decisions(
    runs_dir: Path,
    decision_stage: str,
    seed: int,
    trial_ids: List[str],
    trace_path: str = DEFAULT_TRACE_PATH,
    survivor_count: int = 0,
    audit_quota: int = 0,
    population_size: int = 0,
    max_children_per_parent: int = 0,
    doomed_rule_version: str = "",
    scheduler_version: str = "",
    planner_version: str = "",
) -> Dict[str, Dict[str, Any]]:
    """Rebuild in-memory decision state from the trace JSONL on disk.

    Filters by *cohort_id* (computed from all config params), not just
    stage+seed, so different planning configs on the same trial set are
    isolated.
    """
    _cohort_id = make_cohort_id(
        decision_stage, seed, trial_ids,
        survivor_count=survivor_count, audit_quota=audit_quota,
        population_size=population_size,
        max_children_per_parent=max_children_per_parent,
        doomed_rule_version=doomed_rule_version,
        scheduler_version=scheduler_version,
        planner_version=planner_version,
    )
    entries = read_trace(runs_dir, trace_path)
    result: Dict[str, Dict[str, Any]] = {}
    for entry in entries:
        if entry.get("cohort_id") != _cohort_id:
            continue
        tid = entry.get("trial_id", "")
        if tid not in trial_ids:
            continue
        if tid not in result:
            result[tid] = {}
        etype = entry.get("entry_type", "")
        if etype == "observation":
            result[tid]["observation"] = entry.get("data")
        elif etype == "doomed_decision":
            result[tid]["doomed"] = entry.get("data")
        elif etype == "gwtw_decision":
            result[tid]["gwtw"] = entry.get("data")
    return result


# =============================================================================
# Helpers
# =============================================================================


def _find_trial_in_cohort(
    cohort: List[TrialRecord], trial_id: str,
) -> TrialRecord:
    for t in cohort:
        if t.trial_id == trial_id:
            return t
    raise CohortPlanError(
        f"Trial {trial_id!r} not found in cohort")


def _resolve_child_checkpoint(
    parent_trial: TrialRecord,
    child: TrialRecord,
    child_params: Dict[str, Dict[str, Any]],
    inherited_params: Dict[str, Dict[str, Any]],
    checkpoint_id: str,
    tree: OptimizationTree,
    trial_mgr: TrialManager,
    checkpoint_mgr: CheckpointManager,
    runs_dir: Optional[Path] = None,
) -> ExecutionResolution:
    parent_node_id = _find_parent_node_id(tree, parent_trial.trial_id)

    if parent_node_id is None:
        log.warning(
            "[EXECUTOR] parent trial %s not found in tree; "
            "producing full_restart resolution for child %s",
            parent_trial.trial_id, child.trial_id,
        )
        return ExecutionResolution(
            requested_parent_node_id=parent_trial.trial_id,
            requested_start_stage=child.branch_stage or "CTS",
            effective_start_stage="FP",
            execution_mode="full_restart",
            fallback_reason=(
                f"parent trial {parent_trial.trial_id!r} not found in "
                f"optimization tree — full restart required"
            ),
        )

    parent_cp = parent_trial.checkpoint
    cp_stage = parent_cp.stage if parent_cp else "PL"
    _STAGE_NEXT: Dict[str, str] = {"FP": "PL", "PL": "CTS", "CTS": "RT"}
    effective_stage = _STAGE_NEXT.get(cp_stage, "CTS")

    return resolve_checkpoint(
        requested_parent_node_id=parent_node_id,
        requested_start_stage=effective_stage,
        candidate_params=child_params,
        inherited_params=inherited_params,
        tree=tree,
        trial_mgr=trial_mgr,
        checkpoint_mgr=checkpoint_mgr,
        runs_dir=runs_dir,
    )


def _find_parent_node_id(
    tree: OptimizationTree, source_trial_id: str,
) -> Optional[str]:
    _FLOW_ORDER: Dict[str, int] = {"FP": 0, "PL": 1, "CTS": 2, "RT": 3}
    best_node_id: Optional[str] = None
    best_order: int = -1
    for node in tree._nodes.values():
        if getattr(node, "source_trial_id", None) != source_trial_id:
            continue
        order = _FLOW_ORDER.get(node.stage, -1)
        if order > best_order:
            best_order = order
            best_node_id = node.node_id
    return best_node_id


def _resume_forks_from_disk(
    cohort: List[TrialRecord],
    decision_stage: str,
    seed: int,
    survivor_count: int,
    audit_quota: int,
    population_size: int,
    max_children_per_parent: int,
    parent_params_by_id: Dict[str, Dict[str, Dict[str, Any]]],
    trial_mgr: TrialManager,
    checkpoint_mgr: CheckpointManager,
    tree: OptimizationTree,
    experiment_id: str,
    iteration: int,
    runs_dir: Path,
    trace_path: str,
    planner_version: str,
    doomed_rule_version: str,
    scheduler_version: str,
) -> CohortExecutionResult:
    """Resume fork creation from persisted intents.

    1. Reconstruct decisions and cohort_id from trace.
    2. Read fork intents; for each intent, check whether a child
       TrialRecord AND an ExecutionResolution entry both exist on disk.
       Only create children for intents where either is missing.
    3. Write sentinel only when ALL intents are fulfilled.
    """
    _trace_writer = DecisionTraceWriter(runs_dir, trace_path)

    _trial_ids = [t.trial_id for t in cohort]
    _ver = (doomed_rule_version, scheduler_version, planner_version)
    _cfg = (survivor_count, audit_quota, population_size,
            max_children_per_parent, *_ver)
    _cohort_id = make_cohort_id(decision_stage, seed, _trial_ids, *_cfg)

    # 1) Reconstruct decisions and already-created children from trace.
    recon = reconstruct_cohort_decisions(
        runs_dir, decision_stage, seed, _trial_ids,
        trace_path=trace_path,
        survivor_count=survivor_count, audit_quota=audit_quota,
        population_size=population_size,
        max_children_per_parent=max_children_per_parent,
        doomed_rule_version=doomed_rule_version,
        scheduler_version=scheduler_version,
        planner_version=planner_version,
    )
    trial_outcomes: Dict[str, str] = {}
    for t in cohort:
        if t.trial_id in recon and "gwtw" in recon[t.trial_id]:
            trial_outcomes[t.trial_id] = recon[t.trial_id]["gwtw"].get(
                "action", "pause")
        else:
            trial_outcomes[t.trial_id] = "pause"

    # 2-3) Single-pass collection: trace refs (deduped by decision_id)
    #    + pre-seed already-complete children.
    trace_refs: List[DecisionTraceRef] = []
    child_ids: List[str] = []
    _resolution_map: Dict[str, ExecutionResolution] = {}
    _seen_ref_ids: set = set()

    all_entries = read_trace(runs_dir, trace_path)
    cohort_entries = [e for e in all_entries
                      if e.get("cohort_id") == _cohort_id]

    # Collect cohort trace refs (deduped, single pass).
    for entry in cohort_entries:
        did = entry.get("decision_id", "")
        if did and did not in _seen_ref_ids:
            _seen_ref_ids.add(did)
            trace_refs.append(DecisionTraceRef(
                decision_id=did, trace_path=trace_path))

    fork_tids = {e["trial_id"] for e in cohort_entries
                 if e.get("entry_type") == "fork"}
    er_tids = {e["trial_id"] for e in cohort_entries
               if e.get("entry_type") == "execution_resolution"}
    complete_tids = fork_tids & er_tids

    # Pre-seed: children whose fork+er+trial+persisted-ER are all intact.
    for tid in sorted(complete_tids):
        child_trial = trial_mgr.get(tid)
        if child_trial is None:
            continue
        if child_trial.execution_resolution is None:
            continue
        child_ids.append(tid)
        _resolution_map[tid] = child_trial.execution_resolution

    # 4) Replay fork intents.
    from mutation_planner import plan_child_params
    from gwtw_scheduler import ForkRequest

    intents = read_fork_intents(runs_dir, trace_path, _cohort_id)
    for intent in intents:
        intent_child_id, status = _find_child_for_intent(
            intent, cohort_entries, trial_mgr,
        )
        if status == "complete":
            if intent_child_id not in child_ids:
                child_ids.append(intent_child_id)
            continue

        if status == "fix_er":
            # ER trace exists, TrialRecord exists, but ER inconsistent.
            child_trial = trial_mgr.get(intent_child_id)

            # Re-generate params from intent (may not be persisted).
            parent_params = copy.deepcopy(
                parent_params_by_id[intent["parent_trial_id"]])
            fork_seed = intent["derived_seed"]
            fr = ForkRequest(
                parent_trial_id=intent["parent_trial_id"],
                decision_stage=decision_stage,
                reason="population_replenishment",
            )
            child_params, _evidence = plan_child_params(
                fr, parent_params=parent_params, seed=fork_seed,
                planner_version=planner_version,
            )
            child_trial.params = child_params

            # Fix ER from trace.
            er_entries_for_child = [e for e in cohort_entries
                                    if e.get("entry_type") == "execution_resolution"
                                    and e.get("trial_id") == intent_child_id]
            if er_entries_for_child:
                er_data = er_entries_for_child[-1].get("data", {})
                if isinstance(er_data, dict) and er_data:
                    fixed_er = ExecutionResolution.from_dict(er_data)
                    if fixed_er is not None:
                        child_trial.execution_resolution = fixed_er
                        _resolution_map[intent_child_id] = fixed_er
            # Fix trace refs on the child (deduped).
            _fix_child_trace_refs(child_trial, cohort_entries,
                                  intent_child_id, trace_path,
                                  _seen_ref_ids, trace_refs)
            trial_mgr.update(child_trial)
            if intent_child_id not in child_ids:
                child_ids.append(intent_child_id)
            continue

        if status == "resolve":
            # Fork + trial exist, but NO ER trace entry.
            # Re-generate params, re-run resolver, write ER.
            child_trial = trial_mgr.get(intent_child_id)
            parent_id = intent["parent_trial_id"]
            parent_trial = _find_trial_in_cohort(cohort, parent_id)
            inherited = copy.deepcopy(parent_params_by_id[parent_id])

            # Re-generate params from intent.
            fork_seed = intent["derived_seed"]
            fr = ForkRequest(
                parent_trial_id=parent_id,
                decision_stage=decision_stage,
                reason="population_replenishment",
            )
            child_params, _evidence = plan_child_params(
                fr, parent_params=copy.deepcopy(parent_params_by_id[parent_id]),
                seed=fork_seed, planner_version=planner_version,
            )
            child_trial.params = child_params

            cp_id = intent.get("checkpoint_id", "unknown")
            resolution = _resolve_child_checkpoint(
                parent_trial=parent_trial, child=child_trial,
                child_params=child_trial.params,
                inherited_params=inherited,
                checkpoint_id=cp_id, tree=tree,
                trial_mgr=trial_mgr, checkpoint_mgr=checkpoint_mgr,
                runs_dir=runs_dir,
            )
            child_trial.execution_resolution = resolution
            _resolution_map[intent_child_id] = resolution

            # Write ER trace entry.
            er_ref = _trace_writer.append({
                "entry_type": "execution_resolution",
                "cohort_id": _cohort_id,
                "trial_id": intent_child_id,
                "parent_trial_id": parent_id,
                "cohort_stage": decision_stage,
                "cohort_seed": seed,
                "data": resolution.to_dict(),
            })
            if er_ref.decision_id not in _seen_ref_ids:
                _seen_ref_ids.add(er_ref.decision_id)
                trace_refs.append(er_ref)

            # Fix trace refs.
            _fix_child_trace_refs(child_trial, cohort_entries,
                                  intent_child_id, trace_path,
                                  _seen_ref_ids, trace_refs)
            child_trial.decision_trace_refs.append(er_ref)
            trial_mgr.update(child_trial)
            if intent_child_id not in child_ids:
                child_ids.append(intent_child_id)
            continue

        # status == "missing" — create a new child.
        parent_id = intent["parent_trial_id"]
        parent_trial = _find_trial_in_cohort(cohort, parent_id)

        fr = ForkRequest(
            parent_trial_id=parent_id,
            decision_stage=decision_stage,
            reason="population_replenishment",
        )

        fork_seed = intent["derived_seed"]
        parent_params = copy.deepcopy(parent_params_by_id[parent_id])
        child_params, evidence = plan_child_params(
            fr, parent_params=parent_params, seed=fork_seed,
            planner_version=planner_version,
        )

        child = trial_mgr.create(
            experiment_id=experiment_id,
            parent_trial_id=parent_id,
            branch_stage=decision_stage,
            iteration=iteration,
        )
        child.params = child_params
        child.doomed_decisions = []
        child.gwtw_decisions = []
        child.decision_trace_refs = []

        # Fork trace entry.
        parent_cp = parent_trial.checkpoint
        cp_id = parent_cp.checkpoint_id if parent_cp else "unknown"
        fork_ref = _trace_writer.append({
            "entry_type": "fork",
            "cohort_id": _cohort_id,
            "trial_id": child.trial_id,
            "parent_trial_id": parent_id,
            "cohort_stage": decision_stage,
            "cohort_seed": seed,
            "data": {
                "checkpoint_id": cp_id,
                "param_name": evidence.param_name,
                "old_value": evidence.old_value,
                "new_value": evidence.new_value,
                "derived_seed": fork_seed,
                "planner_version": planner_version,
            },
        })
        trace_refs.append(fork_ref)
        child.decision_trace_refs.append(fork_ref)

        # Resolve checkpoint.
        inherited = copy.deepcopy(parent_params_by_id[parent_id])
        resolution = _resolve_child_checkpoint(
            parent_trial=parent_trial, child=child,
            child_params=child_params, inherited_params=inherited,
            checkpoint_id=cp_id, tree=tree,
            trial_mgr=trial_mgr, checkpoint_mgr=checkpoint_mgr,
            runs_dir=runs_dir,
        )
        child.execution_resolution = resolution
        _resolution_map[child.trial_id] = resolution

        # ER trace entry.
        er_ref = _trace_writer.append({
            "entry_type": "execution_resolution",
            "cohort_id": _cohort_id,
            "trial_id": child.trial_id,
            "parent_trial_id": parent_id,
            "cohort_stage": decision_stage,
            "cohort_seed": seed,
            "data": resolution.to_dict(),
        })
        trace_refs.append(er_ref)
        child.decision_trace_refs.append(er_ref)

        trial_mgr.update(child)
        child_ids.append(child.trial_id)

    # 5) Write sentinel only if all intents are fulfilled.
    if not cohort_already_executed(
        runs_dir, trace_path, decision_stage, seed,
        _trial_ids, *_cfg,
    ):
        sentinel_ref = write_cohort_complete(
            _trace_writer, decision_stage, seed,
            _trial_ids, *_cfg,
        )
        trace_refs.append(sentinel_ref)

    return CohortExecutionResult(
        decision_stage=decision_stage,
        cohort_plan=None,
        trial_outcomes=trial_outcomes,
        child_trial_ids=child_ids,
        child_checkpoint_resolutions=list(_resolution_map.values()),
        trace_refs=trace_refs,
        seed=seed,
    )


def _fix_child_trace_refs(
    child_trial: TrialRecord,
    cohort_entries: List[Dict[str, Any]],
    child_tid: str,
    trace_path: str,
    seen_ref_ids: set,
    trace_refs: List[DecisionTraceRef],
) -> None:
    """Add missing trace refs to *child_trial*, deduped by decision_id."""
    if not child_trial.decision_trace_refs:
        child_trial.decision_trace_refs = []
    existing_dids = {r.decision_id for r in child_trial.decision_trace_refs}
    for entry in cohort_entries:
        if entry.get("trial_id") != child_tid:
            continue
        did = entry.get("decision_id", "")
        if not did or did in existing_dids:
            continue
        child_trial.decision_trace_refs.append(
            DecisionTraceRef(decision_id=did, trace_path=trace_path))
        existing_dids.add(did)
        if did not in seen_ref_ids:
            seen_ref_ids.add(did)
            trace_refs.append(DecisionTraceRef(
                decision_id=did, trace_path=trace_path))


def _find_child_for_intent(
    intent: Dict[str, Any],
    cohort_entries: List[Dict[str, Any]],
    trial_mgr: TrialManager,
) -> Tuple[Optional[str], str]:
    """Given a fork_intent, find the child trial created from it.

    Only considers entries whose ``cohort_id`` matches *intent*.

    Returns ``(child_tid, status)`` where *status* is one of:

    * ``"complete"`` — fork, ER trace, and TrialRecord with matching ER
      all present.
    * ``"fix_er"`` — fork exists, ER trace entry exists, TrialRecord
      exists, but the persisted ER is missing or inconsistent.  Caller
      should patch the TrialRecord in-place.
    * ``"resolve"`` — fork trace entry exists, TrialRecord exists, but
      NO ER trace entry exists.  Caller must re-run the checkpoint
      resolver, write the ER trace entry, and update the TrialRecord.
    * ``"missing"`` — no fork entry, or no TrialRecord.  Caller must
      create a new child from scratch.
    """
    parent_id = intent.get("parent_trial_id", "")
    param_name = intent.get("param_name", "")
    derived_seed = intent.get("derived_seed")
    cid = intent.get("cohort_id", "")

    # Find matching fork entry — must share the intent's cohort_id.
    fork_entries = [e for e in cohort_entries
                    if e.get("entry_type") == "fork"
                    and e.get("cohort_id") == cid
                    and e.get("parent_trial_id") == parent_id]
    matching_fork = None
    for fe in fork_entries:
        fd = fe.get("data", {})
        if (fd.get("param_name") == param_name
                and fd.get("derived_seed") == derived_seed):
            matching_fork = fe
            break
    if matching_fork is None:
        return None, "missing"

    child_tid = matching_fork.get("trial_id", "")
    if not child_tid:
        return None, "missing"

    # Check TrialRecord on disk.
    child_trial = trial_mgr.get(child_tid)
    if child_trial is None:
        return None, "missing"

    # Check ER entry exists in trace for this child (same cohort_id).
    er_entries = [e for e in cohort_entries
                  if e.get("entry_type") == "execution_resolution"
                  and e.get("cohort_id") == cid
                  and e.get("trial_id") == child_tid]
    if not er_entries:
        # Fork exists, trial exists, but NO ER trace entry —
        # caller must resolve and write.
        return child_tid, "resolve"

    # ER trace exists — verify the persisted ER matches.
    if child_trial.execution_resolution is None:
        return child_tid, "fix_er"

    # Full to_dict comparison.
    trace_er_data = er_entries[-1].get("data", {})
    if isinstance(trace_er_data, dict) and trace_er_data:
        trace_er = ExecutionResolution.from_dict(trace_er_data)
        if trace_er is None:
            return child_tid, "fix_er"
        if child_trial.execution_resolution.to_dict() != trace_er.to_dict():
            return child_tid, "fix_er"

    return child_tid, "complete"


def _rebuild_from_disk(
    runs_dir: Path,
    trace_path: str,
    cohort_id: str,
    cohort: List[TrialRecord],
    trial_mgr: TrialManager,
) -> CohortExecutionResult:
    """Reconstruct a CohortExecutionResult from disk (idempotency path).

    Only reads entries whose ``cohort_id`` matches *cohort_id* exactly —
    never leaks data from other cohorts that happen to share the same
    trace file.
    """
    trial_outcomes: Dict[str, str] = {}
    trace_refs: List[DecisionTraceRef] = []
    child_ids: List[str] = []
    child_resolutions: List[ExecutionResolution] = []

    entries = read_trace(runs_dir, trace_path)
    cohort_tids = {t.trial_id for t in cohort}
    for entry in entries:
        if entry.get("cohort_id") != cohort_id:
            continue
        did = entry.get("decision_id", "")
        etype = entry.get("entry_type", "")

        if did:
            trace_refs.append(DecisionTraceRef(
                decision_id=did, trace_path=trace_path))

        tid = entry.get("trial_id", "")

        if etype == "gwtw_decision" and tid in cohort_tids:
            data = entry.get("data", {})
            trial_outcomes[tid] = data.get("action", "pause")

        if etype == "fork":
            child_ids.append(tid)

        if etype == "execution_resolution":
            er_data = entry.get("data", {})
            if isinstance(er_data, dict) and er_data:
                er = ExecutionResolution.from_dict(er_data)
                if er is not None:
                    child_resolutions.append(er)

    _stage = ""
    _seed = 0
    for entry in entries:
        if entry.get("cohort_id") == cohort_id:
            _stage = entry.get("cohort_stage", "")
            _seed = entry.get("cohort_seed", 0)
            break

    return CohortExecutionResult(
        decision_stage=_stage,
        cohort_plan=None,
        trial_outcomes=trial_outcomes,
        child_trial_ids=child_ids,
        child_checkpoint_resolutions=child_resolutions,
        trace_refs=trace_refs,
        seed=_seed,
    )


# =============================================================================
# Self-test
# =============================================================================

if __name__ == "__main__":
    import shutil
    import sys
    import tempfile

    from optimization_tree import OptimizationTree, ROOT_ID

    ok = 0
    fail_count = 0

    def check(cond, msg):
        global ok, fail_count
        if cond:
            ok += 1
        else:
            fail_count += 1
            print(f"  FAIL: {msg}")

    tmpdir = Path(tempfile.mkdtemp())
    flow_dir = tmpdir / "flow"
    runs_dir = tmpdir / "runs"
    runs_dir.mkdir(parents=True)

    platform, design, variant = "sky130hd", "gcd", "base"
    for cat in ("results",):
        d = flow_dir / cat / platform / design / variant
        d.mkdir(parents=True)
    for fname in ("2_floorplan.odb", "2_floorplan.sdc",
                  "3_place.odb", "3_place.sdc",
                  "4_cts.odb", "4_cts.sdc"):
        (flow_dir / "results" / platform / design / variant
         / fname).write_text(f"fake {fname}")

    trial_mgr = TrialManager(runs_dir)
    checkpoint_mgr = CheckpointManager(flow_dir)

    param_hash = CheckpointManager.param_hash(
        {"FP": {"CORE_UTILIZATION": 38}, "PL": {}, "CTS": {}, "RT": {}})

    _BASELINE = {
        "FP": {"CORE_UTILIZATION": 38, "CORE_ASPECT_RATIO": 1.0},
        "PL": {}, "CTS": {},
        "RT": {"FASTROUTE_LAYER_ADJUSTMENT": 0.2,
               "GRT_CONGESTION_ITERATIONS": 30},
    }

    import schemas.trial as _st

    def _make_trial(wns, tns, tree, iteration):
        t = trial_mgr.create(experiment_id="test", iteration=iteration)
        t.status = "ok"
        t.params = copy.deepcopy(_BASELINE)
        t.stage_results = [
            _st.StageResult(stage="FP", status="ok", elapsed_s=10.0, exit_code=0),
            _st.StageResult(stage="PL", status="ok", elapsed_s=15.0, exit_code=0,
                            stage_qor={"PL_tag_ws_ps": wns, "PL_tag_tns_ps": tns}),
        ]
        cp = checkpoint_mgr.create(
            trial=t, stage="PL", platform=platform, design=design,
            variant=variant, param_hash=param_hash, runs_dir=runs_dir)
        t.checkpoint = cp
        trial_mgr.update(t)

        fp_nid = tree.add_path(
            iteration * 10 + 100, ROOT_ID,
            [("FP", f"v-{t.trial_id}-fp", {"CORE_UTILIZATION": 38}, {"fp_ws_ps": -45.0})],
            source_trial_id=t.trial_id)[0]
        tree.add_path(
            iteration * 10 + 100, fp_nid,
            [("PL", f"v-{t.trial_id}-pl", {}, {"pl_ws_ps": float(wns)})],
            source_trial_id=t.trial_id)
        return t

    # -- 1. Basic PL execution --
    tree = OptimizationTree()
    t_a = _make_trial(-50, -100, tree, 0)
    t_b = _make_trial(-200, -500, tree, 1)
    pb = {t.trial_id: copy.deepcopy(_BASELINE) for t in [t_a, t_b]}

    result = execute_cohort(
        [t_a, t_b], "PL", 2, 0, 4, 2, seed=42,
        parent_params_by_id=pb,
        trial_mgr=trial_mgr, checkpoint_mgr=checkpoint_mgr,
        tree=tree, runs_dir=runs_dir,
    )
    check(len(result.trial_outcomes) == 2, f"2 outcomes: {result.trial_outcomes}")
    check(len(result.child_trial_ids) == 2, f"2 children: {result.child_trial_ids}")
    # Trace entries: 2 obs + 2 doomed + 2 gwtw + 2 fork_intent + 2 fork + 2 er + 1 sentinel = 13
    check(len(result.trace_refs) == 13,
          f"13 trace refs: {len(result.trace_refs)}")
    check(result.cohort_plan is not None, "cohort_plan populated")

    # -- 2. Trace file exists with expected entries --
    entries = read_trace(runs_dir, "traces/decisions.jsonl")
    types = [e["entry_type"] for e in entries]
    check(types.count("observation") == 2, f"2 observations: {types.count('observation')}")
    check(types.count("doomed_decision") == 2, f"2 doomed: {types.count('doomed_decision')}")
    check(types.count("gwtw_decision") == 2, f"2 gwtw: {types.count('gwtw_decision')}")
    check(types.count("fork") == 2, f"2 forks: {types.count('fork')}")
    check(types.count("execution_resolution") == 2,
          f"2 ers: {types.count('execution_resolution')}")
    for e in entries:
        check(e["cohort_stage"] == "PL", f"stage PL: {e['cohort_stage']}")
        check(e["cohort_seed"] == 42, f"seed 42: {e['cohort_seed']}")

    # -- 3. Trials have trace refs on disk --
    t_a2 = trial_mgr.get(t_a.trial_id)
    check(len(t_a2.decision_trace_refs) == 3,
          f"t_a 3 refs: {len(t_a2.decision_trace_refs)}")
    for ref in t_a2.decision_trace_refs:
        check(ref.trace_path == "traces/decisions.jsonl", f"ref path: {ref.trace_path}")
        check(ref.decision_id.startswith("dtr-"), f"ref id: {ref.decision_id}")

    # -- 4. Reconstruction from trace --
    recon = reconstruct_cohort_decisions(
        runs_dir, "PL", seed=42,
        trial_ids=[t_a.trial_id, t_b.trial_id],
        survivor_count=2, population_size=4, max_children_per_parent=2,
        doomed_rule_version="1.0.0", scheduler_version="1.0.0",
        planner_version="1.0.0",
    )
    check(len(recon) == 2, f"recon 2 trials: {len(recon)}")
    for tid in [t_a.trial_id, t_b.trial_id]:
        check(tid in recon, f"recon has {tid}")
        check("observation" in recon[tid], f"recon {tid} has observation")
        check("doomed" in recon[tid], f"recon {tid} has doomed")
        check("gwtw" in recon[tid], f"recon {tid} has gwtw")
        check(recon[tid]["gwtw"]["action"] == "continue",
              f"recon {tid} action=continue")

    # -- 5. Idempotency: re-executing the same cohort returns from disk --
    result2 = execute_cohort(
        [t_a, t_b], "PL", 2, 0, 4, 2, seed=42,
        parent_params_by_id=pb,
        trial_mgr=trial_mgr, checkpoint_mgr=checkpoint_mgr,
        tree=tree, runs_dir=runs_dir,
    )
    check(result2.cohort_plan is None, "idempotent: cohort_plan is None")
    check(result2.trial_outcomes == result.trial_outcomes,
          "idempotent: same outcomes")
    check(result2.seed == 42, "idempotent: seed preserved")
    check(len(result2.trace_refs) > 0, "idempotent: has trace refs")

    # -- 6. Different seed is NOT idempotent --
    result3 = execute_cohort(
        [t_a, t_b], "PL", 2, 0, 4, 2, seed=99,
        parent_params_by_id=pb,
        trial_mgr=trial_mgr, checkpoint_mgr=checkpoint_mgr,
        tree=tree, runs_dir=runs_dir,
    )
    check(result3.cohort_plan is not None,
          "different seed → not idempotent, plan created")
    check(len(result3.trace_refs) >= 10,
          f"different seed: new trace entries, got {len(result3.trace_refs)}")

    # -- 7. CTS trace entries have correct stage --
    def _make_cts(wns, tns, tree, iteration):
        t = trial_mgr.create(experiment_id="cts", iteration=iteration)
        t.status = "ok"
        t.params = copy.deepcopy(_BASELINE)
        t.stage_results = [
            _st.StageResult(stage="FP", status="ok", elapsed_s=10.0, exit_code=0),
            _st.StageResult(stage="PL", status="ok", elapsed_s=15.0, exit_code=0),
            _st.StageResult(stage="CTS", status="ok", elapsed_s=12.0, exit_code=0,
                            stage_qor={"CTS_tag_ws_ps": wns, "CTS_tag_tns_ps": tns}),
        ]
        cp = checkpoint_mgr.create(
            trial=t, stage="CTS", platform=platform, design=design,
            variant=variant, param_hash=param_hash, runs_dir=runs_dir)
        t.checkpoint = cp
        trial_mgr.update(t)
        fp_nid = tree.add_path(
            iteration * 10 + 800, ROOT_ID,
            [("FP", f"v-{t.trial_id}-fp", {"CORE_UTILIZATION": 38}, {"fp_ws_ps": -45.0})],
            source_trial_id=t.trial_id)[0]
        pl_nid = tree.add_path(
            iteration * 10 + 800, fp_nid,
            [("PL", f"v-{t.trial_id}-pl", {}, {"pl_ws_ps": -50.0})],
            source_trial_id=t.trial_id)[0]
        tree.add_path(
            iteration * 10 + 800, pl_nid,
            [("CTS", f"v-{t.trial_id}-cts", {}, {"cts_ws_ps": float(wns)})],
            source_trial_id=t.trial_id)
        return t

    tree_cts = OptimizationTree()
    c_a = _make_cts(-50, -100, tree_cts, 10)
    c_b = _make_cts(-200, -600, tree_cts, 11)
    pb_cts = {c_a.trial_id: copy.deepcopy(_BASELINE),
              c_b.trial_id: copy.deepcopy(_BASELINE)}
    r_cts = execute_cohort(
        [c_a, c_b], "CTS", 2, 0, 4, 2, seed=7,
        parent_params_by_id=pb_cts,
        trial_mgr=trial_mgr, checkpoint_mgr=checkpoint_mgr,
        tree=tree_cts, runs_dir=runs_dir,
    )
    cts_entries = [e for e in read_trace(runs_dir, "traces/decisions.jsonl")
                   if e["cohort_stage"] == "CTS" and e["cohort_seed"] == 7]
    check(len(cts_entries) >= 8, f"CTS trace entries: {len(cts_entries)}")
    check(all(e["cohort_stage"] == "CTS" for e in cts_entries),
          "all CTS entries have cohort_stage=CTS")

    shutil.rmtree(tmpdir)

    total = ok + fail_count
    print(f"\n{'='*50}")
    print(f"  {ok}/{total} passed"
          + (f", {fail_count} FAILED" if fail_count else " — ALL OK"))
    print(f"{'='*50}")
    sys.exit(1 if fail_count else 0)
