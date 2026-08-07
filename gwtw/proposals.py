# -*- coding: utf-8 -*-
"""gwtw/proposals.py — Agent proposal and selection result records."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

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

