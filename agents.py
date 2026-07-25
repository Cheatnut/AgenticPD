# -*- coding: utf-8 -*-
"""
agents.py — AgenticPD 多智能体定义（论文对照版）

对照论文 §4-§6：
1. ObservationTool：E(n) 探索平衡度 + B(s) 阶段瓶颈 → 自适应概要
2. BaseAgent：模板方法 act = 构建 prompt → 调 LLM → 校验
3. JudgeAgent：从树中选择分支节点 n_hat 与分支阶段 b，为 {b}∪Aft(b) 生成 hints
4. StageAgent 及 FP/PL/CTS/RT 子类：接收本分支上游 QoR + 跨迭代经验 + Judge hint
"""

from __future__ import annotations

import abc
import logging
from typing import Any, Dict, List, Optional, Tuple

import config
from config import FrameworkConfig, ParamSpec
from llm_interface import LLMError

log = logging.getLogger("agents")

STAGE_CN: Dict[str, str] = {
    "FP": "布局规划（Floorplan）",
    "PL": "标准单元布局（Placement）",
    "CTS": "时钟树综合（Clock Tree Synthesis）",
    "RT": "布线（Routing）",
}


# =========================================================================
# Observation Tool（论文 §4.2）：树 + 历史 → 自适应概要
# =========================================================================

def compute_exploration_balance(tree, max_branch_count: int) -> Dict[str, int]:
    """E(n)：每个 branchable 节点被选为分支起点的次数"""
    nodes = tree.branchable_nodes(max_branch_count)
    return {n.node_id: n.branch_count for n in nodes}


def compute_stage_bottleneck(history: List[Dict[str, Any]],
                              best_qor) -> Dict[str, float]:
    """B(s)：从历史中各阶段最近的非失败区间时序 ws（ps），与全局最优 ws 的差值。

    差值 = best_ws - stage_best_ws（正值 = 该阶段是瓶颈）。
    若某阶段无数据则返回 0。
    """
    from utils import QoR
    scores: Dict[str, List[float]] = {s: [] for s in config.STAGES}
    for entry in history:
        if entry.get("status") != "ok":
            continue
        sq = entry.get("stage_qor", {})
        for stage in config.STAGES:
            sqs = sq.get(stage, {})
            for key, val in sqs.items():
                if key.endswith("_ws_ps"):
                    scores[stage].append(float(val))
    best_ws = best_qor.wns_ps if best_qor and best_qor.wns_ps is not None else 0.0
    result: Dict[str, float] = {}
    for stage in config.STAGES:
        vals = scores[stage]
        if not vals:
            result[stage] = 0.0
        else:
            result[stage] = round(best_ws - max(vals), 1)
    return result


def build_observation_summary(tree, history: List[Dict[str, Any]],
                               best_qor, max_branch_count: int) -> str:
    """组装 Judge 的"自适应概要"文本块。

    包含：
    - 可分支节点列表（node_id / stage / E(n) / 路径上各阶段 ws）
    - 各阶段瓶颈分 B(s)
    """
    parts = ["## 搜索状态概要（观测工具自动生成）"]

    # 可分支节点表
    nodes = tree.branchable_nodes(max_branch_count)
    if nodes:
        parts += ["\n### 可分支节点（选择后 Bef 结果复用，免重跑）",
                  "| node_id | stage | E(n) | 路径 QoR(ws_ps) |",
                  "|---------|-------|------|-----------------|"]
        for n in nodes:
            path_qor = tree.get_path_qor_summary(n.node_id)
            qor_str = " → ".join(
                f"{s}:{ws:.0f}" if ws is not None else f"{s}:?" for s, ws in path_qor)
            parts.append(f"| {n.node_id} | {n.stage} | {n.branch_count} | {qor_str} |")
    else:
        parts += ["\n（无可分支节点，本轮必须从根节点 ROOT 出发、从 FP 开始全跑）"]

    # 阶段瓶颈分
    bottleneck = compute_stage_bottleneck(history, best_qor)
    parts += ["\n### 阶段瓶颈 B(s)（ws 与全局最优的差值，正值越大 = 该阶段越瓶颈）"]
    for stage in config.STAGES:
        parts.append(f"- {stage}（{STAGE_CN[stage]}）：B={bottleneck[stage]:.1f} ps")
    if best_qor is not None:
        parts.append(f"\n全局最优 ws（参考基线）：{best_qor.wns_ps:.1f} ps")

    return "\n".join(parts)


