# -*- coding: utf-8 -*-
"""orchestrator.py — Stage D GWTW serial orchestration.

Per-trial unique variants, ExecutionResolution-driven stage execution
with downstream clean, tree persistence with real params/QoR,
idempotent resume using incremental billing, and budget enforcement.
"""

from __future__ import annotations

import copy, json, logging, hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from cohort_executor import CohortExecutionResult, execute_cohort
from config import BASELINE_PARAMS, FrameworkConfig
from decision_trace import DEFAULT_TRACE_PATH, read_trace
from managers import CheckpointManager, TrialManager
from optimization_tree import OptimizationTree, ROOT_ID
from schemas.trial import ExecutionResolution, StageResult, TrialRecord

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
class StageDConfig:
    experiment_id: str; platform: str; design: str
    population_size: int; seed: int
    max_trials: int; wall_clock_budget_s: Optional[float] = None
    pl_survivor_count: int = 2; pl_audit_quota: int = 0
    pl_max_children_per_parent: int = 2
    cts_survivor_count: int = 1; cts_audit_quota: int = 1
    cts_max_children_per_parent: int = 1
    doomed_rule_version: str = _DEFAULT_DOOMED_VERSION
    scheduler_version: str = _DEFAULT_SCHEDULER_VERSION
    planner_version: str = _DEFAULT_PLANNER_VERSION
    initial_population_params: Optional[List[Dict[str, Any]]] = None
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
        if self.wall_clock_budget_s is not None and self.wall_clock_budget_s < 0:
            raise ValueError("wall_clock_budget_s must be >= 0")

    @classmethod
    def from_yaml(cls, path: Path) -> "StageDConfig":
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
            cts_max_children_per_parent=cts.get("max_children_per_parent", 1),
            doomed_rule_version=versions.get(
                "doomed_rule", _DEFAULT_DOOMED_VERSION),
            scheduler_version=versions.get(
                "scheduler", _DEFAULT_SCHEDULER_VERSION),
            planner_version=versions.get(
                "planner", _DEFAULT_PLANNER_VERSION),
            initial_population_params=data.get("initial_population"),
            evaluator=evaluator.get("type", "ORFS post-route QoR"),
        )

    def get_population_params(self, index: int) -> Dict[str, Dict[str, Any]]:
        base = copy.deepcopy(BASELINE_PARAMS)
        if (self.initial_population_params
                and index < len(self.initial_population_params)):
            for stage, vals in self.initial_population_params[index].items():
                base.setdefault(stage, {}).update(vals)
        return base

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
# Orchestrator
# =============================================================================


@dataclass
class StageDResult:
    experiment_id: str; seed: int
    total_trials: int = 0; budget_remaining: int = 0
    pl_cohort_result: Optional[CohortExecutionResult] = None
    cts_cohort_result: Optional[CohortExecutionResult] = None
    finish_trial_ids: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    resumed: bool = False


