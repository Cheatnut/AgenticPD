# -*- coding: utf-8 -*-
"""gwtw/execution.py — stage execution / tree / budget helpers (extracted from orchestrator)."""

from __future__ import annotations

import json
import logging

from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from storage.decision_trace import DEFAULT_TRACE_PATH
from storage import CheckpointManager
from search.tree import OptimizationTree
from search.tree import ROOT_ID
from core.models import StageResult
from core.models import TrialRecord

_STAGE_ORDER = ["FP", "PL", "CTS", "RT", "finish"]
_CHECKPOINTABLE = {"FP", "PL", "CTS"}
_STAGE_NEXT: Dict[str, str] = {"FP": "PL", "PL": "CTS", "CTS": "RT"}

log = logging.getLogger("gwtw")

_NODE_ID_SEQ = 0


def _reserve_child_budget(
    orch, cohort: List[TrialRecord], survivor_count: int,
    audit_quota: int, population_size: int, max_children_per_parent: int,
    decision_stage: str = "PL",
) -> int:
    tids = [t.trial_id for t in cohort]
    already_done = False
    try:
        from storage.trace_io import cohort_already_executed
        already_done = cohort_already_executed(
            orch._runs_dir, DEFAULT_TRACE_PATH,
            decision_stage, orch.cfg.seed, tids,
            survivor_count, audit_quota,
            population_size, max_children_per_parent,
            orch.cfg.doomed_rule_version,
            orch.cfg.scheduler_version, orch.cfg.planner_version)
    except Exception:
        pass
    if already_done:
        return 0
    worst_active = min(survivor_count + audit_quota, len(cohort))
    needed = max(0, population_size - worst_active)
    orch._enforce_budget(needed)
    return needed



def _enforce_budget(orch, additional: int = 0) -> None:
    current = orch._disk_trials_before + orch._new_trials
    if current + additional > orch.cfg.max_trials:
        raise RuntimeError(
            f"max_trials ({orch.cfg.max_trials}) exceeded "
            f"(have {current}, need +{additional})")

# ------------------------------------------------------------------
# Resume detection
# ------------------------------------------------------------------



def _count_disk_trials(orch) -> int:
    return len(orch.trial_mgr.list_by_experiment(orch.cfg.experiment_id))



def _has_pl_trials(orch) -> bool:
    return any(
        any(sr.stage == "PL" and sr.status == "ok"
            for sr in t.stage_results)
        for t in orch.trial_mgr.list_by_experiment(orch.cfg.experiment_id))

# ------------------------------------------------------------------
# Tree
# ------------------------------------------------------------------



def _tree_path(orch) -> Path:
    return orch._runs_dir / "tree.json"



def _load_tree(orch) -> OptimizationTree:
    tp = orch._tree_path()
    if tp.is_file():
        try:
            return OptimizationTree.from_dict(
                json.loads(tp.read_text(encoding="utf-8")))
        except Exception:
            log.warning("[ORCH-E] corrupt tree.json — fresh start")
    return OptimizationTree()



def _save_tree(orch) -> None:
    tp = orch._tree_path()
    tmp = tp.with_suffix(".tmp")
    tmp.write_text(json.dumps(orch.tree.to_dict(), indent=2))
    tmp.replace(tp)



def _make_unique_nid(orch, stage: str, trial_id: str) -> str:
    global _NODE_ID_SEQ
    _NODE_ID_SEQ += 1
    nid = f"se-{_NODE_ID_SEQ}-{stage}-{trial_id[:6]}"
    orch._node_to_trial[nid] = trial_id
    return nid



def _add_stage_node_to_tree(orch, t: TrialRecord, stage: str,
                             variant: str,
                             stage_qor: Dict[str, float]) -> None:
    for nid, n in orch.tree._nodes.items():
        if (getattr(n, "source_trial_id", None) == t.trial_id
                and n.stage == stage):
            n.stage_qor = dict(stage_qor) if stage_qor else {}
            n.params = dict(t.params.get(stage, {}))
            return
    parent_node = orch._find_deepest_node(t.trial_id)
    parent_id = parent_node.node_id if parent_node else ROOT_ID
    child_nid = orch._make_unique_nid(stage, t.trial_id)
    orch.tree.add_path(
        orch._iteration * 10 + 300, parent_id,
        [(stage, variant, t.params.get(stage, {}),
          dict(stage_qor) if stage_qor else {})],
        source_trial_id=t.trial_id,
        node_ids=[child_nid])



