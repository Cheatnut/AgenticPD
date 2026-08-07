#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""checkpoint_fork_core.py — checkpoint-fork vs full-restart verification core.

Reproducible checkpoint-fork vs full-restart comparison.  Implements the
requirement from the project plan:

  "fork two different actions from the same checkpoint, run each to
   post-route, and compare with full restart in both QoR and elapsed time."

Design:
    - Baseline run as individual stages (FP->PL->CTS->RT->finish) to produce
      real per-stage StageResult records with command, timestamps, and QoR.
    - CheckpointManager.create() after CTS stage -> formal checkpoint with
      SHA-256 manifest.  CheckpointManager.verify() confirms integrity.
    - Action A (NEGATIVE CONTROL): FASTROUTE_LAYER_ADJUSTMENT change.
      is_compatible() correctly REJECTS (param affects FP per make_tracks.tcl).
    - Action B (POSITIVE CONTROL): GRT_CONGESTION_ITERATIONS change.
      is_compatible() accepts, fork runs RT+finish, QoR must match restart.
    - Experiment parameters read from YAML config; CLI overrides available.

Pass criteria:
    - All CheckpointManager methods (create/verify/is_compatible) exercised.
    - Action A correctly rejected by is_compatible().
    - Action B: fork QoR == full restart QoR within tolerance, time saved > 0.
    - Baseline trial has real StageResult data (no fake 0.0 elapsed_s).

Usage (run from flow/ directory)::

    python3 agenticpd/tools/checkpoint_fork_verify.py [--config <yaml>]

All results are written to ``agenticpd/runs/<platform>_<design>/checkpoint_fork/``
as a structured JSON report + checkpoint.json evidence.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

# Ensure the agenticpd package is importable when run from flow/
_SCRIPT_DIR = Path(__file__).resolve().parent  # agenticpd/tools/
_AGENTICPD_DIR = _SCRIPT_DIR.parent            # agenticpd/
_FLOW_DIR = _AGENTICPD_DIR.parent              # flow/
if str(_FLOW_DIR) not in sys.path:
    sys.path.insert(0, str(_FLOW_DIR))
if str(_AGENTICPD_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENTICPD_DIR))

import yaml

from config import (
    FrameworkConfig, STAGES,
    get_design_runs_dir,
)
from core.models import (
    TrialRecord, StageResult, CheckpointRef, FailureClass,
)
from storage import TrialManager, CheckpointManager
from orfs.interface import ORFSRunner
from orfs.parser import parse_qor
from orfs.runner import execute_stage
from core.utils import QoR

log = logging.getLogger("cp_fork_verify")

# ---------------------------------------------------------------------------
# Hardcoded defaults (overridden by YAML config)
# ---------------------------------------------------------------------------

_DEFAULT_YAML = str(_AGENTICPD_DIR / "configs" / "experiments" /
                    "checkpoint-fork.yaml")

# Stage order for compatibility computation.
_STAGE_ORDER = ["FP", "PL", "CTS", "RT"]

# Required top-level YAML keys.  Missing keys cause immediate failure.
from tools.checkpoint_fork_config import (
    build_params,
    qor_equal,
    qor_to_dict,
)


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


def clean_downstream_from_stage(cfg: FrameworkConfig, variant: str,
                                from_stage: str) -> int:
    """Manually delete all files with prefix >= from_stage from results/logs/reports.

    ORFS ``clean_<stage>`` targets do not reliably respect FLOW_VARIANT in all
    ORFS versions, so we nuke downstream artifacts directly.
    """
    stage_prefix: dict[str, int] = {"FP": 2, "PL": 3, "CTS": 4, "RT": 5}
    min_prefix = stage_prefix.get(from_stage)
    if min_prefix is None:
        return 0
    removed = 0
    import shutil as _shutil
    for get_dir in (cfg.results_dir, cfg.logs_dir, cfg.reports_dir):
        d = get_dir(variant)
        if not d.is_dir():
            continue
        for item in sorted(d.iterdir()):
            name = item.name
            should_remove = name.startswith("6_")
            if not should_remove:
                for pfx in range(min_prefix, 10):
                    if name.startswith(f"{pfx}_"):
                        should_remove = True
                        break
            if should_remove:
                (_shutil.rmtree(item) if item.is_dir() else item.unlink())
                removed += 1
        for extra in ("route.guide",):
            p = d / extra
            if p.is_file():
                p.unlink(); removed += 1
    return removed


# ---------------------------------------------------------------------------
# Experiment steps
# ---------------------------------------------------------------------------

