#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""trial_inspect.py — Stage B: CLI for inspecting trial records.

Usage:
    python3 trial_inspect.py <trial_id>              # Show one trial
    python3 trial_inspect.py <trial_id> --stages     # Include per-stage detail
    python3 trial_inspect.py --list                  # List all trials
    python3 trial_inspect.py --latest                # Show most recent trial
    python3 trial_inspect.py --failed                # List only failed trials

Answers the six questions every trial must answer:
    1. Where did this trial come from?  (parent / branch_stage)
    2. What parameters changed?          (param_diff)
    3. How long did each stage take?     (stage_results[*].elapsed_s)
    4. Is it recoverable?                (failure / checkpoint)
    5. Where are the final files?        (artifact_dir)
    6. What is the final QoR?            (final_qor)
"""

import argparse
import sys
from pathlib import Path

# When running from tools/ subdirectory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trial_manager import TrialManager


def _print_trial(trial, show_stages: bool = False) -> None:
    """Pretty-print one TrialRecord."""
    status_icon = {"ok": "OK", "failed": "FAIL", "running": "RUN"}.get(trial.status, "?")
    print(f"{'='*60}")
    print(f"  Trial:    {trial.trial_id}  [{status_icon}]")
    print(f"  Experiment: {trial.experiment_id}")
    print(f"{'='*60}")

    # Q1: Lineage
    if trial.parent_trial_id:
        print(f"  Parent:     {trial.parent_trial_id}  @{trial.branch_stage}")
    else:
        print(f"  Parent:     (root — full restart)")

    # Q2: Params
    if trial.param_diff:
        print(f"  Changes:")
        for param, diff in trial.param_diff.items():
            print(f"    {param}: {diff['from']} -> {diff['to']}")
    elif trial.params:
        stage_count = sum(1 for s, p in trial.params.items() if p)
        print(f"  Parameters: {stage_count} stages configured")

    # Q3: Timing
    print(f"  Elapsed:    {trial.elapsed_s:.1f}s")
    if trial.stage_results:
        if show_stages:
            print(f"  Stages:")
            for sr in trial.stage_results:
                icon = {"ok": "+", "failed": "X", "skipped": "-"}.get(sr.status, "?")
                print(f"    [{icon}] {sr.stage:<8} {sr.elapsed_s:>6.1f}s"
                      + (f"  exit={sr.exit_code}" if sr.exit_code else ""))

    # Q4: Failure / recoverability
    if trial.failure and trial.failure.value != "none":
        print(f"  Failure:    {trial.failure.value} @ {trial.failed_stage}")
        if trial.error_message:
            print(f"  Error:      {trial.error_message}")
        if trial.checkpoint:
            print(f"  Recoverable: YES — checkpoint {trial.checkpoint.checkpoint_id}")
        else:
            print(f"  Recoverable: NO — no checkpoint available")
    elif trial.checkpoint:
        print(f"  Checkpoint: {trial.checkpoint.checkpoint_id} @{trial.checkpoint.stage}"
              + f" ({len(trial.checkpoint.artifact_manifest)} files)")

    # Q5: Location
    if trial.artifact_dir:
        print(f"  Artifacts:  {trial.artifact_dir}")

    # Q6: QoR
    if trial.final_qor:
        q = trial.final_qor
        print(f"  QoR:")
        print(f"    WNS:  {q.get('wns_ps', '?'):>10.1f} ps")
        print(f"    TNS:  {q.get('tns_ps', '?'):>10.1f} ps")
        print(f"    Area: {q.get('area_um2', '?'):>10.1f} um2")
        power_w = q.get("power_w")
        if power_w:
            print(f"    Power:{power_w*1000:>10.4f} mW")
    print()


def main():
    parser = argparse.ArgumentParser(description="Inspect AgenticPD trial records")
    parser.add_argument("trial_id", nargs="?", help="Trial ID to inspect")
    parser.add_argument("--stages", action="store_true", help="Show per-stage detail")
    parser.add_argument("--list", action="store_true", help="List all trials")
    parser.add_argument("--latest", action="store_true", help="Show most recent trial")
    parser.add_argument("--failed", action="store_true", help="List only failed trials")
    parser.add_argument("--runs-dir", default="runs", help="Path to runs directory")
    args = parser.parse_args()

    runs_dir = Path(args.runs_dir)
    if not runs_dir.is_absolute():
        # Resolve relative to project root
        runs_dir = Path(__file__).resolve().parent.parent / args.runs_dir

    mgr = TrialManager(runs_dir)

    if args.list:
        trials = mgr.list_all()
        if not trials:
            print("No trials found.")
            return
        for t in trials:
            status_icon = {"ok": "+", "failed": "X", "running": "~"}.get(t.status, "?")
            qor_str = ""
            if t.final_qor:
                qor_str = f" WNS={t.final_qor['wns_ps']:.0f}ps"
            print(f"  [{status_icon}] {t.trial_id}  {t.experiment_id}  {t.elapsed_s:.0f}s{qor_str}")
        return

    if args.failed:
        trials = mgr.list_by_status("failed")
        if not trials:
            print("No failed trials.")
            return
        for t in trials:
            _print_trial(t, args.stages)
        return

    if args.latest:
        trial = mgr.latest()
        if trial is None:
            print("No trials found.")
            return
        _print_trial(trial, args.stages)
        return

    if args.trial_id:
        trial = mgr.get(args.trial_id)
        if trial is None:
            print(f"Trial '{args.trial_id}' not found.")
            sys.exit(1)
        _print_trial(trial, args.stages)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