def _add_children_to_tree(orch, cr: CohortExecutionResult) -> None:
    for cid in cr.child_trial_ids:
        child = orch.trial_mgr.get(cid)
        if child is None or child.parent_trial_id is None:
            continue
        parent_node = orch._find_deepest_node(child.parent_trial_id)
        if parent_node is None:
            continue
        pt = orch.trial_mgr.get(child.parent_trial_id)
        er = child.execution_resolution
        if er and er.execution_mode == "checkpoint_fork":
            child_stage = er.effective_start_stage
        else:
            cp_stage = pt.checkpoint.stage if (pt and pt.checkpoint) else "PL"
            child_stage = _STAGE_NEXT.get(cp_stage, "CTS")
        child_nid = orch._make_unique_nid(child_stage, cid)
        orch._node_to_trial[child_nid] = cid
        child_params_for_tree = dict(child.params.get(child_stage, {}))
        orch.tree.add_path(
            orch._iteration * 10 + 200, parent_node.node_id,
            [(child_stage, orch._variant_for(child),
              child_params_for_tree, {})],
            source_trial_id=cid,
            node_ids=[child_nid])



def _find_deepest_node(orch, source_trial_id: str) -> Any:
    best, best_order = None, -1
    flow = {"FP": 0, "PL": 1, "CTS": 2, "RT": 3}
    for n in orch.tree._nodes.values():
        if getattr(n, "source_trial_id", None) != source_trial_id:
            continue
        o = flow.get(n.stage, -1)
        if o > best_order:
            best_order, best = o, n
    return best

# ------------------------------------------------------------------
# Advance: copy → clean → execute
# ------------------------------------------------------------------



def _advance_one(orch, trial: TrialRecord, target_stage: str) -> None:
    t = orch.trial_mgr.get(trial.trial_id)
    if t is None:
        return
    if any(sr.stage == target_stage and sr.status == "ok"
           for sr in t.stage_results):
        return

    er = t.execution_resolution
    consumed_variant: Optional[str] = None
    if er and er.execution_mode == "checkpoint_fork":
        effective_start = er.effective_start_stage
        consumed_variant = er.consumed_variant
        if not consumed_variant and t.parent_trial_id:
            parent = orch.trial_mgr.get(t.parent_trial_id)
            if parent:
                consumed_variant = orch._variant_for(parent)
    else:
        cp_stage = t.checkpoint.stage if t.checkpoint else None
        effective_start = (
            _STAGE_NEXT.get(cp_stage, "FP") if cp_stage else "FP")
        consumed_variant = None

    variant = orch._variant_for(t)
    if consumed_variant:
        orch.runner.copy_parent_results(consumed_variant, variant)
        orch.runner.clean_downstream(variant, effective_start)

    if target_stage == "finish":
        orch._run_to_finish(t, effective_start, variant)
    else:
        orch._run_stages(t, effective_start, target_stage, variant)
    orch._iteration += 1



def _run_stages(orch, t: TrialRecord, start: str, end: str,
                variant: str) -> None:
    try:
        si, ei = _STAGE_ORDER.index(start), _STAGE_ORDER.index(end)
    except ValueError:
        return
    for stage in _STAGE_ORDER[si:ei + 1]:
        sr = orch.runner.run_stage(stage, t.params, variant, orch._iteration)
        t.stage_results.append(sr)
        if sr.status != "ok":
            t.status = "failed"; orch.trial_mgr.update(t); return
        orch._add_stage_node_to_tree(t, stage, variant, sr.stage_qor)
    if end in _CHECKPOINTABLE:
        orch._create_checkpoint(t, end, variant)
    t.status = "ok"; orch.trial_mgr.update(t)



