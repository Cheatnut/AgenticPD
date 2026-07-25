# -*- coding: utf-8 -*-
"""
orfs_interface.py — ORFS (OpenROAD Flow Scripts) invocation wrapper

Responsibilities:
1. Translate agent-generated per-stage parameters into make command-line variables
   (including two pseudo-params with special delivery:
   FASTROUTE_LAYER_ADJUSTMENT → generate custom fastroute.tcl;
   GRT_CONGESTION_ITERATIONS → render into GLOBAL_ROUTE_ARGS)
2. Run full flow under an isolated FLOW_VARIANT (with timeout and process group cleanup)
3. Parse final QoR (JSON metrics primary, rpt/log regex fallback) and per-stage
   intermediate QoR
4. Locate failure stage when flow crashes
5. Export best result to flow/results/<plat>/<design>/agenticpd_best/
6. branch_from(): branching run interface (currently falls back to full re-run;
   future extension point)

Important implementation note: make does not detect changes to command-line
variables — if a variant directory already contains old artifacts, changing
params will cause make to skip targets (believing they are already up-to-date).
Therefore, before each run, the four directory trees (results/logs/reports/objects)
for that variant MUST be empty. This is the key to a genuine "re-run from scratch."
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config
from config import FrameworkConfig, ParamSpec
from utils import QoR

log = logging.getLogger("orfs")

# ---------------------------------------------------------------------------
# JSON metrics files for each flow sub-step (in execution order), used for:
# 1. detect_failed_stage: first missing json = crash stage
# 2. parse_stage_qor: extract per-stage intermediate timing as StageAgent
#    "upstream QoR" input
# Filenames and stage mapping based on flow/Makefile do-step rules
# ---------------------------------------------------------------------------
STEP_JSON_SEQUENCE: List[Tuple[str, str]] = [
    ("1_synth.json", "synth"),
    ("2_1_floorplan.json", "floorplan"),
    ("2_2_floorplan_macro.json", "floorplan"),
    ("2_3_floorplan_tapcell.json", "floorplan"),
    ("2_4_floorplan_pdn.json", "floorplan"),
    ("3_1_place_gp_skip_io.json", "place"),
    ("3_2_place_iop.json", "place"),
    ("3_3_place_gp.json", "place"),
    ("3_4_place_resized.json", "place"),
    ("3_5_place_dp.json", "place"),
    ("4_1_cts.json", "cts"),
    ("5_1_grt.json", "globalroute"),
    ("5_2_route.json", "detailedroute"),
    ("5_3_fillcell.json", "route"),
    ("6_report.json", "finish"),
]

# Representative intermediate QoR source files per stage (StageAgent upstream input):
# FP uses floorplan end, PL uses detailed placement end, CTS uses CTS end,
# RT uses both global and detailed routing
STAGE_QOR_SOURCES: Dict[str, List[str]] = {
    "FP": ["2_1_floorplan.json"],
    "PL": ["3_5_place_dp.json"],
    "CTS": ["4_1_cts.json"],
    "RT": ["5_1_grt.json", "5_2_route.json"],
}

# Stage name → ORFS single-stage make target (for per-stage pipeline execution)
_STAGE_MAKE_TARGET: Dict[str, str] = {
    "FP": "floorplan",
    "PL": "place",
    "CTS": "cts",
    "RT": "route",
}


@dataclass
class RunResult:
    """Record of one complete ORFS run"""

    ok: bool                              # whether flow succeeded and QoR is complete
    variant: str                          # FLOW_VARIANT used for this run
    qor: Optional[QoR] = None             # final QoR (may be None or incomplete on failure)
    stage_qor: Dict[str, Dict[str, float]] = field(default_factory=dict)
    failed_stage: Optional[str] = None    # stage where failure occurred (None if success)
    error: Optional[str] = None           # error summary (make log tail, etc.)
    elapsed_s: float = 0.0                # run duration (seconds)
    make_log_path: Optional[str] = None   # make output log file path


class ORFSRunner:
    """ORFS invoker: one instance per optimization run (bound to one FrameworkConfig)"""

    def __init__(self, cfg: FrameworkConfig):
        self.cfg = cfg

    # ------------------------------------------------------------------
    # Main entry: run a complete flow
    # ------------------------------------------------------------------
    def run_flow(self, stage_params: Dict[str, Dict[str, Any]],
                 variant: str, iteration: int) -> RunResult:
        """Run a complete RTL→GDS flow from scratch with the given per-stage
        parameters and an isolated FLOW_VARIANT.

        Args:
            stage_params: {"FP": {...}, "PL": {...}, "CTS": {...}, "RT": {...}}
            variant:      FLOW_VARIANT name for this run (e.g. agenticpd_iter3)
            iteration:    iteration number (used for naming generated fastroute.tcl
                          and make logs)
        """
        cfg = self.cfg
        # 1) Wipe the variant's four directory trees (prevent make skipping due to
        #    stale artifacts from a previous crash)
        self._wipe_variant(variant)

        # 2) Build the make command
        make_cmd, make_log_path = self._build_make_cmd(stage_params, variant, iteration)
        make_target = make_cmd[-1] if make_cmd else "all"
        log.info("#%d [ORFS] make %s...", iteration, make_target)

        # 3) Execute (with timeout and process-group cleanup)
        start = time.monotonic()
        returncode, timed_out = self._run_make(make_cmd, make_log_path)
        elapsed = time.monotonic() - start

        # 4) Parse results
        result = RunResult(ok=False, variant=variant, elapsed_s=elapsed,
                           make_log_path=str(make_log_path))
        result.stage_qor = self.parse_stage_qor(variant)

        if timed_out:
            result.failed_stage = self.detect_failed_stage(variant) or "unknown"
            result.error = f"Timeout (>{cfg.timeout_s}s), process group killed"
            log.error("#%d [ORFS] Timeout, failed stage: %s", iteration, result.failed_stage)
            return result

        # Always try to parse QoR regardless of exit code (partial failures may
        # still have complete reports)
        qor = self.parse_qor(variant)
        result.qor = qor if qor is not None else None

        if returncode != 0:
            result.failed_stage = self.detect_failed_stage(variant) or "unknown"
            result.error = (f"make exit code {returncode}; log tail:\n"
                            f"{self._tail_log(make_log_path)}")
            log.error("#%d [ORFS] make failed (exit %d), failed stage: %s",
                      iteration, returncode, result.failed_stage)
            return result

        if result.qor is None or not result.qor.is_complete():
            # Exit 0 but incomplete metrics: treat as failure (partial QoR won't
            # participate in best comparison)
            result.failed_stage = self.detect_failed_stage(variant) or "metrics"
            result.error = "Flow exited 0 but QoR metrics incomplete"
            log.error("#%d [ORFS] QoR incomplete: %s", iteration,
                      result.qor.to_dict() if result.qor else None)
            return result

        result.ok = True
        log.info("#%d [ORFS] Iter #%d done!(%.1fs)", iteration, iteration, elapsed)
        log.info("#%d [ORFS] Iter #%d final QoR: %s", iteration, iteration, result.qor.pretty())
        return result

    # ------------------------------------------------------------------
    # Branching interface: paper §3.2 "select intermediate node n_hat
    # to start a new branch"
    # ------------------------------------------------------------------
    _CLEAN_TARGETS: Dict[str, str] = {
        "FP": "clean_floorplan",
        "PL": "clean_place",
        "CTS": "clean_cts",
        "RT": "clean_route",
    }

    def copy_parent_results(self, parent_variant: str, new_variant: str) -> None:
        """Copy the parent variant's four directories (results/objects/logs/reports)
        in full to the new variant.

        This is the prerequisite step for branch execution — after copying, Bef
        stage results are in place; only clean + make the target stage is needed
        for incremental re-run.
        """
        cfg = self.cfg
        for get_dir in (cfg.results_dir, cfg.objects_dir, cfg.logs_dir, cfg.reports_dir):
            src = get_dir(parent_variant)
            dst = get_dir(new_variant)
            if not src.is_dir():
                log.warning("[ORFS] Parent variant %s: %s dir not found, skip copy",
                            parent_variant, src.name)
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
        """Paper §3.2: branch run from an intermediate snapshot of parent_variant.

        How it works:
        1. Copy parent variant's results/objects/logs/reports to new variant;
        2. Run make clean_<branch_stage> to remove branch-stage artifacts
           (using ORFS per-stage clean targets);
        3. Run make all with new params — make dependency checks auto-skip
           Bef stages, re-running {branch_stage} ∪ Aft(branch_stage).
        """
        log.info("#%d [ORFS] Branch from %s @%s, new variant=%s",
                 iteration, parent_variant, branch_stage, new_variant)

        # 1) Copy parent variant artifacts
        self.copy_parent_results(parent_variant, new_variant)

        # 2) Clean branch-stage artifacts
        clean_target = self._CLEAN_TARGETS.get(branch_stage)
        if clean_target is None:
            raise ValueError(f"Unknown branch stage: {branch_stage}")
        self._run_clean_make(new_variant, clean_target)

        # 3) Build make command and execute (make will only rebuild cleaned
        #    stage and downstream)
        make_cmd, make_log_path = self._build_make_cmd(stage_params, new_variant, iteration)
        log.info("#%d [ORFS] make (branch from %s)...", iteration, branch_stage)

        start = time.monotonic()
        returncode, timed_out = self._run_make(make_cmd, make_log_path)
        elapsed = time.monotonic() - start

        # 4) Parse results (identical to run_flow)
        cfg = self.cfg
        result = RunResult(ok=False, variant=new_variant, elapsed_s=elapsed,
                           make_log_path=str(make_log_path))
        result.stage_qor = self.parse_stage_qor(new_variant)

        if timed_out:
            result.failed_stage = self.detect_failed_stage(new_variant) or "unknown"
            result.error = f"Timeout (>{cfg.timeout_s}s), process group killed"
            log.error("#%d [ORFS] Branch timeout, failed stage: %s", iteration, result.failed_stage)
            return result

        qor = self.parse_qor(new_variant)
        result.qor = qor if qor is not None else None

        if returncode != 0:
            result.failed_stage = self.detect_failed_stage(new_variant) or "unknown"
            result.error = (f"make exit code {returncode}; log tail:\n"
                            f"{self._tail_log(make_log_path)}")
            log.error("#%d [ORFS] Branch make failed (exit %d), failed stage: %s",
                      iteration, returncode, result.failed_stage)
            return result

        if result.qor is None or not result.qor.is_complete():
            result.failed_stage = self.detect_failed_stage(new_variant) or "metrics"
            result.error = "Flow exited 0 but QoR metrics incomplete"
            log.error("#%d [ORFS] Branch QoR incomplete: %s", iteration,
                      result.qor.to_dict() if result.qor else None)
            return result

        result.ok = True
        log.info("#%d [ORFS] Branch Iter #%d done!(%.1fs)", iteration, iteration, elapsed)
        log.info("#%d [ORFS] Branch Iter #%d final QoR: %s", iteration, iteration,
                 result.qor.pretty())
        return result

    # ------------------------------------------------------------------
    # Per-stage pipeline interface: run one ORFS stage at a time
    # ------------------------------------------------------------------

    def run_stage(self, stage: str,
                  stage_params: Dict[str, Dict[str, Any]],
                  variant: str, iteration: int) -> Tuple[bool, Dict[str, float]]:
        """Run a single ORFS stage (e.g. place, cts) and return its intermediate QoR.

        Prerequisite: the variant directory already contains complete artifacts for
        all Bef stages (established by the caller via copy_parent_results()).

        Process:
        1. Clean the stage (ensure stale artifacts don't cause make to skip)
        2. make <stage_target> (run only this stage, not downstream)
        3. Parse intermediate QoR (ws_ps / tns_ps)

        Returns:
            (ok, stage_qor_dict): ok = stage completed successfully,
            stage_qor_dict contains {<tag>_ws_ps, <tag>_tns_ps} etc.
        """
        cfg = self.cfg
        if stage not in self._CLEAN_TARGETS:
            raise ValueError(f"Unknown stage: {stage}, valid: {'/'.join(config.STAGES)}")
        make_target = _STAGE_MAKE_TARGET[stage]

        # 1) Clean this stage's artifacts
        clean_target = self._CLEAN_TARGETS[stage]
        self._run_clean_make(variant, clean_target)

        # 2) Build and execute single-stage make
        make_cmd, make_log_path = self._build_make_cmd(
            stage_params, variant, iteration,
            target=make_target, log_suffix=f"_{stage}")
        log.info("#%d [ORFS] make %s...", iteration, make_target)

        start = time.monotonic()
        returncode, timed_out = self._run_make(make_cmd, make_log_path)
        elapsed = time.monotonic() - start

        # 3) Parse this stage's intermediate QoR
        all_stage_qor = self.parse_stage_qor(variant)
        stage_qor = all_stage_qor.get(stage, {})

        if timed_out:
            log.error("#%d [ORFS] %s timeout (%.1fs)", iteration, stage, elapsed)
            return False, stage_qor
        if returncode != 0:
            log.error("#%d [ORFS] %s make failed (exit %d, log: %s)",
                      iteration, stage, returncode, make_log_path)
            return False, stage_qor

        log.info("#%d [ORFS] %s done!(%.1fs)", iteration, stage, elapsed)
        log.info("#%d [ORFS] %s QoR: %s", iteration, stage,
                 ", ".join(f"{k}={v}" for k, v in sorted(stage_qor.items())))
        return True, stage_qor

    def run_finish(self, stage_params: Dict[str, Dict[str, Any]],
                   variant: str, iteration: int) -> RunResult:
        """Execute make finish after all downstream stages complete, parsing final QoR.

        Prerequisite: variant already has all stage results for this iteration
        (established by per-stage run_stage calls). make finish only generates
        final reports (6_report.json, etc.), not re-running completed stages.
        """
        cfg = self.cfg
        make_cmd, make_log_path = self._build_make_cmd(
            stage_params, variant, iteration,
            target="finish", log_suffix="_finish")
        log.info("#%d [ORFS] make finish...", iteration)

        start = time.monotonic()
        returncode, timed_out = self._run_make(make_cmd, make_log_path)
        elapsed = time.monotonic() - start

        result = RunResult(ok=False, variant=variant, elapsed_s=elapsed,
                           make_log_path=str(make_log_path))
        result.stage_qor = self.parse_stage_qor(variant)

        if timed_out:
            result.failed_stage = self.detect_failed_stage(variant) or "finish"
            result.error = f"Timeout (>{cfg.timeout_s}s), process group killed"
            log.error("#%d [ORFS] finish timeout", iteration)
            return result

        qor = self.parse_qor(variant)
        result.qor = qor if qor is not None else None

        if returncode != 0:
            result.failed_stage = self.detect_failed_stage(variant) or "finish"
            result.error = (f"finish make exit code {returncode}; log tail:\n"
                            f"{self._tail_log(make_log_path)}")
            log.error("#%d [ORFS] finish failed (exit %d)", iteration, returncode)
            return result

        if result.qor is None or not result.qor.is_complete():
            result.failed_stage = "metrics"
            result.error = "finish exited 0 but QoR metrics incomplete"
            log.error("#%d [ORFS] finish QoR incomplete", iteration)
            return result

        result.ok = True
        log.info("#%d [ORFS] Iter #%d finish!(%.1fs)", iteration, iteration, elapsed)
        log.info("#%d [ORFS] Iter #%d final QoR: %s", iteration, iteration, result.qor.pretty())
        return result

    def _run_clean_make(self, variant: str, clean_target: str) -> None:
        """Execute a clean target under the given variant (120s timeout, fatal on failure)"""
        cmd = [
            "make", "-C", str(self.cfg.flow_dir),
            f"DESIGN_CONFIG={self.cfg.design_config}",
            f"FLOW_VARIANT={variant}",
            clean_target,
        ]
        log.debug("[ORFS] clean make: %s", " ".join(cmd))
        proc = subprocess.Popen(
            cmd, cwd=self.cfg.flow_dir,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True)
        try:
            stdout, _ = proc.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            raise RuntimeError("clean make timeout (>120s)")
        if proc.returncode != 0:
            tail = "\n".join(stdout.decode("utf-8", errors="replace").splitlines()[-10:])
            raise RuntimeError(
                f"clean make failed (exit {proc.returncode}):\n{tail}")

    # ------------------------------------------------------------------
    # Internal: directory wiping / command building / process execution
    # ------------------------------------------------------------------
    def _wipe_variant(self, variant: str) -> None:
        """Delete the variant's results/logs/reports/objects directory trees if present.

        Only operates on variants named by this framework (agenticpd_iter*);
        NEVER touches base.
        """
        assert variant != "base", "Deleting the base baseline directory is forbidden"
        for d in (self.cfg.results_dir(variant), self.cfg.logs_dir(variant),
                  self.cfg.reports_dir(variant), self.cfg.objects_dir(variant)):
            if d.exists():
                log.debug("[ORFS] cleaning stale dir: %s", d)
                shutil.rmtree(d)

    def _build_make_cmd(self, stage_params: Dict[str, Dict[str, Any]],
                        variant: str, iteration: int,
                        target: Optional[str] = None,
                        log_suffix: str = "") -> Tuple[List[str], Path]:
        """Translate per-stage params into a complete make command (list form, no shell).

        target: make target, defaults to cfg.make_target ("all"). For per-stage
                execution, pass a single-stage target (e.g. "floorplan", "place",
                "cts", "route", "finish").
        log_suffix: log filename suffix (e.g. "_FP", "_PL"), used to distinguish
                    logs for different stages within the same iteration.

        Returns (make command list, make output log path).
        """
        cfg = self.cfg
        assert cfg.run_dir is not None, "run_dir not initialized"

        make_vars: Dict[str, str] = {}
        for stage, params in stage_params.items():
            for name, value in params.items():
                spec = config.get_param_spec(name)
                if spec is None:
                    log.warning("[ORFS] Unknown param %s=%s (not in PARAM_SPACE), ignored",
                                name, value)
                    continue
                if spec.kind == config.KIND_MAKE_VAR:
                    # Regular param: directly NAME=value
                    make_vars[name] = str(value)
                elif spec.kind == config.KIND_FASTROUTE_ADJ:
                    # Pseudo-param 1: generate custom fastroute.tcl, pass absolute path
                    tcl_path = self._write_fastroute_tcl(float(value), iteration)
                    make_vars["FASTROUTE_TCL"] = str(tcl_path)
                elif spec.kind == config.KIND_GRT_ARGS:
                    # Pseudo-param 2: render into GLOBAL_ROUTE_ARGS (must include
                    # ORFS default prefix)
                    make_vars["GLOBAL_ROUTE_ARGS"] = (
                        config.GLOBAL_ROUTE_ARGS_TEMPLATE.format(iters=int(value)))

        cmd = [
            "make", "-C", str(cfg.flow_dir),
            f"DESIGN_CONFIG={cfg.design_config}",
            f"FLOW_VARIANT={variant}",
        ]
        cmd += [f"{k}={v}" for k, v in sorted(make_vars.items())]
        cmd.append(target or cfg.make_target)

        log_name = f"iter{iteration}{log_suffix}.make.log"
        make_log_path = cfg.run_dir / log_name
        return cmd, make_log_path

    def _write_fastroute_tcl(self, adjustment: float, iteration: int) -> Path:
        """Generate the custom fastroute.tcl for this iteration from the template,
        returning its absolute path.

        Written to run_dir (not objects/ — the objects/<variant>/ dir may not
        exist yet before make runs).
        """
        assert self.cfg.run_dir is not None
        tcl_path = self.cfg.run_dir / f"fastroute_iter{iteration}.tcl"
        tcl_path.write_text(
            config.FASTROUTE_TCL_TEMPLATE.format(adjustment=f"{adjustment:.2f}"),
            encoding="utf-8")
        return tcl_path.resolve()

    def _run_make(self, cmd: List[str], make_log_path: Path) -> Tuple[int, bool]:
        """Execute make (stdout/stderr streamed to log file), returns (exit_code, timed_out).

        Uses start_new_session=True to create an independent process group: on timeout,
        killpg eliminates all child processes (yosys/openroad) spawned by make,
        preventing zombie processes from continuing to consume CPU.
        """
        make_log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(make_log_path, "w", encoding="utf-8") as fout:
            proc = subprocess.Popen(
                cmd, cwd=self.cfg.flow_dir,
                stdout=fout, stderr=subprocess.STDOUT,
                start_new_session=True)
            try:
                returncode = proc.wait(timeout=self.cfg.timeout_s)
                return returncode, False
            except subprocess.TimeoutExpired:
                log.error("[ORFS] make timeout (>%ds), killing process group %d",
                          self.cfg.timeout_s, proc.pid)
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass  # process exited exactly at timeout moment
                proc.wait()
                return -1, True

    @staticmethod
    def _tail_log(path: Path, lines: int = 20) -> str:
        """Read the last `lines` lines from a make log as an error summary
        (avoid stuffing the entire log into history)"""
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            return "\n".join(content.splitlines()[-lines:])
        except OSError:
            return "(Cannot read make log)"

    # ------------------------------------------------------------------
    # Result parsing
    # ------------------------------------------------------------------
    def parse_qor(self, variant: str) -> Optional[QoR]:
        """Parse final QoR: 6_report.json preferred, rpt/log regex fallback,
        None if nothing found"""
        report_json = self.cfg.logs_dir(variant) / "6_report.json"
        if report_json.is_file():
            try:
                qor = QoR.from_report_json(report_json)
                if qor.is_complete():
                    return qor
                log.warning("[ORFS] 6_report.json metrics incomplete (%s), trying rpt fallback",
                            qor.to_dict())
            except (json.JSONDecodeError, OSError) as e:
                log.warning("[ORFS] Failed to parse 6_report.json (%s), trying rpt fallback", e)

        finish_rpt = self.cfg.reports_dir(variant) / "6_finish.rpt"
        report_log = self.cfg.logs_dir(variant) / "6_report.log"
        if finish_rpt.is_file() or report_log.is_file():
            return QoR.from_reports_fallback(finish_rpt, report_log)
        return None

    def parse_stage_qor(self, variant: str) -> Dict[str, Dict[str, float]]:
        """Extract per-stage intermediate timing metrics (ws/tns, unit ps)
        for StageAgent reference.

        JSON keys are like <prefix>__timing__setup__ws; prefixes differ per stage
        (floorplan/detailedplace/cts/globalroute/detailedroute). We match by
        suffix rather than hardcoding prefixes, increasing robustness against
        ORFS version differences.
        """
        logs_dir = self.cfg.logs_dir(variant)
        stage_qor: Dict[str, Dict[str, float]] = {}
        for stage, json_names in STAGE_QOR_SOURCES.items():
            merged: Dict[str, float] = {}
            for json_name in json_names:
                path = logs_dir / json_name
                if not path.is_file():
                    continue
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                tag = json_name.split(".")[0]  # e.g. 5_1_grt
                for key, value in data.items():
                    if not isinstance(value, (int, float)):
                        continue
                    if key.endswith("__timing__setup__ws"):
                        merged[f"{tag}_ws_ps"] = round(
                            float(value) * config.TIMING_UNIT_TO_PS, 1)
                    elif key.endswith("__timing__setup__tns"):
                        merged[f"{tag}_tns_ps"] = round(
                            float(value) * config.TIMING_UNIT_TO_PS, 1)
            if merged:
                stage_qor[stage] = merged
        return stage_qor

    def detect_failed_stage(self, variant: str) -> Optional[str]:
        """Check JSON sub-step files in execution order; the first missing one
        is the crash stage.

        Returns None if all exist (failure reason is not a flow step issue,
        e.g. a metrics parsing problem).
        """
        logs_dir = self.cfg.logs_dir(variant)
        for json_name, stage in STEP_JSON_SEQUENCE:
            if not (logs_dir / json_name).is_file():
                return stage
        return None

    # ------------------------------------------------------------------
    # Best result export
    # ------------------------------------------------------------------
    def export_best(self, variant: str, best_entry: Dict[str, Any]) -> Path:
        """Export the best iteration's artifacts to
        flow/results/<plat>/<design>/agenticpd_best/.

        Exports:
        1. All results for this variant (GDS/DEF/netlist etc.);
        2. Key reports (6_report.json / 6_finish.rpt / 6_report.log),
           making the result self-contained;
        3. agenticpd_summary.json: winning iteration number, per-stage params
           and QoR, for traceability.
        """
        cfg = self.cfg
        best_dir = cfg.results_dir(cfg.best_variant_name)
        src_results = cfg.results_dir(variant)
        if not src_results.is_dir():
            raise FileNotFoundError(f"Best variant results dir not found: {src_results}")

        best_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src_results, best_dir, dirs_exist_ok=True)

        # Attach key reports (copy only if present; some may be missing in
        # fallback parsing scenarios)
        for src in (cfg.logs_dir(variant) / "6_report.json",
                    cfg.logs_dir(variant) / "6_report.log",
                    cfg.reports_dir(variant) / "6_finish.rpt"):
            if src.is_file():
                shutil.copy2(src, best_dir / src.name)

        summary_path = best_dir / "agenticpd_summary.json"
        summary_path.write_text(
            json.dumps(best_entry, ensure_ascii=False, indent=2),
            encoding="utf-8")
        log.info("[ORFS] Best result exported to %s", best_dir)
        return best_dir


class MockORFSRunner(ORFSRunner):
    """Fake ORFS invoker (--mock-orfs): does not run real EDA; deterministically
    synthesizes QoR from parameters.

    Purpose: validate optimization main loop, history persistence, best comparison,
    prompt rendering, etc. in seconds — zero EDA runtime. Synthesis formulas have
    no physical meaning; they only guarantee:
    1. Same params → same QoR (deterministic, enable assertions);
    2. Parameter changes cause QoR changes (gives comparator/Judge something to do);
    3. CORE_UTILIZATION > 48 simulates routing failure (covers failure handling path).
    """

    def run_flow(self, stage_params: Dict[str, Dict[str, Any]],
                 variant: str, iteration: int) -> RunResult:
        # Check simulated failure condition: excessively high utilization
        flat: Dict[str, float] = {}
        for params in stage_params.values():
            for k, v in params.items():
                try:
                    flat[k] = float(v)
                except (TypeError, ValueError):
                    pass
        if flat.get("CORE_UTILIZATION", 38.0) > 48:
            return RunResult(ok=False, variant=variant, failed_stage="detailedroute",
                             error="mock: utilization too high, routing failed", elapsed_s=0.1)

        qor = self._mock_stage_qor(stage_params, "RT")
        wns = qor.wns_ps or -120.0
        stage_qor = {"FP": {"2_1_floorplan_ws_ps": round(wns + 60, 1)},
                     "PL": {"3_5_place_dp_ws_ps": round(wns + 40, 1)},
                     "CTS": {"4_1_cts_ws_ps": round(wns + 20, 1)},
                     "RT": {"5_2_route_ws_ps": wns}}
        return RunResult(ok=True, variant=variant, qor=qor, stage_qor=stage_qor,
                         elapsed_s=0.1)

    def _mock_stage_qor(self, stage_params: Dict[str, Dict[str, Any]],
                         stage: str) -> QoR:
        """Mock mode: deterministically compute QoR from params (extracted for reuse
        across run_flow / run_stage)"""
        flat: Dict[str, float] = {}
        for params in stage_params.values():
            for k, v in params.items():
                try:
                    flat[k] = float(v)
                except (TypeError, ValueError):
                    pass
        util = flat.get("CORE_UTILIZATION", 38.0)
        addon = flat.get("PLACE_DENSITY_LB_ADDON", 0.10)
        cluster = flat.get("CTS_CLUSTER_SIZE", 100.0)
        adj = flat.get("FASTROUTE_LAYER_ADJUSTMENT", 0.2)
        wns = -120.0 + (40 - util) * 2.0 - abs(addon - 0.05) * 300 \
            - abs(cluster - 60) * 0.3 - abs(adj - 0.22) * 200
        wns = round(wns, 1)
        return QoR(
            wns_ps=wns,
            tns_ps=round(min(0.0, wns) * 8.0, 1),
            area_um2=round(600.0 * 38.0 / max(util, 1.0), 1),
            power_w=round(1.5e-3 + util * 1e-5 + addon * 1e-3, 6),
        )

    def run_stage(self, stage: str,
                  stage_params: Dict[str, Dict[str, Any]],
                  variant: str, iteration: int) -> Tuple[bool, Dict[str, float]]:
        """Mock mode: return synthesized pseudo QoR for this stage (no real make)"""
        qor = self._mock_stage_qor(stage_params, stage)
        # Simulate per-stage intermediate ws, decreasing progressively
        # (later stages closer to final WNS)
        offset_map = {"FP": 60, "PL": 40, "CTS": 20, "RT": 0}
        ws = round(qor.wns_ps + offset_map.get(stage, 0), 1)
        stage_qor: Dict[str, float] = {}
        # Name using STAGE_QOR_SOURCES tags
        sources = STAGE_QOR_SOURCES.get(stage, [])
        for src_name in sources:
            tag = src_name.split(".")[0]  # e.g. 3_5_place_dp → 3_5_place_dp
            stage_qor[f"{tag}_ws_ps"] = ws
        log.info("#%d [MOCK] %s synthesized QoR: %s", iteration, stage,
                 ", ".join(f"{k}={v}" for k, v in sorted(stage_qor.items())))
        return True, stage_qor

    def run_finish(self, stage_params: Dict[str, Dict[str, Any]],
                   variant: str, iteration: int) -> RunResult:
        """Mock mode: synthesize final RunResult (same as run_flow)"""
        return self.run_flow(stage_params, variant, iteration)

    def branch_from(self, parent_variant: str, branch_stage: str,
                    stage_params: Dict[str, Dict[str, Any]],
                    new_variant: str, iteration: int) -> RunResult:
        """Mock mode: branch = same as run_flow (synthesized QoR)"""
        return self.run_flow(stage_params, new_variant, iteration)

    def export_best(self, variant: str, best_entry: Dict[str, Any]) -> Path:
        """Mock mode: no real artifacts; write summary to run_dir to validate call chain"""
        assert self.cfg.run_dir is not None
        summary_path = self.cfg.run_dir / "mock_best_summary.json"
        summary_path.write_text(
            json.dumps(best_entry, ensure_ascii=False, indent=2),
            encoding="utf-8")
        log.info("[MOCK] Best summary written to %s", summary_path)
        return summary_path
