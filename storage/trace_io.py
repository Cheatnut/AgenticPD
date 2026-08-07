# -*- coding: utf-8 -*-
"""storage/trace_io.py — cohort-scoped decision-trace read/write helpers."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List

from storage.decision_trace import (
    DecisionTraceWriter,
    make_cohort_id,
    read_trace,
)

log = logging.getLogger(__name__)

def _filter_by_cohort(
    entries: List[Dict[str, Any]],
    cohort_id: str,
) -> List[Dict[str, Any]]:
    """Return entries whose ``cohort_id`` matches."""
    return [e for e in entries if e.get("cohort_id") == cohort_id]


def cohort_decision_written(
    runs_dir: Path,
    trace_path: str,
    decision_stage: str,
    seed: int,
    trial_ids: List[str],
    survivor_count: int = 0,
    audit_quota: int = 0,
    population_size: int = 0,
    max_children_per_parent: int = 0,
    doomed_rule_version: str = "",
    scheduler_version: str = "",
    planner_version: str = "",
) -> bool:
    """Return True if every *trial_id* already has a gwtw_decision for this
    cohort (isolated by cohort_id, not just stage+seed)."""
    cohort_id = make_cohort_id(
        decision_stage, seed, trial_ids,
        survivor_count=survivor_count, audit_quota=audit_quota,
        population_size=population_size,
        max_children_per_parent=max_children_per_parent,
        doomed_rule_version=doomed_rule_version,
        scheduler_version=scheduler_version,
        planner_version=planner_version,
    )
    cohort_entries = _filter_by_cohort(
        read_trace(runs_dir, trace_path), cohort_id)
    trial_ids_seen: set = set()
    for entry in cohort_entries:
        if entry.get("entry_type") == "gwtw_decision":
            trial_ids_seen.add(entry.get("trial_id", ""))
    return set(trial_ids).issubset(trial_ids_seen)


def cohort_already_executed(
    runs_dir: Path,
    trace_path: str,
    decision_stage: str,
    seed: int,
    trial_ids: List[str],
    survivor_count: int = 0,
    audit_quota: int = 0,
    population_size: int = 0,
    max_children_per_parent: int = 0,
    doomed_rule_version: str = "",
    scheduler_version: str = "",
    planner_version: str = "",
) -> bool:
    """Return True if a ``cohort_complete`` sentinel exists for this cohort_id."""
    cohort_id = make_cohort_id(
        decision_stage, seed, trial_ids,
        survivor_count=survivor_count, audit_quota=audit_quota,
        population_size=population_size,
        max_children_per_parent=max_children_per_parent,
        doomed_rule_version=doomed_rule_version,
        scheduler_version=scheduler_version,
        planner_version=planner_version,
    )
    cohort_entries = _filter_by_cohort(
        read_trace(runs_dir, trace_path), cohort_id)
    for entry in cohort_entries:
        if entry.get("entry_type") == "cohort_complete":
            return True
    return False


def write_cohort_complete(
    writer: "DecisionTraceWriter",
    decision_stage: str,
    seed: int,
    trial_ids: List[str],
    survivor_count: int = 0,
    audit_quota: int = 0,
    population_size: int = 0,
    max_children_per_parent: int = 0,
    doomed_rule_version: str = "",
    scheduler_version: str = "",
    planner_version: str = "",
) -> DecisionTraceRef:
    """Write a ``cohort_complete`` sentinel.  Call AFTER all fork children
    are created and persisted."""
    cohort_id = make_cohort_id(
        decision_stage, seed, trial_ids,
        survivor_count=survivor_count, audit_quota=audit_quota,
        population_size=population_size,
        max_children_per_parent=max_children_per_parent,
        doomed_rule_version=doomed_rule_version,
        scheduler_version=scheduler_version,
        planner_version=planner_version,
    )
    return writer.append({
        "entry_type": "cohort_complete",
        "cohort_id": cohort_id,
        "cohort_stage": decision_stage,
        "cohort_seed": seed,
        "trial_ids": sorted(trial_ids),
        "survivor_count": survivor_count,
        "population_size": population_size,
        "data": {"status": "complete"},
    })


# =============================================================================
# Fork intent helpers
# =============================================================================


def write_fork_intents(
    writer: "DecisionTraceWriter",
    cohort_id: str,
    decision_stage: str,
    seed: int,
    fork_plans: List[Any],  # List[ForkPlan]
) -> List[DecisionTraceRef]:
    """Persist ALL fork intents to trace BEFORE executing any of them.

    Each intent records the deterministic metadata needed to recreate
    the child (parent, checkpoint, param, derived_seed) so recovery can
    replay intents one-by-one without re-planning.
    """
    refs: List[DecisionTraceRef] = []
    for idx, fp in enumerate(fork_plans):
        refs.append(writer.append({
            "entry_type": "fork_intent",
            "cohort_id": cohort_id,
            "cohort_stage": decision_stage,
            "cohort_seed": seed,
            "intent_index": idx,
            "parent_trial_id": fp.fork_request.parent_trial_id,
            "checkpoint_id": fp.checkpoint_id,
            "param_name": fp.evidence.param_name,
            "old_value": fp.evidence.old_value,
            "new_value": fp.evidence.new_value,
            "derived_seed": fp.derived_seed,
            "data": {
                "decision_stage": decision_stage,
                "param_stage": fp.evidence.stage,
            },
        }))
    return refs


def read_fork_intents(
    runs_dir: Path,
    trace_path: str,
    cohort_id: str,
) -> List[Dict[str, Any]]:
    """Read fork intents for *cohort_id* in intent_index order."""
    entries = _filter_by_cohort(
        read_trace(runs_dir, trace_path), cohort_id)
    intents = [e for e in entries if e.get("entry_type") == "fork_intent"]
    intents.sort(key=lambda e: e.get("intent_index", 0))
    return intents


# =============================================================================
# Helpers
# =============================================================================

