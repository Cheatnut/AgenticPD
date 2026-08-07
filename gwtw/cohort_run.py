# -*- coding: utf-8 -*-
"""gwtw/cohort_run.py — Doomed/GWTW orchestration helpers (extracted from orchestrator)."""

from __future__ import annotations

import logging

import copy
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from gwtw.cohort_execute import CohortExecutionResult
from gwtw.cohort_execute import execute_cohort
from gwtw.cohort_plan import plan_cohort
from config import BASELINE_PARAMS
from storage.decision_trace import make_cohort_id
from storage.trace_io import write_cohort_complete
from core.models import DecisionTraceRef
from core.models import ExecutionResolution
from core.models import TrialRecord

_STAGE_NEXT: Dict[str, str] = {"FP": "PL", "PL": "CTS", "CTS": "RT"}

log = logging.getLogger("gwtw")


def _run_cohort(
    orch, cohort: List[TrialRecord], decision_stage: str,
    survivor_count: int, audit_quota: int, population_size: int,
    max_children_per_parent: int, doomed_rule_version: str,
    scheduler_version: str, planner_version: str,
) -> Optional[CohortExecutionResult]:
    """Execute one cohort cycle.

    When Agents are available:
    - Uses Judge to select parent from survivor whitelist.
    - StageAgents generate downstream child params.
    - In real-LLM mode, Agent failures → result.errors; NEVER falls
      back to mutation_planner silently.

    Falls back to :func:`execute_cohort` (mutation_planner) ONLY when
    Agents are not available, preserving the component-level behavior.
    """
    if not cohort:
        return None

    _reserved = orch._reserve_child_budget(
        cohort, survivor_count, audit_quota, population_size,
        max_children_per_parent, decision_stage)
    if _reserved == 0:
        log.info("[ORCH-E] cohort %s: no new children needed", decision_stage)

    params_by_id = {t.trial_id: t.params for t in cohort}

    # Agent path: Judge selects parent, StageAgents generate params.
    if orch._has_agents:
        try:
            cr = orch._execute_cohort_with_agents(
                cohort=cohort, decision_stage=decision_stage,
                survivor_count=survivor_count, audit_quota=audit_quota,
                population_size=population_size,
                max_children_per_parent=max_children_per_parent,
                doomed_rule_version=doomed_rule_version,
                scheduler_version=scheduler_version,
                planner_version=planner_version,
                parent_params_by_id=params_by_id,
            )
            if cr.cohort_plan is not None:
                actual_children = len(cr.child_trial_ids)
                if actual_children > _reserved:
                    log.warning(
                        "[ORCH-E] budget mismatch: reserved %d, got %d",
                        _reserved, actual_children)
                orch._new_trials += actual_children

            # Real-LLM guard: if any child proposal is fallback, that's
            # an error — do not silently accept mutation-planner children.
            if orch._is_real_llm:
                for cid in cr.child_trial_ids:
                    ap = orch._agent_proposals.get(cid)
                    if ap and ap.is_fallback:
                        log.error(
                            "[ORCH-E] real-LLM: child %s has fallback "
                            "Agent proposal — cohort %s degraded",
                            cid[:6], decision_stage)
                        # Don't return None (cohort still ran) but the
                        # caller should check result.errors.

            return cr
        except Exception as e:
            if orch._is_real_llm:
                # Real-LLM mode: Agent failure is a hard error.
                # Never silently switch to mutation_planner.
                log.error(
                    "[ORCH-E] real-LLM agent cohort %s FAILED: %s — "
                    "NOT falling back to mutation_planner",
                    decision_stage, e)
                raise  # propagate to caller
            # Mock / no-LLM mode: fall back to mutation_planner.
            log.error("[ORCH-E] agent cohort failed: %s — "
                      "falling back to mutation_planner", e)

    # Default: mutation_planner path (no Agents, or Agent
    # path failed in non-real-LLM mode).
    try:
        cr = execute_cohort(
            cohort=cohort, decision_stage=decision_stage,
            survivor_count=survivor_count, audit_quota=audit_quota,
            population_size=population_size,
            max_children_per_parent=max_children_per_parent,
            seed=orch.cfg.seed, parent_params_by_id=params_by_id,
            trial_mgr=orch.trial_mgr,
            checkpoint_mgr=orch.checkpoint_mgr,
            tree=orch.tree, experiment_id=orch.cfg.experiment_id,
            iteration=orch._iteration, runs_dir=orch._runs_dir,
            doomed_rule_version=doomed_rule_version,
            scheduler_version=scheduler_version,
            planner_version=planner_version,
        )
        if cr.cohort_plan is not None:
            actual_children = len(cr.child_trial_ids)
            if actual_children > _reserved:
                log.warning(
                    "[ORCH-E] budget mismatch: reserved %d, got %d children",
                    _reserved, actual_children)
            orch._new_trials += actual_children
        return cr
    except Exception as e:
        log.error("[ORCH-E] cohort failed: %s", e); return None



