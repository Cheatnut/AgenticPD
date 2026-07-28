#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""trial_inspect.py — CLI for inspecting trial records.

Usage:
    python3 tools/trial_inspect.py --sessions <platform> <design>  # List sessions
    python3 tools/trial_inspect.py --list <platform> <design> <seq>  # List trials
    python3 tools/trial_inspect.py <trial_id>                 # Inspect by ID
    python3 tools/trial_inspect.py <trial_id> --stages        # + per-stage detail
    python3 tools/trial_inspect.py --latest <platform> <design>  # Latest trial
    python3 tools/trial_inspect.py --failed <platform> <design>  # Failed trials
"""

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import RUNS_DIR, get_design_runs_dir
from schemas.trial import TrialRecord
from managers import TrialManager


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def _list_sessions(platform: str, design: str) -> List[Path]:
    """Return sorted list of session directories for a design."""
    d = get_design_runs_dir(platform, design)
    if not d.is_dir():
        return []
    return sorted(
        (p for p in d.iterdir() if p.is_dir() and not p.name.startswith(".")),
        key=lambda p: p.name,
    )


def _resolve_session(platform: str, design: str, seq: str) -> Path:
    """Find a session directory by sequence number (e.g. '001')."""
    seq = seq.lstrip("0") or "0"
    for s in _list_sessions(platform, design):
        if s.name.startswith(f"{int(seq):03d}_"):
            return s
    sys.exit(f"Session {seq} not found under runs/{platform}_{design}/.")


def _find_trial_by_id(trial_id: str) -> Optional[Tuple[TrialRecord, Path]]:
    """Scan all sessions to find a trial by its ID.  Returns (record, session_dir)."""
    if not RUNS_DIR.is_dir():
        return None
    for design_dir in sorted(RUNS_DIR.iterdir()):
        if not design_dir.is_dir() or design_dir.name.startswith("."):
            continue
        for session_dir in sorted(design_dir.iterdir()):
            if not session_dir.is_dir() or session_dir.name.startswith("."):
                continue
            mgr = TrialManager(session_dir)
            trial = mgr.get(trial_id)
            if trial is not None:
                return trial, session_dir
            # Also scan directly (handles naming variations)
            for d in session_dir.iterdir():
                if not d.is_dir() or not d.name.startswith("iter-"):
                    continue
                tj = d / "trial.json"
                if not tj.is_file():
                    continue
                try:
                    t = TrialRecord.from_dict(json.loads(tj.read_text(encoding="utf-8")))
                    if t.trial_id == trial_id:
                        return t, session_dir
                except Exception:
                    pass
    return None


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def _print_trial(trial: TrialRecord, show_stages: bool = False) -> None:
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
    if trial.stage_results and show_stages:
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
        # Guard against None values — dict.get() returns None (not the default)
        # when the key exists but maps to None
        def _fmt(key: str, unit: str, spec: str) -> str:
            v = q.get(key)
            if v is None:
                return f"{'?':>{spec}} {unit}"
            return f"{v:{spec}} {unit}"
        print(f"  QoR:")
        print(f"    WNS:  {_fmt('wns_ps', 'ps', '>10.1f')}")
        print(f"    TNS:  {_fmt('tns_ps', 'ps', '>10.1f')}")
        print(f"    Area: {_fmt('area_um2', 'um2', '>10.1f')}")
        power_w = q.get("power_w")
        if power_w is not None:
            print(f"    Power:{power_w*1000:>10.4f} mW")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inspect AgenticPD trial records",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  python3 tools/trial_inspect.py --sessions sky130hd gcd\n"
               "  python3 tools/trial_inspect.py --list sky130hd gcd 001\n"
               "  python3 tools/trial_inspect.py abc12345\n"
               "  python3 tools/trial_inspect.py abc12345 --stages\n"
               "  python3 tools/trial_inspect.py --latest sky130hd gcd\n"
               "  python3 tools/trial_inspect.py --failed sky130hd gcd",
    )
    parser.add_argument("trial_id_or_platform", nargs="?", default=None,
                        help="Trial ID, or platform name (with --list/--latest/--failed/--sessions)")
    parser.add_argument("design_or_seq", nargs="?", default=None,
                        help="Design name or session sequence number")
    parser.add_argument("session_seq", nargs="?", default=None,
                        help="Session sequence number (for --list)")
    parser.add_argument("--stages", action="store_true", help="Show per-stage detail")
    parser.add_argument("--list", action="store_true", help="List trials in a session")
    parser.add_argument("--latest", action="store_true", help="Show most recent trial")
    parser.add_argument("--failed", action="store_true", help="List only failed trials")
    parser.add_argument("--sessions", action="store_true", help="List all sessions for a design")
    args = parser.parse_args()

    # ── Mode: --sessions ──
    if args.sessions:
        platform, design = args.trial_id_or_platform, args.design_or_seq
        if not platform or not design:
            sys.exit("Usage: trial_inspect.py --sessions <platform> <design>")
        sessions = _list_sessions(platform, design)
        if not sessions:
            print(f"No sessions found for {platform}/{design}.")
            return
        print(f"Sessions for {platform}/{design}:")
        for s in sessions:
            # Count trials in this session
            n_trials = 0
            jl = s / "trials.jsonl"
            if jl.is_file():
                n_trials = len(set(  # dedup by trial_id
                    json.loads(line)["trial_id"]
                    for line in jl.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ))
            print(f"  {s.name}  ({n_trials} trials)")
        return

    # ── Mode: trial_id lookup (no flag) ──
    if args.trial_id_or_platform and not any([args.list, args.latest, args.failed, args.sessions]):
        trial_id = args.trial_id_or_platform
        result = _find_trial_by_id(trial_id)
        if result is None:
            sys.exit(f"Trial '{trial_id}' not found in any session under runs/.")
        trial, session_dir = result
        print(f"[session: {session_dir.parent.name}/{session_dir.name}]")
        _print_trial(trial, show_stages=args.stages)
        return

    # ── All remaining modes need platform + design ──
    platform = args.trial_id_or_platform
    design = args.design_or_seq
    if not platform or not design:
        sys.exit("Usage: trial_inspect.py --list|--latest|--failed|--sessions <platform> <design> [seq]")

    # ── Mode: --list ──
    if args.list:
        seq = args.session_seq
        if not seq:
            # No seq given — pick latest session
            sessions = _list_sessions(platform, design)
            if not sessions:
                sys.exit(f"No sessions found for {platform}/{design}.")
            session_dir = sessions[-1]
            seq = session_dir.name.split("_")[0]
        else:
            session_dir = _resolve_session(platform, design, seq)
        mgr = TrialManager(session_dir)
        trials = mgr.list_all()
        if not trials:
            print(f"No trials found in session {seq}.")
            return
        print(f"Session {seq} ({session_dir.name}):")
        print(f"{'':4}{'Trial ID':<12} {'Status':<8} {'QoR (WNS/TNS/Area/Power)':<55} {'Elapsed':<10}")
        print("-" * 85)
        for t in trials:
            qor_str = ""
            if t.final_qor:
                qor_str = (f"WNS={t.final_qor.get('wns_ps','?'):.1f}ps  "
                           f"TNS={t.final_qor.get('tns_ps','?'):.1f}ps  "
                           f"Area={t.final_qor.get('area_um2','?'):.1f}um2  "
                           f"Power={t.final_qor.get('power_w',0)*1000:.4f}mW")
            has_params = bool(t.params and "FP" in t.params)
            note = "" if has_params else " [no params]"
            print(f"  {t.trial_id[:12]:<12} {t.status:<8} {qor_str:<55} {t.elapsed_s:<10.1f}s{note}")
        return

    # ── Mode: --latest ──
    if args.latest:
        sessions = _list_sessions(platform, design)
        if not sessions:
            sys.exit(f"No sessions found for {platform}/{design}.")
        session_dir = sessions[-1]
        mgr = TrialManager(session_dir)
        trials = mgr.list_all()
        if not trials:
            print(f"No trials in latest session ({session_dir.name}).")
            return
        print(f"[session: {platform}_{design}/{session_dir.name}]")
        _print_trial(trials[-1], show_stages=args.stages)
        return

    # ── Mode: --failed ──
    if args.failed:
        sessions = _list_sessions(platform, design)
        found_any = False
        for session_dir in sessions:
            mgr = TrialManager(session_dir)
            for t in mgr.list_all():
                if t.status == "failed":
                    if not found_any:
                        print(f"Failed trials for {platform}/{design}:")
                        found_any = True
                    print(f"  [{session_dir.name}] {t.trial_id}  "
                          f"@ {t.failed_stage or '?'}  "
                          f"{t.failure.value if t.failure else '?'}")
        if not found_any:
            print(f"No failed trials found for {platform}/{design}.")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
