#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checkpoint_fork_verify.py — Stage C acceptance experiment.

Reproducible checkpoint-fork vs full-restart comparison for Stage C
acceptance per ``docs/plans/AgenticPD-Demo审查与迭代计划.md`` requirement:

    "在同一 checkpoint fork 两个不同 action，分别跑到 post-route，
     并与 full restart 结果/耗时对照。"

Design:
    - 1 baseline (full flow, baseline params, produces CTS checkpoint)
    - 2 fork actions from the baseline CTS checkpoint, each changing one
      RT-only parameter (compatible with CTS checkpoint per ParamSpec.affects)
    - 2 corresponding full-restart runs with the same parameter changes
    - Compare final QoR equality (fork must match full restart) and wall-clock
      savings (fork must be faster)

Usage (run from flow/ directory)::

    python3 agenticpd/tools/checkpoint_fork_verify.py

All results are written to ``agenticpd/runs/sky130hd_gcd/checkpoint_fork/``
as a structured JSON report.

Pre-requisites:
    - ORFS environment sourced (``source env.sh``)
    - ``agenticpd/.env`` with DEEPSEEK_API_KEY (not used by this script)
    - Baseline must succeed on the target machine before running this script
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Ensure the agenticpd package is importable when run from flow/
_SCRIPT_DIR = Path(__file__).resolve().parent  # agenticpd/tools/
_AGENTICPD_DIR = _SCRIPT_DIR.parent            # agenticpd/
_FLOW_DIR = _AGENTICPD_DIR.parent              # flow/
if str(_FLOW_DIR) not in sys.path:
    sys.path.insert(0, str(_FLOW_DIR))
if str(_AGENTICPD_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENTICPD_DIR))

from config import (
    FrameworkConfig, BASELINE_PARAMS, STAGES, RUNS_DIR,
    get_design_runs_dir,
)
from orfs.interface import ORFSRunner
from orfs.parser import parse_qor, CLEAN_TARGETS, STAGE_MAKE_TARGET, STAGE_QOR_SOURCES
from orfs.runner import run_clean_make, execute_stage, tail_log
from utils import QoR

# ---------------------------------------------------------------------------
# Experiment constants (fixed for reproducibility)
# ---------------------------------------------------------------------------

PLATFORM = "sky130hd"
DESIGN = "gcd"
BASELINE_VARIANT = "agenticpd_baseline"

# Two RT-only parameter changes (compatible with CTS checkpoint:
# FASTROUTE_LAYER_ADJUSTMENT affects (RT,), GRT_CONGESTION_ITERATIONS affects (RT,))
ACTIONS = {
    "A_rt_layer_adj": {
        "description": "FASTROUTE_LAYER_ADJUSTMENT 0.2 -> 0.25",
        "stage": "RT",
        "param_name": "FASTROUTE_LAYER_ADJUSTMENT",
        "baseline_value": 0.2,
        "changed_value": 0.25,
    },
    "B_grt_iters": {
        "description": "GRT_CONGESTION_ITERATIONS 30 -> 50",
        "stage": "RT",
        "param_name": "GRT_CONGESTION_ITERATIONS",
        "baseline_value": 30,
        "changed_value": 50,
    },
}

# Checkpoint stage: fork from CTS (both actions change RT-only params,
# so the CTS checkpoint is compatible per ParamSpec.affects)
CHECKPOINT_STAGE = "CTS"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log = logging.getLogger("cp_fork_verify")


def setup_logging(report_dir: Path) -> None:
    fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    log.setLevel(logging.DEBUG)

    fh = logging.FileHandler(report_dir / "experiment.log")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    log.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    log.addHandler(sh)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def build_params(action_key: str) -> dict:
    """Build per-stage params for an action: baseline + one RT override."""
    action = ACTIONS[action_key]
    params = {s: dict(BASELINE_PARAMS.get(s, {})) for s in STAGES}
    params[action["stage"]][action["param_name"]] = action["changed_value"]
    return params


def qor_to_dict(qor) -> dict | None:
    """Convert QoR to a plain dict for JSON serialisation."""
    if qor is None:
        return None
    if hasattr(qor, "to_dict"):
        return qor.to_dict()
    if isinstance(qor, dict):
        return qor
    return {"wns_ps": getattr(qor, "wns_ps", None),
            "tns_ps": getattr(qor, "tns_ps", None),
            "area_um2": getattr(qor, "area_um2", None),
            "power_w": getattr(qor, "power_w", None)}


