#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
trial_reproduce.py — Reproduce a trial from its recorded parameters.

Given a trial ID and the session directory, this tool extracts the exact
parameter set used in that trial and re-runs ORFS with them.  The resulting
QoR is compared side-by-side with the recorded QoR.

Usage:
    # List trials in a session
    python3 tools/trial_reproduce.py --runs-dir runs/sky130hd_gcd/20260727_214023 --list

    # Reproduce a specific trial
    python3 tools/trial_reproduce.py <trial_id> --runs-dir runs/sky130hd_gcd/20260727_214023

    # Reproduce and also export the result
    python3 tools/trial_reproduce.py <trial_id> --runs-dir ... --export

Requirements:
    - ORFS must be set up and runnable (``make DESIGN_CONFIG=...`` works).
    - The trial must have ``status == "ok"`` and contain a full ``params`` dict
      (trials from stage C onwards).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage import TrialManager
from core.utils import QoR, qor_is_better


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _load_params_from_trial(trial_json: Path) -> Optional[Dict[str, Dict[str, object]]]:
    """Extract the full 4-stage params dict from a trial.json file."""
    if not trial_json.is_file():
        return None
    data = json.loads(trial_json.read_text(encoding="utf-8"))
    params = data.get("params")
    if not params or not isinstance(params, dict):
        return None
    # Expect FP/PL/CTS/RT keys
    expected = {"FP", "PL", "CTS", "RT"}
    if not expected.issubset(params.keys()):
        return None
    return params