def _execute_cohort_with_agents(
    orch, cohort: List[TrialRecord], decision_stage: str,
    survivor_count: int, audit_quota: int, population_size: int,
    max_children_per_parent: int, doomed_rule_version: str,
    scheduler_version: str, planner_version: str,
    parent_params_by_id: Dict[str, Dict[str, Dict[str, Any]]],
) -> CohortExecutionResult:
    """Run cohort with Agent-generated child params.

    1. plan_cohort → observations/doomed/gwtw/fork_plans.
    2. Compute survivor whitelist from plan BEFORE child creation.
    3. Write trace entries (same shape as execute_cohort).
    4. For each required child: validate parent against whitelist,
       StageAgents generate downstream params.
    5. Resolve checkpoints, persist children.
    6. Write cohort_complete sentinel.
    """
    from gwtw.cohort_plan import CohortPlanError
    from gwtw.doom import DEFAULT_RULE_VERSION as _DDV
    from gwtw.scheduler import (
        AllHardDeadError, PopulationCapacityError,
    )
    from gwtw.mutation import NoLegalMutationError

    # Phase 1: plan the cohort.
    plan = plan_cohort(
        cohort, decision_stage=decision_stage,
        survivor_count=survivor_count, audit_quota=audit_quota,
        population_size=population_size,
        max_children_per_parent=max_children_per_parent,
        seed=orch.cfg.seed, parent_params_by_id=parent_params_by_id,
        doomed_rule_version=doomed_rule_version,
        scheduler_version=scheduler_version,
        planner_version=planner_version,
    )

    # Phase 1.5: compute survivor whitelist from plan NOW,
    # before any child creation needs it for parent validation.
    cohort_survivors = [
        obs.trial_id
        for obs, dd in zip(plan.observations, plan.doomed_decisions)
        if dd.risk_class == "survivor"
    ]
    if decision_stage == "PL":
        orch._survivor_whitelist_pl = list(cohort_survivors)
    else:
        orch._survivor_whitelist_cts = list(cohort_survivors)

    tids = [t.trial_id for t in cohort]
    cfg_tuple = (
        survivor_count, audit_quota, population_size,
        max_children_per_parent,
        doomed_rule_version, scheduler_version, planner_version,
    )
    _cohort_id = make_cohort_id(
        decision_stage, orch.cfg.seed, tids, *cfg_tuple)

    trace_refs: List[DecisionTraceRef] = []

    # Phase 2: write trace entries (observation, doomed, gwtw).
    trial_outcomes: Dict[str, str] = {}
    for obs, doomed, gwtw in zip(plan.observations,
                                 plan.doomed_decisions,
                                 plan.gwtw_decisions):
        trial_outcomes[obs.trial_id] = gwtw.action

        obs_ref = orch._trace_writer.append({
            "entry_type": "observation", "cohort_id": _cohort_id,
            "trial_id": obs.trial_id, "cohort_stage": decision_stage,
            "cohort_seed": orch.cfg.seed, "data": obs.to_dict(),
        })
        trace_refs.append(obs_ref)

        doomed_ref = orch._trace_writer.append({
            "entry_type": "doomed_decision", "cohort_id": _cohort_id,
            "trial_id": obs.trial_id, "cohort_stage": decision_stage,
            "cohort_seed": orch.cfg.seed, "data": doomed.to_dict(),
            "rule_version": doomed_rule_version,
        })
        trace_refs.append(doomed_ref)

        gwtw_ref = orch._trace_writer.append({
            "entry_type": "gwtw_decision", "cohort_id": _cohort_id,
            "trial_id": obs.trial_id, "cohort_stage": decision_stage,
            "cohort_seed": orch.cfg.seed, "data": gwtw.to_dict(),
            "scheduler_version": scheduler_version,
        })
        trace_refs.append(gwtw_ref)

        trial = orch._find_trial_in_cohort(cohort, obs.trial_id)
        if not trial.doomed_decisions:
            trial.doomed_decisions = []
        trial.doomed_decisions.append(doomed)
        if not trial.gwtw_decisions:
            trial.gwtw_decisions = []
        trial.gwtw_decisions.append(gwtw)
        if not trial.decision_trace_refs:
            trial.decision_trace_refs = []
        trial.decision_trace_refs.extend([obs_ref, doomed_ref, gwtw_ref])

        if gwtw.action == "pause":
            trial.status = "paused"
        orch.trial_mgr.update(trial)

    # Phase 3: create children with Agent-generated params.
    # Judge selects parent from survivor whitelist; StageAgents
    # generate downstream params.  Whitelist violations are rejected
    # with deterministic fallback and trace evidence.
    child_ids: List[str] = []
    child_resolutions: List[ExecutionResolution] = []
    child_index = 0

    # Use fork_plans to determine how many children are needed.
    for fp in plan.fork_plans:
        # ---- 3a) Judge selects parent from whitelist ----
        judge_parent = orch._judge_select_parent(
            whitelist=cohort_survivors,
            decision_stage=decision_stage,
            fork_index=child_index,
            gwtk_parent=fp.fork_request.parent_trial_id,
        )
        # Validate Judge's choice against whitelist.
        sel = orch._select_and_validate_parent(
            judge_parent, decision_stage)
        effective_parent = sel.effective_parent

        if not effective_parent:
            log.error("[ORCH-E] no valid parent for fork — skipping child")
            continue

        parent_trial = orch._find_trial_in_cohort(cohort, effective_parent)

        # ---- 3b) StageAgents generate downstream params ----
        role = "pl_child" if decision_stage == "PL" else "cts_child"
        child = orch.trial_mgr.create(
            experiment_id=orch.cfg.experiment_id,
            parent_trial_id=effective_parent,
            branch_stage=decision_stage,
            iteration=orch._iteration,
        )

        child_params, child_proposal = orch._generate_params_for_candidate(
            trial_id=child.trial_id, index=child_index,
            role=role,
            parent_trial_id=effective_parent,
            decision_stage=decision_stage,
        )
        child_index += 1

        child.params = child_params
        child.doomed_decisions = []
        child.gwtw_decisions = []
        child.decision_trace_refs = []
        orch._agent_proposals[child.trial_id] = child_proposal
        if not child_proposal.is_fallback:
            orch._any_real_proposal = True
        orch._write_agent_proposal_trace(child_proposal)

        # ---- 3c) Write fork trace with Judge provenance ----
        parent_cp = parent_trial.checkpoint
        cp_id = parent_cp.checkpoint_id if parent_cp else "unknown"
        fork_ref = orch._trace_writer.append({
            "entry_type": "fork",
            "cohort_id": _cohort_id,
            "trial_id": child.trial_id,
            "parent_trial_id": effective_parent,
            "cohort_stage": decision_stage,
            "cohort_seed": orch.cfg.seed,
            "data": {
                "checkpoint_id": cp_id,
                "agent_params_provided": True,
                "agent_proposal_role": role,
                "agent_is_fallback": child_proposal.is_fallback,
                "judge_requested_parent": judge_parent,
                "judge_accepted": sel.accepted,
                "planner_version": planner_version,
            },
        })
        trace_refs.append(fork_ref)
        child.decision_trace_refs.append(fork_ref)

        # Resolve checkpoint.
        inherited_params = copy.deepcopy(
            parent_params_by_id.get(effective_parent,
                                    copy.deepcopy(BASELINE_PARAMS)))
        resolution = orch._resolve_child_checkpoint(
            parent_trial=parent_trial, child=child,
            child_params=child_params,
            inherited_params=inherited_params,
            checkpoint_id=cp_id,
        )
        child.execution_resolution = resolution

        er_ref = orch._trace_writer.append({
            "entry_type": "execution_resolution",
            "cohort_id": _cohort_id,
            "trial_id": child.trial_id,
            "parent_trial_id": effective_parent,
            "cohort_stage": decision_stage,
            "cohort_seed": orch.cfg.seed,
            "data": resolution.to_dict(),
        })
        trace_refs.append(er_ref)
        child.decision_trace_refs.append(er_ref)

        orch.trial_mgr.update(child)
        child_ids.append(child.trial_id)
        child_resolutions.append(resolution)

    # Phase 4: write sentinel.
    sentinel_ref = write_cohort_complete(
        orch._trace_writer, decision_stage, orch.cfg.seed,
        trial_ids=tids,
        survivor_count=survivor_count, audit_quota=audit_quota,
        population_size=population_size,
        max_children_per_parent=max_children_per_parent,
        doomed_rule_version=doomed_rule_version,
        scheduler_version=scheduler_version,
        planner_version=planner_version,
    )
    trace_refs.append(sentinel_ref)

    return CohortExecutionResult(
        decision_stage=decision_stage,
        cohort_plan=plan,
        trial_outcomes=trial_outcomes,
        child_trial_ids=child_ids,
        child_checkpoint_resolutions=child_resolutions,
        trace_refs=trace_refs,
        seed=orch.cfg.seed,
    )



