# -*- coding: utf-8 -*-
"""gwtw/population.py — Doomed/GWTW orchestration helpers (extracted from orchestrator)."""

from __future__ import annotations

import logging

import copy
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple
from gwtw.cohort_execute import CohortExecutionResult
from gwtw.fake_runner import _hash_params, _resolve_trial_id
from gwtw.proposals import AgentProposal, ParentSelectionRecord
from config import BASELINE_PARAMS
from search.tree import ROOT_ID
from core.models import TrialRecord

_STAGE_ORDER = ["FP", "PL", "CTS", "RT", "finish"]
_STAGE_NEXT: Dict[str, str] = {"FP": "PL", "PL": "CTS", "CTS": "RT"}

log = logging.getLogger("gwtw")


def _bootstrap_population(orch) -> List[TrialRecord]:
    existing = orch.trial_mgr.list_by_experiment(orch.cfg.experiment_id)
    pl_trials = [t for t in existing
                 if any(sr.stage == "PL" and sr.status == "ok"
                        for sr in t.stage_results)]
    if len(pl_trials) >= orch.cfg.population_size:
        log.info("[ORCH-E] reusing %d existing PL trials", len(pl_trials))
        return pl_trials[:orch.cfg.population_size]

    trials = list(pl_trials)
    for i in range(len(pl_trials), orch.cfg.population_size):
        orch._enforce_budget(1)
        t = orch._bootstrap_one(i)
        trials.append(t)
        orch._iteration += 1
    orch._save_tree()
    return trials



def _bootstrap_one(orch, index: int) -> TrialRecord:
    t = orch.trial_mgr.create(
        experiment_id=orch.cfg.experiment_id, iteration=orch._iteration)
    orch._new_trials += 1

    stage_params, proposal = orch._generate_params_for_candidate(
        t.trial_id, index, role="bootstrap")
    t.params = stage_params
    orch._agent_proposals[t.trial_id] = proposal
    if not proposal.is_fallback:
        orch._any_real_proposal = True

    variant = orch._variant_for(t)
    for stage in ["FP", "PL"]:
        sr = orch.runner.run_stage(stage, t.params, variant, orch._iteration)
        t.stage_results.append(sr)
        if sr.status != "ok":
            t.status = "failed"; orch.trial_mgr.update(t); return t
    orch._create_checkpoint(t, "PL", variant)
    t.config_hash = _hash_params(t.params)
    t.status = "ok"; orch.trial_mgr.update(t)

    orch._write_agent_proposal_trace(proposal)

    fp_qor = t.stage_results[0].stage_qor
    pl_qor = t.stage_results[1].stage_qor
    fp_nid = orch._make_unique_nid("FP", t.trial_id)
    pl_nid = orch._make_unique_nid("PL", t.trial_id)
    orch._node_to_trial[fp_nid] = t.trial_id
    orch._node_to_trial[pl_nid] = t.trial_id
    orch.tree.add_path(
        orch._iteration * 10 + 100, ROOT_ID,
        [("FP", variant, t.params.get("FP", {}), fp_qor)],
        source_trial_id=t.trial_id,
        node_ids=[fp_nid])
    orch.tree.add_path(
        orch._iteration * 10 + 100, fp_nid,
        [("PL", variant, t.params.get("PL", {}), pl_qor)],
        source_trial_id=t.trial_id,
        node_ids=[pl_nid])
    return t