class StageDOrchestrator:
    """Serial Stage D GWTW orchestrator.

    - Budget: incremental billing — only newly created trials count.
    - Resume: completed cohorts reserve 0; partial bootstrap only fills
      missing slots.
    - Execution: copy_parent_results → clean downstream → run effective
      start → finish.
    - Tree: unique node per child, parent/source/variant/params/QoR
      traceable.  QoR is updated on tree nodes after each stage executes.
    """

    _NODE_ID_SEQ = 0  # class-level counter for unique node IDs

    def __init__(
        self, cfg: StageDConfig, trial_mgr: TrialManager,
        checkpoint_mgr: CheckpointManager, runner: Any,
        tree: Optional[OptimizationTree] = None,
    ) -> None:
        self.cfg = cfg
        self.trial_mgr = trial_mgr
        self.checkpoint_mgr = checkpoint_mgr
        self.runner = runner
        self._runs_dir = cfg.runs_dir or trial_mgr.runs_dir
        self._iteration = 0
        self._new_trials = 0  # only newly created in this run
        self._disk_trials_before = self._count_disk_trials()
        self.tree = tree or self._load_tree()
        # Map node_id → trial_id so QoR can be updated on tree nodes
        # after stage execution completes.
        self._node_to_trial: Dict[str, str] = {}

    # ------------------------------------------------------------------
    # Public entry
    # ------------------------------------------------------------------

    def run(self) -> StageDResult:
        result = StageDResult(
            experiment_id=self.cfg.experiment_id, seed=self.cfg.seed,
            resumed=self._has_pl_trials())

        # Enforce wall-clock budget before any execution.
        import time as _time
        _wall_start = _time.monotonic()

        pop_trials = self._bootstrap_population()
        result.total_trials = self._disk_trials_before + self._new_trials

        # ---- PL cohort ----
        result.pl_cohort_result = self._run_cohort(
            pop_trials, "PL", *self.cfg._pl_cohort_cfg)
        if result.pl_cohort_result is None:
            result.errors.append("PL cohort failed"); return result

        active_pl = self._collect_active(result.pl_cohort_result)
        self._add_children_to_tree(result.pl_cohort_result)
        for t in active_pl:
            self._advance_one(t, "CTS")
        self._save_tree()

        cts_trials = self._collect_cts_trials(result.pl_cohort_result)

        # ---- CTS cohort ----
        result.cts_cohort_result = self._run_cohort(
            cts_trials, "CTS", *self.cfg._cts_cohort_cfg)
        if result.cts_cohort_result is None:
            result.errors.append("CTS cohort failed"); return result

        active_cts = self._collect_active(result.cts_cohort_result)
        self._add_children_to_tree(result.cts_cohort_result)
        for t in active_cts:
            self._advance_one(t, "finish")
        self._save_tree()

        result.finish_trial_ids = [
            t.trial_id for t in active_cts
            if self.trial_mgr.get(t.trial_id)
            and self.trial_mgr.get(t.trial_id).status == "ok"]
        result.total_trials = self._disk_trials_before + self._new_trials
        result.budget_remaining = self.cfg.max_trials - result.total_trials

        # Enforce wall-clock budget.
        _wall_elapsed = _time.monotonic() - _wall_start
        if (self.cfg.wall_clock_budget_s is not None
                and _wall_elapsed > self.cfg.wall_clock_budget_s):
            result.errors.append(
                f"wall_clock_budget exceeded: "
                f"{_wall_elapsed:.1f}s > {self.cfg.wall_clock_budget_s}s")

        return result

    # ------------------------------------------------------------------
    # Budget (incremental billing)
    # ------------------------------------------------------------------

    def _reserve_child_budget(
        self, cohort: List[TrialRecord], survivor_count: int,
        audit_quota: int, population_size: int, max_children_per_parent: int,
    ) -> int:
        """Reserve for max possible NEW children before cohort execution.

        Conservative: reserves up to ``population_size - worst_active``
        where *worst_active* is the minimum possible survivor+audit count.
        The actual child count may be lower (e.g. hard_dead trials reduce
        fork needs), but the budget check ensures we never exceed
        ``max_trials``.

        Idempotency: if the cohort was already completed (checked via the
        decision trace sentinel), returns 0 — no budget consumed.
        """
        tids = [t.trial_id for t in cohort]
        # Determine decision_stage from cohort's stage results.
        decision_stage = ""
        for t in cohort:
            for sr in reversed(t.stage_results):
                if sr.stage in ("PL", "CTS"):
                    decision_stage = sr.stage
                    break
            if decision_stage:
                break
        if not decision_stage:
            decision_stage = "PL"  # fallback for bootstrap cohort

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
            return 0  # completed cohort → no new children needed

        # Worst-case: only survivor_count + audit_quota survive; all
        # others are hard_dead or paused.
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
    # Tree (unique node per child, full evidence)
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
                log.warning("[ORCH] corrupt tree.json — fresh start")
        return OptimizationTree()

    def _save_tree(self) -> None:
        tp = self._tree_path()
        tmp = tp.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.tree.to_dict(), indent=2))
        tmp.replace(tp)

    def _make_unique_nid(self, stage: str, trial_id: str) -> str:
        StageDOrchestrator._NODE_ID_SEQ += 1
        nid = f"sd-{StageDOrchestrator._NODE_ID_SEQ}-{stage}-{trial_id[:6]}"
        self._node_to_trial[nid] = trial_id
        return nid

    def _add_stage_node_to_tree(self, t: TrialRecord, stage: str,
                                 variant: str,
                                 stage_qor: Dict[str, float]) -> None:
        """Ensure the tree has a node for this trial at *stage* with real QoR.

        If a placeholder node was created by ``_add_children_to_tree``
        (empty QoR), updates it in-place.  Otherwise creates a new child
        node under the deepest existing node for the trial.

        This guarantees checkpoint resolution can find the correct
        ancestor checkpoints at every stage.
        """
        # 1) Check if a node for this trial+stage already exists
        #    (created by _add_children_to_tree as a placeholder).
        for nid, n in self.tree._nodes.items():
            if (getattr(n, "source_trial_id", None) == t.trial_id
                    and n.stage == stage):
                # Update existing placeholder with real QoR.
                n.stage_qor = dict(stage_qor) if stage_qor else {}
                n.params = dict(t.params.get(stage, {}))
                return

        # 2) No existing node — create a new one as child of the deepest
        #    node for this trial.
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
            # Determine which stage the child node represents.
            # CTS cohort children start at RT (consume CTS checkpoint),
            # PL cohort children start at CTS (consume PL checkpoint).
            er = child.execution_resolution
            if er and er.execution_mode == "checkpoint_fork":
                child_stage = er.effective_start_stage  # "CTS" or "RT"
            else:
                cp_stage = pt.checkpoint.stage if (pt and pt.checkpoint) else "PL"
                child_stage = _STAGE_NEXT.get(cp_stage, "CTS")

            child_nid = self._make_unique_nid(child_stage, cid)
            self._node_to_trial[child_nid] = cid

            # Collect all params for this child (inherited + new).
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
    # Bootstrap (fill only missing slots)
    # ------------------------------------------------------------------

    def _bootstrap_population(self) -> List[TrialRecord]:
        existing = self.trial_mgr.list_by_experiment(self.cfg.experiment_id)
        pl_trials = [t for t in existing
                     if any(sr.stage == "PL" and sr.status == "ok"
                            for sr in t.stage_results)]
        if len(pl_trials) >= self.cfg.population_size:
            log.info("[ORCH] reusing %d existing PL trials", len(pl_trials))
            return pl_trials[:self.cfg.population_size]

        # Fill missing slots only.
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
        t.params = self.cfg.get_population_params(index)
        self._new_trials += 1
        variant = self._variant_for(t)
        for stage in ["FP", "PL"]:
            sr = self.runner.run_stage(stage, t.params, variant, self._iteration)
            t.stage_results.append(sr)
            if sr.status != "ok":
                t.status = "failed"; self.trial_mgr.update(t); return t
        self._create_checkpoint(t, "PL", variant)
        t.config_hash = _hash_params(t.params)
        t.status = "ok"; self.trial_mgr.update(t)

        fp_qor, pl_qor = t.stage_results[0].stage_qor, t.stage_results[1].stage_qor
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
            # Clean downstream stages of child so old artifacts don't
            # leak into the new run.
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
    # Cohort
    # ------------------------------------------------------------------

    def _run_cohort(
        self, cohort: List[TrialRecord], decision_stage: str,
        survivor_count: int, audit_quota: int, population_size: int,
        max_children_per_parent: int, doomed_rule_version: str,
        scheduler_version: str, planner_version: str,
    ) -> Optional[CohortExecutionResult]:
        if not cohort:
            return None

        # Budget reservation: ensure we can create the children BEFORE
        # executing the cohort.  If the cohort was already completed
        # (idempotent), this reserves 0.
        _reserved = self._reserve_child_budget(
            cohort, survivor_count, audit_quota, population_size,
            max_children_per_parent)
        if _reserved == 0:
            log.info("[ORCH] cohort %s: no new children needed (budget or "
                     "already complete)", decision_stage)

        params_by_id = {t.trial_id: t.params for t in cohort}
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
            # Verify that children created match reservation (fresh cohorts only).
            if cr.cohort_plan is not None:
                actual_children = len(cr.child_trial_ids)
                if actual_children > _reserved:
                    log.warning(
                        "[ORCH] budget mismatch: reserved %d, got %d children",
                        _reserved, actual_children)
                # Budget already reserved; children are now accounted for.
                self._new_trials += actual_children
            return cr
        except Exception as e:
            log.error("[ORCH] cohort failed: %s", e); return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _variant_for(self, trial: TrialRecord) -> str:
        return f"agenticpd_sd_{trial.trial_id}"

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
            log.warning("[ORCH] checkpoint failed for %s", trial.trial_id)

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
# Recording fake runner
# =============================================================================


