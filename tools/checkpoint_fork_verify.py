#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checkpoint_fork_verify.py — CLI for checkpoint-fork vs full-restart verification.

Usage (run from flow/ directory)::

    python3 agenticpd/tools/checkpoint_fork_verify.py [--config <yaml>]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
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

from config import FrameworkConfig, get_design_runs_dir
from core.models import CheckpointRef
from storage import CheckpointManager, TrialManager
from orfs.interface import ORFSRunner
from tools.checkpoint_fork_core import (
    _DEFAULT_YAML,
    _validate_acceptance,
    run_baseline_per_stage,
    run_checkpoint_fork,
    run_full_restart,
    setup_logging,
)
from tools.checkpoint_fork_config import build_params, load_experiment_config, qor_equal

log = logging.getLogger("cp_fork_verify")

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Checkpoint-fork vs full-restart verification")
    ap.add_argument("--config", default=_DEFAULT_YAML,
                    help="Experiment YAML config path")
    args = ap.parse_args()

    exp_cfg = load_experiment_config(args.config)

    # Compute safe config path for logging and report (no /home/ leak).
    # Only allow paths inside the project tree; external paths use basename only.
    config_path_raw = Path(args.config).resolve()
    if config_path_raw.is_relative_to(_AGENTICPD_DIR):
        config_path_rel = str(config_path_raw.relative_to(_AGENTICPD_DIR))
    else:
        # External config — store only the filename, not the full path.
        config_path_rel = f"<external>/{config_path_raw.name}"
        log.warning("Config outside project tree: using safe display name '%s'",
                    config_path_rel)

    platform = exp_cfg["platform"]
    design = exp_cfg["design"]
    checkpoint_stage = exp_cfg["checkpoint_stage"]
    checkpoint_source = exp_cfg["checkpoint_source"]
    baseline_params = exp_cfg["baseline_params"]
    actions = exp_cfg["actions"]
    qor_tolerances = exp_cfg["qor_tolerances"]
    acceptance = exp_cfg["acceptance"]

    design_dir = get_design_runs_dir(platform, design)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = design_dir / "checkpoint_fork" / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(run_dir)

    log.info("=== Checkpoint-Fork Verification ===")
    log.info("Config: %s", config_path_rel)
    log.info("Platform: %s  Design: %s  Checkpoint: %s",
             platform, design, checkpoint_stage)
    log.info("Report dir: %s", str(run_dir.relative_to(_AGENTICPD_DIR)) if run_dir.is_relative_to(_AGENTICPD_DIR) else str(run_dir))

    cfg = FrameworkConfig(platform=platform, design=design, run_dir=run_dir)
    runner = ORFSRunner(cfg)
    tm = TrialManager(run_dir)
    cm = CheckpointManager(cfg.flow_dir)

    report: dict = {
        "experiment": exp_cfg["experiment_id"],
        "platform": platform, "design": design,
        "checkpoint_stage": checkpoint_stage,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "actions": {}, "results": {},
        "checkpoint_manager_used": True,
        "config_yaml": config_path_rel,
    }

    # ---- Step 1: Baseline (per-stage, real StageResult data) ----
    baseline = run_baseline_per_stage(
        cfg, runner, tm, cm, baseline_params, platform, design,
        experiment_id=exp_cfg["experiment_id"],
        baseline_variant=checkpoint_source,
        checkpoint_stage=checkpoint_stage,
    )
    report["baseline"] = baseline

    if not baseline.get("checkpoint_verified"):
        log.error("Baseline checkpoint verification failed — cannot proceed.")
        sys.exit(1)

    # ---- Step 2 & 3: For each action ----
    for action_key, action in actions.items():
        log.info("")
        log.info(">>> Action: %s", action_key)

        action_params = build_params(action, baseline_params)

        # CheckpointManager.is_compatible()
        cp_data = baseline["checkpoint"]
        baseline_cp = CheckpointRef(
            checkpoint_id=cp_data["checkpoint_id"],
            source_trial_id=cp_data["source_trial_id"],
            stage=cp_data["stage"],
            param_hash=cp_data["param_hash"],
            orfs_commit=cp_data["orfs_commit"],
            created_at=cp_data["created_at"],
            artifact_manifest=cp_data["artifact_manifest"],
            artifact_dir=cp_data.get("artifact_dir"),
        )
        is_compat = cm.is_compatible(baseline_cp, action_params, baseline_params)
        expect_compat = action.get("expect_compatible", True)
        log.info("is_compatible() = %s (expected %s)", is_compat, expect_compat)

        # Full restart (always run for reference QoR)
        restart = run_full_restart(cfg, action, action_key, baseline_params)

        # Fork (only if compatible)
        if is_compat:
            fork = run_checkpoint_fork(
                cfg, action, action_key,
                checkpoint_source, baseline["checkpoint"],
                baseline_params, checkpoint_stage=checkpoint_stage)
        else:
            fork = {
                "action": action_key, "mode": "checkpoint_fork",
                "variant": None, "ok": False, "elapsed_s": 0, "qor": None,
                "error": "REJECTED by CheckpointManager.is_compatible()",
                "rejected_by_checkpoint_manager": True,
            }
            log.info("Fork REJECTED by is_compatible() — correct behaviour.")

        report["actions"][action_key] = {
            "full_restart": restart,
            "checkpoint_fork": fork,
            "is_compatible": is_compat,
            "expect_compatible": expect_compat,
        }

        # Compare
        expect_match = action.get("expect_qor_match", True)
        if not fork.get("rejected_by_checkpoint_manager"):
            if restart["ok"] and fork["ok"]:
                equal, reason = qor_equal(restart["qor"], fork["qor"],
                                          wns_tol=qor_tolerances["wns_ps"],
                                          tns_tol=qor_tolerances["tns_ps"])
                time_saved = restart["elapsed_s"] - fork["elapsed_s"]
                pct = ((time_saved / restart["elapsed_s"] * 100)
                       if restart["elapsed_s"] > 0 else 0)
                passed = (equal and time_saved > 0) if expect_match else ((not equal) and time_saved > 0)
                report["results"][action_key] = {
                    "control_type": "positive" if expect_match else "negative",
                    "qor_match": equal, "qor_match_reason": reason,
                    "qor_match_expected": expect_match,
                    "full_restart_elapsed_s": restart["elapsed_s"],
                    "fork_elapsed_s": fork["elapsed_s"],
                    "time_saved_s": round(time_saved, 1),
                    "time_saved_pct": round(pct, 1),
                    "passed": passed,
                }
                log.info(">>> %s: %s | QoR match=%s (expected=%s) | time=%.1fs",
                         action_key,
                         "PASS" if passed else "FAIL",
                         equal, expect_match, time_saved)
            else:
                report["results"][action_key] = {
                    "passed": False,
                    "error": f"restart_ok={restart['ok']} fork_ok={fork['ok']}",
                }
        else:
            passed = (not expect_compat) and restart["ok"]
            report["results"][action_key] = {
                "control_type": "negative",
                "fork_rejected_by_checkpoint_manager": True,
                "is_compatible": is_compat,
                "expect_compatible": expect_compat,
                "full_restart_ok": restart["ok"],
                "full_restart_elapsed_s": restart["elapsed_s"],
                "passed": passed,
            }
            log.info(">>> %s: PASS (rejected by is_compatible)", action_key)

    # ---- Acceptance criteria validation ----
    acceptance_errors = _validate_acceptance(report, acceptance, actions)
    report["acceptance_validation"] = {
        "checked": True,
        "errors": acceptance_errors,
        "passed": len(acceptance_errors) == 0,
    }
    if acceptance_errors:
        for err in acceptance_errors:
            log.error("ACCEPTANCE FAIL: %s", err)

    # Final report
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    results_ok = all(r.get("passed", False) for r in report["results"].values())
    acceptance_ok = len(acceptance_errors) == 0
    report["verdict"] = "PASS" if (results_ok and acceptance_ok and report["results"]) else "FAIL"

    report_path = run_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    (run_dir / "checkpoint_evidence.json").write_text(
        json.dumps(baseline["checkpoint"], ensure_ascii=False, indent=2),
        encoding="utf-8")
    log.info("")
    log.info("=== Experiment complete: %s ===", report["verdict"])
    log.info("Report: %s", str(report_path.relative_to(_AGENTICPD_DIR)) if report_path.is_relative_to(_AGENTICPD_DIR) else str(report_path))
    if report["verdict"] != "PASS":
        sys.exit(1)


if __name__ == "__main__":
    main()