# =========================================================================
# 历史记录文本序列化（Judge 与 StageAgent 共用）
# =========================================================================

def _format_params_inline(stage_params: Dict[str, Dict[str, Any]]) -> str:
    parts = []
    for stage in config.STAGES:
        params = stage_params.get(stage, {})
        inner = ",".join(f"{k}:{v}" for k, v in params.items())
        parts.append(f"{stage}{{{inner}}}")
    return " ".join(parts)


def _format_entry_line(entry: Dict[str, Any], is_best: bool) -> str:
    """单条历史记录 → 一行文本（含分支信息）"""
    it = entry.get("iteration")
    params_str = _format_params_inline(entry.get("params", {}))

    branch_info = ""
    bn = entry.get("branch_node")
    bs = entry.get("branch_stage")
    if bn and bs:
        # 只显示有意义的片段避免行太长：提取 iter 号与阶段
        short_node = bn.split("_")[-2] + "_" + bn.split("_")[-1] if "_" in bn else bn
        branch_info = f", from {short_node}@{bs}"

    if entry.get("status") == "ok" and entry.get("qor"):
        from utils import QoR
        qor = QoR.from_dict(entry["qor"])
        status_str = "[ok" + branch_info + "]"
        qor_str = qor.pretty() if qor else "QoR=N/A"
    else:
        failed_stage = entry.get("failed_stage") or "unknown"
        status_str = f"[FAILED@{failed_stage}{branch_info}]"
        qor_str = "QoR=N/A"

    line = f"#{it} {status_str} {params_str} | {qor_str}"

    decision = entry.get("judge_decision")
    if decision:
        bs_short = decision.get("branch_stage", "")
        hint = (decision.get("hints", {}).get(bs_short) or
                decision.get("hint") or "")[:50]
        line += f" | judge:{bs_short} \"{hint}\""
    if is_best:
        line += "  *BEST*"
    return line


def format_history(history: List[Dict[str, Any]], window: int,
                   best_iteration: Optional[int]) -> str:
    """序列化优化历史：取最近 window 条，且永远包含最佳条目（标 *BEST*）"""
    if not history:
        return "（暂无历史记录）"

    recent = history[-window:]
    lines: List[str] = []
    recent_iters = {e.get("iteration") for e in recent}
    if best_iteration is not None and best_iteration not in recent_iters:
        best_entry = next((e for e in history
                           if e.get("iteration") == best_iteration), None)
        if best_entry:
            lines.append(_format_entry_line(best_entry, is_best=True))
            lines.append("...")
    for entry in recent:
        lines.append(_format_entry_line(
            entry, is_best=(entry.get("iteration") == best_iteration)))
    return "\n".join(lines)


def render_param_table(stage: str) -> str:
    lines = []
    for spec in config.PARAM_SPACE[stage]:
        rng = (f"{int(spec.vmin)}~{int(spec.vmax)}" if spec.ptype == "int"
               else f"{spec.vmin}~{spec.vmax}")
        base = "基线未设置" if spec.default is None else f"基线={spec.default}"
        lines.append(f"- {spec.name}（{spec.ptype}，范围 {rng}，{base}）：{spec.description}")
    return "\n".join(lines)


def default_stage_params(stage: str) -> Dict[str, Any]:
    params = dict(config.BASELINE_PARAMS.get(stage, {}))
    for spec in config.PARAM_SPACE[stage]:
        if spec.name not in params and spec.default is not None:
            params[spec.name] = spec.default
    return params


# =========================================================================
# 智能体基类
# =========================================================================

class AgentOutputError(Exception):
    """LLM 输出通过 JSON 解析但内容不符合业务要求"""


