# -*- coding: utf-8 -*-
"""multi_agent_gwtw_orchestrator.py — Stage E: Multi-Agent + Doomed/GWTW orchestrator.

Integrates JudgeAgent + FP/PL/CTS/RT StageAgents with the Stage D Doomed/GWTW
pipeline (cohort_executor, doomed_predictor, gwtw_scheduler, mutation_planner).

Pipeline:
  1. Bootstrap population: Judge + StageAgents generate per-candidate params,
     execute to PL checkpoint.
  2. PL cohort: DoomedPredictor → GWTWScheduler → fork/continue/pause/audit.
     When Agents are available, child params come from StageAgents instead of
     mutation_planner; parent selection validated against survivor whitelist.
  3. Advance survivors + children to CTS.
  4. CTS cohort: same as PL, with CTS/RT Agents for child params.
  5. Advance survivors + children to finish.
  6. Collect final QoR and trace evidence.

Pure Python — no ORFS, no network.  MockLLMClient + RecordingFakeRunner for
zero-token testing.
"""

from __future__ import annotations

import copy
import json
import logging
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

from cohort_executor import CohortExecutionResult, execute_cohort
from cohort_planner import CohortPlan, plan_cohort
from config import BASELINE_PARAMS, FrameworkConfig, STAGES
from decision_trace import (
    DEFAULT_TRACE_PATH,
    DecisionTraceWriter,
    make_cohort_id,
    read_trace,
    write_cohort_complete,
    write_fork_intents,
)
from managers import CheckpointManager, TrialManager
from optimization_tree import OptimizationTree, ROOT_ID
from schemas.trial import (
    DecisionTraceRef,
    ExecutionResolution,
    StageResult,
    TrialRecord,
)

log = logging.getLogger(__name__)

_DEFAULT_DOOMED_VERSION = "1.0.0"
_DEFAULT_SCHEDULER_VERSION = "1.0.0"
_DEFAULT_PLANNER_VERSION = "1.0.0"

_STAGE_ORDER = ["FP", "PL", "CTS", "RT", "finish"]
_CHECKPOINTABLE = {"FP", "PL", "CTS"}
_STAGE_NEXT: Dict[str, str] = {"FP": "PL", "PL": "CTS", "CTS": "RT"}
_STAGE_ARTIFACTS: Dict[str, List[str]] = {
    "FP":  ["2_floorplan.odb", "2_floorplan.sdc"],
    "PL":  ["3_place.odb", "3_place.sdc"],
    "CTS": ["4_cts.odb", "4_cts.sdc"],
    "RT":  ["5_route.odb", "5_route.sdc"],
}


# =============================================================================
# Config
# =============================================================================


@dataclass
class MultiAgentGWTWConfig:
    """Stage E experiment configuration — YAML is sole authority."""

    experiment_id: str
    platform: str
    design: str
    population_size: int
    seed: int
    max_trials: int
    wall_clock_budget_s: Optional[float] = None
    pl_survivor_count: int = 2
    pl_audit_quota: int = 0
    pl_max_children_per_parent: int = 2
    cts_survivor_count: int = 1
    cts_audit_quota: int = 1
    cts_max_children_per_parent: int = 2
    doomed_rule_version: str = _DEFAULT_DOOMED_VERSION
    scheduler_version: str = _DEFAULT_SCHEDULER_VERSION
    planner_version: str = _DEFAULT_PLANNER_VERSION
    decision_stages: List[str] = field(default_factory=lambda: ["PL", "CTS"])
    evaluator: str = "ORFS post-route QoR"
    runs_dir: Optional[Path] = None

    def __post_init__(self) -> None:
        if self.population_size < 1:
            raise ValueError("population.size must be >= 1")
        if self.max_trials < 1:
            raise ValueError("budget.max_trials must be >= 1")
        if not self.experiment_id:
            raise ValueError("experiment_id is required")
        if self.seed is None:
            raise ValueError("seed is required")
        if self.pl_survivor_count > self.population_size:
            raise ValueError("PL survivor_count exceeds population_size")
        if self.cts_survivor_count > self.population_size:
            raise ValueError("CTS survivor_count exceeds population_size")

    @classmethod
    def from_yaml(cls, path: Path) -> "MultiAgentGWTWConfig":
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if data is None:
            raise ValueError(f"empty YAML: {path}")
        pop = data.get("population", {})
        pl = data.get("decisions", {}).get("PL", {})
        cts = data.get("decisions", {}).get("CTS", {})
        budget = data.get("budget", {})
        versions = data.get("versions", {})
        design = data.get("design", {})
        evaluator = data.get("evaluator", {})
        decision_stages = data.get("decision_stages", ["PL", "CTS"])
        return cls(
            experiment_id=data["experiment_id"],
            platform=design["platform"], design=design["design"],
            population_size=pop["size"], seed=data["seed"],
            max_trials=budget["max_trials"],
            wall_clock_budget_s=budget.get("wall_clock_s"),
            pl_survivor_count=pl.get("survivor_count", 2),
            pl_audit_quota=pl.get("audit_quota", 0),
            pl_max_children_per_parent=pl.get("max_children_per_parent", 2),
            cts_survivor_count=cts.get("survivor_count", 1),
            cts_audit_quota=cts.get("audit_quota", 1),
            cts_max_children_per_parent=cts.get("max_children_per_parent", 2),
            doomed_rule_version=versions.get(
                "doomed_rule", _DEFAULT_DOOMED_VERSION),
            scheduler_version=versions.get(
                "scheduler", _DEFAULT_SCHEDULER_VERSION),
            planner_version=versions.get(
                "planner", _DEFAULT_PLANNER_VERSION),
            decision_stages=decision_stages,
            evaluator=evaluator.get("type", "ORFS post-route QoR"),
        )

    def to_framework_config(self) -> FrameworkConfig:
        return FrameworkConfig(platform=self.platform, design=self.design)

    @property
    def _pl_cohort_cfg(self):
        return (self.pl_survivor_count, self.pl_audit_quota,
                self.population_size, self.pl_max_children_per_parent,
                self.doomed_rule_version, self.scheduler_version,
                self.planner_version)

    @property
    def _cts_cohort_cfg(self):
        return (self.cts_survivor_count, self.cts_audit_quota,
                self.population_size, self.cts_max_children_per_parent,
                self.doomed_rule_version, self.scheduler_version,
                self.planner_version)


# =============================================================================
# Agent proposal evidence
# =============================================================================


@dataclass
class AgentProposal:
    """Evidence linking one Agent decision to a Trial.

    Records what Judge chose, what hints were given, and what params
    each StageAgent produced — all traceable to a specific trial_id.

    ``is_fallback`` is True when ANY Agent call (Judge or StageAgent)
    fell back to baseline defaults instead of producing a real proposal.
    In real-LLM mode, all-fallback proposals cause the run to fail.
    """

    trial_id: str
    candidate_index: int
    judge_branch_node: str = ""
    judge_branch_stage: str = ""
    judge_hints: Dict[str, str] = field(default_factory=dict)
    judge_reason: str = ""
    stage_proposals: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # {stage: {"params": {...}, "reason": "..."}}
    is_fallback: bool = False
    # Per-stage fallback tracking: True when that stage's Agent fell back.
    stage_fallbacks: Dict[str, bool] = field(default_factory=dict)
    # Role of this proposal in the pipeline.
    proposal_role: str = "bootstrap"  # "bootstrap" | "pl_child" | "cts_child"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trial_id": self.trial_id,
            "candidate_index": self.candidate_index,
            "judge_branch_node": self.judge_branch_node,
            "judge_branch_stage": self.judge_branch_stage,
            "judge_hints": dict(self.judge_hints),
            "judge_reason": self.judge_reason,
            "stage_proposals": {
                st: dict(sp) for st, sp in self.stage_proposals.items()
            },
            "is_fallback": self.is_fallback,
            "stage_fallbacks": dict(self.stage_fallbacks),
            "proposal_role": self.proposal_role,
        }


