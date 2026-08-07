# -*- coding: utf-8 -*-
"""gwtw/cohort_resume.py — idempotent cohort resume / disk-rebuild helpers."""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Dict
from typing import List
from typing import Any
from storage.decision_trace import DecisionTraceWriter
from storage.decision_trace import make_cohort_id
from storage.decision_trace import read_trace
from storage.trace_io import read_fork_intents
from storage.trace_io import write_cohort_complete
from storage import CheckpointManager
from storage import TrialManager
from search.tree import OptimizationTree
from core.models import ExecutionResolution
from core.models import TrialRecord
from core.models import DecisionTraceRef
from gwtw.cohort_execute import CohortExecutionResult
from gwtw.cohort_common import _find_child_for_intent
from gwtw.cohort_common import _find_trial_in_cohort
from gwtw.cohort_common import _fix_child_trace_refs
from gwtw.cohort_common import _resolve_child_checkpoint
from gwtw.cohort_common import reconstruct_cohort_decisions

log = logging.getLogger("gwtw")


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
    from gwtw.mutation import plan_child_params
    from gwtw.scheduler import ForkRequest

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