def run_baseline_per_stage(cfg: FrameworkConfig, runner: ORFSRunner,
                           tm: TrialManager, cm: CheckpointManager,
                           baseline_params: dict,
                           platform: str, design: str,
                           experiment_id: str = "unknown",
                           baseline_variant: str = "agenticpd_baseline",
                           checkpoint_stage: str = "CTS") -> dict:
    """Run baseline as individual stages to get real per-stage StageResults.

    Returns dict with trial_id, checkpoint (evidence dict), elapsed_s, qor.
    """
    log.info("=" * 60)
    log.info("STEP 1: BASELINE (per-stage: FP->PL->CTS->RT->finish)")
    log.info("=" * 60)

    variant = baseline_variant
    trial = tm.create(
        experiment_id=experiment_id,
        iteration=0,
    )

    t0 = time.monotonic()
    stage_results: list[StageResult] = []
    cp_evidence: dict = {}
    final_qor = None

    for stage in STAGES:
        sr = execute_stage(cfg, stage, baseline_params, variant, 0)
        stage_results.append(sr)
        log.info("  %s: %s (%.1fs)", stage, sr.status, sr.elapsed_s)

        if sr.status == "failed":
            trial.stage_results = stage_results
            trial.status = "failed"
            tm.update(trial)
            log.error("Baseline %s FAILED: %s", stage, sr.error_message)
            sys.exit(1)

        # Create checkpoint immediately after checkpoint_stage completes,
        # before continuing to downstream stages.  This ensures the
        # checkpoint is persisted even if a downstream stage crashes.
        if stage == checkpoint_stage:
            param_hash = CheckpointManager.param_hash(baseline_params)
            cp = cm.create(
                trial=trial, stage=checkpoint_stage,
                platform=platform, design=design,
                variant=variant, param_hash=param_hash,
                runs_dir=cfg.run_dir,
            )
            trial.checkpoint = cp
            tm.update(trial)

            cp_ok, cp_errors = cm.verify(cp)
            if cp_ok:
                log.info("Checkpoint %s created & verified: %d files intact",
                         cp.checkpoint_id, len(cp.artifact_manifest))
                cp_evidence = {
                    "checkpoint_id": cp.checkpoint_id,
                    "stage": cp.stage,
                    "source_trial_id": cp.source_trial_id,
                    "param_hash": cp.param_hash,
                    "orfs_commit": cp.orfs_commit,
                    "created_at": cp.created_at,
                    "artifact_manifest": cp.artifact_manifest,
                    "artifact_dir": cp.artifact_dir,
                    "verified": True,
                    "verify_errors": [],
                }
            else:
                log.error("Checkpoint verification FAILED: %s", cp_errors)
                cp_evidence = {
                    "checkpoint_id": cp.checkpoint_id,
                    "stage": cp.stage,
                    "source_trial_id": cp.source_trial_id,
                    "param_hash": cp.param_hash,
                    "orfs_commit": cp.orfs_commit,
                    "created_at": cp.created_at,
                    "artifact_manifest": cp.artifact_manifest,
                    "artifact_dir": cp.artifact_dir,
                    "verified": False,
                    "verify_errors": cp_errors,
                }
                # Do NOT exit — the checkpoint metadata is still recorded;
                # downstream stages can proceed.  The caller checks
                # checkpoint_verified before attempting forks.

    # Run finish to get final QoR
    finish = runner.run_finish(baseline_params, variant, 0)
    elapsed = time.monotonic() - t0
    final_qor = parse_qor(cfg, variant)

    if not finish.ok:
        # Persist failed trial before exit — audit trail must be complete.
        # Append a failed finish StageResult so failed_stage resolves correctly.
        finish_sr = StageResult(
            stage="finish", status="failed",
            elapsed_s=finish.elapsed_s,
            failure=FailureClass.from_exit_code(-1) if finish.failed_stage else FailureClass.QOR_INCOMPLETE,
            error_message=finish.error,
        )
        stage_results.append(finish_sr)
        trial.stage_results = stage_results
        trial.status = "failed"
        trial.failure = finish_sr.failure
        trial.error_message = finish.error
        trial.end_time = datetime.now(timezone.utc).isoformat()
        tm.update(trial)
        log.error("Baseline finish FAILED: %s (trial %s updated, failed_stage=%s)",
                  finish.error, trial.trial_id, trial.failed_stage)
        sys.exit(1)

    # Append successful finish StageResult so elapsed covers all 5 stages
    # and per-stage audit trail is complete.
    finish_sr = StageResult(
        stage="finish", status="ok",
        elapsed_s=finish.elapsed_s,
        exit_code=finish.exit_code,
        log_path=finish.make_log_path,
        command=finish.command,
        start_time=finish.start_time,
        end_time=finish.end_time,
        report_path=finish.report_path,
    )
    stage_results.append(finish_sr)

    # Populate trial record with real data
    trial.stage_results = stage_results
    trial.status = "ok"
    trial.final_qor = qor_to_dict(final_qor)
    trial.params = baseline_params
    tm.update(trial)

    log.info("Baseline OK (%.1fs). WNS=%.1f ps", elapsed,
             final_qor.wns_ps if final_qor else float("nan"))

    # Checkpoint already created immediately after checkpoint_stage above.
    checkpoint_verified = cp_evidence.get("verified", False)

    return {
        "ok": True, "elapsed_s": round(elapsed, 1),
        "qor": qor_to_dict(final_qor), "variant": variant,
        "trial_id": trial.trial_id,
        "stage_elapsed_s": sum(sr.elapsed_s for sr in stage_results),
        "checkpoint": cp_evidence,
        "checkpoint_verified": checkpoint_verified,
    }