class BaseAgent(abc.ABC):
    tag: str = "base"

    def __init__(self, llm: Any, cfg: FrameworkConfig):
        self.llm = llm
        self.cfg = cfg

    @abc.abstractmethod
    def system_prompt(self) -> str: ...

    @abc.abstractmethod
    def build_user_prompt(self, context: Dict[str, Any]) -> str: ...

    @abc.abstractmethod
    def schema_desc(self) -> str: ...

    @abc.abstractmethod
    def validate(self, raw: Dict[str, Any],
                 context: Dict[str, Any]) -> Dict[str, Any]: ...

    def act(self, context: Dict[str, Any]) -> Dict[str, Any]:
        system = self.system_prompt()
        user = self.build_user_prompt(context)
        log.debug("[%s] SYSTEM PROMPT:\n%s", self.tag, system)
        log.debug("[%s] USER PROMPT:\n%s", self.tag, user)
        raw = self.llm.chat_json(
            system=system, user=user,
            schema_desc=self.schema_desc(), tag=self.tag)
        return self.validate(raw, context)


# =========================================================================
# JudgeAgent（论文 §4）
# =========================================================================

class JudgeAgent(BaseAgent):
    """法官：分析优化树 + 观测概要，选择分支节点 n_hat 与分支阶段 b_k，
    并为 {{b_k}} ∪ Aft(b_k) 每个阶段生成专属 hint。"""

    tag = "judge"

    def system_prompt(self) -> str:
        stage_sections = "\n\n".join(
            f"### {stage} —— {STAGE_CN[stage]}\n{render_param_table(stage)}"
            for stage in config.STAGES)
        return f"""你是芯片数字后端物理设计的 QoR 优化总控（法官智能体）。
优化对象：OpenROAD Flow Scripts 完整 RTL→GDS 流程，设计 {self.cfg.design}，工艺 {self.cfg.platform}。

流程包含四个可优化阶段，各阶段可调参数如下：

{stage_sections}

QoR 优先级（依次比较，前者打平才看后者）：
1. WNS（最差时序裕量，ps，越大越好，>=0 表示时序收敛；差异 <{self.cfg.wns_tol_ps}ps 视为打平）
2. TNS（总负时序裕量，ps，越大越好；差异 <{self.cfg.tns_tol_ps}ps 视为打平）
3. 功耗（W，越小越好）
4. 面积（um2，越小越好）

## 分支机制（你的核心能力）

优化过程以树结构组织：每个已完成流程的阶段（FP/PL/CTS/RT）为该次迭代留下一个
快照节点。你可以选择任意历史节点 n_hat 作为"分支起点"，从该节点所对应的阶段
开始重新执行，复用此前所有 Bef 阶段的结果（零成本），只重跑 {{branch_stage}} ∪
Aft(branch_stage) 阶段。若选择从根节点（ROOT）分支、branch_stage=FP，则等价于
从头运行全新流程。

输入中的"观测概要"包含两个关键信号：
- E(n)：该节点被选作分支起点的次数。次数低 → 未充分探索，值得尝试；
  次数高但无改善 → 可能要避开该参数域；
- B(s)：阶段瓶颈分 = 全局最优 ws - 该阶段历史最好 ws。正值越大的阶段越可能是
  当前瓶颈，应优先选为 branch_stage。

## 你的决策要求

分析观测概要 + 历史趋势，选择：
- branch_node：从可分支节点表中选一个 node_id（或 "ROOT" 表示从根从头开始）
- branch_stage：从该节点出发开始重跑的起始阶段。**一致性约束**：branch_stage
  必须是 branch_node 所在阶段的下一阶段（ROOT→FP、FP 节点→PL、PL 节点→CTS、
  CTS 节点→RT）——即"选择节点"就唯一决定了重跑起点；若你想从 CTS 开始重跑，
  应选择某条路径上的 PL 节点作为 branch_node。不一致的输出会被系统强制修正。
- hints：为 {{branch_stage}} ∪ Aft(branch_stage) 中每个阶段写一条专属的、具体
  可执行的中文调参提示（往哪个方向调、大约调多少、为什么）。Bef 阶段会自动
  复用该节点的结果，不需要 hints。

决策原则：
- 避免重复探索、无法产生改善的区域（参考 E(n) + history 中的 FAILED 标记）；
- 时序未收敛（WNS<0）时优先瓶颈最大的阶段；时序已收敛可考虑面积/功耗优化；
- 连续多轮无改善应换阶段或从更早节点重启（甚至回 root 全跑）。

输出要求：只输出一个 JSON 对象，不要输出任何其他文字。"""

    def build_user_prompt(self, context: Dict[str, Any]) -> str:
        summary = context.get("summary", "（无可用的观测概要）")
        history: List[Dict[str, Any]] = context.get("history", [])
        best = context.get("best")
        best_iter = best.get("iteration") if best else None

        parts = [summary]
        if best:
            from utils import QoR
            qor = QoR.from_dict(best.get("qor"))
            parts += [
                "\n## 当前最佳",
                f"迭代 #{best.get('iteration')}：{qor.pretty() if qor else 'N/A'}",
                f"参数：{_format_params_inline(best.get('params', {}))}",
            ]
        parts += [
            f"\n## 近期优化历史（共 {len(history)} 轮）",
            format_history(history, self.cfg.history_window, best_iter),
            "\n请给出下一轮分支决策。JSON 格式：",
            self.schema_desc(),
        ]
        return "\n".join(parts)

    def schema_desc(self) -> str:
        stages = "|".join(config.STAGES)
        return (
            '{"branch_node": "<ROOT 或可分支节点表中的 node_id>", '
            f'"branch_stage": "<{stages} 四选一>", '
            '"hints": {"FP": "<给 FP 的中文提示>", '
            '"PL": "<给 PL 的中文提示>", '
            '"CTS": "<给 CTS 的中文提示>", '
            '"RT": "<给 RT 的中文提示>"}, '
            '"reason": "<判断依据，简要>"}'
        )

    def validate(self, raw: Dict[str, Any],
                 context: Dict[str, Any]) -> Dict[str, Any]:
        branch_node = str(raw.get("branch_node", "ROOT")).strip()
        branch_stage = str(raw.get("branch_stage", "")).strip().upper()
        if branch_stage not in config.STAGES:
            raise AgentOutputError(f"非法的 branch_stage：{raw.get('branch_stage')!r}")
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
                          + f"\n\n注意：你上次输出非法（{e}），"
                            f"branch_stage 必须为 {'/'.join(config.STAGES)} 之一。"),
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
                "hints": {s: ("（法官故障，请小步探索）" if s == stage else "（故障兜底）")
                          for s in config.STAGES},
                "reason": f"fallback: {e}",
            }