def _generate_params_for_candidate(
    orch, trial_id: str, index: int, role: str = "bootstrap",
    parent_trial_id: Optional[str] = None,
    decision_stage: Optional[str] = None,
) -> Tuple[Dict[str, Dict[str, Any]], AgentProposal]:
    """Use Judge + StageAgents to generate per-stage params.

    Tracks per-stage fallback status.  ``is_fallback`` is True when
    ANY stage relied on baseline defaults (real Agent proposal absent).

    When *decision_stage* is provided (PL/CTS fork), only stages
    downstream of *decision_stage* are generated (upstream inherited).
    """
    proposal = AgentProposal(
        trial_id=trial_id, candidate_index=index, proposal_role=role)
    proposal.stage_fallbacks = {}

    if not orch._has_agents:
        params = copy.deepcopy(BASELINE_PARAMS)
        util = 38 + (index % 4) * 3
        params["FP"]["CORE_UTILIZATION"] = int(util)
        proposal.judge_reason = "fallback: no agents available"
        proposal.is_fallback = True
        for stage in ["FP", "PL", "CTS", "RT"]:
            proposal.stage_proposals[stage] = {
                "params": dict(params.get(stage, {})),
                "reason": "fallback baseline",
            }
            proposal.stage_fallbacks[stage] = True
        return params, proposal

    # Determine which stages need Agent-generated params.
    # For forks: only downstream stages from the decision stage.
    # For bootstrap: all four stages.
    if decision_stage and decision_stage in _STAGE_NEXT:
        agent_stages = orch._downstream_stages(decision_stage)
    else:
        agent_stages = ["FP", "PL", "CTS", "RT"]

    # Inherit parent params for stages before decision_stage.
    stage_params: Dict[str, Dict[str, Any]] = {}
    if parent_trial_id and decision_stage:
        parent = orch.trial_mgr.get(parent_trial_id)
        if parent and parent.params:
            for s in _STAGE_ORDER[:4]:
                if s not in agent_stages:
                    stage_params[s] = copy.deepcopy(parent.params.get(s, {}))
        else:
            for s in _STAGE_ORDER[:4]:
                if s not in agent_stages:
                    stage_params[s] = copy.deepcopy(BASELINE_PARAMS.get(s, {}))
    else:
        for s in _STAGE_ORDER[:4]:
            if s not in agent_stages:
                stage_params[s] = copy.deepcopy(BASELINE_PARAMS.get(s, {}))

    # Build Judge context.
    if decision_stage:
        judge_summary = (
            f"## Fork Child Generation — {decision_stage} decision\n"
            f"Parent trial: {parent_trial_id}\n"
            f"Child index: {index}\n"
            f"Decision stage: {decision_stage} — "
            f"target stages: {agent_stages}\n"
        )
    else:
        judge_summary = (
            f"## Population Bootstrap — Candidate #{index}\n"
            f"Generating initial parameters for candidate {index} "
            f"of {orch.cfg.population_size}.\n"
            f"Platform: {orch.cfg.platform}, Design: {orch.cfg.design}\n"
        )

    judge_context = {
        "summary": judge_summary,
        "history": [],
        "best": None,
    }

    judge_fallback = False
    try:
        judge_decision = orch.judge_agent.act(judge_context)
    except Exception as e:
        log.warning("[ORCH-E] Judge failed for %s: %s", trial_id[:6], e)
        judge_decision = {
            "branch_node": "ROOT", "branch_stage": "FP",
            "hints": {s: "explore baseline" for s in ["FP", "PL", "CTS", "RT"]},
            "reason": f"fallback: {e}",
        }
        judge_fallback = True

    # Detect return-style fallback: Agent succeeded but its reason
    # indicates a degraded/fallback response.
    _judge_reason = str(judge_decision.get("reason", ""))
    if not judge_fallback and _judge_reason.lower().startswith("fallback:"):
        judge_fallback = True
        log.warning("[ORCH-E] Judge returned fallback reason: %s",
                   _judge_reason[:80])

    proposal.judge_branch_node = judge_decision.get("branch_node", "ROOT")
    proposal.judge_branch_stage = judge_decision.get("branch_stage", "FP")
    proposal.judge_hints = judge_decision.get("hints", {})
    proposal.judge_reason = judge_decision.get("reason", "")

    any_fallback = judge_fallback

    for stage in agent_stages:
        hint = proposal.judge_hints.get(stage, "")
        stage_context = {
            "upstream_qor": [],
            "cross_iteration_exp": [],
            "hint": hint,
            "global_best": None,
        }
        agent = orch.stage_agents.get(stage)
        stage_fallback = False

        if agent is None:
            stage_params[stage] = dict(BASELINE_PARAMS.get(stage, {}))
            proposal.stage_proposals[stage] = {
                "params": stage_params[stage],
                "reason": "no agent available",
            }
            proposal.stage_fallbacks[stage] = True
            any_fallback = True
            continue

        try:
            agent_output = agent.act(stage_context)
        except Exception as e:
            log.warning("[ORCH-E] StageAgent %s failed: %s", stage, e)
            agent_output = {
                "params": dict(BASELINE_PARAMS.get(stage, {})),
                "reason": f"fallback: {e}",
            }
            stage_fallback = True

        # Detect return-style fallback: Agent succeeded but reason
        # indicates degraded response.
        _sa_reason = str(agent_output.get("reason", ""))
        if not stage_fallback and _sa_reason.lower().startswith("fallback:"):
            stage_fallback = True
            log.warning("[ORCH-E] StageAgent %s returned fallback: %s",
                       stage, _sa_reason[:80])

        stage_params[stage] = agent_output.get("params", {})
        proposal.stage_proposals[stage] = {
            "params": dict(stage_params[stage]),
            "reason": _sa_reason,
        }
        proposal.stage_fallbacks[stage] = stage_fallback
        if stage_fallback:
            any_fallback = True

    # For non-agent stages, record baseline params in proposal too.
    for stage in _STAGE_ORDER[:4]:
        if stage not in agent_stages:
            proposal.stage_proposals[stage] = {
                "params": dict(stage_params.get(stage, {})),
                "reason": "inherited from parent",
            }
            proposal.stage_fallbacks[stage] = False

    # Ensure baseline is the floor for agent stages.
    for stage in agent_stages:
        base = BASELINE_PARAMS.get(stage, {})
        for k, v in base.items():
            if k not in stage_params.get(stage, {}):
                stage_params.setdefault(stage, {})[k] = v

    proposal.is_fallback = any_fallback
    return stage_params, proposal

