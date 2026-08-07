# -*- coding: utf-8 -*-
"""agents/judge.py — JudgeAgent: selects branch node / stage + hints."""
from __future__ import annotations
import logging
from typing import Any, Dict, List

import config
from agents.base import AgentOutputError, BaseAgent, STAGE_CN
from agents.llm import LLMError
from agents.observation import _format_params_inline, format_history, render_param_table

log = logging.getLogger("agents")

class JudgeAgent(BaseAgent):
    """Judge: analyzes optimization tree + observation summary, selects branch
    node n_hat and branch stage b_k, and generates a dedicated hint for each
    stage in {b_k} ∪ Aft(b_k)."""

    tag = "judge"

    def system_prompt(self) -> str:
        stage_sections = "\n\n".join(
            f"### {stage} — {STAGE_CN[stage]}\n{render_param_table(stage)}"
            for stage in config.STAGES)
        return f"""You are the QoR optimization controller (Judge Agent) for digital backend physical design.
Target: OpenROAD Flow Scripts full RTL→GDS flow, design {self.cfg.design}, PDK {self.cfg.platform}.

The flow consists of four optimizable stages. Tunable parameters per stage:

{stage_sections}

QoR priority (evaluate in order; move to next only if the current is a tie):
1. WNS (Worst Negative Slack, ps, higher is better, >= 0 means timing met;
   differences < {self.cfg.wns_tol_ps} ps are considered a tie)
2. TNS (Total Negative Slack, ps, higher is better;
   differences < {self.cfg.tns_tol_ps} ps are considered a tie)
3. Power (W, lower is better)
4. Area (µm², lower is better)

## Branching Mechanism (your core capability)

The optimization process is organized as a tree: each completed flow stage (FP/PL/CTS/RT)
leaves a snapshot node for that iteration. You may select any historical node n_hat as the
"branch origin", restarting execution from the stage following n_hat. All Bef stages'
results are reused at zero cost; only {{branch_stage}} ∪ Aft(branch_stage) stages are
re-run. Selecting the root node (ROOT) with branch_stage=FP is equivalent to running a
full flow from scratch.

The "Observation Summary" in your input contains two key signals:
- E(n): how many times this node has been chosen as a branch origin.
  Low count → underexplored, worth trying;
  high count with no improvement → possibly avoid this parameter region;
- B(s): stage bottleneck score = global best ws − best historical ws for this stage.
  The stage with the largest positive B(s) is likely the current bottleneck and should
  be prioritized as branch_stage.

## Your Decision Requirements

Analyze the observation summary + historical trends and choose:
- branch_node: pick a node_id from the branchable node table (or "ROOT" to start fresh)
- branch_stage: the stage at which to start re-running, starting from the chosen node.
  **Consistency constraint**: branch_stage MUST be the stage immediately following
  branch_node's stage (ROOT→FP, FP node→PL, PL node→CTS, CTS node→RT) — choosing a
  node uniquely determines the re-run start point. If you want to start re-running from
  CTS, pick a PL node on some path as branch_node. Inconsistent output will be forcibly
  corrected by the system.
- hints: write one dedicated, specific, actionable tuning hint for each stage in
  {{branch_stage}} ∪ Aft(branch_stage) (which direction to tune, roughly how much, why).
  Bef stages automatically reuse the node's results and need no hints.

Decision principles:
- Avoid re-exploring regions that cannot produce improvements (consider E(n) + FAILED
  markers in history);
- When timing is unmet (WNS < 0), prioritize the most bottlenecked stage; when timing is
  met, consider area/power optimization;
- If multiple consecutive rounds show no improvement, switch stages or restart from an
  earlier node (even back to ROOT for a full re-run).

Output requirements: output ONLY one JSON object, no other text."""

    def build_user_prompt(self, context: Dict[str, Any]) -> str:
        summary = context.get("summary", "(No observation summary available)")
        history: List[Dict[str, Any]] = context.get("history", [])
        best = context.get("best")
        best_iter = best.get("iteration") if best else None

        parts = [summary]
        if best:
            from core.utils import QoR
            qor = QoR.from_dict(best.get("qor"))
            parts += [
                "\n## Current Best",
                f"Iteration #{best.get('iteration')}: {qor.pretty() if qor else 'N/A'}",
                f"Params: {_format_params_inline(best.get('params', {}))}",
            ]
        parts += [
            f"\n## Recent Optimization History ({len(history)} rounds total)",
            format_history(history, self.cfg.history_window, best_iter),
            "\nPlease provide the next branch decision. JSON format:",
            self.schema_desc(),
        ]
        return "\n".join(parts)

    def schema_desc(self) -> str:
        stages = "|".join(config.STAGES)
        return (
            '{"branch_node": "<ROOT or a node_id from the branchable node table>", '
            f'"branch_stage": "<one of {stages}>", '
            '"hints": {"FP": "<hint for FP>", '
            '"PL": "<hint for PL>", '
            '"CTS": "<hint for CTS>", '
            '"RT": "<hint for RT>"}, '
            '"reason": "<brief justification>"}'
        )

    def validate(self, raw: Dict[str, Any],
                 context: Dict[str, Any]) -> Dict[str, Any]:
        branch_node = str(raw.get("branch_node", "ROOT")).strip()
        branch_stage = str(raw.get("branch_stage", "")).strip().upper()
        if branch_stage not in config.STAGES:
            raise AgentOutputError(f"Invalid branch_stage: {raw.get('branch_stage')!r}")
        hints_in = raw.get("hints")
        hints: Dict[str, str] = {}
        if isinstance(hints_in, dict):
            for stage in config.STAGES:
                hints[stage] = str(hints_in.get(stage, "")).strip()
        else:
            hints = {s: "" for s in config.STAGES}
        return {
            "branch_node": branch_node,
            "branch_stage": branch_stage,
            "hints": hints,
            "reason": str(raw.get("reason", "")).strip(),
        }

    def act(self, context: Dict[str, Any]) -> Dict[str, Any]:
        try:
            try:
                return super().act(context)
            except AgentOutputError as e:
                log.warning("[Judge Agent] invalid output (%s), retrying once", e)
                raw = self.llm.chat_json(
                    system=self.system_prompt(),
                    user=(self.build_user_prompt(context)
                          + f"\n\nNote: your last output was invalid ({e}), "
                            f"branch_stage must be one of {'/'.join(config.STAGES)}."),
                    schema_desc=self.schema_desc(),
                    tag=self.tag)
                return self.validate(raw, context)
        except (LLMError, AgentOutputError) as e:
            n_done = len(context.get("history", []))
            stage = config.STAGES[n_done % len(config.STAGES)]
            log.error("[Judge Agent] fallback (%s): branch from ROOT, choose %s", e, stage)
            return {
                "branch_node": "ROOT",
                "branch_stage": stage,
                "hints": {s: ("(Judge fault, explore with small steps)" if s == stage else "(fallback)")
                          for s in config.STAGES},
                "reason": f"fallback: {e}",
            }


# =========================================================================
# StageAgent (paper §5): receives upstream QoR + cross-iteration experience
# + Judge hint
# =========================================================================