def run_full_restart(cfg: FrameworkConfig, action: dict, action_key: str,
                     baseline_params: dict) -> dict:
    """Full RTL-to-GDS run with one parameter changed."""
    variant = f"cpfv_full_{action_key.lower()}"
    params = build_params(action, baseline_params)

    log.info("-" * 50)
    log.info("FULL RESTART: %s (%s)", action_key, action["description"])
    log.info("Variant: %s", variant)

    runner = ORFSRunner(cfg)
    t0 = time.monotonic()
    result = runner.run_flow(params, variant, 1)
    elapsed = time.monotonic() - t0
    qor = parse_qor(cfg, variant)

    return {
        "action": action_key, "mode": "full_restart",
        "variant": variant, "ok": result.ok,
        "elapsed_s": round(elapsed, 1), "qor": qor_to_dict(qor),
        "error": result.error, "failed_stage": result.failed_stage,
        "param_changed": {action["param_name"]: action["changed_value"]},
    }


def run_checkpoint_fork(cfg: FrameworkConfig, action: dict, action_key: str,
                        baseline_variant: str, baseline_cp_data: dict,
                        baseline_params: dict,
                        checkpoint_stage: str = "CTS") -> dict:
    """Fork from CTS checkpoint: copy -> clean downstream -> run downstream."""
    variant = f"cpfv_fork_{action_key.lower()}"
    params = build_params(action, baseline_params)

    log.info("-" * 50)
    log.info("CHECKPOINT FORK: %s (%s)", action_key, action["description"])
    log.info("Fork from: %s @%s -> variant: %s", baseline_variant, checkpoint_stage, variant)

    # Rebuild CheckpointRef from evidence
    cp = CheckpointRef(
        checkpoint_id=baseline_cp_data["checkpoint_id"],
        source_trial_id=baseline_cp_data["source_trial_id"],
        stage=baseline_cp_data["stage"],
        param_hash=baseline_cp_data["param_hash"],
        orfs_commit=baseline_cp_data["orfs_commit"],
        created_at=baseline_cp_data["created_at"],
        artifact_manifest=baseline_cp_data["artifact_manifest"],
        artifact_dir=baseline_cp_data.get("artifact_dir"),
    )

    runner = ORFSRunner(cfg)
    cm = CheckpointManager(cfg.flow_dir)
    t_start = time.monotonic()

    # Re-verify checkpoint integrity before fork
    cp_ok, cp_errors = cm.verify(cp)
    if not cp_ok:
        return {
            "action": action_key, "mode": "checkpoint_fork",
            "variant": variant, "ok": False,
            "elapsed_s": round(time.monotonic() - t_start, 1),
            "error": f"pre-fork checkpoint verify failed: {cp_errors}",
        }

    # Copy baseline artifacts
    try:
        runner.copy_parent_results(baseline_variant, variant)
    except FileNotFoundError as e:
        return {
            "action": action_key, "mode": "checkpoint_fork",
            "variant": variant, "ok": False,
            "elapsed_s": round(time.monotonic() - t_start, 1),
            "error": f"copy_parent_results: {e}",
        }

    # Manually clean downstream files (ORFS clean_* targets are unreliable
    # with FLOW_VARIANT in some ORFS versions)
    clean_downstream_from_stage(cfg, variant, action["stage"])

    # Run downstream stages
    rt_sr = execute_stage(cfg, action["stage"], params, variant, 1)
    if rt_sr.status == "failed":
        return {
            "action": action_key, "mode": "checkpoint_fork",
            "variant": variant, "ok": False,
            "elapsed_s": round(time.monotonic() - t_start, 1),
            "error": f"{action['stage']} stage: {rt_sr.error_message}",
            "failed_stage": action["stage"],
        }

    finish = runner.run_finish(params, variant, 1)
    elapsed = time.monotonic() - t_start
    qor = parse_qor(cfg, variant)

    return {
        "action": action_key, "mode": "checkpoint_fork",
        "variant": variant, "ok": finish.ok,
        "elapsed_s": round(elapsed, 1), "qor": qor_to_dict(qor),
        "error": finish.error, "failed_stage": finish.failed_stage,
        "param_changed": {action["param_name"]: action["changed_value"]},
    }


