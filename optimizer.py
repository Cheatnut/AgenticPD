# -*- coding: utf-8 -*-
"""
optimizer.py — AgenticPD optimization main loop (paper §6 aligned)

Key design (cf. §3.2 / §6 pseudocode):
- Simultaneously maintains optimization tree T (OptimizationTree) and history list H
- Each round: ObservationTool generates adaptive summary → Judge outputs
  {branch_node, branch_stage, hints}
- Only invokes StageAgents for s ∈ {b} ∪ Aft(b); Bef params inherited from tree ancestors
- Uses ORFSRunner per-stage pipeline to incrementally re-run downstream stages
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
from utils import QoR, load_history, qor_is_better, save_history_atomic

log = logging.getLogger("optimizer")


def _aft_stages(stage: str) -> List[str]:
    """Paper §1.2: return stages strictly after `stage`"""
    idx = config.STAGES.index(stage)
    return config.STAGES[idx + 1:]


def _next_stage_of_node(node_stage: str) -> Optional[str]:
    """Node stage → re-run start stage when branching from this node
    (paper §3.2 consistency constraint).

    root → FP; FP → PL; PL → CTS; CTS → RT; RT → None (leaf, cannot branch).
    """
    if node_stage == "root":
        return "FP"
    idx = config.STAGES.index(node_stage)
    if idx + 1 < len(config.STAGES):
        return config.STAGES[idx + 1]
    return None


def _downstream_stages(branch_stage: str) -> List[str]:
    """{branch_stage} ∪ Aft(branch_stage) = stages needing LLM param generation + re-run"""
    return [branch_stage] + _aft_stages(branch_stage)


def _uid_to_iter(node_id: str) -> int:
    """Extract iteration number from node_id (e.g. "iter3_PL"), with tolerance"""
    try:
        return int(node_id.split("_")[0].replace("iter", ""))
    except (ValueError, IndexError):
        return -1


class Optimizer:
    """Optimization main loop controller"""

    def __init__(self, cfg: FrameworkConfig, llm: Any, runner: ORFSRunner):
        self.cfg = cfg
        self.runner = runner
        self.judge = JudgeAgent(llm, cfg)
        self.stage_agents = build_stage_agents(llm, cfg)

        self.tree = OptimizationTree()
        self.history: List[Dict[str, Any]] = []
        self.best_idx: Optional[int] = None

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
    def _cross_exp(self, stage: str, window: int = 5) -> List[Dict[str, Any]]:
        """Paper e_s: history entries where this stage was the branch_stage
        (most recent `window` entries)"""
        candidates = [e for e in self.history
                      if (e.get("judge_decision") or {}).get("branch_stage") == stage]
        return candidates[-window:]

    def _persist(self) -> None:
        """Atomically persist both tree and history at end of each round"""
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
        # Branch info (paper §6 line 5 output: n_hat, b_k)
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
                log.info("#%d [OPTIMIZER] * Global best updated to Iter #%d: %s",
                         iteration, iteration, new_qor.pretty())

    # ------------------------------------------------------------------
    def _add_to_tree(self, iteration: int, parent_id: str,
                     stages_chain: List[tuple]) -> List[str]:
        """Mount a new node chain in the tree, incrementing the parent's
        branch_count (unless extending from the node itself).

        stages_chain: [(stage, variant, params, stage_qor), ...]
        downstream stages only.
        """
        # Branch count increment (paper E(n) update)
        if parent_id != ROOT_ID:
            self.tree.increment_branch_count(parent_id)
        return self.tree.add_path(iteration, parent_id, stages_chain)

    # ------------------------------------------------------------------
    def run_baseline(self) -> RunResult:
        """Baseline iteration: full run from root, building tree's first four layers"""
        log.info("========== Iter #0 (Baseline, full run from ROOT) ==========")
        stage_params = {s: dict(config.BASELINE_PARAMS.get(s, {}))
                        for s in config.STAGES}
        variant = self.cfg.variant_name(0)
        result = self.runner.run_flow(stage_params, variant, 0)

        # Register in tree: root → FP → PL → CTS → RT
        if result.ok:
            chain = [(s, variant, stage_params.get(s, {}),
                      result.stage_qor.get(s)) for s in config.STAGES]
            self._add_to_tree(0, ROOT_ID, chain)

        self._record(0, stage_params, result, judge_decision=None)
        if not result.ok:
            log.error("#0 [OPTIMIZER] Baseline failed (%s), continuing without reference",
                      result.error)
        return result

    # ------------------------------------------------------------------
    def run_iteration(self, iteration: int) -> RunResult:
        """Paper §6 lines 4–13: observation summary → Judge → branch → downstream exec"""
        log.info("========== Iter #%d ==========", iteration)

        # 4) Observation summary
        summary = build_observation_summary(
            self.tree, self.history, self._best_qor(),
            self.cfg.max_branch_count)

        # 5) Judge decision
        decision = self.judge.act({
            "summary": summary, "history": self.history, "best": self.best_entry})
        branch_node_id = (decision["branch_node"] or "").strip()
        # Normalize: ROOT / root → ROOT_ID
        if branch_node_id.upper() == "ROOT":
            branch_node_id = ROOT_ID
        branch_stage = decision["branch_stage"]
        hints = decision["hints"]
        log.info("#%d [Judge Agent] branch_node = %s", iteration, branch_node_id)
        log.info("#%d [Judge Agent] branch_stage = %s", iteration, branch_stage)
        for s, h in hints.items():
            if h:
                log.info("#%d [Judge Agent] @%s Agent: %s", iteration, s, h[:80])

        # 6) Resolve branch node: get ancestor params and QoR (reuse Bef results)
        branch_node = self.tree.find_node(branch_node_id)
        if branch_node is None:
            log.warning("#%d [Judge Agent] branch_node=%s not in tree, fallback to ROOT",
                        iteration, branch_node_id)
            branch_node = self.tree.root
            branch_node_id = ROOT_ID
        parent_variant = branch_node.variant

        # Consistency constraint (paper §3.2): branch_stage must be the stage
        # immediately following branch_node's stage — choosing a node uniquely
        # determines the re-run start point. If Judge output is inconsistent,
        # correct based on branch_node (missing links would break tree path integrity).
        expected_stage = _next_stage_of_node(branch_node.stage)
        if expected_stage is None:
            log.warning("#%d [Judge Agent] branch_node=%s is a leaf (RT), cannot branch, "
                        "fallback to ROOT+FP",
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

        # Extract Bef stage params from tree ancestors (inherited, no LLM call)
        inherited_params = self.tree.get_params_chain(branch_node_id)
        # Bef stage QoR: ancestor chain + branch origin node itself (it is the last
        # Bef stage — e.g. when branching from an FP node to re-run PL,
        # Bef(PL)={FP}=branch origin itself)
        inherited_qor_map: Dict[str, Dict[str, float]] = {}
        bef_nodes = self.tree.ancestors(branch_node_id)
        if branch_node.stage in config.STAGES:
            bef_nodes = bef_nodes + [branch_node]
        for node in bef_nodes:
            if node.stage in config.STAGES and node.stage_qor:
                inherited_qor_map[node.stage] = node.stage_qor

        # Bef stage upstream QoR list (initial input for the pipeline; after each
        # downstream stage completes, its real QoR is appended for the next
        # StageAgent to use)
        def _build_upstream_qor() -> List[dict]:
            result: List[dict] = []
            for stage in config.STAGES:
                if stage not in inherited_qor_map:
                    break  # Bef chain stops at branch origin
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

        # 7) Per-stage pipeline: StageAgent calls LLM → make single stage →
        #    get real QoR → pass to next StageAgent
        #    (paper §5: ctx_s = Q_k(i)_{i∈Bef(s)})
        stage_params = dict(inherited_params)  # Bef inheritance
        stage_reasons: Dict[str, str] = {}
        live_upstream_qor = _build_upstream_qor()  # dynamically growing upstream QoR
        collected_stage_qor: Dict[str, Dict[str, float]] = {}
        failed_stage: Optional[str] = None

        # 7a) Establish variant baseline
        if branch_node_id == ROOT_ID:
            # Starting from root: ensure clean variant directory (prevent make from
            # skipping due to stale artifacts from a previous crash)
            self.runner._wipe_variant(new_variant)  # type: ignore[attr-defined]
        else:
            # Branching from intermediate node: copy parent artifacts to new variant
            # (Bef stage results are now in place)
            self.runner.copy_parent_results(parent_variant, new_variant)

        # 7b) Per-stage: LLM → make → get QoR → next stage
        for s in downstream:
            # a) StageAgent generates params (using live accumulated upstream QoR)
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

            # b) Make only the current stage
            stage_ok, stage_qor_dict = self.runner.run_stage(
                s, stage_params, new_variant, iteration)

            if not stage_ok:
                failed_stage = s
                log.error("#%d [%s Agent] stage %s failed, stopping downstream stages",
                          iteration, s, s)
                break

            collected_stage_qor[s] = stage_qor_dict

            # c) Append real QoR to live_upstream_qor; the next StageAgent sees
            #    the complete upstream including this stage's real values
            ws = tns = None
            for k, v in stage_qor_dict.items():
                if k.endswith("_ws_ps"):
                    ws = v
                elif k.endswith("_tns_ps"):
                    tns = v
            live_upstream_qor.append(
                {"stage": s, "ws_ps": ws, "tns_ps": tns})

        # 8) Final QoR: after all downstream stages succeed, run make finish
        #    to get full metrics (including area/power)
        if failed_stage is not None:
            result = RunResult(
                ok=False, variant=new_variant,
                failed_stage=failed_stage,
                error=f"Stage {failed_stage} make failed",
                stage_qor=collected_stage_qor)
        else:
            result = self.runner.run_finish(stage_params, new_variant, iteration)

        # 9) Register new nodes in tree (downstream stages only)
        if result.ok:
            chain = [(s, new_variant, stage_params.get(s, {}),
                      collected_stage_qor.get(s)) for s in downstream]
            self._add_to_tree(iteration, branch_node_id, chain)

        # 10) Record history
        self._record(iteration, stage_params, result, decision, stage_reasons)
        return result

    # ------------------------------------------------------------------
    def run(self, resume: bool = False) -> None:
        if resume:
            self.history = load_history(self.cfg.history_path)
            self.tree = load_tree(self.cfg.tree_path)
            self._recompute_best()
            log.info("[OPTIMIZER] --resume: loaded %d history entries, %d tree nodes, "
                     "best=Iter #%s",
                     len(self.history), self.tree.node_count(),
                     self.best_entry.get("iteration") if self.best_entry else "none")

        try:
            if not self.history:
                self.run_baseline()
            # Next iteration = max historical iteration + 1 (cannot use len(history):
            # history may contain non-consecutive iteration numbers, e.g. test entries
            # inserted during debugging)
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