def qor_equal(a, b, wns_tol=1.0, tns_tol=5.0) -> tuple[bool, str]:
    """Compare two QoR dicts within tolerance.  Returns (equal, reason)."""
    if a is None and b is None:
        return True, "both None"
    if a is None or b is None:
        return False, "one is None"
    da = qor_to_dict(a)
    db = qor_to_dict(b)
    for key in ("wns_ps", "tns_ps", "area_um2", "power_w"):
        va = da.get(key)
        vb = db.get(key)
        if va is None and vb is None:
            continue
        if va is None or vb is None:
            return False, f"{key}: one is None"
        tol = wns_tol if key == "wns_ps" else tns_tol if key == "tns_ps" else 0.01 * abs(va) if va != 0 else 0.01
        if key in ("area_um2", "power_w"):
            tol = max(1.0, 0.001 * abs(va or vb or 1))
        if abs(va - vb) > tol:
            return False, f"{key}: {va} vs {vb} (delta={abs(va - vb):.3g} > tol={tol:.3g})"
    return True, "all metrics within tolerance"


# ---------------------------------------------------------------------------
# Experiment steps
# ---------------------------------------------------------------------------

def run_baseline(cfg: FrameworkConfig) -> dict:
    """Run the baseline flow (full FP->PL->CTS->RT) with baseline params."""
    log.info("=" * 60)
    log.info("STEP 1: BASELINE (full flow, baseline params)")
    log.info("=" * 60)

    runner = ORFSRunner(cfg)
    baseline_params = {s: dict(BASELINE_PARAMS.get(s, {})) for s in STAGES}

    t0 = time.monotonic()
    result = runner.run_flow(baseline_params, BASELINE_VARIANT, 0)
    elapsed = time.monotonic() - t0

    qor = parse_qor(cfg, BASELINE_VARIANT)

    outcome = {
        "variant": BASELINE_VARIANT,
        "ok": result.ok,
        "elapsed_s": round(elapsed, 1),
        "qor": qor_to_dict(qor),
        "error": result.error,
    }

    if not result.ok:
        log.error("Baseline FAILED: %s", result.error)
        log.error("Cannot continue — baseline is required for forking.")
        sys.exit(1)

    log.info("Baseline OK (%.1fs).  WNS=%.1f ps  area=%.0f um²  power=%.6f W",
             elapsed,
             qor.wns_ps if qor else float("nan"),
             qor.area_um2 if qor else float("nan"),
             qor.power_w if qor else float("nan"))
    return outcome


def run_full_restart(cfg: FrameworkConfig, action_key: str) -> dict:
    """Full RTL-to-GDS run with one parameter changed (from scratch)."""
    action = ACTIONS[action_key]
    variant = f"cpfv_full_{action_key.lower()}"
    params = build_params(action_key)

    log.info("-" * 50)
    log.info("FULL RESTART: %s (%s)", action_key, action["description"])
    log.info("Variant: %s", variant)

    runner = ORFSRunner(cfg)
    t0 = time.monotonic()
    result = runner.run_flow(params, variant, 1)
    elapsed = time.monotonic() - t0
    qor = parse_qor(cfg, variant)

    outcome = {
        "action": action_key,
        "mode": "full_restart",
        "variant": variant,
        "ok": result.ok,
        "elapsed_s": round(elapsed, 1),
        "qor": qor_to_dict(qor),
        "error": result.error,
        "failed_stage": result.failed_stage,
    }

    if result.ok:
        log.info("Full restart %s OK (%.1fs).  WNS=%.1f ps",
                 action_key, elapsed, qor.wns_ps if qor else float("nan"))
    else:
        log.error("Full restart %s FAILED (%.1fs): %s", action_key, elapsed, result.error)
    return outcome


