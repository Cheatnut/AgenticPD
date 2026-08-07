# -*- coding: utf-8 -*-
"""agents/stage.py — StageAgent and FP/PL/CTS/RT parameter agents."""
from __future__ import annotations
import logging
from typing import Any, Dict, List

import config
from config import FrameworkConfig, ParamSpec
from agents.base import AgentOutputError, BaseAgent, STAGE_CN
from agents.llm import LLMError
from agents.observation import default_stage_params, render_param_table

log = logging.getLogger("agents")

class StageAgent(BaseAgent):
    """Stage parameter generation agent.

    Only invoked when selected (s ∈ {b_k} ∪ Aft(b_k)).
    Context fields (paper §5.1):
        upstream_qor          : QoR of Bef(stage) stages in current branch
                                [{stage, ws_ps, tns_ps}, ...]
        cross_iteration_exp   : historical records where this stage was
                                the branch_stage (cross-iteration experience e_s)
        hint                  : Judge's dedicated hint for this stage
        global_best           : global best entry (reference baseline)
    """

    stage: str = ""
    persona: str = ""

    def __init__(self, llm: Any, cfg: FrameworkConfig):
        super().__init__(llm, cfg)
        assert self.stage in config.STAGES, f"Invalid stage: {self.stage}"
        self.tag = f"stage:{self.stage}"
        self.specs: List[ParamSpec] = config.PARAM_SPACE[self.stage]

    def system_prompt(self) -> str:
        return f"""You are {self.persona}
You are responsible for parameter decisions in the {STAGE_CN[self.stage]} stage of the
OpenROAD flow. Your output only affects this stage's parameter values (Bef stage results
are inherited from the parent branch node and will not be re-run).

Tunable parameters you manage (output ALL of them, strictly within range):
{render_param_table(self.stage)}

Notes:
- Parameter value types must be correct (int params should not have decimals);
- Do not output any parameter not listed above;
- Small-step adjustments are usually safer than large jumps, but you may increase
  step size when stuck.

Output requirements: output ONLY one JSON object, no other text."""

    def build_user_prompt(self, context: Dict[str, Any]) -> str:
        parts: List[str] = []

        # 1) Upstream QoR in this branch (paper ctx_s: {Q_k(i)}_{i∈Bef(s)})
        upstream = context.get("upstream_qor", [])
        if upstream:
            parts += ["## Completed Upstream (Bef) Stage QoR in This Branch",
                      "(These stages' params and results are inherited from the parent "
                      "branch node and will NOT be re-run)"]
            for item in upstream:
                if isinstance(item, dict):
                    parts.append(
                        f"- {item.get('stage', '?')}: ws={item.get('ws_ps', '?')}ps "
                        f"tns={item.get('tns_ps', '?')}ps")
                elif isinstance(item, tuple) and len(item) == 2:
                    parts.append(f"- {item[0]}: ws={item[1]}ps")
                else:
                    parts.append(f"- {item}")
        else:
            parts += ["## Upstream Stages in This Branch",
                      "(You are the first stage to execute in this branch; no upstream QoR)"]

        # 2) Cross-iteration experience e_s (past attempts targeting this stage)
        cross = context.get("cross_iteration_exp", [])
        if cross:
            parts += ["\n## Past Attempts Targeting This Stage (cross-iteration exp e_s)"]
            for entry in cross:
                it = entry.get("iteration", "?")
                params = entry.get("params", {}).get(self.stage, {})
                sq = (entry.get("stage_qor") or {}).get(self.stage, {})
                ws_str = "N/A"
                for k, v in sq.items():
                    if k.endswith("_ws_ps"):
                        ws_str = f"{v:.1f}ps"
                        break
                parts.append(f"  #{it} params={params} ws={ws_str}")
        else:
            parts += ["\n## Past Attempts for This Stage",
                      "(No records yet — start from reasonable conservative values)"]

        # 3) Global best (reference baseline)
        best = context.get("global_best")
        if best:
            from core.utils import QoR
            qor = QoR.from_dict(best.get("qor"))
            best_my = best.get("params", {}).get(self.stage, {})
            parts += [
                "\n## Global Best (reference baseline)",
                f"Iteration #{best.get('iteration')}: {qor.pretty() if qor else 'N/A'}",
                f"This stage's params in that iteration: {best_my or '(baseline defaults)'}",
            ]

        # 4) Judge hint
        hint = context.get("hint", "")
        parts += ["\n## Judge Hint",
                  hint if hint else "(No dedicated hint from Judge for this stage — "
                  "use your own judgment)",
                  "Please adjust parameters accordingly and explain reasoning in 'reason'."]

        parts += ["\nJSON format:", self.schema_desc()]
        return "\n".join(parts)

    def schema_desc(self) -> str:
        fields = ", ".join(
            f'"{s.name}": <{s.ptype} {s.vmin}–{s.vmax}>' for s in self.specs)
        return '{"params": {' + fields + '}, "reason": "<brief justification>"}'

    def _fallback_params(self, context: Dict[str, Any]) -> Dict[str, Any]:
        best = context.get("global_best")
        if best:
            best_my = best.get("params", {}).get(self.stage)
            if best_my:
                return dict(best_my)
        return default_stage_params(self.stage)

    def validate(self, raw: Dict[str, Any],
                 context: Dict[str, Any]) -> Dict[str, Any]:
        params_in = raw.get("params")
        if not isinstance(params_in, dict):
            raise AgentOutputError(f"[{self.tag}] output missing 'params' dict")

        known_names = {s.name for s in self.specs}
        unknown = set(params_in) - known_names
        if unknown:
            log.warning("[%s] discarding unknown params: %s", self.tag, sorted(unknown))

        fallback = self._fallback_params(context)
        out: Dict[str, Any] = {}
        for spec in self.specs:
            if spec.name in params_in:
                try:
                    out[spec.name] = spec.cast(params_in[spec.name])
                    continue
                except (TypeError, ValueError):
                    log.warning("[%s] param %s=%r cannot convert, using fallback",
                                self.tag, spec.name, params_in[spec.name])
            if spec.name in fallback:
                out[spec.name] = fallback[spec.name]
        return {"params": out, "reason": str(raw.get("reason", "")).strip()}

    def act(self, context: Dict[str, Any]) -> Dict[str, Any]:
        try:
            return super().act(context)
        except (LLMError, AgentOutputError) as e:
            params = self._fallback_params(context)
            log.error("[%s] fallback (%s): reuse params %s", self.tag, e, params)
            return {"params": params, "reason": f"fallback: {e}"}


class FPAgent(StageAgent):
    stage = "FP"
    persona = ("A senior floorplan engineer skilled in trading off core utilization "
               "and aspect ratio among chip area, routability, and timing.")


class PLAgent(StageAgent):
    stage = "PL"
    persona = ("A senior standard cell placement engineer with deep understanding of "
               "how placement density margin and cell padding affect wirelength, "
               "congestion, and timing.")


class CTSAgent(StageAgent):
    stage = "CTS"
    persona = ("A senior clock tree synthesis (CTS) engineer, familiar with the "
               "trade-offs between sink cluster size/diameter and clock skew, "
               "insertion delay, buffer power, and the role of setup repair margin.")


class RTAgent(StageAgent):
    stage = "RT"
    persona = ("A senior routing engineer, familiar with how global routing layer "
               "capacity adjustment factor and congestion elimination iterations "
               "affect wirelength, DRC convergence, and timing.")


def build_stage_agents(llm: Any, cfg: FrameworkConfig) -> Dict[str, StageAgent]:
    return {"FP": FPAgent(llm, cfg), "PL": PLAgent(llm, cfg),
            "CTS": CTSAgent(llm, cfg), "RT": RTAgent(llm, cfg)}
