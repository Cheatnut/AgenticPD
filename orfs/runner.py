# -*- coding: utf-8 -*-
"""orfs.runner — Stage C1: subprocess execution for ORFS stages.

Extracted from orfs_interface.py.  Handles make process invocation,
timeout / process-group cleanup, and log capture.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from config import FrameworkConfig
from orfs.command import build_make_cmd
from orfs.parser import (
    parse_qor, parse_stage_qor, detect_failed_stage,
    CLEAN_TARGETS, STAGE_MAKE_TARGET,
)
from utils import QoR

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Low-level subprocess helpers
# ---------------------------------------------------------------------------

def run_make(cfg: FrameworkConfig, cmd: List[str],
             make_log_path: Path) -> Tuple[int, bool]:
    """Execute ``make ...`` with timeout and process-group cleanup.

    Uses ``start_new_session=True`` so that ``killpg`` on timeout cleans up
    all child processes (yosys, openroad, ...) spawned by make.

    Returns:
        (exit_code, timed_out): exit_code is -1 when timed out.
    """
    make_log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(make_log_path, "w", encoding="utf-8") as fout:
        proc = subprocess.Popen(
            cmd, cwd=cfg.flow_dir,
            stdout=fout, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            returncode = proc.wait(timeout=cfg.timeout_s)
            return returncode, False
        except subprocess.TimeoutExpired:
            log.error(
                "[ORFS] make timeout (>%ds), killing process group %d",
                cfg.timeout_s, proc.pid,
            )
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
            return -1, True


def tail_log(path: Path, lines: int = 20) -> str:
    """Return the last *lines* lines of a make log (error summary)."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        return "\n".join(content.splitlines()[-lines:])
    except OSError:
        return "(unable to read make log)"


def run_clean_make(cfg: FrameworkConfig, variant: str,
                   clean_target: str) -> None:
    """Run ``make clean_<stage>`` for a single stage cleanup."""
    cmd = [
        "make", "-C", str(cfg.flow_dir),
        f"DESIGN_CONFIG={cfg.design_config}",
        f"FLOW_VARIANT={variant}",
        clean_target,
    ]
    assert cfg.run_dir is not None
    log_path = cfg.run_dir / f"clean_{clean_target}.log"
    returncode, _ = run_make(cfg, cmd, log_path)
    if returncode != 0:
        log.warning("[ORFS] %s returned %d (non-fatal)", clean_target, returncode)


# ---------------------------------------------------------------------------
# Stage / flow execution
# ---------------------------------------------------------------------------

def execute_stage(
    cfg: FrameworkConfig,
    stage: str,
    stage_params: Dict[str, Dict[str, Any]],
    variant: str,
    iteration: int,
) -> "StageResult":
    """Execute a single ORFS stage and return a StageResult.

    1. ``make clean_<stage>`` to reset this stage's artifacts
    2. ``make <stage>`` with the stage's parameters
    3. Parse and return intermediate QoR

    Returns a StageResult with elapsed_s, exit_code, and stage_qor.
    Failed stages correctly record their elapsed time (never 0.0).
    """
    from schemas.trial import StageResult, FailureClass

    clean_target = CLEAN_TARGETS.get(stage)
    make_target = STAGE_MAKE_TARGET.get(stage)
    if not clean_target or not make_target:
        log.error("[ORFS] Unknown stage '%s'", stage)
        return StageResult(stage=stage, status="failed", elapsed_s=0.0,
                          failure=FailureClass.TOOL_CRASH,
                          error_message=f"Unknown stage: {stage}")

    # 1) Clean this stage
    run_clean_make(cfg, variant, clean_target)

    # 2) Build make command for this single stage
    cmd, log_path = build_make_cmd(
        cfg, stage_params, variant, iteration, target=make_target,
    )
    log_path_str = str(log_path)

    log.info("#%d [ORFS] make %s...", iteration, make_target)
    start = time.monotonic()
    returncode, timed_out = run_make(cfg, cmd, log_path)
    elapsed = time.monotonic() - start

    if timed_out:
        log.error("#%d [ORFS] %s timeout (%.1fs)", iteration, stage, elapsed)
        return StageResult(stage=stage, status="failed", elapsed_s=elapsed,
                          exit_code=-1, log_path=log_path_str,
                          failure=FailureClass.TIMEOUT,
                          error_message=f"Stage timeout after {elapsed:.1f}s")

    if returncode != 0:
        log.error("#%d [ORFS] %s failed (exit=%d, elapsed=%.1fs)",
                  iteration, stage, returncode, elapsed)
        return StageResult(stage=stage, status="failed", elapsed_s=elapsed,
                          exit_code=returncode, log_path=log_path_str,
                          failure=FailureClass.from_exit_code(returncode),
                          error_message=f"make exit code {returncode}")

    # 3) Parse stage QoR
    stage_qor_raw = parse_stage_qor(cfg, variant)
    stage_qor = stage_qor_raw.get(stage, {})
    log.info("#%d [ORFS] %s done!(%.1fs)", iteration, stage, elapsed)
    if stage_qor:
        log.info("#%d [ORFS] %s QoR: %s", iteration, stage,
                 ", ".join(f"{k}={v}" for k, v in list(stage_qor.items())[:3]))
    return StageResult(stage=stage, status="ok", elapsed_s=elapsed,
                      exit_code=0, log_path=log_path_str,
                      stage_qor=stage_qor)


def execute_flow(
    cfg: FrameworkConfig,
    stage_params: Dict[str, Dict[str, Any]],
    variant: str,
    iteration: int,
) -> Tuple[bool, Optional[QoR], Dict[str, Dict[str, float]],
           Optional[str], Optional[str], float]:
    """Run a complete RTL-to-GDS flow and return structured results.

    Returns:
        (ok, qor, stage_qor, failed_stage, error_message, elapsed_s)
    """
    # 1) Build the make command for the full flow
    cmd, log_path = build_make_cmd(cfg, stage_params, variant, iteration)

    log.info("#%d [ORFS] make all...", iteration)
    start = time.monotonic()
    returncode, timed_out = run_make(cfg, cmd, log_path)
    elapsed = time.monotonic() - start

    # 2) Parse results
    sq = parse_stage_qor(cfg, variant)

    if timed_out:
        failed_stage = detect_failed_stage(cfg, variant) or "unknown"
        return (False, None, sq, failed_stage,
                f"Timeout (>{cfg.timeout_s}s), process group killed", elapsed)

    qor = parse_qor(cfg, variant)

    if returncode != 0:
        failed_stage = detect_failed_stage(cfg, variant) or "unknown"
        return (False, qor, sq, failed_stage,
                f"make exit code {returncode}; log tail:\n{tail_log(log_path)}",
                elapsed)

    if qor is None or not qor.is_complete():
        failed_stage = detect_failed_stage(cfg, variant) or "metrics"
        return (False, qor, sq, failed_stage,
                "Flow exited 0 but QoR metrics incomplete", elapsed)

    log.info("#%d [ORFS] Iter #%d done!(%.1fs)", iteration, iteration, elapsed)
    log.info("#%d [ORFS] Iter #%d final QoR: %s", iteration, iteration,
             qor.pretty())
    return True, qor, sq, None, None, elapsed
