# -*- coding: utf-8 -*-
"""orfs.interface — Stage C1: high-level ORFS orchestrator.

Wraps command building, subprocess execution, and result parsing into a
single ``ORFSRunner`` class.  Also provides ``MockORFSRunner`` for
zero-EDA testing.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config
from config import FrameworkConfig
from utils import QoR

from orfs.command import build_make_cmd
from orfs.parser import (
    parse_qor, parse_stage_qor, detect_failed_stage,
    CLEAN_TARGETS, STAGE_MAKE_TARGET,
)
from orfs.runner import (
    run_make, run_clean_make, tail_log,
    execute_stage, execute_flow,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# RunResult
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    """Record of one complete ORFS execution."""

    ok: bool                                    # flow succeeded and QoR is complete
    variant: str                                # FLOW_VARIANT name
    qor: Optional[QoR] = None                   # final post-route QoR
    stage_qor: Dict[str, Dict[str, float]] = field(default_factory=dict)
    failed_stage: Optional[str] = None          # stage at which failure occurred
    error: Optional[str] = None                 # error summary (log tail, etc.)
    elapsed_s: float = 0.0                      # wall-clock seconds
    make_log_path: Optional[str] = None         # path to the make stdout/stderr log


# ---------------------------------------------------------------------------
# ORFSRunner
# ---------------------------------------------------------------------------

class ORFSRunner:
    """ORFS invoker: one instance per optimisation run (binds one FrameworkConfig)."""

    def __init__(self, cfg: FrameworkConfig):
        self.cfg = cfg

    # ------------------------------------------------------------------
    # Full-flow execution
    # ------------------------------------------------------------------

    def run_flow(self, stage_params: Dict[str, Dict[str, Any]],
                 variant: str, iteration: int) -> RunResult:
        """Run a complete RTL-to-GDS flow with isolated FLOW_VARIANT.

        Args:
            stage_params: {"FP": {...}, "PL": {...}, "CTS": {...}, "RT": {...}}
            variant:      FLOW_VARIANT name (e.g. "agenticpd_iter3")
            iteration:    iteration number (for log / fastroute.tcl naming)
        """
        # 1) Wipe stale artifacts from previous crashes
        self._wipe_variant(variant)

        # 2) Execute the full flow
        ok, qor, sq, failed_stage, error, elapsed = execute_flow(
            self.cfg, stage_params, variant, iteration,
        )

        result = RunResult(
            ok=ok, variant=variant, qor=qor, stage_qor=sq,
            failed_stage=failed_stage, error=error, elapsed_s=elapsed,
        )
        return result

    # ------------------------------------------------------------------
    # Single-stage execution (for per-stage pipeline)
    # ------------------------------------------------------------------

    def run_stage(self, stage: str,
                  stage_params: Dict[str, Dict[str, Any]],
                  variant: str, iteration: int) -> Tuple[bool, Optional[Dict[str, float]]]:
        """Execute a single ORFS stage and return (ok, stage_qor)."""
        return execute_stage(self.cfg, stage, stage_params, variant, iteration)

    def run_finish(self, stage_params: Dict[str, Dict[str, Any]],
                   variant: str, iteration: int) -> RunResult:
        """Run ``make finish`` after all downstream stages.

        Returns a RunResult with final QoR parsed from 6_report.json
        (or rpt/log fallback).  Handles timeout, non-zero exit, and
        incomplete metrics consistently with run_flow().
        """
        cmd, log_path = build_make_cmd(
            self.cfg, stage_params, variant, iteration, target="finish",
        )
        log.info("#%d [ORFS] make finish...", iteration)
        start = time.monotonic()
        returncode, timed_out = run_make(self.cfg, cmd, log_path)
        elapsed = time.monotonic() - start

        result = RunResult(ok=False, variant=variant, elapsed_s=elapsed,
                           make_log_path=str(log_path))
        result.stage_qor = parse_stage_qor(self.cfg, variant)

        if timed_out:
            result.failed_stage = detect_failed_stage(self.cfg, variant) or "finish"
            result.error = f"finish timeout (>{self.cfg.timeout_s}s), process group killed"
            log.error("#%d [ORFS] finish timeout", iteration)
            return result

        qor = parse_qor(self.cfg, variant)
        result.qor = qor if qor is not None else None

        if returncode != 0:
            result.failed_stage = detect_failed_stage(self.cfg, variant) or "finish"
            result.error = (f"finish make exit code {returncode}; log tail:\n"
                            f"{tail_log(log_path)}")
            log.error("#%d [ORFS] finish failed (exit %d)", iteration, returncode)
            return result

        if result.qor is None or not result.qor.is_complete():
            result.failed_stage = "metrics"
            result.error = "finish exited 0 but QoR metrics incomplete"
            log.error("#%d [ORFS] finish QoR incomplete", iteration)
            return result

        result.ok = True
        log.info("#%d [ORFS] Iter #%d finish!(%.1fs)", iteration, iteration, elapsed)
        log.info("#%d [ORFS] Iter #%d final QoR: %s", iteration, iteration,
                 result.qor.pretty())
        return result

    # ------------------------------------------------------------------
    # Branching helpers
    # ------------------------------------------------------------------

    def copy_parent_results(self, parent_variant: str,
                            new_variant: str) -> None:
        """Copy a parent variant's four directory trees to a new variant.

        Prerequisite for branch execution: after copying, Bef-stage results
        are in place; only clean + make the target stage is needed.
        """
        cfg = self.cfg
        for get_dir in (cfg.results_dir, cfg.objects_dir,
                        cfg.logs_dir, cfg.reports_dir):
            src = get_dir(parent_variant)
            dst = get_dir(new_variant)
            if not src.is_dir():
                log.warning(
                    "[ORFS] Parent variant %s: %s dir not found, skip copy",
                    parent_variant, src.name,
                )
                continue
            dst.mkdir(parents=True, exist_ok=True)
            for item in src.iterdir():
                item_dst = dst / item.name
                if item.is_dir():
                    if item_dst.exists():
                        shutil.rmtree(item_dst)
                    shutil.copytree(item, item_dst, symlinks=True)
                else:
                    shutil.copy2(item, item_dst)

    def branch_from(self, parent_variant: str, branch_stage: str,
                    stage_params: Dict[str, Dict[str, Any]],
                    new_variant: str, iteration: int) -> RunResult:
        """Branch from an existing variant at a specific stage.

        1. Copy the parent variant's artifacts
        2. Clean the branch stage and all downstream stages
        3. Run ``make all`` (which will reuse upstream artifacts and
           re-run from the cleaned stage onward)
        """
        self.copy_parent_results(parent_variant, new_variant)
        for stage_name in ("FP", "PL", "CTS", "RT"):
            if stage_name == branch_stage or (
                branch_stage and config.STAGES.index(stage_name) >= config.STAGES.index(branch_stage)
            ):
                clean_target = CLEAN_TARGETS.get(stage_name)
                if clean_target:
                    run_clean_make(self.cfg, new_variant, clean_target)
        return self.run_flow(stage_params, new_variant, iteration)

    # ------------------------------------------------------------------
    # Result export
    # ------------------------------------------------------------------

    def export_best(self, variant: str, best_entry: Dict[str, Any]) -> Path:
        """Export the best iteration's artifacts to agenticpd_best/.

        Copies final results (GDS/DEF/netlist), key reports, and a summary
        JSON for traceability.
        """
        cfg = self.cfg
        best_dir = cfg.results_dir(cfg.best_variant_name)
        src_results = cfg.results_dir(variant)
        if not src_results.is_dir():
            raise FileNotFoundError(
                f"Best variant results dir not found: {src_results}")

        best_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src_results, best_dir, dirs_exist_ok=True)

        for src in (cfg.logs_dir(variant) / "6_report.json",
                    cfg.logs_dir(variant) / "6_report.log",
                    cfg.reports_dir(variant) / "6_finish.rpt"):
            if src.is_file():
                shutil.copy2(src, best_dir / src.name)

        summary_path = best_dir / "agenticpd_summary.json"
        summary_path.write_text(
            json.dumps(best_entry, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.info("[ORFS] Best result exported to %s", best_dir)
        return best_dir

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _wipe_variant(self, variant: str) -> None:
        """Remove all four directory trees for a variant (prevents make
        from skipping stages due to stale artifacts)."""
        cfg = self.cfg
        for get_dir in (cfg.results_dir, cfg.objects_dir,
                        cfg.logs_dir, cfg.reports_dir):
            d = get_dir(variant)
            if d.is_dir():
                shutil.rmtree(d)


# ---------------------------------------------------------------------------
# MockORFSRunner
# ---------------------------------------------------------------------------

class MockORFSRunner(ORFSRunner):
    """Fake ORFS runner for zero-cost logic validation.

    Produces deterministic, parameter-dependent synthetic QoR so that
    the optimisation loop, history persistence, and prompt rendering can
    be tested without any EDA tool invocation.
    """

    def run_flow(self, stage_params, variant, iteration) -> RunResult:
        time.sleep(0.01)  # simulate minimal latency
        qor = self._mock_qor(stage_params)
        sq = self._mock_stage_qor(stage_params)
        return RunResult(ok=True, variant=variant, qor=qor, stage_qor=sq,
                         elapsed_s=0.05)

    def run_stage(self, stage, stage_params, variant, iteration):
        time.sleep(0.01)
        sq = self._mock_stage_qor(stage_params)
        return True, sq.get(stage, {})

    def run_finish(self, stage_params, variant, iteration):
        sq = self._mock_stage_qor(stage_params)
        qor = self._mock_qor(stage_params)
        return RunResult(ok=True, variant=variant, qor=qor, stage_qor=sq,
                         elapsed_s=0.02)

    def branch_from(self, parent_variant, branch_stage, stage_params,
                    new_variant, iteration):
        return self.run_flow(stage_params, new_variant, iteration)

    def export_best(self, variant, best_entry):
        d = self.cfg.results_dir(self.cfg.best_variant_name)
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ---- Synthetic QoR generation ----

    @staticmethod
    def _mock_qor(stage_params: Dict[str, Dict[str, Any]]) -> QoR:
        """Deterministic synthetic QoR from parameters (not physically meaningful)."""
        fp = stage_params.get("FP", {})
        pl = stage_params.get("PL", {})
        cts = stage_params.get("CTS", {})
        util = fp.get("CORE_UTILIZATION", 38)
        density = pl.get("PLACE_DENSITY_LB_ADDON", 0.0)
        cluster = cts.get("CTS_CLUSTER_SIZE", 100)
        wns = -1500 + (util - 20) * 5 - density * 200 + (cluster - 100) * 0.5
        tns = wns * 40
        area = 5000 + (util - 35) * 30
        power = 0.008 + density * 0.01
        return QoR(wns_ps=round(wns, 1), tns_ps=round(tns, 1),
                   area_um2=round(area, 1), power_w=round(power, 5))

    @staticmethod
    def _mock_stage_qor(stage_params: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, float]]:
        sq: Dict[str, Dict[str, float]] = {}
        fp = stage_params.get("FP", {})
        util = fp.get("CORE_UTILIZATION", 38)
        ws = -1500 + (util - 20) * 5
        sq["FP"] = {"2_1_floorplan_ws_ps": round(ws, 1)}
        sq["PL"] = {"3_5_place_dp_ws_ps": round(ws * 1.05, 1)}
        sq["CTS"] = {"4_1_cts_ws_ps": round(ws * 1.02, 1)}
        sq["RT"] = {"5_1_grt_ws_ps": round(ws * 1.01, 1)}
        return sq
