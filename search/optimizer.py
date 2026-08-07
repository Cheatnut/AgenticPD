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
from agents.judge import JudgeAgent
from agents.stage import build_stage_agents
from config import FrameworkConfig
from storage import TrialManager, CheckpointManager
from search.tree import OptimizationTree, ROOT_ID, load_tree, save_tree_atomic
from orfs.interface import ORFSRunner, RunResult
from core.models import TrialRecord, StageResult, FailureClass
from core.utils import QoR, qor_is_better

log = logging.getLogger("optimizer")


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
        self.trial_mgr = TrialManager(cfg.run_dir if cfg.run_dir else config.RUNS_DIR)
        self._current_trial: Optional[TrialRecord] = None
        self._parent_trial_id: Optional[str] = None

        # CheckpointManager for creating/verifying stage checkpoints
        self.checkpoint_mgr = CheckpointManager(cfg.flow_dir)

    # ------------------------------------------------------------------
    @property
    def _experiment_id(self) -> str:
        """Derive experiment ID from platform and design (never hardcoded)."""
        return f"agenticpd-{self.cfg.platform}-{self.cfg.design}"

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
            env_manifest = config.AGENTICPD_DIR / "environment_manifest.json"
            if env_manifest.is_file():
                env_hash = hashlib.sha256(env_manifest.read_bytes()).hexdigest()[:16]
        trial = self.trial_mgr.create(
            experiment_id=self._experiment_id,
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
                     stages_chain: List[tuple],
                     source_trial_id: Optional[str] = None) -> List[str]:
        """Append new node chain to tree, increment parent branch_count
        (unless extending from itself)."""
        if parent_id != ROOT_ID:
            self.tree.increment_branch_count(parent_id)
        return self.tree.add_path(
            iteration, parent_id, stages_chain,
            source_trial_id=source_trial_id)

    # ------------------------------------------------------------------
    def run_baseline(self) -> RunResult:
        """Baseline iteration: full run from root, building the first four tree layers.

        Baseline params are always the same for a given design, so the
        result is cached under ``runs/<platform>_<design>/.baseline/``.
        Subsequent sessions reuse the cached trial and skip the ORFS run.

        baseline now creates FP/PL/CTS checkpoints so
        downstream iterations can resolve against them.  Both cache-hit
        and cache-miss paths persist the trial to the current session
        and create per-stage checkpoints.
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
                from core.utils import QoR as _QoR
                qor = _QoR(**qor_dict) if qor_dict else None
                result = RunResult(ok=True, variant=variant, qor=qor,
                                   stage_qor=sq, elapsed_s=trial.elapsed_s)
                self._record(0, stage_params, result, judge_decision=None)
                if result.ok:
                    chain = [(s, variant, stage_params.get(s, {}), sq.get(s))
                             for s in config.STAGES]
                    self._add_to_tree(0, ROOT_ID, chain,
                                      source_trial_id=trial.trial_id)
                    # persist trial to current session and
                    # create FP/PL/CTS checkpoints so the resolver can consume them.
                    self._persist_baseline_trial(trial, stage_params, variant)
                self._persist()
                return result

        # ---- cache miss: run ORFS ----
        log.info("========== Iter #0 (Baseline, full run from ROOT) ==========")
        result = self.runner.run_flow(stage_params, variant, 0)
        # Build a TrialRecord for the shared cache — must exist before
        # _add_to_tree so tree nodes can reference source_trial_id.
        baseline_trial_id: Optional[str] = None
        if result.ok:
            from core.models import _new_trial_id
            baseline_trial_id = _new_trial_id()
            trial = TrialRecord(
                trial_id=baseline_trial_id,
                experiment_id=self._experiment_id,
                status="ok",
                params=stage_params,
                final_qor=result.qor.to_dict() if result.qor else None,
            )
            # Cache to .baseline/ for cross-session reuse
            if cache_dir:
                cache_dir.mkdir(parents=True, exist_ok=True)
                (cache_dir / "trial.json").write_text(
                    json.dumps(trial.to_dict(), ensure_ascii=False, indent=2),
                    encoding="utf-8")
                log.info("[OPTIMIZER] Baseline cached to %s", cache_dir)
            chain = [(s, variant, stage_params.get(s, {}),
                      result.stage_qor.get(s)) for s in config.STAGES]
            self._add_to_tree(0, ROOT_ID, chain,
                              source_trial_id=baseline_trial_id)
            # persist trial to current session and create
            # FP/PL/CTS checkpoints.
            self._persist_baseline_trial(trial, stage_params, variant)
        self._record(0, stage_params, result, judge_decision=None)
        if not result.ok:
            log.error("#0 [OPTIMIZER] Baseline failed (%s), continuing without reference",
                      result.error)
        return result

    def _persist_baseline_trial(self, trial: TrialRecord,
                                 stage_params: Dict[str, Dict[str, Any]],
                                 variant: str) -> None:
        """Persist a baseline trial to the current session and create
        FP/PL/CTS checkpoints.

        The trial gets an artifact_dir under the session runs_dir so
        TrialManager.get() and CheckpointManager.load() can find it.
        Three per-stage checkpoints reference the persistent baseline
        variant artifacts (``agenticpd_baseline``), which are never wiped.
        """
        # Set artifact_dir so checkpoints have a home in the current session
        trial.artifact_dir = f"iter-0-{trial.trial_id}"
        self._current_trial = trial

        # Persist trial to session (enables TrialManager.get for resolver)
        self.trial_mgr._write_trial(trial)
        self.trial_mgr._append_index(trial)

        # Create FP/PL/CTS checkpoints against the baseline variant artifacts
        ph = CheckpointManager.param_hash(stage_params)
        cp_stages = []
        for stage in ("FP", "PL", "CTS"):
            try:
                cp = self.checkpoint_mgr.create(
                    trial=trial,
                    stage=stage,
                    platform=self.cfg.platform,
                    design=self.cfg.design,
                    variant=variant,
                    param_hash=ph,
                    runs_dir=self.cfg.run_dir,
                )
                cp_stages.append(stage)
                log.info("[OPTIMIZER] Baseline checkpoint %s created @%s",
                         cp.checkpoint_id, stage)
            except Exception as e:
                log.warning("[OPTIMIZER] Baseline checkpoint @%s creation failed "
                           "(non-fatal): %s", stage, e)
        if cp_stages:
            log.info("[OPTIMIZER] Baseline checkpoints created for: %s",
                     ", ".join(cp_stages))

    # ------------------------------------------------------------------
    def run_iteration(self, iteration: int) -> RunResult:
        """Run one search iteration (delegated to search/stage_pipeline.py)."""
        from search.stage_pipeline import run_iteration as _run_iteration
        return _run_iteration(self, iteration)


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