@staticmethod


def _downstream_stages(decision_stage: str) -> List[str]:
    """Return stages AFTER *decision_stage* (not including it).

    PL fork → CTS, RT.  CTS fork → RT.
    For bootstrap (no decision_stage), returns all four stages.
    """
    if not decision_stage:
        return ["FP", "PL", "CTS", "RT"]
    try:
        idx = _STAGE_ORDER.index(decision_stage)
    except ValueError:
        return ["FP", "PL", "CTS", "RT"]
    # Stages strictly after decision_stage.
    return [s for s in _STAGE_ORDER[idx + 1:4]]



def _write_agent_proposal_trace(orch, proposal: AgentProposal) -> None:
    orch._trace_writer.append({
        "entry_type": "agent_proposal",
        "trial_id": proposal.trial_id,
        "candidate_index": proposal.candidate_index,
        "judge_branch_node": proposal.judge_branch_node,
        "judge_branch_stage": proposal.judge_branch_stage,
        "judge_hints": proposal.judge_hints,
        "judge_reason": proposal.judge_reason,
        "stage_proposals": {
            st: {"params": sp["params"], "reason": sp["reason"]}
            for st, sp in proposal.stage_proposals.items()
        },
        "is_fallback": proposal.is_fallback,
        "stage_fallbacks": proposal.stage_fallbacks,
        "proposal_role": proposal.proposal_role,
        "cohort_stage": proposal.proposal_role,
        "cohort_seed": orch.cfg.seed,
    })

# ------------------------------------------------------------------
# Parent selection with whitelist enforcement
# ------------------------------------------------------------------



def _judge_select_parent(
    orch, whitelist: List[str], decision_stage: str,
    fork_index: int, gwtk_parent: str,
) -> str:
    """Ask Judge to select a parent from the survivor whitelist.

    Builds a context presenting the whitelist as branchable nodes and
    calls Judge.act().  The Judge's ``branch_node`` output is
    interpreted as a trial_id from the whitelist.

    On Judge failure or invalid output, returns *gwtk_parent* as
    fallback (the GWTW scheduler's original choice).

    Returns:
        A trial_id — the Judge's chosen parent.
    """
    if orch.judge_agent is None:
        return gwtk_parent

    whitelist_short = [w[:8] for w in whitelist]
    judge_context = {
        "summary": (
            f"## Parent Selection for {decision_stage} Fork #{fork_index}\n"
            f"Survivor whitelist (choose one as branch_node):\n"
            + "\n".join(f"  - {w}" for w in whitelist_short)
            + f"\n\nGWTW scheduler suggested parent: {gwtk_parent[:8]}\n"
            f"Decision stage: {decision_stage}\n"
            f"Select a survivor trial_id as branch_node. "
            f"Your choice must be one of the whitelist entries above."
        ),
        "history": [],
        "best": None,
    }

    try:
        judge_decision = orch.judge_agent.act(judge_context)
    except Exception as e:
        log.warning("[ORCH-E] Judge parent selection failed: %s — "
                    "using GWTW parent %s", e, gwtk_parent[:8])
        if orch._is_real_llm:
            orch._parent_selection_errors.append(
                f"{decision_stage} fork#{fork_index}: Judge exception — "
                f"{e}")
        orch._trace_writer.append({
            "entry_type": "judge_parent_selection",
            "cohort_stage": decision_stage,
            "cohort_seed": orch.cfg.seed,
            "fork_index": fork_index,
            "data": {
                "judge_failed": True,
                "judge_error": str(e),
                "gwtk_parent": gwtk_parent,
                "whitelist": whitelist_short,
            },
        })
        return gwtk_parent

    branch_node = str(judge_decision.get("branch_node", gwtk_parent))
    _ps_reason = str(judge_decision.get("reason", ""))

    # Return-style fallback: Judge didn't raise but returned degraded reason.
    _ps_is_fallback = _ps_reason.lower().startswith("fallback:")
    if _ps_is_fallback:
        log.warning("[ORCH-E] Judge parent selection returned fallback: %s",
                   _ps_reason[:80])

    # Map short prefixes back to full trial IDs.
    chosen = _resolve_trial_id(branch_node, whitelist)
    if chosen is None:
        log.warning("[ORCH-E] Judge chose %r — not in whitelist %s",
                   branch_node[:8], whitelist_short)
        chosen = gwtk_parent

    # Fallback on return-style degradation: use GWTW parent.
    if _ps_is_fallback:
        if orch._is_real_llm:
            orch._parent_selection_errors.append(
                f"{decision_stage} fork#{fork_index}: Judge returned "
                f"fallback reason — {_ps_reason[:80]}")
        if chosen != gwtk_parent:
            log.warning("[ORCH-E] Judge parent selection fallback — "
                       "using GWTW parent %s instead of %s",
                       gwtk_parent[:8], chosen[:8])
            chosen = gwtk_parent

    # Write Judge's parent choice to trace.
    orch._trace_writer.append({
        "entry_type": "judge_parent_selection",
        "cohort_stage": decision_stage,
        "cohort_seed": orch.cfg.seed,
        "fork_index": fork_index,
        "data": {
            "judge_output_branch_node": branch_node,
            "resolved_parent": chosen,
            "gwtk_parent": gwtk_parent,
            "whitelist": whitelist_short,
            "judge_reason": _ps_reason,
            "judge_fallback_reason": _ps_is_fallback,
        },
    })
    return chosen