def run_checkpoint_fork(cfg: FrameworkConfig, action_key: str) -> dict:
    """Fork from baseline CTS checkpoint: copy, clean RT, run RT + finish."""
    action = ACTIONS[action_key]
    variant = f"cpfv_fork_{action_key.lower()}"
    params = build_params(action_key)

    log.info("-" * 50)
    log.info("CHECKPOINT FORK: %s (%s)", action_key, action["description"])
    log.info("Fork from: %s @%s -> variant: %s",
             BASELINE_VARIANT, CHECKPOINT_STAGE, variant)

    runner = ORFSRunner(cfg)

    # 1) Copy parent (baseline) artifacts to fork variant
    t_start = time.monotonic()
    try:
        runner.copy_parent_results(BASELINE_VARIANT, variant)
    except FileNotFoundError as e:
        log.error("copy_parent_results failed: %s", e)
        return {
            "action": action_key, "mode": "checkpoint_fork",
            "variant": variant, "ok": False,
            "elapsed_s": round(time.monotonic() - t_start, 1),
            "error": f"copy_parent_results: {e}",
        }

    # 2) Clean only the RT stage (FP/PL/CTS artifacts are reused)
    #    Both actions change RT-only params, so only RT needs re-running.
    clean_target = CLEAN_TARGETS.get("RT", "clean_route")
    run_clean_make(cfg, variant, clean_target)

    # 3) Run RT stage
    from schemas.trial import StageResult
    rt_result = execute_stage(cfg, "RT", params, variant, 1)
    if rt_result.status == "failed":
        elapsed = time.monotonic() - t_start
        log.error("Fork %s RT stage FAILED: %s", action_key, rt_result.error_message)
        return {
            "action": action_key, "mode": "checkpoint_fork",
            "variant": variant, "ok": False,
            "elapsed_s": round(elapsed, 1),
            "error": f"RT stage: {rt_result.error_message}",
            "failed_stage": "RT",
        }

    # 4) Run finish (make finish) to get final QoR
    finish_result = runner.run_finish(params, variant, 1)
    elapsed = time.monotonic() - t_start
    qor = parse_qor(cfg, variant)

    outcome = {
        "action": action_key,
        "mode": "checkpoint_fork",
        "variant": variant,
        "ok": finish_result.ok,
        "elapsed_s": round(elapsed, 1),
        "qor": qor_to_dict(qor),
        "error": finish_result.error,
        "failed_stage": finish_result.failed_stage,
    }

    if finish_result.ok:
        log.info("Fork %s OK (%.1fs).  WNS=%.1f ps",
                 action_key, elapsed, qor.wns_ps if qor else float("nan"))
    else:
        log.error("Fork %s FAILED (%.1fs): %s", action_key, elapsed, finish_result.error)
    return outcome


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # Report directory
    design_dir = get_design_runs_dir(PLATFORM, DESIGN)
    report_dir = design_dir / "checkpoint_fork"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = report_dir / ts
    run_dir.mkdir(parents=True, exist_ok=True)

    setup_logging(run_dir)

    log.info("=== Stage C Checkpoint-Fork Verification ===")
    log.info("Platform: %s  Design: %s", PLATFORM, DESIGN)
    log.info("Report dir: %s", run_dir)

    cfg = FrameworkConfig(
        platform=PLATFORM,
        design=DESIGN,
        run_dir=run_dir,
    )

    report: dict = {
        "experiment": "stage-c-checkpoint-fork-v1",
        "platform": PLATFORM,
        "design": DESIGN,
        "checkpoint_stage": CHECKPOINT_STAGE,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "actions": {},
        "results": {},
    }

    # ---- Step 1: Baseline ----
    baseline = run_baseline(cfg)
    report["baseline"] = baseline

    # ---- Step 2 & 3: For each action, run fork + full restart ----
    for action_key in ACTIONS:
        log.info("")
        log.info(">>> Action: %s", action_key)

        # Full restart
        restart = run_full_restart(cfg, action_key)
        # Checkpoint fork
        fork = run_checkpoint_fork(cfg, action_key)

        report["actions"][action_key] = {
            "full_restart": restart,
            "checkpoint_fork": fork,
        }

        # ---- Compare ----
        if restart["ok"] and fork["ok"]:
            equal, reason = qor_equal(restart["qor"], fork["qor"])
            time_saved = restart["elapsed_s"] - fork["elapsed_s"]
            pct = (time_saved / restart["elapsed_s"] * 100) if restart["elapsed_s"] > 0 else 0

            report["results"][action_key] = {
                "qor_match": equal,
                "qor_match_reason": reason,
                "full_restart_elapsed_s": restart["elapsed_s"],
                "fork_elapsed_s": fork["elapsed_s"],
                "time_saved_s": round(time_saved, 1),
                "time_saved_pct": round(pct, 1),
                "passed": equal and time_saved > 0,
            }

            status = "PASS" if (equal and time_saved > 0) else "FAIL"
            log.info(">>> %s: %s | QoR match=%s | time_saved=%.1fs (%.1f%%)",
                     action_key, status, equal, time_saved, pct)
            if not equal:
                log.warning(">>> QoR MISMATCH: %s", reason)
        else:
            report["results"][action_key] = {
                "passed": False,
                "error": f"restart_ok={restart['ok']} fork_ok={fork['ok']}",
                "restart_error": restart.get("error"),
                "fork_error": fork.get("error"),
            }
            log.error(">>> %s: FAIL (restart_ok=%s, fork_ok=%s)",
                      action_key, restart["ok"], fork["ok"])

    # ---- Final report ----
    report["finished_at"] = datetime.now(timezone.utc).isoformat()

    # Overall verdict
    all_passed = all(r.get("passed", False) for r in report["results"].values())
    report["verdict"] = "PASS" if all_passed and report["results"] else "FAIL"

    report_path = run_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    log.info("")
    log.info("=== Experiment complete: %s ===", report["verdict"])
    log.info("Report: %s", report_path)

    if not all_passed:
        sys.exit(1)


if __name__ == "__main__":
    main()
