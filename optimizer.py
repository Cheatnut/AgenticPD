# -*- coding: utf-8 -*-
"""
optimizer.py — AgenticPD optimisation main loop (paper sec. 6)

Key changes vs. paper pseudo-code:
- Maintains both the optimisation tree T (OptimizationTree) and the history list H
- Each iteration begins with ObservationTool generating an adaptive summary
- Judge outputs {branch_node, branch_stage, hints}
- Only invokes StageAgents for s in {b} U Aft(b); Bef parameters are inherited from tree ancestors
- Uses copy_parent_results() + per-stage pipeline for incremental downstream re-runs
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

import config
from agents import (JudgeAgent, StageAgent, build_observation_summary,
                    build_stage_agents, default_stage_params)
from config import FrameworkConfig
from optimization_tree import (OptimizationTree, ROOT_ID,
                               load_tree, save_tree_atomic)
from orfs_interface import ORFSRunner, RunResult
from managers import TrialManager
from schemas.trial import TrialRecord, StageResult, FailureClass
from utils import QoR, qor_is_better

log = logging.getLogger("optimizer")


def _aft_stages(stage: str) -> List[str]:
    """Paper sec. 1.2: return stages strictly after *stage*."""
    idx = config.STAGES.index(stage)
    return config.STAGES[idx + 1:]


def _next_stage_of_node(node_stage: str) -> Optional[str]:
    """Map node stage to the re-run start stage when branching (paper sec. 3.2 consistency constraint).

    root -> FP; FP -> PL; PL -> CTS; CTS -> RT; RT -> None (leaf, cannot branch).
    """
    if node_stage == "root":
        return "FP"
    idx = config.STAGES.index(node_stage)
    if idx + 1 < len(config.STAGES):
        return config.STAGES[idx + 1]
    return None


def _downstream_stages(branch_stage: str) -> List[str]:
    """{branch_stage} U Aft(branch_stage) = stages that need LLM param generation + re-run."""
    return [branch_stage] + _aft_stages(branch_stage)


def _load_history_from_trials(run_dir: "Path") -> "List[Dict[str, Any]]":
    """Rebuild the in-memory history list from trials.jsonl.

    Converts each TrialRecord back to the flat dict format expected by
    agents.py, visualize.py, and the Optimizer's internal loops.
    This replaces the old load_history() that read history.json.
    """
    from pathlib import Path
    from managers import TrialManager
    mgr = TrialManager(Path(run_dir))
    trials = mgr.list_all()
    history: List[Dict[str, Any]] = []
    for t in sorted(trials, key=lambda t: t.trial_id):
        entry: Dict[str, Any] = {
            "iteration": len(history),  # reconstruct sequential iteration number
            "status": t.status,
            "variant": "",  # not stored in TrialRecord; acceptable loss for resume
            "params": t.params,
            "qor": t.final_qor,
            "stage_qor": {sr.stage: sr.stage_qor for sr in t.stage_results},
            "failed_stage": t.failed_stage,
            "error": t.error_message,
            "elapsed_s": t.elapsed_s,
            "branch_node": "",
            "branch_stage": t.branch_stage,
            "judge_decision": None,
            "stage_reasons": {},
        }
        history.append(entry)
    return history


def _uid_to_iter(node_id: str) -> int:
    """Extract iteration number from node_id (e.g. "iter3_PL"), with fallback."""
    try:
        return int(node_id.split("_")[0].replace("iter", ""))
    except (ValueError, IndexError):
        return -1


class Optimizer:
    """Optimisation main-loop controller."""

    def __init__(self, cfg: FrameworkConfig, llm: Any, runner: ORFSRunner):
        self.cfg = cfg
        self.runner = runner
        self.judge = JudgeAgent(llm, cfg)
        self.stage_agents = build_stage_agents(llm, cfg)

        self.tree = OptimizationTree()
        self.history: List[Dict[str, Any]] = []
        self.best_idx: Optional[int] = None

        # Stage C6: TrialManager for structured trial recording
        self.trial_mgr = TrialManager(cfg.run_dir if cfg.run_dir else Path("runs"))
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
        # Compute reproducibility hashes
        import hashlib
        config_hash = None
        env_hash = None
        if self.cfg.run_dir:
            snap = self.cfg.run_dir / "config_snapshot.json"
            if snap.is_file():
                config_hash = hashlib.sha256(snap.read_bytes()).hexdigest()[:16]
            env_manifest = self.cfg.flow_dir / "agenticpd" / "environment_manifest.json"
            if env_manifest.is_file():
                env_hash = hashlib.sha256(env_manifest.read_bytes()).hexdigest()[:16]
        trial = self.trial_mgr.create(
            experiment_id="agenticpd-gcd",
            parent_trial_id=parent_trial_id,
            branch_stage=branch_stage,
            config_hash=config_hash,
            env_hash=env_hash,
            iteration=iteration,
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
        # Store full params for reproducibility (even without tree.json)
        if current_params:
            t.params = current_params
        # Compute param_diff: only if there is a parent trial AND params actually changed.
        # Load parent's full params from its TrialRecord, not just inherited Bef-stage params.
        parent_params = None
        if self._parent_trial_id:
            parent_trial = self.trial_mgr.get(self._parent_trial_id)
            if parent_trial and parent_trial.params:
                parent_params = parent_trial.params
        if parent_params and current_params:
            diff = {}
            for stage in config.STAGES:
                old = parent_params.get(stage, {})
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
        """Paper e_s: historical entries where this stage was the branch_stage (most recent *window* entries)."""
        candidates = [e for e in self.history
                      if (e.get("judge_decision") or {}).get("branch_stage") == stage]
        return candidates[-window:]

    def _persist(self) -> None:
        """Atomically persist the optimisation tree.

        History is no longer written separately — it is derived from
        trials.jsonl (via TrialManager).  This eliminates the dual-write
        between history.json and trial.json.
        """
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
        # Branch info (paper sec. 6 line 5 output: n_hat, b_k)
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
        """Append new node chain to tree, increment parent branch_count (unless extending from itself).

        stages_chain: [(stage, variant, params, stage_qor), ...]  downstream stages only.
        """
        # Increment branch count (paper E(n) update)
        if parent_id != ROOT_ID:
            self.tree.increment_branch_count(parent_id)
        return self.tree.add_path(iteration, parent_id, stages_chain)

    # ------------------------------------------------------------------
    def run_baseline(self) -> RunResult:
        """Baseline iteration: full run from root, building the first four tree layers.

        Baseline params are always the same for a given design, so the
        result is cached under ``runs/<platform>_<design>/.baseline/``.
        Subsequent sessions reuse the cached trial and skip the ORFS run.
        """
        stage_params = {s: dict(config.BASELINE_PARAMS.get(s, {}))
                        for s in config.STAGES}
        variant = self.cfg.baseline_variant_name
        cache_dir = self.cfg.run_dir.parent / ".baseline" if self.cfg.run_dir else None

        # ---- try cache first ----
        if cache_dir and cache_dir.is_dir():
            cached_trial = cache_dir / "trial.json"
            if cached_trial.is_file():
                log.info("[OPTIMIZER] Baseline cache hit: %s (skipping ORFS run)", cache_dir)
                data = json.loads(cached_trial.read_text(encoding="utf-8"))
                trial = TrialRecord.from_dict(data)
                self._current_trial = trial
                # Rebuild history entry from cached trial
                qor_dict = trial.final_qor
                sq = {sr.stage: sr.stage_qor for sr in trial.stage_results if sr.stage_qor}
                from utils import QoR as _QoR
                qor = _QoR(**qor_dict) if qor_dict else None
                result = RunResult(ok=True, variant=variant, qor=qor,
                                   stage_qor=sq, elapsed_s=trial.elapsed_s)
                self._record(0, stage_params, result, judge_decision=None)
                if result.ok:
                    chain = [(s, variant, stage_params.get(s, {}), sq.get(s))
                             for s in config.STAGES]
                    self._add_to_tree(0, ROOT_ID, chain)
                self._persist()
                return result

        # ---- cache miss: run ORFS ----
        log.info("========== Iter #0 (Baseline, full run from ROOT) ==========")
        result = self.runner.run_flow(stage_params, variant, 0)
        if result.ok:
            chain = [(s, variant, stage_params.get(s, {}),
                      result.stage_qor.get(s)) for s in config.STAGES]
            self._add_to_tree(0, ROOT_ID, chain)
        self._record(0, stage_params, result, judge_decision=None)
        # Build a TrialRecord for the shared cache only — no session-local
        # iter-0-* directory (the cache is the single source of truth).
        if result.ok and cache_dir:
            from schemas.trial import _new_trial_id
            trial = TrialRecord(
                trial_id=_new_trial_id(),
                experiment_id="agenticpd-gcd",
                status="ok",
                params=stage_params,
                final_qor=result.qor.to_dict() if result.qor else None,
            )
            cache_dir.mkdir(parents=True, exist_ok=True)
            (cache_dir / "trial.json").write_text(
                json.dumps(trial.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8")
            log.info("[OPTIMIZER] Baseline cached to %s", cache_dir)
        if not result.ok:
            log.error("#0 [OPTIMIZER] Baseline failed (%s), continuing without reference",
                      result.error)
        return result

    # ------------------------------------------------------------------
    def run_iteration(self, iteration: int) -> RunResult:
        """Paper sec. 6 lines 4-13: observation summary -> Judge -> branch -> downstream execution."""
        log.info("========== Iter #%d ==========", iteration)

        # 4) Observation summary
        summary = build_observation_summary(
            self.tree, self.history, self._best_qor(),
            self.cfg.max_branch_count)

        # 5) Judge decision
        decision = self.judge.act({
            "summary": summary, "history": self.history, "best": self.best_entry})
        branch_node_id = (decision["branch_node"] or "").strip()
        # Normalise: ROOT / root -> ROOT_ID
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
                        branch_node_id)
            branch_node = self.tree.root
            branch_node_id = ROOT_ID
        parent_variant = branch_node.variant

        # Consistency constraint (paper sec. 3.2): branch_stage must be the stage
        # immediately following the branch_node stage — choosing a node uniquely
        # determines the re-run start point. If Judge output is inconsistent,
        # correct based on branch_node (missing links break tree path integrity).
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

        # Extract Bef-stage params from tree ancestors (inherited, no LLM call)
        inherited_params = self.tree.get_params_chain(branch_node_id)
        # Bef-stage QoR: ancestor chain + the branch origin node itself (it is the
        # last Bef stage — e.g. branching from an FP node to re-run PL,
        # Bef(PL)={FP}=branch origin itself)
        inherited_qor_map: Dict[str, Dict[str, float]] = {}
        bef_nodes = self.tree.ancestors(branch_node_id)
        if branch_node.stage in config.STAGES:
            bef_nodes = bef_nodes + [branch_node]
        for node in bef_nodes:
            if node.stage in config.STAGES and node.stage_qor:
                inherited_qor_map[node.stage] = node.stage_qor

        # Upstream QoR list for Bef stages (initial pipeline input: only branch
        # ancestor QoR; each completed stage appends its real QoR for the next
        # StageAgent to consume)
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

        # Stage C6: begin trial record for this iteration
        self._begin_trial(
            iteration,
            parent_trial_id=self._current_trial.trial_id if self._current_trial else None,
            branch_stage=branch_stage,
            parent_params=inherited_params,
        )

        # 7) Per-stage pipeline: StageAgent calls LLM -> make single stage ->
        #    obtain real QoR -> pass to next StageAgent
        #    (paper sec. 5: ctx_s = Q_k(i)_{i in Bef(s)})
        stage_params = dict(inherited_params)  # Bef inheritance
        stage_reasons: Dict[str, str] = {}
        live_upstream_qor = _build_upstream_qor()  # dynamically growing upstream QoR
        collected_stage_qor: Dict[str, Dict[str, float]] = {}
        failed_stage: Optional[str] = None

        # 7a) Establish variant baseline
        if branch_node_id == ROOT_ID:
            # From root: ensure variant directory is clean (prevents make from
            # skipping stages due to stale crash artifacts)
            self.runner._wipe_variant(new_variant)  # type: ignore[attr-defined]
        else:
            # Branch from intermediate node: copy parent artifacts to new variant
            # (Bef-stage results are now in place)
            self.runner.copy_parent_results(parent_variant, new_variant)

        # 7b) Per stage: LLM -> make -> obtain QoR -> next stage
        for s in downstream:
            # a) StageAgent generates parameters (using accumulated real upstream QoR)
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

        # 8) Final QoR: after all downstream stages succeed, run make finish
        #    to obtain complete metrics (including area/power)
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
            self.history = _load_history_from_trials(self.cfg.run_dir)
            self.tree = load_tree(self.cfg.tree_path)
            self._recompute_best()
            log.info("[OPTIMIZER] --resume: loaded %d history entries, %d tree nodes, best=Iter #%s",
                     len(self.history), self.tree.node_count(),
                     self.best_entry.get("iteration") if self.best_entry else "none")

        try:
            if not self.history:
                self.run_baseline()
            # Next iteration number = max iteration in history + 1.
            # (Cannot use len(history): history may contain non-consecutive
            # iteration numbers from ad-hoc test entries.)
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
