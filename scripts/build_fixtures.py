"""B5: Build real-world test fixtures from a gcd baseline run.

Reads the latest ORFS run artifacts, creates TrialRecord + StageResults +
CheckpointRef, and persists them as test fixtures.

Usage (after running --baseline-only --design gcd):
    python3 build_fixtures.py
"""

import json, os, sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent)
                   if "__file__" in dir() else ".")

from schemas.trial import TrialRecord, StageResult, CheckpointRef, FailureClass
from trial_manager import TrialManager
from checkpoint_manager import CheckpointManager

# ---- Config ----
RUNS_DIR = Path("/home/cheatnut/OpenROAD-flow-scripts/flow/agenticpd/runs")
FLOW_DIR = Path("/home/cheatnut/OpenROAD-flow-scripts/flow")
PLATFORM = "sky130hd"
DESIGN = "gcd"
VARIANT = "agenticpd_iter0"

# Find the latest run
run_dirs = sorted(d for d in RUNS_DIR.iterdir() if d.is_dir() and (d / "history.json").exists())
if not run_dirs:
    print("No run with history.json found.")
    sys.exit(1)
run_dir = run_dirs[-1]
print(f"Using run: {run_dir.name}")

# ---- Load history.json ----
history = json.loads((run_dir / "history.json").read_text())
entry = history[0]  # baseline is always entry 0
print(f"  variant={entry['variant']}, elapsed_s={entry.get('elapsed_s', '?')}")
print(f"  QoR: {entry['qor']}")

# ---- Load 6_report.json for richer metrics ----
report_json = FLOW_DIR / "logs" / PLATFORM / DESIGN / VARIANT / "6_report.json"
report_data = json.loads(report_json.read_text()) if report_json.exists() else {}
print(f"  6_report.json: {report_json.exists()}")

# ---- Build TrialRecord ----
trial = TrialRecord(
    trial_id="fixture-gcd-001",   # fixed ID for test reproducibility
    experiment_id="stage-b-fixture",
    parent_trial_id=None,
    branch_stage=None,
    status="ok",
    start_time="2026-07-27T16:04:26+00:00",
    end_time="2026-07-27T16:06:44+00:00",
    params=entry.get("params", {}),
    param_diff={},
    final_qor=entry.get("qor"),
    config_hash=None,
    env_hash=None,
    artifact_dir=str(run_dir),
)

# ---- Build StageResults from history ----
# The history.json has a "stage_qor" dict with per-stage intermediate timing
# but doesn't record per-stage elapsed time.  We know the total was ~138s.
# For the fixture we model approximate per-stage breakdown.
raw_stage_qor = entry.get("stage_qor", {})
stage_durations = {
    "FP":  ... ,  # unknown from history alone — needs ORFS make log parsing
    "PL":  ... ,
    "CTS": ... ,
    "RT":  ... ,
    "finish": ... ,
}

# Since history.json doesn't have per-stage timing, we mark stages as "ok"
# with elapsed_s = 0 (real per-stage timing requires parsing make logs,
# which is Stage C work).  We record the stage_qor we DO have.
stage_order = ["FP", "PL", "CTS", "RT", "finish"]
stage_results = []
for stage in stage_order:
    qor_for_stage = {}
    for key, val in raw_stage_qor.items():
        # Map stage prefixes: 2_*=FP, 3_*=PL, 4_*=CTS, 5_*=RT
        prefix_map = {"2_": "FP", "3_": "PL", "4_": "CTS", "5_": "RT"}
        for prefix, s in prefix_map.items():
            if key.startswith(prefix):
                if s == stage:
                    qor_for_stage[key] = val
    stage_results.append(StageResult(
        stage=stage,
        status="ok",
        elapsed_s=0.0,   # NOT YET AVAILABLE — needs Stage C ORFS log parsing
        exit_code=0,
        stage_qor=qor_for_stage,
        log_path=f"iter0_{stage}.make.log",
    ))
trial.stage_results = stage_results

# ---- Build CheckpointRef for the finish stage ----
cm = CheckpointManager(FLOW_DIR)
param_hash = CheckpointManager.param_hash(entry.get("params", {}))
cp = cm.create(
    trial=trial,
    stage="finish",
    platform=PLATFORM,
    design=DESIGN,
    variant=VARIANT,
    param_hash=param_hash,
)
trial.checkpoint = cp
print(f"  Checkpoint: {cp.checkpoint_id} ({len(cp.artifact_manifest)} files)")

# ---- Persist with TrialManager ----
mgr = TrialManager(run_dir.parent)  # runs/ directory
mgr._write_trial(trial)
print(f"  Trial written to {trial.artifact_dir}/trial.json")

# ---- Save as test fixture ----
fixture_dir = Path("/home/cheatnut/OpenROAD-flow-scripts/flow/agenticpd/tests/fixtures/stage_b")
fixture_dir.mkdir(parents=True, exist_ok=True)

# Save trial.json
trial_json = fixture_dir / "ok_trial.json"
trial_json.write_text(json.dumps(trial.to_dict(), ensure_ascii=False, indent=2))
print(f"  Fixture: {trial_json}")

# Save checkpoint.json
cp_json = fixture_dir / "ok_checkpoint.json"
cp_json.write_text(json.dumps(cp.to_dict(), ensure_ascii=False, indent=2))
print(f"  Fixture: {cp_json}")

# ---- Also build a failed_trial fixture ----
failed_trial = TrialRecord(
    trial_id="fixture-gcd-002",
    experiment_id="stage-b-fixture",
    parent_trial_id="fixture-gcd-001",
    branch_stage="PL",
    status="failed",
    failure=FailureClass.TOOL_CRASH,
    error_message="Simulated PL crash for fixture testing",
    stage_results=[
        StageResult("FP", "ok", 10.0, 0, stage_qor={"2_1_floorplan_ws_ps": -1154.1}),
        StageResult("PL", "failed", 15.0, -11, failure=FailureClass.TOOL_CRASH,
                   error_message="SIGSEGV in openroad", stage_qor={}),
        StageResult("CTS", "skipped", 0.0),
        StageResult("RT", "skipped", 0.0),
        StageResult("finish", "skipped", 0.0),
    ],
    artifact_dir=str(run_dir.parent / "fixture-gcd-002"),
)
failed_json = fixture_dir / "failed_trial.json"
failed_json.write_text(json.dumps(failed_trial.to_dict(), ensure_ascii=False, indent=2))
print(f"  Fixture: {failed_json}")

# ---- Summary ----
print(f"\n=== B5 fixtures created ===")
print(f"  ok_trial.json       — complete gcd baseline run with checkpoint")
print(f"  ok_checkpoint.json   — finish-stage checkpoint with real artifact hashes")
print(f"  failed_trial.json    — simulated PL crash (branch from CTS)")
print(f"\nLimitation: per-stage elapsed_s = 0 for ok_trial.")
print(f"Stage C (ORFS log parsing) will provide real per-stage timing.")