def _format_qor(qor: Optional[Dict]) -> str:
    """Pretty-print a QoR dict."""
    if not qor:
        return "N/A"
    return (f"WNS={qor.get('wns_ps', '?'):.1f}ps  "
            f"TNS={qor.get('tns_ps', '?'):.1f}ps  "
            f"Area={qor.get('area_um2', '?'):.1f}um2  "
            f"Power={qor.get('power_w', '?'):.4f}mW")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reproduce a trial from its recorded parameters",
    )
    parser.add_argument(
        "trial_id", nargs="?", default=None,
        help="Trial ID to reproduce (omit with --list to show available trials)",
    )
    parser.add_argument(
        "--runs-dir", required=True,
        help="Path to the session directory (e.g. runs/sky130hd_gcd/20260727_214023)",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="List all reproducible trials in the session without running anything",
    )
    parser.add_argument(
        "--export", action="store_true",
        help="After reproduction, export the result to agenticpd_best/",
    )
    parser.add_argument(
        "--variant-suffix", default="_repro",
        help="Suffix appended to the original variant name for the reproduction run "
             "(default: _repro)",
    )
    args = parser.parse_args()

    runs_dir = Path(args.runs_dir)
    if not runs_dir.is_absolute():
        runs_dir = Path(__file__).resolve().parent.parent / args.runs_dir
    if not runs_dir.is_dir():
        sys.exit(f"Error: runs directory not found: {runs_dir}")

    mgr = TrialManager(runs_dir)

    # --list mode
    if args.list or args.trial_id is None:
        trials = mgr.list_all()
        if not trials:
            print(f"No trials found in {runs_dir}")
            return
        print(f"Trials in {runs_dir}:")
        print(f"{'Trial ID':<12} {'Status':<8} {'QoR (WNS/TNS/Area/Power)':<55} {'Elapsed':<10}")
        print("-" * 85)
        for t in trials:
            qor_str = _format_qor(t.final_qor) if t.status == "ok" else "N/A"
            has_params = bool(t.params and "FP" in t.params)
            note = "" if has_params else " [no params — cannot reproduce]"
            print(f"{t.trial_id[:12]:<12} {t.status:<8} {qor_str:<55} {t.elapsed_s:<10.1f}s{note}")
        return

    trial_id = args.trial_id

    # Load the trial record
    trial = mgr.get(trial_id)
    if trial is None:
        # Try partial match
        all_trials = mgr.list_all()
        matches = [t for t in all_trials if t.trial_id.startswith(trial_id)]
        if len(matches) == 1:
            trial = matches[0]
            trial_id = trial.trial_id
        elif len(matches) > 1:
            sys.exit(f"Ambiguous prefix '{trial_id}' matches {len(matches)} trials.  "
                     f"Use a longer prefix or the full ID.")
        else:
            sys.exit(f"Trial '{trial_id}' not found in {runs_dir}.  "
                     f"Use --list to see available trials.")

    if trial.status != "ok":
        sys.exit(f"Trial {trial_id} status is '{trial.status}', not 'ok'.  "
                 f"Cannot reproduce a failed/running trial.")

    # Extract params
    params = trial.params
    if not params or not isinstance(params, dict) or "FP" not in params:
        # Fallback: try loading from trial.json directly (old format may differ)
        trial_json = runs_dir / trial_id / "trial.json"
        params = _load_params_from_trial(trial_json)
    if not params:
        sys.exit(
            f"Trial {trial_id} has no full params stored.\n"
            f"This trial was created before the stage-C params storage fix.\n"
            f"Params can still be reconstructed from tree.json, but that requires\n"
            f"the full session context (see docs/ for details)."
        )

    # --- Ready to reproduce ---
    print(f"Reproducing trial {trial_id[:12]}...")
    print(f"  Original QoR: {_format_qor(trial.final_qor)}")
    print(f"  Params:")
    for stage in ["FP", "PL", "CTS", "RT"]:
        p = params.get(stage, {})
        if p:
            print(f"    {stage}: {json.dumps(p)}")

    # Build ORFS runner
    from orfs.interface import ORFSRunner
    from config import FrameworkConfig

    # Derive platform/design from runs_dir path: runs/<platform>_<design>/<ts>
    design_dir = runs_dir.parent.name  # e.g. "sky130hd_gcd"
    platform, design = design_dir.split("_", 1)
    cfg = FrameworkConfig(platform=platform, design=design, run_dir=runs_dir)
    runner = ORFSRunner(cfg)

    # Run with a suffixed variant name
    original_variant = trial.params.get("_variant", f"repro_{trial_id[:8]}")
    repro_variant = original_variant + args.variant_suffix

    print(f"\n  Running ORFS with variant = {repro_variant} ...")
    print(f"  (this will take several minutes for a real design)")
    print()

    result = runner.run_flow(params, repro_variant, iteration=999)

    # --- Compare ---
    print()
    print("=" * 60)
    print("Reproduction complete.")
    print(f"  Original  QoR: {_format_qor(trial.final_qor)}")
    print(f"  Reproduced QoR: {_format_qor(result.qor.to_dict() if result.qor else None)}")

    if result.qor and trial.final_qor:
        # qor_is_better() expects QoR objects, not plain dicts
        recorded_qor = QoR.from_dict(trial.final_qor)
        actual_qor = result.qor
        recorded = trial.final_qor
        actual = actual_qor.to_dict()
        # Compute deltas
        for key, label in [("wns_ps", "WNS"), ("tns_ps", "TNS"),
                           ("area_um2", "Area"), ("power_w", "Power")]:
            old_v = recorded.get(key)
            new_v = actual.get(key)
            if old_v is not None and new_v is not None:
                delta = new_v - old_v
                print(f"  Δ{label}: {delta:+.2f}")
        if qor_is_better(actual_qor, recorded_qor):
            print("  → Reproduced result is BETTER than the original.")
        elif qor_is_better(recorded_qor, actual_qor):
            print("  → Reproduced result is WORSE than the original "
                  "(expected within tolerance).")
        else:
            print("  → Results are equivalent (within tolerance).")

    # Export if requested
    if args.export and result.ok:
        runner.export_best(repro_variant, {"qor": result.qor})
        print(f"\nExported to {cfg.results_dir(cfg.best_variant_name)}")


if __name__ == "__main__":
    main()