def _run_to_finish(orch, t: TrialRecord, effective_start: str,
                   variant: str) -> None:
    try:
        si = _STAGE_ORDER.index(effective_start)
    except ValueError:
        return
    for stage in _STAGE_ORDER[si:4]:
        sr = orch.runner.run_stage(stage, t.params, variant, orch._iteration)
        t.stage_results.append(sr)
        if sr.status != "ok":
            t.status = "failed"; orch.trial_mgr.update(t); return
        orch._add_stage_node_to_tree(t, stage, variant, sr.stage_qor)
    fr = orch.runner.run_finish(t.params, variant, orch._iteration)
    t.stage_results.append(StageResult(
        stage="finish", status="ok" if fr.ok else "failed",
        elapsed_s=fr.elapsed_s, exit_code=0 if fr.ok else 1,
        report_path=getattr(fr, "report_path", None),
        command=getattr(fr, "command", None),
        stage_qor=getattr(fr, "stage_qor", {}),
        log_path=getattr(fr, "make_log_path", None)))
    if fr.qor:
        t.final_qor = {"wns_ps": fr.qor.wns_ps, "tns_ps": fr.qor.tns_ps,
                       "area_um2": fr.qor.area_um2, "power_w": fr.qor.power_w}
    t.status = "ok" if fr.ok else "failed"
    if fr.ok:
        t.end_time = getattr(fr, "end_time", None)
    orch.trial_mgr.update(t)

# ------------------------------------------------------------------
# Survivor whitelist
# ------------------------------------------------------------------



def _collect_survivors(orch, cr: CohortExecutionResult) -> List[str]:
    survivors: List[str] = []
    if cr.cohort_plan is not None:
        for obs, dd in zip(cr.cohort_plan.observations,
                           cr.cohort_plan.doomed_decisions):
            if dd.risk_class == "survivor":
                survivors.append(obs.trial_id)
    else:
        for tid, action in cr.trial_outcomes.items():
            if action in ("continue", "audit_continue"):
                t = orch.trial_mgr.get(tid)
                if t and t.doomed_decisions:
                    for dd in t.doomed_decisions:
                        if dd.risk_class == "survivor":
                            survivors.append(tid)
                            break
    return survivors

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------



def _variant_for(orch, trial: TrialRecord) -> str:
    return f"agenticpd_se_{trial.trial_id}"



def _create_checkpoint(orch, trial: TrialRecord, stage: str,
                       variant: str) -> None:
    try:
        trial.checkpoint = orch.checkpoint_mgr.create(
            trial=trial, stage=stage,
            platform=orch.cfg.platform, design=orch.cfg.design,
            variant=variant,
            param_hash=CheckpointManager.param_hash(trial.params),
            runs_dir=orch._runs_dir)
    except Exception:
        log.warning("[ORCH-E] checkpoint failed for %s", trial.trial_id)



def _collect_active(orch, cr: CohortExecutionResult) -> List[TrialRecord]:
    active, seen = [], set()
    for tid, action in cr.trial_outcomes.items():
        if action in ("continue", "audit_continue"):
            t = orch.trial_mgr.get(tid)
            if t and t.trial_id not in seen:
                active.append(t); seen.add(t.trial_id)
    for cid in cr.child_trial_ids:
        t = orch.trial_mgr.get(cid)
        if t and t.trial_id not in seen:
            active.append(t); seen.add(t.trial_id)
    return active



def _collect_cts_trials(
    orch, pl_result: CohortExecutionResult) -> List[TrialRecord]:
    cts, seen = [], set()
    for tid, action in pl_result.trial_outcomes.items():
        if action in ("continue", "audit_continue"):
            t = orch.trial_mgr.get(tid)
            if t and any(sr.stage == "CTS" for sr in t.stage_results):
                if t.trial_id not in seen:
                    cts.append(t); seen.add(t.trial_id)
    for cid in pl_result.child_trial_ids:
        t = orch.trial_mgr.get(cid)
        if t and any(sr.stage == "CTS" for sr in t.stage_results):
            if t.trial_id not in seen:
                cts.append(t); seen.add(t.trial_id)
    return cts


# =============================================================================
# Recording fake runner (for mock mode)
# =============================================================================