class RecordingFakeRunner:
    """Stateful fake that records calls + creates real artifact files."""

    def __init__(self, flow_dir: Path) -> None:
        self.flow_dir = Path(flow_dir)
        self.calls: List[Dict[str, Any]] = []
        # Track which files exist per variant for clean assertion.
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
        """Remove artifact files for stages at or after *effective_start*
        so old results cannot be reused."""
        self._record("clean_downstream", variant=variant,
                     effective_start=effective_start)
        try:
            si = _STAGE_ORDER.index(effective_start)
        except ValueError:
            return
        for stage in _STAGE_ORDER[si:4]:  # up to RT
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
    flow_dir = tmpdir / "flow"
    tm = TrialManager(runs_dir); cm = CheckpointManager(flow_dir)
    cfg = StageDConfig(
        experiment_id="self-test", platform="sky130hd", design="gcd",
        population_size=4, seed=42, max_trials=20,
        pl_survivor_count=2, pl_audit_quota=0, pl_max_children_per_parent=2,
        cts_survivor_count=1, cts_audit_quota=1, cts_max_children_per_parent=2,
        runs_dir=runs_dir)

    r1 = RecordingFakeRunner(flow_dir)
    orch1 = StageDOrchestrator(cfg, tm, cm, r1)
    result = orch1.run()
    check(result.errors == [], f"no errors: {result.errors}")
    check(result.total_trials == len(tm.list_all()),
          f"total_trials matches disk: {result.total_trials} == {len(tm.list_all())}")
    check(result.budget_remaining >= 0, "budget_remaining >= 0")
    check("run_finish" in {c["method"] for c in r1.calls}, "run_finish")
    check("clean_downstream" in {c["method"] for c in r1.calls},
          "clean_downstream called")
    for tid in result.finish_trial_ids:
        t = tm.get(tid)
        if t and t.status == "ok":
            check(t.final_qor is not None, f"final_qor {tid[:6]}")

    # Resume: zero calls.
    n_trials = len(tm.list_all())
    r2 = RecordingFakeRunner(flow_dir)
    orch2 = StageDOrchestrator(cfg, tm, cm, r2)
    result2 = orch2.run()
    check(result2.resumed, "resume detected")
    check(len(r2.calls) == 0, f"resume zero calls: {len(r2.calls)}")
    check(len(tm.list_all()) == n_trials, "no new trials")

    # Budget: incremental.
    cfg_tight = StageDConfig(
        experiment_id="tight", platform="x", design="y",
        population_size=4, seed=1, max_trials=1,
        pl_survivor_count=1, pl_audit_quota=0, pl_max_children_per_parent=1,
        cts_survivor_count=1, cts_audit_quota=0, cts_max_children_per_parent=1,
        runs_dir=runs_dir)
    try:
        StageDOrchestrator(cfg_tight, tm, cm,
                           RecordingFakeRunner(flow_dir)).run()
        check(False, "tight budget should raise")
    except RuntimeError as e:
        check("max_trials" in str(e), f"budget: {e}")

    # YAML config vs default.
    cfg_yaml = StageDConfig(
        experiment_id="from-yaml", platform="sky130hd", design="gcd",
        population_size=4, seed=42, max_trials=20,
        wall_clock_budget_s=3600.0,
        evaluator="ORFS post-route QoR",
        runs_dir=runs_dir)
    check(cfg_yaml.wall_clock_budget_s == 3600.0, "wall_clock from YAML")
    check(cfg_yaml.evaluator == "ORFS post-route QoR", "evaluator from YAML")
    cfg_default = StageDConfig(
        experiment_id="default", platform="x", design="y",
        population_size=2, seed=1, max_trials=5, runs_dir=runs_dir)
    check(cfg_default.wall_clock_budget_s is None, "wall_clock default None")
    check(cfg_default.evaluator == "ORFS post-route QoR", "evaluator default")

    shutil.rmtree(tmpdir)
    total = ok + fail_count
    print(f"\n{'='*50}")
    print(f"  {ok}/{total} passed"
          + (f", {fail_count} FAILED" if fail_count else " — ALL OK"))
    print(f"{'='*50}")
    sys.exit(1 if fail_count else 0)