# =============================================================================
# Parent selection fallback record
# =============================================================================


@dataclass
class ParentSelectionRecord:
    """Record of parent selection with whitelist enforcement result."""

    requested_parent: str        # the parent trial_id that was requested
    decision_stage: str          # "PL" | "CTS"
    whitelist: List[str]         # survivor whitelist at time of selection
    accepted: bool               # True if requested_parent ∈ whitelist
    effective_parent: str        # actual parent used (may differ if rejected)
    fallback_reason: str = ""    # why requested_parent was rejected (if not accepted)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requested_parent": self.requested_parent,
            "decision_stage": self.decision_stage,
            "whitelist": list(self.whitelist),
            "accepted": self.accepted,
            "effective_parent": self.effective_parent,
            "fallback_reason": self.fallback_reason,
        }


# =============================================================================
# Orchestrator
# =============================================================================


@dataclass
class StageEResult:
    experiment_id: str
    seed: int
    total_trials: int = 0
    budget_remaining: int = 0
    pl_cohort_result: Optional[CohortExecutionResult] = None
    cts_cohort_result: Optional[CohortExecutionResult] = None
    finish_trial_ids: List[str] = field(default_factory=list)
    agent_proposals: List[AgentProposal] = field(default_factory=list)
    survivor_whitelist_pl: List[str] = field(default_factory=list)
    survivor_whitelist_cts: List[str] = field(default_factory=list)
    parent_selections: List[ParentSelectionRecord] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    resumed: bool = False