def _select_and_validate_parent(
    orch, requested_parent: str, decision_stage: str,
) -> ParentSelectionRecord:
    """Validate a parent choice against the survivor whitelist.

    If *requested_parent* is in the whitelist, returns it as effective.
    Otherwise, picks the first whitelist entry as fallback and records
    the rejection reason.

    Returns:
        ParentSelectionRecord with the effective parent.
    """
    whitelist = (orch._survivor_whitelist_pl if decision_stage == "PL"
                 else orch._survivor_whitelist_cts)

    if requested_parent in whitelist:
        record = ParentSelectionRecord(
            requested_parent=requested_parent,
            decision_stage=decision_stage,
            whitelist=list(whitelist),
            accepted=True,
            effective_parent=requested_parent,
        )
    else:
        reason = (
            f"requested parent {requested_parent[:8]} not in "
            f"{decision_stage} survivor whitelist "
            f"({[w[:8] for w in whitelist]})"
        )
        if not whitelist:
            reason += " — whitelist is empty, cannot proceed"

        # Deterministic fallback: first survivor in whitelist order.
        fallback = whitelist[0] if whitelist else ""
        record = ParentSelectionRecord(
            requested_parent=requested_parent,
            decision_stage=decision_stage,
            whitelist=list(whitelist),
            accepted=False,
            effective_parent=fallback,
            fallback_reason=reason,
        )
        log.warning("[ORCH-E] parent whitelist rejection: %s", reason)

    orch._parent_selections.append(record)
    # Write to trace.
    orch._trace_writer.append({
        "entry_type": "parent_selection",
        "trial_id": record.effective_parent,
        "cohort_stage": decision_stage,
        "cohort_seed": orch.cfg.seed,
        "data": record.to_dict(),
    })
    return record



def validate_parent_in_whitelist(
    orch, parent_trial_id: str, decision_stage: str,
) -> bool:
    whitelist = (orch._survivor_whitelist_pl if decision_stage == "PL"
                 else orch._survivor_whitelist_cts)
    return parent_trial_id in whitelist



def is_survivor(orch, trial_id: str, stage: str) -> bool:
    if stage == "PL":
        return trial_id in orch._survivor_whitelist_pl
    elif stage == "CTS":
        return trial_id in orch._survivor_whitelist_cts
    return False



def _enforce_child_parent_whitelist(
    orch, cr: CohortExecutionResult, decision_stage: str,
) -> None:
    """Validate that every child's parent is in the survivor whitelist.

    Runs after cohort execution.  For each child trial:
    - If parent not in whitelist, record rejection and mark child as
      invalid (does not delete — preserves evidence).
    """
    whitelist = (orch._survivor_whitelist_pl if decision_stage == "PL"
                 else orch._survivor_whitelist_cts)
    for cid in cr.child_trial_ids:
        child = orch.trial_mgr.get(cid)
        if child is None or child.parent_trial_id is None:
            continue
        orch._select_and_validate_parent(
            child.parent_trial_id, decision_stage)

# ------------------------------------------------------------------
# Cohort (with optional Agent-based child param generation)
# ------------------------------------------------------------------
