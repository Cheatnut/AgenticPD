# -*- coding: utf-8 -*-
"""
optimizer.py — AgenticPD 优化主循环（论文 §6 对照版）

关键变化（对照 §3.2 / §6 伪代码）：
- 同时维护优化树 T（OptimizationTree）和历史列表 H
- 每轮先调 ObservationTool 生成自适应概要
- Judge 输出 {branch_node, branch_stage, hints}
- 只对 s ∈ {b} ∪ Aft(b) 调阶段智能体；Bef 参数从树祖先继承
- 使用 ORFSRunner.branch_from() 增量重跑下游阶段
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import config
from agents import (JudgeAgent, StageAgent, build_observation_summary,
                    build_stage_agents, default_stage_params)
from config import FrameworkConfig
from optimization_tree import (OptimizationTree, ROOT_ID,
                               load_tree, save_tree_atomic)
from orfs_interface import ORFSRunner, RunResult
from trial_manager import TrialManager
from schemas.trial import TrialRecord, StageResult, FailureClass
from utils import QoR, load_history, qor_is_better, save_history_atomic

log = logging.getLogger("optimizer")


def _aft_stages(stage: str) -> List[str]:
    """论文 1.2 节：return stages strictly after `stage`"""
    idx = config.STAGES.index(stage)
    return config.STAGES[idx + 1:]


def _next_stage_of_node(node_stage: str) -> Optional[str]:
    """节点所在阶段 → 从该节点分支时的重跑起始阶段（论文 §3.2 一致性约束）。

    root → FP；FP → PL；PL → CTS；CTS → RT；RT → None（叶子不可分支）。
    """
    if node_stage == "root":
        return "FP"
    idx = config.STAGES.index(node_stage)
    if idx + 1 < len(config.STAGES):
        return config.STAGES[idx + 1]
    return None


def _downstream_stages(branch_stage: str) -> List[str]:
    """{branch_stage} ∪ Aft(branch_stage) = 需要 LLM 生成参数 + 重跑的阶段"""
    return [branch_stage] + _aft_stages(branch_stage)


def _uid_to_iter(node_id: str) -> int:
    """从 node_id（如 "iter3_PL"）提取迭代号，容错"""
    try:
        return int(node_id.split("_")[0].replace("iter", ""))
    except (ValueError, IndexError):
        return -1


class Optimizer:
    """优化主循环控制器"""

    def __init__(self, cfg: FrameworkConfig, llm: Any, runner: ORFSRunner):
        self.cfg = cfg
        self.runner = runner
        self.judge = JudgeAgent(llm, cfg)
        self.stage_agents = build_stage_agents(llm, cfg)

        self.tree = OptimizationTree()
        self.history: List[Dict[str, Any]] = []
        self.best_idx: Optional[int] = None

        # Stage C6: TrialManager for structured trial recording
        self.trial_mgr = TrialManager(cfg.run_dir.parent if cfg.run_dir else Path("runs"))
        self._current_trial: Optional[TrialRecord] = None
        self._parent_trial_id: Optional[str] = None

    # ------------------------------------------------------------------
    @property
    def best_entry(self) -> Optional[Dict[str, Any]]:
        return self.history[self.best_idx] if self.best_idx is not None else None

    def _best_qor(self) -> Optional[QoR]:
        entry = self.best_entry
        return QoR.from_dict(entry.get("qor")) if entry else None

    def _recompute_best(self) -> None:
        self.best_idx = None
        for idx, entry in enumerate(self.history):
            if entry.get("status") != "ok":
                continue
            qor = QoR.from_dict(entry.get("qor"))
            if qor_is_better(qor, self._best_qor(),
                             self.cfg.wns_tol_ps, self.cfg.tns_tol_ps):
                self.best_idx = idx

    # ------------------------------------------------------------------
    # Stage C6: TrialManager integration helpers
    # ------------------------------------------------------------------

    def _begin_trial(self, iteration: int, parent_trial_id: Optional[str] = None,
                     branch_stage: Optional[str] = None,
                     parent_params: Optional[dict] = None) -> TrialRecord:
        """Create a TrialRecord and persist a 'running' entry before flow start."""
        trial = self.trial_mgr.create(
            experiment_id="agenticpd-gcd",
            parent_trial_id=parent_trial_id,
            branch_stage=branch_stage,
        )
        # Compute param_diff against parent (if available)
        self._current_trial = trial
        self._parent_trial_id = parent_trial_id
        self._parent_params = parent_params
        log.debug("[OPTIMIZER] Trial %s started (parent=%s, branch=%s)",
                  trial.trial_id, parent_trial_id, branch_stage)
        return trial

    def _add_stage_result(self, stage_result: StageResult) -> None:
        """Record a single stage result in the current trial."""
        if self._current_trial is None:
            return
        self._current_trial.stage_results.append(stage_result)

    def _finalize_trial(self, status: str, final_qor: Optional[QoR] = None,
                        failure: Optional[FailureClass] = None,
                        error_message: Optional[str] = None,
                        current_params: Optional[dict] = None) -> None:
        """Persist the completed trial to disk."""
        if self._current_trial is None:
            return
        t = self._current_trial
        t.status = status
        if final_qor:
            t.final_qor = final_qor.to_dict() if hasattr(final_qor, 'to_dict') else final_qor
        if failure:
            t.failure = failure
        if error_message:
            t.error_message = error_message
        # Compute param_diff
        if current_params and self._parent_params:
            diff = {}
            for stage in config.STAGES:
                old = self._parent_params.get(stage, {})
                new = current_params.get(stage, {})
                all_names = set(old.keys()) | set(new.keys())
                for name in sorted(all_names):
                    ov = old.get(name)
                    nv = new.get(name)
                    if ov != nv:
                        diff[name] = {"from": ov, "to": nv}
            if diff:
                t.param_diff = diff
        self.trial_mgr.update(t)
        log.info("[OPTIMIZER] Trial %s finalized: status=%s elapsed=%.1fs",
                 t.trial_id, t.status, t.elapsed_s)

    # ------------------------------------------------------------------
    def _cross_exp(self, stage: str, window: int = 5) -> List[Dict[str, Any]]:
        """论文 e_s：历史中本阶段作为 branch_stage 的条目（最近 window 条）"""
        candidates = [e for e in self.history
                      if (e.get("judge_decision") or {}).get("branch_stage") == stage]
        return candidates[-window:]

    def _persist(self) -> None:
        """每轮结束时同时原子化落盘树与历史"""
        save_history_atomic(self.cfg.history_path, self.history)
        save_tree_atomic(self.cfg.tree_path, self.tree)

    # ------------------------------------------------------------------
    def _record(self, iteration: int, stage_params: Dict[str, Dict[str, Any]],
                result: RunResult,
                judge_decision: Optional[Dict[str, Any]],
                stage_reasons: Optional[Dict[str, str]] = None) -> None:
        entry: Dict[str, Any] = {
            "iteration": iteration,
            "status": "ok" if result.ok else "failed",
            "variant": result.variant,
            "params": stage_params,
            "qor": result.qor.to_dict() if result.qor else None,
            "stage_qor": result.stage_qor,
            "failed_stage": result.failed_stage,
            "error": result.error,
            "elapsed_s": round(result.elapsed_s, 1),
            "judge_decision": judge_decision,
            "stage_reasons": stage_reasons or {},
        }
        # 分支信息（论文 §6 第 5 行的 (n_hat, b_k) 输出）
        if judge_decision:
            entry["branch_node"] = judge_decision.get("branch_node")
            entry["branch_stage"] = judge_decision.get("branch_stage")

        self.history.append(entry)
        self._persist()

        if result.ok:
            new_qor = QoR.from_dict(entry["qor"])
            if qor_is_better(new_qor, self._best_qor(),
                             self.cfg.wns_tol_ps, self.cfg.tns_tol_ps):
                self.best_idx = len(self.history) - 1
                log.info("#%d [OPTIMIZER] ★ Global best updated to Iter #%d: %s", iteration, iteration, new_qor.pretty())

    # ------------------------------------------------------------------
    def _add_to_tree(self, iteration: int, parent_id: str,
                     stages_chain: List[tuple]) -> List[str]:
        """往树中挂载新节点链，并对父节点递增 branch_count（若并非从自身延伸）。

        stages_chain: [(stage, variant, params, stage_qor), ...] 仅含下游阶段。
        """
        # 分支计数递增（论文 E(n) 更新）
        if parent_id != ROOT_ID:
            self.tree.increment_branch_count(parent_id)
        return self.tree.add_path(iteration, parent_id, stages_chain)

    # ------------------------------------------------------------------
    def run_baseline(self) -> RunResult:
        """基线迭代：从根节点全跑，构建树的最初四层节点"""
        log.info("========== Iter #0 (Baseline, full run from ROOT) ==========")
        stage_params = {s: dict(config.BASELINE_PARAMS.get(s, {}))
                        for s in config.STAGES}
        variant = self.cfg.variant_name(0)

        # Stage C6: begin trial record
        self._begin_trial(0)

        result = self.runner.run_flow(stage_params, variant, 0)

        # 在树中登记：root → FP → PL → CTS → RT
        if result.ok:
            chain = [(s, variant, stage_params.get(s, {}),
                      result.stage_qor.get(s)) for s in config.STAGES]
            self._add_to_tree(0, ROOT_ID, chain)

        self._record(0, stage_params, result, judge_decision=None)

        # Stage C6: finalize trial
        self._finalize_trial(
            status="ok" if result.ok else "failed",
            final_qor=result.qor,
            failure=FailureClass.TOOL_CRASH if not result.ok else None,
            error_message=result.error,
            current_params=stage_params,
        )

        if not result.ok:
            log.error("#0 [OPTIMIZER] Baseline failed (%s), continuing without reference",
                      result.error)
        return result

    # ------------------------------------------------------------------
    def run_iteration(self, iteration: int) -> RunResult:
        """论文 §6 第 4–13 行：观测概要 → Judge → 分支 → 下游执行"""
        log.info("========== Iter #%d ==========", iteration)

        # 4) 观测概要
        summary = build_observation_summary(
            self.tree, self.history, self._best_qor(),
            self.cfg.max_branch_count)

        # 5) Judge 决策
        decision = self.judge.act({
            "summary": summary, "history": self.history, "best": self.best_entry})
        branch_node_id = (decision["branch_node"] or "").strip()
        # 规范化：ROOT / root → ROOT_ID
        if branch_node_id.upper() == "ROOT":
            branch_node_id = ROOT_ID
        branch_stage = decision["branch_stage"]
        hints = decision["hints"]
        log.info("#%d [Judge Agent] branch_node = %s", iteration, branch_node_id)
        log.info("#%d [Judge Agent] branch_stage = %s", iteration, branch_stage)
        for s, h in hints.items():
            if h:
                log.info("#%d [Judge Agent] @%s Agent: %s", iteration, s, h[:80])

        # 6) 解析分支节点：获取祖先参数与 QoR（复用 Bef 结果）
        branch_node = self.tree.find_node(branch_node_id)
        if branch_node is None:
            log.warning("#%d [Judge Agent] branch_node=%s not in tree, fallback to ROOT",
                        branch_node_id)
            branch_node = self.tree.root
            branch_node_id = ROOT_ID
        parent_variant = branch_node.variant

        # 一致性约束（论文 §3.2）：branch_stage 必须是 branch_node 所在阶段的
        # 下一阶段——选择节点即唯一决定了重跑起点。若 Judge 输出不一致，
        # 以 branch_node 为准修正（缺链挂载会破坏树的路径完整性）。
        expected_stage = _next_stage_of_node(branch_node.stage)
        if expected_stage is None:
            log.warning("#%d [Judge Agent] branch_node=%s is a leaf (RT), cannot branch, fallback to ROOT+FP",
                        iteration, branch_node_id)
            branch_node = self.tree.root
            branch_node_id = ROOT_ID
            expected_stage = "FP"
            parent_variant = branch_node.variant
        if branch_stage != expected_stage:
            log.warning("#%d [Judge Agent] branch_stage=%s inconsistent with "
                        "branch_node=%s (stage=%s), corrected to %s",
                        iteration,
                        branch_stage, branch_node_id, branch_node.stage,
                        expected_stage)
            branch_stage = expected_stage
            decision["branch_stage"] = branch_stage

        # 从树祖先提取 Bef 阶段参数（继承，不调 LLM）
        inherited_params = self.tree.get_params_chain(branch_node_id)
        # Bef 阶段的 QoR：祖先链 + 分支起点节点自身（它是最后一个 Bef 阶段——
        # 例如从 FP 节点分支重跑 PL 时，Bef(PL)={FP}=分支起点本身）
        inherited_qor_map: Dict[str, Dict[str, float]] = {}
        bef_nodes = self.tree.ancestors(branch_node_id)
        if branch_node.stage in config.STAGES:
            bef_nodes = bef_nodes + [branch_node]
        for node in bef_nodes:
            if node.stage in config.STAGES and node.stage_qor:
                inherited_qor_map[node.stage] = node.stage_qor

        # Bef 阶段的上游 QoR 列表（流水线的初始输入：只含分支祖先的 QoR，
        # 后续每个阶段跑完后追加该阶段的真实 QoR，供下一个 StageAgent 使用）
        def _build_upstream_qor() -> List[dict]:
            result: List[dict] = []
            for stage in config.STAGES:
                if stage not in inherited_qor_map:
                    break  # Bef 链止于分支起点
                sq = inherited_qor_map[stage]
                ws = tns = None
                for k, v in sq.items():
                    if k.endswith("_ws_ps"):
                        ws = v
                    elif k.endswith("_tns_ps"):
                        tns = v
                result.append({"stage": stage, "ws_ps": ws, "tns_ps": tns})
            return result

        new_variant = self.cfg.variant_name(iteration)
        downstream = _downstream_stages(branch_stage)

        # Stage C6: begin trial record for this iteration
        self._begin_trial(
            iteration,
            parent_trial_id=self._current_trial.trial_id if self._current_trial else None,
            branch_stage=branch_stage,
            parent_params=inherited_params,
        )

        # 7) 逐阶段流水线：StageAgent 调 LLM → make 单阶段 → 获取真实 QoR →
        #    传递给下一个 StageAgent（论文 §5 的 ctx_s = Q_k(i)_{i∈Bef(s)}）
        stage_params = dict(inherited_params)  # Bef 继承
        stage_reasons: Dict[str, str] = {}
        live_upstream_qor = _build_upstream_qor()  # 流水线中动态增长的上游 QoR
        collected_stage_qor: Dict[str, Dict[str, float]] = {}
        failed_stage: Optional[str] = None

        # 7a) 建立 variant 基线
        if branch_node_id == ROOT_ID:
            # 从根节点出发：确保 variant 目录干净（防止上次崩溃残留导致 make 跳步）
            self.runner._wipe_variant(new_variant)  # type: ignore[attr-defined]
        else:
            # 从中间节点分支：复制父产物到新 variant（Bef 阶段结果就位）
            self.runner.copy_parent_results(parent_variant, new_variant)

        # 7b) 逐阶段：LLM → make → 获取 QoR → 下一阶段
        for s in downstream:
            # a) StageAgent 生成参数（使用当前积累的真实上游 QoR）
            ctx = {
                "upstream_qor": live_upstream_qor,
                "cross_iteration_exp": self._cross_exp(s),
                "hint": hints.get(s, ""),
                "global_best": self.best_entry,
            }
            out = self.stage_agents[s].act(ctx)
            stage_params[s] = out["params"]
            stage_reasons[s] = out["reason"]
            log.info("#%d [%s Agent] %s", iteration, s, out["reason"][:120])
            log.info("#%d [%s Agent] set %s params...", iteration, s, s)
            for pname, pvalue in out["params"].items():
                log.info("#%d [%s Agent] %s: %s", iteration, s, pname, pvalue)

            # b) Execute single stage via make (returns StageResult with elapsed_s)
            stage_result = self.runner.run_stage(
                s, stage_params, new_variant, iteration)

            # Stage C6: record per-stage result
            self._add_stage_result(stage_result)

            if stage_result.status != "ok":
                failed_stage = s
                log.error("#%d [%s Agent] stage %s failed (%s, %.1fs), stopping downstream",
                         iteration, s, s,
                         stage_result.failure.value if stage_result.failure else "unknown",
                         stage_result.elapsed_s)
                break

            collected_stage_qor[s] = stage_result.stage_qor

            # c) Append real intermediate QoR to live_upstream_qor;
            #    the next StageAgent sees this stage's actual results
            ws = tns = None
            for k, v in stage_result.stage_qor.items():
                if k.endswith("_ws_ps"):
                    ws = v
                elif k.endswith("_tns_ps"):
                    tns = v
            live_upstream_qor.append(
                {"stage": s, "ws_ps": ws, "tns_ps": tns})

        # 8) 最终 QoR：所有下游阶段成功后跑 make finish 获取完整指标（含面积/功耗）
        if failed_stage is not None:
            result = RunResult(
                ok=False, variant=new_variant,
                failed_stage=failed_stage,
                error=f"阶段 {failed_stage} make 失败",
                stage_qor=collected_stage_qor)
        else:
            result = self.runner.run_finish(stage_params, new_variant, iteration)

        # 9) 登记新节点到树（仅下游阶段）
        if result.ok:
            chain = [(s, new_variant, stage_params.get(s, {}),
                      collected_stage_qor.get(s)) for s in downstream]
            self._add_to_tree(iteration, branch_node_id, chain)

        # 10) 记录历史
        self._record(iteration, stage_params, result, decision, stage_reasons)

        # Stage C6: finalize trial record
        self._finalize_trial(
            status="ok" if result.ok else "failed",
            final_qor=result.qor,
            failure=FailureClass.TOOL_CRASH if not result.ok else None,
            error_message=result.error,
            current_params=stage_params,
        )

        return result

    # ------------------------------------------------------------------
    def run(self, resume: bool = False) -> None:
        if resume:
            self.history = load_history(self.cfg.history_path)
            self.tree = load_tree(self.cfg.tree_path)
            self._recompute_best()
            log.info("[OPTIMIZER] --resume: loaded %d history entries, %d tree nodes, best=Iter #%s",
                     len(self.history), self.tree.node_count(),
                     self.best_entry.get("iteration") if self.best_entry else "none")

        try:
            if not self.history:
                self.run_baseline()
            # 下一个迭代号 = 历史中最大迭代号 + 1（不能用 len(history)：
            # 历史中可能存在非连续迭代号，如调试期插入的测试条目）
            start = max((e.get("iteration", -1) for e in self.history),
                        default=-1) + 1
            for i in range(start, self.cfg.iterations + 1):
                self.run_iteration(i)
        except KeyboardInterrupt:
            log.warning("[OPTIMIZER] Ctrl-C: history, tree and best will be preserved")
        finally:
            self.finalize()

    def finalize(self) -> None:
        self._print_summary()
        best = self.best_entry
        if best is None:
            log.warning("[OPTIMIZER] No successful iterations, skipping best export")
            return
        try:
            summary = {
                "best_iteration": best["iteration"],
                "variant": best["variant"],
                "params": best["params"],
                "qor": best["qor"],
                "total_iterations": len(self.history),
                "tree_nodes": self.tree.node_count(),
            }
            self.runner.export_best(best["variant"], summary)
        except (OSError, FileNotFoundError) as e:
            log.error("[OPTIMIZER] Export best failed: %s", e)

    def _print_summary(self) -> None:
        if not self.history:
            return
        log.info("=================== Final Results ===================")
        for idx, entry in enumerate(self.history):
            qor = QoR.from_dict(entry.get("qor"))
            mark = "  *BEST*" if idx == self.best_idx else ""
            if entry.get("status") == "ok" and qor:
                log.info("[OPTIMIZER] #%d %s%s", entry["iteration"], qor.pretty(), mark)
            else:
                log.info("[OPTIMIZER] #%d FAILED@%s", entry["iteration"],
                         entry.get("failed_stage"))
        best = self.best_entry
        if best:
            log.info("[OPTIMIZER] Global best: Iter #%d", best["iteration"])