# =========================================================================
# StageAgent（论文 §5）：接收本分支上游 QoR + 跨迭代经验 + Judge hint
# =========================================================================

class StageAgent(BaseAgent):
    """阶段参数生成智能体。

    只在被选中（s ∈ {b_k} ∪ Aft(b_k)）时被调用。
    context 字段（论文 §5.1）：
        upstream_qor          : 本分支中 Bef(stage) 各阶段的 QoR
                                 [{stage, ws_ps, tns_ps}, ...]
        cross_iteration_exp   : 历史中本阶段作为 branch_stage 时的记录
                                （跨迭代经验 e_s）
        hint                  : Judge 给本阶段的专属提示
        global_best           : 全局最佳条目（参考基线）
    """

    stage: str = ""
    persona: str = ""

    def __init__(self, llm: Any, cfg: FrameworkConfig):
        super().__init__(llm, cfg)
        assert self.stage in config.STAGES, f"非法 stage：{self.stage}"
        self.tag = f"stage:{self.stage}"
        self.specs: List[ParamSpec] = config.PARAM_SPACE[self.stage]

    def system_prompt(self) -> str:
        return f"""你是{self.persona}
你负责 OpenROAD 流程中 {STAGE_CN[self.stage]} 阶段的参数决策。你的输出仅影响本阶段
的参数值（Bef 阶段的结果已从父分支节点继承，不会重跑）。

你管理的可调参数（输出时必须全部给出，且严格落在范围内）：
{render_param_table(self.stage)}

注意：
- 参数值类型必须正确（int 参数不要输出小数）；
- 不要输出任何不在上表中的参数；
- 小步调整通常比大幅跳变更稳妥，但停滞时可适当加大步长。

输出要求：只输出一个 JSON 对象，不要输出任何其他文字。"""

    def build_user_prompt(self, context: Dict[str, Any]) -> str:
        parts: List[str] = []

        # 1) 本分支上游 QoR（论文 ctx_s 的 {Q_k(i)}_{i∈Bef(s)}）
        upstream = context.get("upstream_qor", [])
        if upstream:
            parts += ["## 本分支上游阶段（Bef）已完成的 QoR",
                      "（这些阶段的参数与结果已从父分支节点继承，不会重跑）"]
            for item in upstream:
                if isinstance(item, dict):
                    parts.append(
                        f"- {item.get('stage', '?')}：ws={item.get('ws_ps', '?')}ps "
                        f"tns={item.get('tns_ps', '?')}ps")
                elif isinstance(item, tuple) and len(item) == 2:
                    parts.append(f"- {item[0]}：ws={item[1]}ps")
                else:
                    parts.append(f"- {item}")
        else:
            parts += ["## 本分支上游阶段", "（你是本分支的第一个执行阶段，无上游 QoR）"]

        # 2) 跨迭代经验 e_s（本阶段作为 branch_stage 的历史尝试）
        cross = context.get("cross_iteration_exp", [])
        if cross:
            parts += ["\n## 本阶段作为优化目标的过往尝试（跨迭代经验 e_s）"]
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
            parts += ["\n## 本阶段的过往尝试", "（尚无记录，请从合理保守值开始）"]

        # 3) 全局最佳（参考基线）
        best = context.get("global_best")
        if best:
            from utils import QoR
            qor = QoR.from_dict(best.get("qor"))
            best_my = best.get("params", {}).get(self.stage, {})
            parts += [
                "\n## 全局最佳（参考基线）",
                f"迭代 #{best.get('iteration')}：{qor.pretty() if qor else 'N/A'}",
                f"本阶段在该轮使用的参数：{best_my or '（基线默认值）'}",
            ]

        # 4) Judge hint
        hint = context.get("hint", "")
        parts += ["\n## 法官提示",
                  hint if hint else "（法官未给本阶段提供专属提示，请自行判断）",
                  "请据此调整参数并在 reason 中说明理由。"]

        parts += ["\nJSON 格式：", self.schema_desc()]
        return "\n".join(parts)

    def schema_desc(self) -> str:
        fields = ", ".join(
            f'"{s.name}": <{s.ptype} {s.vmin}~{s.vmax}>' for s in self.specs)
        return '{"params": {' + fields + '}, "reason": "<简要理由>"}'

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
            raise AgentOutputError(f"[{self.tag}] 输出缺少 params 字典")

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
    persona = ("一位资深布局规划（Floorplan）工程师，擅长在芯片面积、"
               "可布线性与时序之间权衡核心利用率与长宽比。")


class PLAgent(StageAgent):
    stage = "PL"
    persona = ("一位资深标准单元布局（Placement）工程师，深刻理解布局密度余量、"
               "单元 padding 对线长、拥塞与时序的影响。")


class CTSAgent(StageAgent):
    stage = "CTS"
    persona = ("一位资深时钟树综合（CTS）工程师，熟悉 sink 聚类规模/直径与时钟 "
               "skew、插入延迟、缓冲器功耗之间的权衡，以及 setup 修复裕量的作用。")


class RTAgent(StageAgent):
    stage = "RT"
    persona = ("一位资深布线（Routing）工程师，熟悉全局布线层容量调整系数与"
               "拥塞消除迭代次数对绕线长度、DRC 收敛与时序的影响。")


def build_stage_agents(llm: Any, cfg: FrameworkConfig) -> Dict[str, StageAgent]:
    return {"FP": FPAgent(llm, cfg), "PL": PLAgent(llm, cfg),
            "CTS": CTSAgent(llm, cfg), "RT": RTAgent(llm, cfg)}
