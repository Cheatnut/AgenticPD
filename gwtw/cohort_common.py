# -*- coding: utf-8 -*-
"""gwtw/cohort_common.py — cohort decision reconstruction and disk-rebuild helpers."""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple
from gwtw.cohort_plan import CohortPlanError
from gwtw.cohort_execute import CohortExecutionResult
from gwtw.resolver import resolve_checkpoint
from storage.decision_trace import DEFAULT_TRACE_PATH
from storage.decision_trace import DecisionTraceWriter
from storage.decision_trace import make_cohort_id
from storage.decision_trace import read_trace
from storage.trace_io import cohort_already_executed
from storage.trace_io import read_fork_intents
from storage.trace_io import write_cohort_complete
from storage import CheckpointManager
from storage import TrialManager
from search.tree import OptimizationTree
from core.models import ExecutionResolution
from core.models import DecisionTraceRef
from core.models import TrialRecord

log = logging.getLogger("gwtw")


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

from gwtw.cohort_resume import _rebuild_from_disk, _resume_forks_from_disk