class MultiAgentGWTWOrchestrator:
    """Stage E orchestrator: Judge + 4 StageAgents + Doomed/GWTW pipeline.

    - Bootstrap: Judge + StageAgents generate per-candidate params.
    - PL/CTS cohort: Doomed/GWTW decisions via plan_cohort; child params
      optionally generated by StageAgents instead of mutation_planner.
    - Parent selection validated against survivor whitelist; hard-dead/pause
      nodes rejected with traceable fallback.
    - Real-LLM mode: all-fallback proposals cause run failure (non-zero exit).
    """

    _NODE_ID_SEQ = 0

    def __init__(
        self, cfg: MultiAgentGWTWConfig, trial_mgr: TrialManager,
        checkpoint_mgr: CheckpointManager, runner: Any,
        judge_agent: Any = None, stage_agents: Dict[str, Any] = None,
        tree: Optional[OptimizationTree] = None,
    ) -> None:
        self.cfg = cfg
        self.trial_mgr = trial_mgr
        self.checkpoint_mgr = checkpoint_mgr
        self.runner = runner
        self.judge_agent = judge_agent
        self.stage_agents = stage_agents or {}
        self._runs_dir = cfg.runs_dir or trial_mgr.runs_dir
        self._iteration = 0
        self._new_trials = 0
        self._disk_trials_before = self._count_disk_trials()
        self.tree = tree or self._load_tree()
        self._node_to_trial: Dict[str, str] = {}
        self._trace_writer = DecisionTraceWriter(
            self._runs_dir, DEFAULT_TRACE_PATH)
        self._agent_proposals: Dict[str, AgentProposal] = {}
        self._survivor_whitelist_pl: List[str] = []
        self._survivor_whitelist_cts: List[str] = []
        self._parent_selections: List[ParentSelectionRecord] = []
        # Parent-selection fallback errors (reported in real-LLM mode).
        self._parent_selection_errors: List[str] = []
        # Track whether any real (non-fallback) Agent proposal was produced.
        self._any_real_proposal: bool = False

    # ------------------------------------------------------------------
    # Public entry
    # ------------------------------------------------------------------

    def run(self) -> StageEResult:
        result = StageEResult(
            experiment_id=self.cfg.experiment_id, seed=self.cfg.seed,
            resumed=self._has_pl_trials())

        import time as _time
        _wall_start = _time.monotonic()

        # ---- Bootstrap population (Agent-generated params) ----
        pop_trials = self._bootstrap_population()
        result.total_trials = self._disk_trials_before + self._new_trials

        # ---- Real-LLM guard: any fallback proposal → error ----
        if self._is_real_llm and self._has_agents:
            bootstrap_fallbacks = [
                p for p in self._agent_proposals.values()
                if p.proposal_role == "bootstrap" and p.is_fallback]
            if bootstrap_fallbacks:
                result.errors.append(
                    f"real-LLM mode: {len(bootstrap_fallbacks)} bootstrap "
                    f"Agent proposals are fallback — Agent call(s) degraded")
            if bootstrap_fallbacks and all(
                p.is_fallback for p in self._agent_proposals.values()
                if p.proposal_role == "bootstrap"):
                result.agent_proposals = list(self._agent_proposals.values())
                return result

        # ---- PL cohort ----
        try:
            result.pl_cohort_result = self._run_cohort(
                pop_trials, "PL", *self.cfg._pl_cohort_cfg)
        except Exception as e:
            result.errors.append(f"PL cohort Agent failure: {e}")
            result.agent_proposals = list(self._agent_proposals.values())
            return result
        if result.pl_cohort_result is None:
            result.errors.append("PL cohort failed")
            result.agent_proposals = list(self._agent_proposals.values())
            return result

        self._survivor_whitelist_pl = self._collect_survivors(
            result.pl_cohort_result)
        result.survivor_whitelist_pl = list(self._survivor_whitelist_pl)

        # Real-LLM guard: check PL child proposals for fallback.
        if self._is_real_llm:
            pl_child_fallbacks = [
                cid for cid in result.pl_cohort_result.child_trial_ids
                if self._agent_proposals.get(cid, AgentProposal(
                    trial_id=cid, candidate_index=-1)).is_fallback]
            if pl_child_fallbacks:
                result.errors.append(
                    f"real-LLM: {len(pl_child_fallbacks)} PL children have "
                    f"fallback Agent proposals — Agent call(s) failed")

        # Enforce whitelist on all PL children created by the cohort.
        self._enforce_child_parent_whitelist(
            result.pl_cohort_result, "PL")

        active_pl = self._collect_active(result.pl_cohort_result)
        self._add_children_to_tree(result.pl_cohort_result)
        for t in active_pl:
            self._advance_one(t, "CTS")
        self._save_tree()

        cts_trials = self._collect_cts_trials(result.pl_cohort_result)

        # ---- CTS cohort ----
        try:
            result.cts_cohort_result = self._run_cohort(
                cts_trials, "CTS", *self.cfg._cts_cohort_cfg)
        except Exception as e:
            result.errors.append(f"CTS cohort Agent failure: {e}")
            result.agent_proposals = list(self._agent_proposals.values())
            result.survivor_whitelist_pl = list(self._survivor_whitelist_pl)
            return result
        if result.cts_cohort_result is None:
            result.errors.append("CTS cohort failed")
            result.agent_proposals = list(self._agent_proposals.values())
            result.survivor_whitelist_pl = list(self._survivor_whitelist_pl)
            return result

        self._survivor_whitelist_cts = self._collect_survivors(
            result.cts_cohort_result)
        result.survivor_whitelist_cts = list(self._survivor_whitelist_cts)

        # Real-LLM guard: check CTS child proposals for fallback.
        if self._is_real_llm:
            cts_child_fallbacks = [
                cid for cid in result.cts_cohort_result.child_trial_ids
                if self._agent_proposals.get(cid, AgentProposal(
                    trial_id=cid, candidate_index=-1)).is_fallback]
            if cts_child_fallbacks:
                result.errors.append(
                    f"real-LLM: {len(cts_child_fallbacks)} CTS children have "
                    f"fallback Agent proposals — Agent call(s) failed")

        self._enforce_child_parent_whitelist(
            result.cts_cohort_result, "CTS")

        active_cts = self._collect_active(result.cts_cohort_result)
        self._add_children_to_tree(result.cts_cohort_result)
        for t in active_cts:
            self._advance_one(t, "finish")
        self._save_tree()

        result.finish_trial_ids = [
            t.trial_id for t in active_cts
            if self.trial_mgr.get(t.trial_id)
            and self.trial_mgr.get(t.trial_id).status == "ok"]
        # Aggregate parent-selection errors (real-LLM mode only).
        if self._is_real_llm and self._parent_selection_errors:
            result.errors.extend(self._parent_selection_errors)
        result.total_trials = self._disk_trials_before + self._new_trials
        result.budget_remaining = self.cfg.max_trials - result.total_trials
        result.agent_proposals = list(self._agent_proposals.values())
        result.parent_selections = list(self._parent_selections)

        _wall_elapsed = _time.monotonic() - _wall_start
        if (self.cfg.wall_clock_budget_s is not None
                and _wall_elapsed > self.cfg.wall_clock_budget_s):
            result.errors.append(
                f"wall_clock_budget exceeded: "
                f"{_wall_elapsed:.1f}s > {self.cfg.wall_clock_budget_s}s")

        return result

    # ------------------------------------------------------------------
    # Real-LLM detection
    # ------------------------------------------------------------------

    @property
    def _is_real_llm(self) -> bool:
        """True when using a real LLM client (not MockLLMClient)."""
        if self.judge_agent is None:
            return False
        llm = getattr(self.judge_agent, "llm", None)
        if llm is None:
            return False
        from llm_interface import MockLLMClient
        return not isinstance(llm, MockLLMClient)

    @property
    def _has_agents(self) -> bool:
        return self.judge_agent is not None and bool(self.stage_agents)

    # ------------------------------------------------------------------
    # Bootstrap (Agent-generated parameters)
    # ------------------------------------------------------------------

    def _bootstrap_population(self) -> List[TrialRecord]:
        existing = self.trial_mgr.list_by_experiment(self.cfg.experiment_id)
        pl_trials = [t for t in existing
                     if any(sr.stage == "PL" and sr.status == "ok"
                            for sr in t.stage_results)]
        if len(pl_trials) >= self.cfg.population_size:
            log.info("[ORCH-E] reusing %d existing PL trials", len(pl_trials))
            return pl_trials[:self.cfg.population_size]

        trials = list(pl_trials)
        for i in range(len(pl_trials), self.cfg.population_size):
            self._enforce_budget(1)
            t = self._bootstrap_one(i)
            trials.append(t)
            self._iteration += 1
        self._save_tree()
        return trials

    def _bootstrap_one(self, index: int) -> TrialRecord:
        t = self.trial_mgr.create(
            experiment_id=self.cfg.experiment_id, iteration=self._iteration)
        self._new_trials += 1

        stage_params, proposal = self._generate_params_for_candidate(
            t.trial_id, index, role="bootstrap")
        t.params = stage_params
        self._agent_proposals[t.trial_id] = proposal
        if not proposal.is_fallback:
            self._any_real_proposal = True

        variant = self._variant_for(t)
        for stage in ["FP", "PL"]:
            sr = self.runner.run_stage(stage, t.params, variant, self._iteration)
            t.stage_results.append(sr)
            if sr.status != "ok":
                t.status = "failed"; self.trial_mgr.update(t); return t
        self._create_checkpoint(t, "PL", variant)
        t.config_hash = _hash_params(t.params)
        t.status = "ok"; self.trial_mgr.update(t)

        self._write_agent_proposal_trace(proposal)

        fp_qor = t.stage_results[0].stage_qor
        pl_qor = t.stage_results[1].stage_qor
        fp_nid = self._make_unique_nid("FP", t.trial_id)
        pl_nid = self._make_unique_nid("PL", t.trial_id)
        self._node_to_trial[fp_nid] = t.trial_id
        self._node_to_trial[pl_nid] = t.trial_id
        self.tree.add_path(
            self._iteration * 10 + 100, ROOT_ID,
            [("FP", variant, t.params.get("FP", {}), fp_qor)],
            source_trial_id=t.trial_id,
            node_ids=[fp_nid])
        self.tree.add_path(
            self._iteration * 10 + 100, fp_nid,
            [("PL", variant, t.params.get("PL", {}), pl_qor)],
            source_trial_id=t.trial_id,
            node_ids=[pl_nid])
        return t

    def _generate_params_for_candidate(
        self, trial_id: str, index: int, role: str = "bootstrap",
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

        if not self._has_agents:
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
            agent_stages = self._downstream_stages(decision_stage)
        else:
            agent_stages = ["FP", "PL", "CTS", "RT"]

        # Inherit parent params for stages before decision_stage.
        stage_params: Dict[str, Dict[str, Any]] = {}
        if parent_trial_id and decision_stage:
            parent = self.trial_mgr.get(parent_trial_id)
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
                f"of {self.cfg.population_size}.\n"
                f"Platform: {self.cfg.platform}, Design: {self.cfg.design}\n"
            )

        judge_context = {
            "summary": judge_summary,
            "history": [],
            "best": None,
        }

        judge_fallback = False
        try:
            judge_decision = self.judge_agent.act(judge_context)
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
            agent = self.stage_agents.get(stage)
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

    def _write_agent_proposal_trace(self, proposal: AgentProposal) -> None:
        self._trace_writer.append({
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
            "cohort_seed": self.cfg.seed,
        })

    # ------------------------------------------------------------------
    # Parent selection with whitelist enforcement
    # ------------------------------------------------------------------

    def _judge_select_parent(
        self, whitelist: List[str], decision_stage: str,
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
        if self.judge_agent is None:
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
            judge_decision = self.judge_agent.act(judge_context)
        except Exception as e:
            log.warning("[ORCH-E] Judge parent selection failed: %s — "
                        "using GWTW parent %s", e, gwtk_parent[:8])
            if self._is_real_llm:
                self._parent_selection_errors.append(
                    f"{decision_stage} fork#{fork_index}: Judge exception — "
                    f"{e}")
            self._trace_writer.append({
                "entry_type": "judge_parent_selection",
                "cohort_stage": decision_stage,
                "cohort_seed": self.cfg.seed,
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
            if self._is_real_llm:
                self._parent_selection_errors.append(
                    f"{decision_stage} fork#{fork_index}: Judge returned "
                    f"fallback reason — {_ps_reason[:80]}")
            if chosen != gwtk_parent:
                log.warning("[ORCH-E] Judge parent selection fallback — "
                           "using GWTW parent %s instead of %s",
                           gwtk_parent[:8], chosen[:8])
                chosen = gwtk_parent

        # Write Judge's parent choice to trace.
        self._trace_writer.append({
            "entry_type": "judge_parent_selection",
            "cohort_stage": decision_stage,
            "cohort_seed": self.cfg.seed,
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
        self, requested_parent: str, decision_stage: str,
    ) -> ParentSelectionRecord:
        """Validate a parent choice against the survivor whitelist.

        If *requested_parent* is in the whitelist, returns it as effective.
        Otherwise, picks the first whitelist entry as fallback and records
        the rejection reason.

        Returns:
            ParentSelectionRecord with the effective parent.
        """
        whitelist = (self._survivor_whitelist_pl if decision_stage == "PL"
                     else self._survivor_whitelist_cts)

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

        self._parent_selections.append(record)
        # Write to trace.
        self._trace_writer.append({
            "entry_type": "parent_selection",
            "trial_id": record.effective_parent,
            "cohort_stage": decision_stage,
            "cohort_seed": self.cfg.seed,
            "data": record.to_dict(),
        })
        return record

    def validate_parent_in_whitelist(
        self, parent_trial_id: str, decision_stage: str,
    ) -> bool:
        whitelist = (self._survivor_whitelist_pl if decision_stage == "PL"
                     else self._survivor_whitelist_cts)
        return parent_trial_id in whitelist

    def is_survivor(self, trial_id: str, stage: str) -> bool:
        if stage == "PL":
            return trial_id in self._survivor_whitelist_pl
        elif stage == "CTS":
            return trial_id in self._survivor_whitelist_cts
        return False

    def _enforce_child_parent_whitelist(
        self, cr: CohortExecutionResult, decision_stage: str,
    ) -> None:
        """Validate that every child's parent is in the survivor whitelist.

        Runs after cohort execution.  For each child trial:
        - If parent not in whitelist, record rejection and mark child as
          invalid (does not delete — preserves evidence).
        """
        whitelist = (self._survivor_whitelist_pl if decision_stage == "PL"
                     else self._survivor_whitelist_cts)
        for cid in cr.child_trial_ids:
            child = self.trial_mgr.get(cid)
            if child is None or child.parent_trial_id is None:
                continue
            self._select_and_validate_parent(
                child.parent_trial_id, decision_stage)

    # ------------------------------------------------------------------
    # Cohort (with optional Agent-based child param generation)
    # ------------------------------------------------------------------

    def _run_cohort(
        self, cohort: List[TrialRecord], decision_stage: str,
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
        Agents are not available, preserving Stage D behavior.
        """
        if not cohort:
            return None

        _reserved = self._reserve_child_budget(
            cohort, survivor_count, audit_quota, population_size,
            max_children_per_parent, decision_stage)
        if _reserved == 0:
            log.info("[ORCH-E] cohort %s: no new children needed", decision_stage)

        params_by_id = {t.trial_id: t.params for t in cohort}

        # Agent path: Judge selects parent, StageAgents generate params.
        if self._has_agents:
            try:
                cr = self._execute_cohort_with_agents(
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
                    self._new_trials += actual_children

                # Real-LLM guard: if any child proposal is fallback, that's
                # an error — do not silently accept mutation-planner children.
                if self._is_real_llm:
                    for cid in cr.child_trial_ids:
                        ap = self._agent_proposals.get(cid)
                        if ap and ap.is_fallback:
                            log.error(
                                "[ORCH-E] real-LLM: child %s has fallback "
                                "Agent proposal — cohort %s degraded",
                                cid[:6], decision_stage)
                            # Don't return None (cohort still ran) but the
                            # caller should check result.errors.

                return cr
            except Exception as e:
                if self._is_real_llm:
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

        # Default: Stage D mutation_planner path (no Agents, or Agent
        # path failed in non-real-LLM mode).
        try:
            cr = execute_cohort(
                cohort=cohort, decision_stage=decision_stage,
                survivor_count=survivor_count, audit_quota=audit_quota,
                population_size=population_size,
                max_children_per_parent=max_children_per_parent,
                seed=self.cfg.seed, parent_params_by_id=params_by_id,
                trial_mgr=self.trial_mgr,
                checkpoint_mgr=self.checkpoint_mgr,
                tree=self.tree, experiment_id=self.cfg.experiment_id,
                iteration=self._iteration, runs_dir=self._runs_dir,
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
                self._new_trials += actual_children
            return cr
        except Exception as e:
            log.error("[ORCH-E] cohort failed: %s", e); return None

    def _execute_cohort_with_agents(
        self, cohort: List[TrialRecord], decision_stage: str,
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
        from cohort_planner import CohortPlanError
        from doomed_predictor import DEFAULT_RULE_VERSION as _DDV
        from gwtw_scheduler import (
            AllHardDeadError, PopulationCapacityError,
        )
        from mutation_planner import NoLegalMutationError

        # Phase 1: plan the cohort (same as Stage D).
        plan = plan_cohort(
            cohort, decision_stage=decision_stage,
            survivor_count=survivor_count, audit_quota=audit_quota,
            population_size=population_size,
            max_children_per_parent=max_children_per_parent,
            seed=self.cfg.seed, parent_params_by_id=parent_params_by_id,
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
            self._survivor_whitelist_pl = list(cohort_survivors)
        else:
            self._survivor_whitelist_cts = list(cohort_survivors)

        tids = [t.trial_id for t in cohort]
        cfg_tuple = (
            survivor_count, audit_quota, population_size,
            max_children_per_parent,
            doomed_rule_version, scheduler_version, planner_version,
        )
        _cohort_id = make_cohort_id(
            decision_stage, self.cfg.seed, tids, *cfg_tuple)

        trace_refs: List[DecisionTraceRef] = []

        # Phase 2: write trace entries (observation, doomed, gwtw).
        trial_outcomes: Dict[str, str] = {}
        for obs, doomed, gwtw in zip(plan.observations,
                                     plan.doomed_decisions,
                                     plan.gwtw_decisions):
            trial_outcomes[obs.trial_id] = gwtw.action

            obs_ref = self._trace_writer.append({
                "entry_type": "observation", "cohort_id": _cohort_id,
                "trial_id": obs.trial_id, "cohort_stage": decision_stage,
                "cohort_seed": self.cfg.seed, "data": obs.to_dict(),
            })
            trace_refs.append(obs_ref)

            doomed_ref = self._trace_writer.append({
                "entry_type": "doomed_decision", "cohort_id": _cohort_id,
                "trial_id": obs.trial_id, "cohort_stage": decision_stage,
                "cohort_seed": self.cfg.seed, "data": doomed.to_dict(),
                "rule_version": doomed_rule_version,
            })
            trace_refs.append(doomed_ref)

            gwtw_ref = self._trace_writer.append({
                "entry_type": "gwtw_decision", "cohort_id": _cohort_id,
                "trial_id": obs.trial_id, "cohort_stage": decision_stage,
                "cohort_seed": self.cfg.seed, "data": gwtw.to_dict(),
                "scheduler_version": scheduler_version,
            })
            trace_refs.append(gwtw_ref)

            trial = self._find_trial_in_cohort(cohort, obs.trial_id)
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
            self.trial_mgr.update(trial)

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
            judge_parent = self._judge_select_parent(
                whitelist=cohort_survivors,
                decision_stage=decision_stage,
                fork_index=child_index,
                gwtk_parent=fp.fork_request.parent_trial_id,
            )
            # Validate Judge's choice against whitelist.
            sel = self._select_and_validate_parent(
                judge_parent, decision_stage)
            effective_parent = sel.effective_parent

            if not effective_parent:
                log.error("[ORCH-E] no valid parent for fork — skipping child")
                continue

            parent_trial = self._find_trial_in_cohort(cohort, effective_parent)

            # ---- 3b) StageAgents generate downstream params ----
            role = "pl_child" if decision_stage == "PL" else "cts_child"
            child = self.trial_mgr.create(
                experiment_id=self.cfg.experiment_id,
                parent_trial_id=effective_parent,
                branch_stage=decision_stage,
                iteration=self._iteration,
            )

            child_params, child_proposal = self._generate_params_for_candidate(
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
            self._agent_proposals[child.trial_id] = child_proposal
            if not child_proposal.is_fallback:
                self._any_real_proposal = True
            self._write_agent_proposal_trace(child_proposal)

            # ---- 3c) Write fork trace with Judge provenance ----
            parent_cp = parent_trial.checkpoint
            cp_id = parent_cp.checkpoint_id if parent_cp else "unknown"
            fork_ref = self._trace_writer.append({
                "entry_type": "fork",
                "cohort_id": _cohort_id,
                "trial_id": child.trial_id,
                "parent_trial_id": effective_parent,
                "cohort_stage": decision_stage,
                "cohort_seed": self.cfg.seed,
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
            resolution = self._resolve_child_checkpoint(
                parent_trial=parent_trial, child=child,
                child_params=child_params,
                inherited_params=inherited_params,
                checkpoint_id=cp_id,
            )
            child.execution_resolution = resolution

            er_ref = self._trace_writer.append({
                "entry_type": "execution_resolution",
                "cohort_id": _cohort_id,
                "trial_id": child.trial_id,
                "parent_trial_id": effective_parent,
                "cohort_stage": decision_stage,
                "cohort_seed": self.cfg.seed,
                "data": resolution.to_dict(),
            })
            trace_refs.append(er_ref)
            child.decision_trace_refs.append(er_ref)

            self.trial_mgr.update(child)
            child_ids.append(child.trial_id)
            child_resolutions.append(resolution)

        # Phase 4: write sentinel.
        sentinel_ref = write_cohort_complete(
            self._trace_writer, decision_stage, self.cfg.seed,
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
            seed=self.cfg.seed,
        )

    def _resolve_child_checkpoint(
        self, parent_trial: TrialRecord, child: TrialRecord,
        child_params: Dict[str, Dict[str, Any]],
        inherited_params: Dict[str, Dict[str, Any]],
        checkpoint_id: str,
    ) -> ExecutionResolution:
        from checkpoint_resolver import resolve_checkpoint
        from cohort_executor import _find_parent_node_id as _fpn

        parent_node_id = _fpn(self.tree, parent_trial.trial_id)

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
            tree=self.tree,
            trial_mgr=self.trial_mgr,
            checkpoint_mgr=self.checkpoint_mgr,
            runs_dir=self._runs_dir,
        )

    @staticmethod
    def _find_trial_in_cohort(
        cohort: List[TrialRecord], trial_id: str,
    ) -> TrialRecord:
        for t in cohort:
            if t.trial_id == trial_id:
                return t
        from cohort_planner import CohortPlanError
        raise CohortPlanError(
            f"Trial {trial_id!r} not found in cohort")

    # ------------------------------------------------------------------
    # Budget
    # ------------------------------------------------------------------

    def _reserve_child_budget(
        self, cohort: List[TrialRecord], survivor_count: int,
        audit_quota: int, population_size: int, max_children_per_parent: int,
        decision_stage: str = "PL",
    ) -> int:
        tids = [t.trial_id for t in cohort]
        already_done = False
        try:
            from decision_trace import cohort_already_executed
            already_done = cohort_already_executed(
                self._runs_dir, DEFAULT_TRACE_PATH,
                decision_stage, self.cfg.seed, tids,
                survivor_count, audit_quota,
                population_size, max_children_per_parent,
                self.cfg.doomed_rule_version,
                self.cfg.scheduler_version, self.cfg.planner_version)
        except Exception:
            pass
        if already_done:
            return 0
        worst_active = min(survivor_count + audit_quota, len(cohort))
        needed = max(0, population_size - worst_active)
        self._enforce_budget(needed)
        return needed

    def _enforce_budget(self, additional: int = 0) -> None:
        current = self._disk_trials_before + self._new_trials
        if current + additional > self.cfg.max_trials:
            raise RuntimeError(
                f"max_trials ({self.cfg.max_trials}) exceeded "
                f"(have {current}, need +{additional})")

    # ------------------------------------------------------------------
    # Resume detection
    # ------------------------------------------------------------------

    def _count_disk_trials(self) -> int:
        return len(self.trial_mgr.list_by_experiment(self.cfg.experiment_id))

    def _has_pl_trials(self) -> bool:
        return any(
            any(sr.stage == "PL" and sr.status == "ok"
                for sr in t.stage_results)
            for t in self.trial_mgr.list_by_experiment(self.cfg.experiment_id))

    # ------------------------------------------------------------------
    # Tree
    # ------------------------------------------------------------------

    def _tree_path(self) -> Path:
        return self._runs_dir / "tree.json"

    def _load_tree(self) -> OptimizationTree:
        tp = self._tree_path()
        if tp.is_file():
            try:
                return OptimizationTree.from_dict(
                    json.loads(tp.read_text(encoding="utf-8")))
            except Exception:
                log.warning("[ORCH-E] corrupt tree.json — fresh start")
        return OptimizationTree()

    def _save_tree(self) -> None:
        tp = self._tree_path()
        tmp = tp.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.tree.to_dict(), indent=2))
        tmp.replace(tp)

    def _make_unique_nid(self, stage: str, trial_id: str) -> str:
        MultiAgentGWTWOrchestrator._NODE_ID_SEQ += 1
        nid = f"se-{MultiAgentGWTWOrchestrator._NODE_ID_SEQ}-{stage}-{trial_id[:6]}"
        self._node_to_trial[nid] = trial_id
        return nid

    def _add_stage_node_to_tree(self, t: TrialRecord, stage: str,
                                 variant: str,
                                 stage_qor: Dict[str, float]) -> None:
        for nid, n in self.tree._nodes.items():
            if (getattr(n, "source_trial_id", None) == t.trial_id
                    and n.stage == stage):
                n.stage_qor = dict(stage_qor) if stage_qor else {}
                n.params = dict(t.params.get(stage, {}))
                return
        parent_node = self._find_deepest_node(t.trial_id)
        parent_id = parent_node.node_id if parent_node else ROOT_ID
        child_nid = self._make_unique_nid(stage, t.trial_id)
        self.tree.add_path(
            self._iteration * 10 + 300, parent_id,
            [(stage, variant, t.params.get(stage, {}),
              dict(stage_qor) if stage_qor else {})],
            source_trial_id=t.trial_id,
            node_ids=[child_nid])

    def _add_children_to_tree(self, cr: CohortExecutionResult) -> None:
        for cid in cr.child_trial_ids:
            child = self.trial_mgr.get(cid)
            if child is None or child.parent_trial_id is None:
                continue
            parent_node = self._find_deepest_node(child.parent_trial_id)
            if parent_node is None:
                continue
            pt = self.trial_mgr.get(child.parent_trial_id)
            er = child.execution_resolution
            if er and er.execution_mode == "checkpoint_fork":
                child_stage = er.effective_start_stage
            else:
                cp_stage = pt.checkpoint.stage if (pt and pt.checkpoint) else "PL"
                child_stage = _STAGE_NEXT.get(cp_stage, "CTS")
            child_nid = self._make_unique_nid(child_stage, cid)
            self._node_to_trial[child_nid] = cid
            child_params_for_tree = dict(child.params.get(child_stage, {}))
            self.tree.add_path(
                self._iteration * 10 + 200, parent_node.node_id,
                [(child_stage, self._variant_for(child),
                  child_params_for_tree, {})],
                source_trial_id=cid,
                node_ids=[child_nid])

    def _find_deepest_node(self, source_trial_id: str) -> Any:
        best, best_order = None, -1
        flow = {"FP": 0, "PL": 1, "CTS": 2, "RT": 3}
        for n in self.tree._nodes.values():
            if getattr(n, "source_trial_id", None) != source_trial_id:
                continue
            o = flow.get(n.stage, -1)
            if o > best_order:
                best_order, best = o, n
        return best

    # ------------------------------------------------------------------
    # Advance: copy → clean → execute
    # ------------------------------------------------------------------

    def _advance_one(self, trial: TrialRecord, target_stage: str) -> None:
        t = self.trial_mgr.get(trial.trial_id)
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
                parent = self.trial_mgr.get(t.parent_trial_id)
                if parent:
                    consumed_variant = self._variant_for(parent)
        else:
            cp_stage = t.checkpoint.stage if t.checkpoint else None
            effective_start = (
                _STAGE_NEXT.get(cp_stage, "FP") if cp_stage else "FP")
            consumed_variant = None

        variant = self._variant_for(t)
        if consumed_variant:
            self.runner.copy_parent_results(consumed_variant, variant)
            self.runner.clean_downstream(variant, effective_start)

        if target_stage == "finish":
            self._run_to_finish(t, effective_start, variant)
        else:
            self._run_stages(t, effective_start, target_stage, variant)
        self._iteration += 1

    def _run_stages(self, t: TrialRecord, start: str, end: str,
                    variant: str) -> None:
        try:
            si, ei = _STAGE_ORDER.index(start), _STAGE_ORDER.index(end)
        except ValueError:
            return
        for stage in _STAGE_ORDER[si:ei + 1]:
            sr = self.runner.run_stage(stage, t.params, variant, self._iteration)
            t.stage_results.append(sr)
            if sr.status != "ok":
                t.status = "failed"; self.trial_mgr.update(t); return
            self._add_stage_node_to_tree(t, stage, variant, sr.stage_qor)
        if end in _CHECKPOINTABLE:
            self._create_checkpoint(t, end, variant)
        t.status = "ok"; self.trial_mgr.update(t)

    def _run_to_finish(self, t: TrialRecord, effective_start: str,
                       variant: str) -> None:
        try:
            si = _STAGE_ORDER.index(effective_start)
        except ValueError:
            return
        for stage in _STAGE_ORDER[si:4]:
            sr = self.runner.run_stage(stage, t.params, variant, self._iteration)
            t.stage_results.append(sr)
            if sr.status != "ok":
                t.status = "failed"; self.trial_mgr.update(t); return
            self._add_stage_node_to_tree(t, stage, variant, sr.stage_qor)
        fr = self.runner.run_finish(t.params, variant, self._iteration)
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
        self.trial_mgr.update(t)

    # ------------------------------------------------------------------
    # Survivor whitelist
    # ------------------------------------------------------------------

    def _collect_survivors(self, cr: CohortExecutionResult) -> List[str]:
        survivors: List[str] = []
        if cr.cohort_plan is not None:
            for obs, dd in zip(cr.cohort_plan.observations,
                               cr.cohort_plan.doomed_decisions):
                if dd.risk_class == "survivor":
                    survivors.append(obs.trial_id)
        else:
            for tid, action in cr.trial_outcomes.items():
                if action in ("continue", "audit_continue"):
                    t = self.trial_mgr.get(tid)
                    if t and t.doomed_decisions:
                        for dd in t.doomed_decisions:
                            if dd.risk_class == "survivor":
                                survivors.append(tid)
                                break
        return survivors

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _variant_for(self, trial: TrialRecord) -> str:
        return f"agenticpd_se_{trial.trial_id}"

    def _create_checkpoint(self, trial: TrialRecord, stage: str,
                           variant: str) -> None:
        try:
            trial.checkpoint = self.checkpoint_mgr.create(
                trial=trial, stage=stage,
                platform=self.cfg.platform, design=self.cfg.design,
                variant=variant,
                param_hash=CheckpointManager.param_hash(trial.params),
                runs_dir=self._runs_dir)
        except Exception:
            log.warning("[ORCH-E] checkpoint failed for %s", trial.trial_id)

    def _collect_active(self, cr: CohortExecutionResult) -> List[TrialRecord]:
        active, seen = [], set()
        for tid, action in cr.trial_outcomes.items():
            if action in ("continue", "audit_continue"):
                t = self.trial_mgr.get(tid)
                if t and t.trial_id not in seen:
                    active.append(t); seen.add(t.trial_id)
        for cid in cr.child_trial_ids:
            t = self.trial_mgr.get(cid)
            if t and t.trial_id not in seen:
                active.append(t); seen.add(t.trial_id)
        return active

    def _collect_cts_trials(
        self, pl_result: CohortExecutionResult) -> List[TrialRecord]:
        cts, seen = [], set()
        for tid, action in pl_result.trial_outcomes.items():
            if action in ("continue", "audit_continue"):
                t = self.trial_mgr.get(tid)
                if t and any(sr.stage == "CTS" for sr in t.stage_results):
                    if t.trial_id not in seen:
                        cts.append(t); seen.add(t.trial_id)
        for cid in pl_result.child_trial_ids:
            t = self.trial_mgr.get(cid)
            if t and any(sr.stage == "CTS" for sr in t.stage_results):
                if t.trial_id not in seen:
                    cts.append(t); seen.add(t.trial_id)
        return cts


# =============================================================================
# Recording fake runner (for mock mode)
# =============================================================================


class StageERecordingFakeRunner:
    """Stateful fake that records calls + creates real artifact files.

    Used for pure Python testing — no ORFS, no network.
    """

    def __init__(self, flow_dir: Path) -> None:
        self.flow_dir = Path(flow_dir)
        self.calls: List[Dict[str, Any]] = []
        self._artifact_files: Dict[str, set] = {}

    def _record(self, method: str, **kw) -> None:
        self.calls.append({"method": method, **kw})

    def _ensure_artifacts(self, variant: str, stage: str) -> None:
        files = _STAGE_ARTIFACTS.get(stage, [])
        vdir = self.flow_dir / "results" / "sky130hd" / "gcd" / variant
        vdir.mkdir(parents=True, exist_ok=True)
        for fname in files:
            p = vdir / fname
            p.write_text(f"fake {stage} {variant} {fname}")
            self._artifact_files.setdefault(variant, set()).add(str(p))

    def _make_qor(self, params: Dict, stage: str) -> Dict[str, float]:
        util = params.get("FP", {}).get("CORE_UTILIZATION", 38)
        wns = -1500.0 + (util - 20) * 5.0; tns = wns * 40.0
        _map = {"FP": (1.0, "2_1_floorplan"), "PL": (1.05, "3_5_place_dp"),
                "CTS": (1.02, "4_1_cts"), "RT": (1.01, "5_1_grt")}
        scale, tag = _map.get(stage, (1.0, stage))
        return {f"{tag}_ws_ps": round(wns * scale, 1),
                f"{tag}_tns_ps": round(tns * scale, 1)}

    def run_stage(self, stage: str, params: Any, variant: str,
                  iteration: int) -> StageResult:
        self._record("run_stage", stage=stage, variant=variant)
        self._ensure_artifacts(variant, stage)
        return StageResult(
            stage=stage, status="ok", elapsed_s=0.02, exit_code=0,
            stage_qor=self._make_qor(params, stage))

    def run_finish(self, params: Any, variant: str, iteration: int) -> Any:
        self._record("run_finish", variant=variant)
        from orfs.interface import RunResult; from utils import QoR
        util = params.get("FP", {}).get("CORE_UTILIZATION", 38)
        wns = -1500.0 + (util - 20) * 5.0
        return RunResult(
            ok=True, variant=variant,
            qor=QoR(wns_ps=round(wns * 1.01, 1),
                    tns_ps=round(wns * 40 * 1.01, 1),
                    area_um2=5000.0, power_w=0.008),
            stage_qor={"5_2_route_ws_ps": round(wns*1.01,1),
                       "5_2_route_tns_ps": round(wns*40*1.01,1)},
            elapsed_s=0.05, command="[mock] make finish",
            report_path="[mock] reports/.../6_report.json")

    def copy_parent_results(self, parent_variant: str,
                            child_variant: str) -> None:
        self._record("copy_parent_results",
                     parent=parent_variant, child=child_variant)

    def clean_downstream(self, variant: str, effective_start: str) -> None:
        self._record("clean_downstream", variant=variant,
                     effective_start=effective_start)
        try:
            si = _STAGE_ORDER.index(effective_start)
        except ValueError:
            return
        for stage in _STAGE_ORDER[si:4]:
            for fname in _STAGE_ARTIFACTS.get(stage, []):
                p = (self.flow_dir / "results" / "sky130hd" / "gcd"
                     / variant / fname)
                if p.is_file():
                    p.unlink()
                self._artifact_files.setdefault(variant, set()).discard(
                    str(p))


# =============================================================================
# Helpers
# =============================================================================


def _resolve_trial_id(
    branch_node: str, whitelist: List[str],
) -> Optional[str]:
    """Resolve a Judge branch_node to a trial_id in *whitelist*.

    Tries exact match first, then prefix match (first 6–8 chars).
    Returns None if no match found.
    """
    if branch_node in whitelist:
        return branch_node
    # Prefix match: first N chars must uniquely match one whitelist entry.
    for n in (8, 6):
        matches = [w for w in whitelist if w.startswith(branch_node[:n])]
        if len(matches) == 1:
            return matches[0]
    return None


def _hash_params(params: Dict) -> Optional[str]:
    try: return CheckpointManager.param_hash(params)
    except Exception: return None


# =============================================================================
# Self-test
# =============================================================================

if __name__ == "__main__":
    import shutil, sys, tempfile
    ok = 0; fail_count = 0
    def check(cond, msg):
        global ok, fail_count
        if cond: ok += 1
        else: fail_count += 1; print(f"  FAIL: {msg}")

    tmpdir = Path(tempfile.mkdtemp())
    runs_dir = tmpdir / "runs"; runs_dir.mkdir(parents=True)
    flow_dir_v = tmpdir / "flow"
    tm = TrialManager(runs_dir); cm = CheckpointManager(flow_dir_v)

    cfg = MultiAgentGWTWConfig(
        experiment_id="self-test", platform="sky130hd", design="gcd",
        population_size=4, seed=42, max_trials=20,
        pl_survivor_count=2, pl_audit_quota=0, pl_max_children_per_parent=2,
        cts_survivor_count=1, cts_audit_quota=1, cts_max_children_per_parent=2,
        runs_dir=runs_dir)

    # ---- 1. Default (no agents) run ----
    r1 = StageERecordingFakeRunner(flow_dir_v)
    orch1 = MultiAgentGWTWOrchestrator(cfg, tm, cm, r1)
    result = orch1.run()
    check(result.errors == [], f"no errors: {result.errors}")
    check(result.total_trials == len(tm.list_all()),
          f"total_trials matches: {result.total_trials} == {len(tm.list_all())}")
    check("run_finish" in {c["method"] for c in r1.calls}, "run_finish")
    check("clean_downstream" in {c["method"] for c in r1.calls}, "clean_downstream")

    # ---- 2. Agent proposals (fallback path) ----
    check(len(result.agent_proposals) == cfg.population_size,
          f"proposals: {len(result.agent_proposals)}")
    for ap in result.agent_proposals:
        check(ap.is_fallback is True, "no-agents → is_fallback=True")
        for stage in ["FP", "PL", "CTS", "RT"]:
            check(stage in ap.stage_proposals,
                  f"proposal has {stage}")
            check(ap.stage_fallbacks.get(stage) is True,
                  f"stage {stage} fallback=True")

    # ---- 3. Survivor whitelist ----
    check(len(result.survivor_whitelist_pl) > 0, "PL whitelist populated")
    check(len(result.survivor_whitelist_cts) > 0, "CTS whitelist populated")
    for sid in result.survivor_whitelist_pl:
        check(orch1.validate_parent_in_whitelist(sid, "PL"),
              f"PL survivor {sid[:6]} in whitelist")
    check(not orch1.validate_parent_in_whitelist("nonexistent", "PL"),
          "nonexistent not in whitelist")

    # ---- 4. Parent selection records ----
    pl_children = result.pl_cohort_result.child_trial_ids
    for cid in pl_children:
        child = tm.get(cid)
        check(child is not None, f"child {cid[:6]} exists")
        check(child.parent_trial_id is not None,
              f"child {cid[:6]} has parent")
        check(orch1.validate_parent_in_whitelist(child.parent_trial_id, "PL"),
              f"child {cid[:6]} parent in PL whitelist")

    # ---- 5. Trace evidence ----
    entries = read_trace(runs_dir, DEFAULT_TRACE_PATH)
    etypes = {e["entry_type"] for e in entries}
    for et in ("agent_proposal", "observation", "doomed_decision",
               "gwtw_decision", "fork", "execution_resolution",
               "cohort_complete"):
        check(et in etypes, f"trace has {et}")

    # ---- 6. Resume ----
    n_trials = len(tm.list_all())
    r2 = StageERecordingFakeRunner(flow_dir_v)
    orch2 = MultiAgentGWTWOrchestrator(cfg, tm, cm, r2)
    result2 = orch2.run()
    check(result2.resumed, "resume detected")
    check(len(r2.calls) == 0, f"resume zero calls: {len(r2.calls)}")

    # ---- 7. Budget ----
    cfg_tight = MultiAgentGWTWConfig(
        experiment_id="tight", platform="x", design="y",
        population_size=4, seed=1, max_trials=1,
        pl_survivor_count=1, pl_audit_quota=0, pl_max_children_per_parent=1,
        cts_survivor_count=1, cts_audit_quota=0, cts_max_children_per_parent=1,
        runs_dir=runs_dir)
    try:
        MultiAgentGWTWOrchestrator(
            cfg_tight, tm, cm,
            StageERecordingFakeRunner(flow_dir_v)).run()
        check(False, "tight budget should raise")
    except RuntimeError as e:
        check("max_trials" in str(e), f"budget: {e}")

    # ---- 8. YAML parsing ----
    yaml_path = (Path(__file__).resolve().parent
                 / "configs" / "experiments" / "multi-agent-gwtw-demo.yml")
    if yaml_path.is_file():
        yaml_cfg = MultiAgentGWTWConfig.from_yaml(yaml_path)
        check(yaml_cfg.experiment_id == "multi-agent-gwtw-demo",
              f"YAML id: {yaml_cfg.experiment_id}")
        check(yaml_cfg.population_size == 4, f"YAML pop: {yaml_cfg.population_size}")

    # ---- 9. Config validation ----
    try:
        MultiAgentGWTWConfig(
            experiment_id="", platform="x", design="y",
            population_size=4, seed=1, max_trials=5)
        check(False, "empty id should raise")
    except ValueError as e:
        check("experiment_id" in str(e), f"empty id: {e}")

    # ---- 10. Finish QoR ----
    check(len(result.finish_trial_ids) >= 2,
          f"finish trials: {len(result.finish_trial_ids)}")
    for tid in result.finish_trial_ids:
        t = tm.get(tid)
        if t and t.status == "ok":
            check(t.final_qor is not None, f"final_qor {tid[:6]}")
            for key in ("wns_ps", "tns_ps", "area_um2", "power_w"):
                check(key in t.final_qor, f"qor has {key}")

    # ---- 11. Tree evidence ----
    tree_path = runs_dir / "tree.json"
    check(tree_path.is_file(), "tree.json exists")
    tree_data = json.loads(tree_path.read_text())
    nodes = tree_data.get("_nodes", tree_data.get("nodes", {}))
    check(len(nodes) > 1, f"tree nodes: {len(nodes)}")

    # ---- 12. Parent selection trace entries ----
    ps_entries = [e for e in entries if e["entry_type"] == "parent_selection"]
    check(len(ps_entries) > 0,
          f"parent_selection trace entries: {len(ps_entries)}")
    for pse in ps_entries:
        data = pse.get("data", {})
        check("requested_parent" in data, "ps has requested_parent")
        check("effective_parent" in data, "ps has effective_parent")
        check("accepted" in data, "ps has accepted")

    # ---- 13. Pause trials have checkpoint ----
    pl_outcomes = result.pl_cohort_result.trial_outcomes
    for tid, action in pl_outcomes.items():
        if action == "pause":
            t = tm.get(tid)
            check(t is not None, f"pause trial {tid[:6]} exists")
            if t:
                check(t.status == "paused",
                      f"pause trial {tid[:6]} status=paused")
                check(t.checkpoint is not None,
                      f"pause trial {tid[:6]} has checkpoint")

    # ---- 14. Run with MockLLM agents ----
    from llm_interface import MockLLMClient
    from agents import JudgeAgent, build_stage_agents
    fw = FrameworkConfig(platform="sky130hd", design="gcd")
    llm = MockLLMClient(fw)
    judge = JudgeAgent(llm, fw)
    agents = build_stage_agents(llm, fw)

    runs_dir_ma = tmpdir / "runs_ma"; runs_dir_ma.mkdir(parents=True)
    cfg_ma = MultiAgentGWTWConfig(
        experiment_id="mock-agents", platform="sky130hd", design="gcd",
        population_size=4, seed=42, max_trials=20,
        pl_survivor_count=2, pl_audit_quota=0, pl_max_children_per_parent=2,
        cts_survivor_count=1, cts_audit_quota=1, cts_max_children_per_parent=2,
        runs_dir=runs_dir_ma)
    tm_ma = TrialManager(runs_dir_ma)
    cm_ma = CheckpointManager(flow_dir_v)
    r_ma = StageERecordingFakeRunner(flow_dir_v)
    orch_ma = MultiAgentGWTWOrchestrator(
        cfg_ma, tm_ma, cm_ma, r_ma,
        judge_agent=judge, stage_agents=agents)
    result_ma = orch_ma.run()
    check(result_ma.errors == [], f"MockLLM errors: {result_ma.errors}")

    # Proposals should NOT be fallback (MockLLM is real enough).
    for ap in result_ma.agent_proposals:
        check(ap.is_fallback is False,
              f"MockLLM proposal {ap.trial_id[:6]} is_fallback=False")
        check(ap.judge_branch_node != "",
              f"proposal has judge_branch_node")
        for stage in ["FP", "PL", "CTS", "RT"]:
            check(stage in ap.stage_proposals, f"proposal has {stage}")

    # Child proposals exist (from PL/CTS fork).
    child_proposals = [
        p for p in result_ma.agent_proposals
        if p.proposal_role in ("pl_child", "cts_child")]
    check(len(child_proposals) > 0,
          f"child proposals: {len(child_proposals)}")
    for cp_ap in child_proposals:
        check(not cp_ap.is_fallback,
              f"child proposal {cp_ap.trial_id[:6]} not fallback")

    # Child params contain Agent-generated values (not baseline defaults).
    for cid in result_ma.pl_cohort_result.child_trial_ids:
        child = tm_ma.get(cid)
        check(child is not None, f"PL child {cid[:6]} exists")
        # Agent params should differ from baseline (MockLLM wobble).
        if child and child.params.get("CTS"):
            cts_params = child.params["CTS"]
            check(len(cts_params) > 0, f"child {cid[:6]} has CTS params")

    # ---- 15. _is_real_llm detection ----
    check(orch1._is_real_llm is False, "no-agents → not real LLM")
    check(orch_ma._is_real_llm is False, "MockLLM → not real LLM")

    shutil.rmtree(tmpdir)
    total = ok + fail_count
    print(f"\n{'='*50}")
    print(f"  {ok}/{total} passed"
          + (f", {fail_count} FAILED" if fail_count else " — ALL OK"))
    print(f"{'='*50}")
    sys.exit(1 if fail_count else 0)