# ---------------------------------------------------------------------------
# Acceptance validation
# ---------------------------------------------------------------------------

def _validate_acceptance(report: dict, acceptance: dict, actions: dict) -> list[str]:
    """Cross-check experiment results against YAML acceptance gates.

    Returns a list of human-readable error messages; empty list = all gates pass.
    """
    errors: list[str] = []

    # -- CheckpointManager usage --
    if acceptance.get("checkpoint_manager_used") and not report.get("checkpoint_manager_used"):
        errors.append("checkpoint_manager_used: expected True but not set in report")

    baseline = report.get("baseline", {})
    if acceptance.get("require_checkpoint_created") and not baseline.get("checkpoint"):
        errors.append("require_checkpoint_created: no checkpoint in baseline")

    if acceptance.get("require_checkpoint_verified") and not baseline.get("checkpoint_verified"):
        errors.append("require_checkpoint_verified: baseline checkpoint not verified")

    if acceptance.get("require_compatibility_checked"):
        for action_key in actions:
            action_report = report.get("actions", {}).get(action_key, {})
            if "is_compatible" not in action_report:
                errors.append(f"require_compatibility_checked: is_compatible not recorded for {action_key}")

    # -- Map action keys to acceptance prefixes (A_*, B_*) --
    action_keys = sorted(actions.keys())
    prefixes = ["action_a", "action_b"]
    if len(action_keys) != len(prefixes):
        errors.append(f"Expected {len(prefixes)} actions, got {len(action_keys)}: {action_keys}")
    key_to_prefix = dict(zip(action_keys, prefixes)) if len(action_keys) == len(prefixes) else {}

    for action_key, prefix in key_to_prefix.items():
        action_report = report.get("actions", {}).get(action_key, {})
        result = report.get("results", {}).get(action_key, {})

        # is_compatible gate
        expected_compat = acceptance.get(f"{prefix}_is_compatible")
        if expected_compat is not None:
            actual_compat = action_report.get("is_compatible")
            if actual_compat != expected_compat:
                errors.append(
                    f"{action_key}: {prefix}_is_compatible expected {expected_compat}, got {actual_compat}")

        # Fork rejected gate (negative control)
        expected_rejected = acceptance.get(f"{prefix}_fork_rejected")
        if expected_rejected is not None:
            fork_block = action_report.get("checkpoint_fork", {})
            actual_rejected = fork_block.get("rejected_by_checkpoint_manager", False)
            if actual_rejected != expected_rejected:
                errors.append(
                    f"{action_key}: {prefix}_fork_rejected expected {expected_rejected}, got {actual_rejected}")

        # Full restart must pass gate (negative control)
        if acceptance.get(f"{prefix}_full_restart_must_pass"):
            restart_block = action_report.get("full_restart", {})
            if not restart_block.get("ok"):
                errors.append(f"{action_key}: {prefix}_full_restart_must_pass but full restart failed")

        # QoR match gate (positive control)
        if acceptance.get(f"{prefix}_require_qor_match"):
            if not result.get("qor_match", False):
                errors.append(
                    f"{action_key}: {prefix}_require_qor_match but qor_match={result.get('qor_match')} "
                    f"({result.get('qor_match_reason', 'no reason')})")

        # Time saved gate (positive control)
        if acceptance.get(f"{prefix}_require_time_saved_gt_zero"):
            time_saved = result.get("time_saved_s", 0)
            if time_saved <= 0:
                errors.append(
                    f"{action_key}: {prefix}_require_time_saved_gt_zero but time_saved_s={time_saved}")

    return errors


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