def _resolve_child_checkpoint(
    orch, parent_trial: TrialRecord, child: TrialRecord,
    child_params: Dict[str, Dict[str, Any]],
    inherited_params: Dict[str, Dict[str, Any]],
    checkpoint_id: str,
) -> ExecutionResolution:
    from gwtw.resolver import resolve_checkpoint
    from gwtw.cohort_common import _find_parent_node_id as _fpn

    parent_node_id = _fpn(orch.tree, parent_trial.trial_id)

    if parent_node_id is None:
        log.warning(
            "[ORCH-E] parent trial %s not found in tree; "
            "full_restart for child %s",
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
    effective_stage = _STAGE_NEXT.get(cp_stage, "CTS")

    return resolve_checkpoint(
        requested_parent_node_id=parent_node_id,
        requested_start_stage=effective_stage,
        candidate_params=child_params,
        inherited_params=inherited_params,
        tree=orch.tree,
        trial_mgr=orch.trial_mgr,
        checkpoint_mgr=orch.checkpoint_mgr,
        runs_dir=orch._runs_dir,
    )

@staticmethod


def _find_trial_in_cohort(
    cohort: List[TrialRecord], trial_id: str,
) -> TrialRecord:
    for t in cohort:
        if t.trial_id == trial_id:
            return t
    from gwtw.cohort_plan import CohortPlanError
    raise CohortPlanError(
        f"Trial {trial_id!r} not found in cohort")

# ------------------------------------------------------------------
# Budget
# ------------------------------------------------------------------
