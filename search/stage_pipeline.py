# -*- coding: utf-8 -*-
"""search/stage_pipeline.py — single-iteration search pipeline (phase 1-3).

Executes one optimizer iteration: observation -> Judge decision -> param
generation -> checkpoint resolution -> stage execution -> QoR finalize.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

import config
from agents.observation import build_observation_summary
from core.models import ExecutionResolution
from core.qor import QoR
from gwtw.resolver import resolve_checkpoint
from orfs.interface import RunResult
from search.tree import ROOT_ID
from storage import CheckpointManager

log = logging.getLogger("optimizer")


def _aft_stages(stage: str) -> List[str]:
    """Stages strictly after *stage* in the fixed flow order."""
    return config.STAGES[config.STAGES.index(stage) + 1:]


def _next_stage_of_node(node_stage: str) -> Optional[str]:
    """Stage that follows a tree node stage (ROOT -> FP, FP -> PL, ...)."""
    if node_stage == "ROOT":
        return "FP"
    if node_stage in config.STAGES and node_stage != "RT":
        return config.STAGES[config.STAGES.index(node_stage) + 1]
    return None


def _downstream_stages(branch_stage: str) -> List[str]:
    """All stages to re-run when starting from *branch_stage*."""
    return config.STAGES[config.STAGES.index(branch_stage):]


def run_iteration(optimizer: "Optimizer", iteration: int) -> RunResult:
    """Paper sec. 6 lines 4-13, with checkpoint resolution.

    Three-phase approach:
      Phase 1 — Pre-generate ALL downstream params from StageAgents
                (using inherited upstream QoR, no ORFS execution).
      Phase 2 — Call the checkpoint resolver to get an
                ExecutionResolution.
      Phase 3 — Execute stages from effective_start_stage; create
                FP/PL/CTS checkpoints on success.
    """
    log.info("========== Iter #%d ==========", iteration)

    # ---- 4) Observation summary (unchanged) ----
    summary = build_observation_summary(
        optimizer.tree, optimizer.history, optimizer._best_qor(),
        optimizer.cfg.max_branch_count)

    # ---- 5) Judge decision (unchanged) ----
    decision = optimizer.judge.act({
        "summary": summary, "history": optimizer.history, "best": optimizer.best_entry})
    branch_node_id = (decision["branch_node"] or "").strip()
    if branch_node_id.upper() == "ROOT":
        branch_node_id = ROOT_ID
    requested_start_stage = decision["branch_stage"]
    hints = decision["hints"]
    log.info("#%d [Judge Agent] branch_node = %s", iteration, branch_node_id)
    log.info("#%d [Judge Agent] branch_stage = %s", iteration, requested_start_stage)
    for s, h in hints.items():
        if h:
            log.info("#%d [Judge Agent] @%s Agent: %s", iteration, s, h[:80])

    # ---- 6) Resolve branch node & parent ----
    branch_node = optimizer.tree.find_node(branch_node_id)
    if branch_node is None:
        log.warning("#%d [Judge Agent] branch_node=%s not in tree, fallback to ROOT",
                    branch_node_id)
        branch_node = optimizer.tree.root
        branch_node_id = ROOT_ID
    parent_variant = branch_node.variant

    # Consistency constraint (paper sec. 3.2)
    expected_stage = _next_stage_of_node(branch_node.stage)
    if expected_stage is None:
        log.warning("#%d [Judge Agent] branch_node=%s is a leaf (RT), cannot branch, fallback to ROOT+FP",
                    iteration, branch_node_id)
        branch_node = optimizer.tree.root
        branch_node_id = ROOT_ID
        expected_stage = "FP"
        parent_variant = branch_node.variant
    if requested_start_stage != expected_stage:
        log.warning("#%d [Judge Agent] branch_stage=%s inconsistent with "
                    "branch_node=%s (stage=%s), corrected to %s",
                    iteration,
                    requested_start_stage, branch_node_id, branch_node.stage,
                    expected_stage)
        requested_start_stage = expected_stage
        decision["branch_stage"] = requested_start_stage

    # Inherit params + QoR from tree ancestors
    inherited_params = optimizer.tree.get_params_chain(branch_node_id)
    inherited_qor_map: Dict[str, Dict[str, float]] = {}
    bef_nodes = optimizer.tree.ancestors(branch_node_id)
    if branch_node.stage in config.STAGES:
        bef_nodes = bef_nodes + [branch_node]
    for node in bef_nodes:
        if node.stage in config.STAGES and node.stage_qor:
            inherited_qor_map[node.stage] = node.stage_qor

    # Parent trial: the source_trial_id of the branch node, or None
    parent_trial_id = branch_node.source_trial_id

    def _build_inherited_upstream_qor() -> List[dict]:
        result: List[dict] = []
        for stage in config.STAGES:
            if stage not in inherited_qor_map:
                break
            sq = inherited_qor_map[stage]
            ws = tns = None
            for k, v in sq.items():
                if k.endswith("_ws_ps"):
                    ws = v
                elif k.endswith("_tns_ps"):
                    tns = v
            result.append({"stage": stage, "ws_ps": ws, "tns_ps": tns})
        return result

    new_variant = optimizer.cfg.variant_name(iteration)
    requested_downstream = _downstream_stages(requested_start_stage)

    # ================================================================
    # Phase 1: Pre-generate ALL params for downstream stages.
    # StageAgents use inherited upstream QoR only (real ORFS hasn't run
    # yet).  We need the complete candidate param set for checkpoint
    # compatibility checks.
    # ================================================================
    stage_params = dict(inherited_params)
    stage_reasons: Dict[str, str] = {}
    collected_stage_qor: Dict[str, Dict[str, float]] = {}
    failed_stage: Optional[str] = None
    live_upstream_qor = _build_inherited_upstream_qor()

    for s in requested_downstream:
        ctx = {
            "upstream_qor": live_upstream_qor,
            "cross_iteration_exp": optimizer._cross_exp(s),
            "hint": hints.get(s, ""),
            "global_best": optimizer.best_entry,
        }
        out = optimizer.stage_agents[s].act(ctx)
        stage_params[s] = out["params"]
        stage_reasons[s] = out["reason"]
        log.info("#%d [%s Agent] %s", iteration, s, out["reason"][:120])
        for pname, pvalue in out["params"].items():
            log.info("#%d [%s Agent] %s: %s", iteration, s, pname, pvalue)

    # ================================================================
    # Phase 2: Resolve checkpoint against complete candidate params.
    # ================================================================
    resolution = resolve_checkpoint(
        requested_parent_node_id=branch_node_id,
        requested_start_stage=requested_start_stage,
        candidate_params=stage_params,
        inherited_params=inherited_params,
        tree=optimizer.tree,
        trial_mgr=optimizer.trial_mgr,
        checkpoint_mgr=optimizer.checkpoint_mgr,
        runs_dir=optimizer.cfg.run_dir,
    )
    effective_start_stage = resolution.effective_start_stage
    effective_downstream = _downstream_stages(effective_start_stage)
    log.info("#%d [RESOLVER] requested=%s effective=%s mode=%s cp=%s",
             iteration, requested_start_stage, effective_start_stage,
             resolution.execution_mode,
             resolution.consumed_checkpoint or "none")
    if resolution.fallback_reason:
        log.info("#%d [RESOLVER] fallback_reason: %s", iteration,
                 resolution.fallback_reason)

    # ================================================================
    # Phase 3: Execute from effective_start_stage.
    # ================================================================

    # ---- validate checkpoint_fork metadata ----
    # If consumed_variant or consumed_node_id is missing, the fork
    # cannot proceed safely (we'd be copying unknown artifacts).
    # Force full_restart instead of falling back to parent_variant.
    if (resolution.execution_mode == "checkpoint_fork"
            and (not resolution.consumed_variant
                 or not resolution.consumed_node_id)):
        log.error(
            "#%d [OPTIMIZER] checkpoint_fork with incomplete metadata "
            "(consumed_variant=%s, consumed_node_id=%s), forcing full_restart",
            iteration,
            resolution.consumed_variant,
            resolution.consumed_node_id,
        )
        resolution.execution_mode = "full_restart"
        resolution.effective_start_stage = "FP"
        resolution.consumed_checkpoint = None
        resolution.consumed_node_id = None
        resolution.consumed_variant = None
        resolution.fallback_reason = (
            "checkpoint_fork metadata incomplete (missing consumed_variant "
            "or consumed_node_id), forced full_restart"
        )
        effective_start_stage = "FP"
        effective_downstream = _downstream_stages("FP")

    # ---- compute actual parent from resolution ----
    # The tree parent and trial parent must reflect the ACTUAL execution,
    # not the Judge's request.  When the resolver falls back from CTS to
    # PL checkpoint, the tree parent is the PL node (not CTS), and the
    # parent trial is the PL node's source trial.
    if resolution.execution_mode == "checkpoint_fork":
        actual_parent_node_id = resolution.consumed_node_id
        consumed_node = optimizer.tree.find_node(resolution.consumed_node_id)
        actual_parent_trial_id = (
            consumed_node.source_trial_id if consumed_node else None
        )
        # Recompute inherited QoR from the ACTUAL parent chain
        # (not the Judge's branch_node chain).
        actual_inherited_qor_map: Dict[str, Dict[str, float]] = {}
        actual_bef_nodes = optimizer.tree.ancestors(actual_parent_node_id)
        actual_bef_node = optimizer.tree.find_node(actual_parent_node_id)
        if actual_bef_node and actual_bef_node.stage in config.STAGES:
            actual_bef_nodes = actual_bef_nodes + [actual_bef_node]
        for node in actual_bef_nodes:
            if node.stage in config.STAGES and node.stage_qor:
                actual_inherited_qor_map[node.stage] = node.stage_qor
        # Recompute inherited params from actual parent for
        # _finalize_trial param_diff computation.
        actual_inherited_params = optimizer.tree.get_params_chain(
            actual_parent_node_id)
    else:
        # full_restart: no parent, no inherited QoR
        actual_parent_node_id = ROOT_ID
        actual_parent_trial_id = None
        actual_inherited_qor_map = {}
        actual_inherited_params = {}

    # Open trial record (uses actual parent, not Judge-requested parent).
    optimizer._begin_trial(
        iteration,
        parent_trial_id=actual_parent_trial_id,
        branch_stage=effective_start_stage,
        parent_params=actual_inherited_params,
    )

    # 7a) Establish variant baseline based on resolution
    if resolution.execution_mode == "full_restart":
        optimizer.runner._wipe_variant(new_variant)  # type: ignore[attr-defined]
    else:
        # checkpoint_fork: copy from the CONSUMED checkpoint's source
        # variant, NOT the Judge-requested parent_variant.
        optimizer.runner.copy_parent_results(
            resolution.consumed_variant, new_variant)
        # after copying the parent variant's
        # complete directory trees, run ORFS make clean_<stage> for all
        # downstream stages so stale artifacts (including unprefixed files
        # like route.guide) don't cause make to skip stages.
        optimizer.runner._clean_downstream_stages(  # type: ignore[attr-defined]
            new_variant, effective_start_stage)

    # 7b) Execute stages from effective_start_stage.
    # Rebuild live_upstream_qor from the ACTUAL inherited QoR.
    live_upstream_qor = []
    for stage in config.STAGES:
        if stage in actual_inherited_qor_map:
            sq = actual_inherited_qor_map[stage]
            ws = tns = None
            for k, v in sq.items():
                if k.endswith("_ws_ps"):
                    ws = v
                elif k.endswith("_tns_ps"):
                    tns = v
            live_upstream_qor.append(
                {"stage": stage, "ws_ps": ws, "tns_ps": tns})
        else:
            break  # Bef chain stops

    for s in effective_downstream:
        # b) Execute single stage via make
        stage_result = optimizer.runner.run_stage(
            s, stage_params, new_variant, iteration)

        optimizer._add_stage_result(stage_result)

        if stage_result.status != "ok":
            failed_stage = s
            log.error("#%d [%s Agent] stage %s failed (%s, %.1fs), stopping downstream",
                     iteration, s, s,
                     stage_result.failure.value if stage_result.failure else "unknown",
                     stage_result.elapsed_s)
            break

        collected_stage_qor[s] = stage_result.stage_qor

        # Create checkpoint for FP/PL/CTS (stages that have downstream
        # stages to fork from).
        if s in ("FP", "PL", "CTS") and optimizer._current_trial:
            try:
                ph = CheckpointManager.param_hash(
                    {st: stage_params.get(st, {}) for st in config.STAGES})
                cp = optimizer.checkpoint_mgr.create(
                    trial=optimizer._current_trial,
                    stage=s,
                    platform=optimizer.cfg.platform,
                    design=optimizer.cfg.design,
                    variant=new_variant,
                    param_hash=ph,
                    runs_dir=optimizer.cfg.run_dir,
                )
                optimizer._current_trial.checkpoint = cp
                log.info("#%d [OPTIMIZER] checkpoint %s created @%s",
                         iteration, cp.checkpoint_id, s)
            except Exception as e:
                log.warning("#%d [OPTIMIZER] checkpoint @%s creation failed (non-fatal): %s",
                            iteration, s, e)

        # Update live_upstream_qor for next stage
        ws = tns = None
        for k, v in stage_result.stage_qor.items():
            if k.endswith("_ws_ps"):
                ws = v
            elif k.endswith("_tns_ps"):
                tns = v
        live_upstream_qor.append(
            {"stage": s, "ws_ps": ws, "tns_ps": tns})

    # ---- 8) Final QoR ----
    if failed_stage is not None:
        result = RunResult(
            ok=False, variant=new_variant,
            failed_stage=failed_stage,
            error=f"Stage {failed_stage} make failed",
            stage_qor=collected_stage_qor)
    else:
        result = optimizer.runner.run_finish(stage_params, new_variant, iteration)

    # ---- 9) Register new nodes in tree ----
    if result.ok:
        chain = [(s, new_variant, stage_params.get(s, {}),
                  collected_stage_qor.get(s)) for s in effective_downstream]
        source_tid = (optimizer._current_trial.trial_id
                      if optimizer._current_trial else None)
        optimizer._add_to_tree(iteration, actual_parent_node_id, chain,
                          source_trial_id=source_tid)

    # ---- 10) Record history (unchanged) ----
    optimizer._record(iteration, stage_params, result, decision, stage_reasons)

    # Resolution fields are merged into the current trial before recording
    # but the QoR is not known until _finalize_trial, so update now.
    if optimizer._current_trial:
        optimizer._current_trial.execution_resolution = resolution

    # ---- Stage C6: finalize trial record ----
    optimizer._finalize_trial(
        status="ok" if result.ok else "failed",
        final_qor=result.qor,
        failure=FailureClass.TOOL_CRASH if not result.ok else None,
        error_message=result.error,
        current_params=stage_params,
    )

    return result
