# -*- coding: utf-8 -*-
"""trial_manager.py — Stage B2: Trial lifecycle manager.

Thin persistence layer over TrialRecord.  Responsibilities:

    - Create a unique trial directory under runs/<trial_id>/
    - Write trial.json (full TrialRecord) inside that directory
    - Append a one-line summary to runs/trials.jsonl (global index)
    - Read back any trial by ID or list all trials

This module does NOT execute ORFS flows — it only manages trial metadata.
The actual ORFS invocation remains in orfs_interface.py (to be refactored
in Stage C).

Usage:
    mgr = TrialManager(Path("agenticpd/runs"))
    trial = mgr.create(experiment_id="smoke-gcd-v1")
    # ... run flow, populate stage_results, final_qor ...
    mgr.update(trial)          # persist to trial.json + trials.jsonl
    mgr.get(trial.trial_id)    # load TrialRecord back
    mgr.list_all()             # all trials in index
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

# When run directly (python3 managers/trial_manager.py), ensure the parent
# agenticpd/ directory is on sys.path so relative imports resolve.
if __name__ == "__main__":
    _parent = Path(__file__).resolve().parent.parent
    if str(_parent) not in sys.path:
        sys.path.insert(0, str(_parent))

from schemas.trial import TrialRecord, StageResult, CheckpointRef, FailureClass
from schemas.trial import append_trial_to_jsonl, load_trials_from_jsonl

log = logging.getLogger(__name__)


class TrialManager:
    """Create, read, update, and list TrialRecord instances on disk.

    Directory layout::

        runs/
        ├── trials.jsonl            # global append-only index
        ├── <trial_id>/
        │   ├── trial.json          # full TrialRecord
        │   └── (results, logs, ... # ORFS artifacts, managed separately)
        └── ...
    """

    def __init__(self, runs_dir: Path) -> None:
        self.runs_dir = Path(runs_dir)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self.runs_dir / "trials.jsonl"

    # ------------------------------------------------------------------
    # Create
    # ------------------------------------------------------------------

    def create(
        self,
        experiment_id: str = "unknown",
        parent_trial_id: Optional[str] = None,
        branch_stage: Optional[str] = None,
        config_hash: Optional[str] = None,
        env_hash: Optional[str] = None,
        iteration: int = 0,
    ) -> TrialRecord:
        """Create a new trial directory and return an empty TrialRecord.

        The trial is immediately persisted (so a crash after flow start
        leaves a running record); update() overwrites it on completion.

        Directory naming: iter-{iteration}-{trial_id} — human-readable
        iteration number prefix + unique 8-char hex ID.
        """
        # Generate trial_id first so artifact_dir can reference it
        from schemas.trial import _new_trial_id
        trial_id = _new_trial_id()
        artifact_dir = str(self.runs_dir / f"iter-{iteration}-{trial_id}")
        trial = TrialRecord(
            trial_id=trial_id,
            experiment_id=experiment_id,
            parent_trial_id=parent_trial_id,
            branch_stage=branch_stage,
            status="running",
            start_time=datetime.now(timezone.utc).isoformat(),
            config_hash=config_hash,
            env_hash=env_hash,
            artifact_dir=artifact_dir,
        )
        self._write_trial(trial)
        self._append_index(trial)
        log.info("Trial %s created (parent=%s, branch=%s)",
                 trial.trial_id, parent_trial_id, branch_stage)
        return trial

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    def update(self, trial: TrialRecord) -> None:
        """Persist the current state of a trial.

        Overwrites trial.json in-place and appends a new line to the JSONL
        index (so the index shows the latest state of every trial).
        """
        if trial.status != "running" and trial.end_time is None:
            trial.end_time = datetime.now(timezone.utc).isoformat()
        self._write_trial(trial)
        self._append_index(trial)

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get(self, trial_id: str) -> Optional[TrialRecord]:
        """Load a single trial by ID from its trial.json.

        Trial directories use the naming convention iter-{N}-{trial_id}
        (see create()), so we scan for directories whose name ends with the
        trial ID rather than assuming a flat layout.
        """
        # Scan iter-{N}-{trial_id} directories; also accept legacy bare trial_id names
        for d in sorted(self.runs_dir.iterdir()):
            if not d.is_dir():
                continue
            if not (d.name.endswith(f"-{trial_id}") or d.name == trial_id):
                continue
            trial_json = d / "trial.json"
            if not trial_json.is_file():
                continue
            try:
                data = json.loads(trial_json.read_text(encoding="utf-8"))
                return TrialRecord.from_dict(data)
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                log.warning("Corrupt trial.json for %s: %s", trial_id, e)
                return None
        return None

    def list_all(self) -> List[TrialRecord]:
        """Return every trial from the JSONL index (fast, no directory walk)."""
        return load_trials_from_jsonl(self._index_path)

    def list_by_experiment(self, experiment_id: str) -> List[TrialRecord]:
        return [t for t in self.list_all() if t.experiment_id == experiment_id]

    def list_by_status(self, status: str) -> List[TrialRecord]:
        return [t for t in self.list_all() if t.status == status]

    def latest(self) -> Optional[TrialRecord]:
        """Most recently created trial, or None."""
        all_trials = self.list_all()
        return all_trials[-1] if all_trials else None

    # ------------------------------------------------------------------
    # Internal persistence helpers
    # ------------------------------------------------------------------

    def _trial_dir(self, trial_id: str) -> Path:
        return self.runs_dir / trial_id

    def _write_trial(self, trial: TrialRecord) -> None:
        """Atomically write trial.json for a single trial."""
        trial_dir = Path(trial.artifact_dir) if trial.artifact_dir else self._trial_dir(trial.trial_id)
        trial_dir.mkdir(parents=True, exist_ok=True)
        trial_path = trial_dir / "trial.json"
        tmp = trial_path.with_suffix(trial_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(trial.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, trial_path)

    def _append_index(self, trial: TrialRecord) -> None:
        """Append one-line summary to the global JSONL index."""
        append_trial_to_jsonl(trial, self._index_path)


# =============================================================================
# Self-test
# =============================================================================

if __name__ == "__main__":
    import shutil
    import tempfile

    ok = 0
    fail = 0

    def check(cond, msg):
        global ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  FAIL: {msg}")

    tmpdir = Path(tempfile.mkdtemp())
    runs_dir = tmpdir / "runs"
    mgr = TrialManager(runs_dir)

    # -- Create --
    t1 = mgr.create(experiment_id="smoke-gcd-v1", config_hash="abc123")
    check(t1.status == "running", "create: status=running")
    check(t1.trial_id is not None and len(t1.trial_id) == 8, "create: 8-char trial_id")
    check(t1.start_time is not None, "create: start_time set")
    check((Path(t1.artifact_dir) / "trial.json").is_file(), "create: trial.json exists")

    # -- Get --
    t1_loaded = mgr.get(t1.trial_id)
    check(t1_loaded is not None, "get: returns TrialRecord")
    check(t1_loaded.trial_id == t1.trial_id, "get: same id")
    check(t1_loaded.experiment_id == "smoke-gcd-v1", "get: experiment_id preserved")

    # -- Update (success) --
    t1.status = "ok"
    t1.final_qor = {"wns_ps": -1460.3, "tns_ps": -61747.6, "area_um2": 5400.2, "power_w": 0.00938}
    t1.stage_results = [
        StageResult(stage="FP", status="ok", elapsed_s=10.0, exit_code=0),
        StageResult(stage="PL", status="ok", elapsed_s=15.0, exit_code=0),
        StageResult(stage="CTS", status="ok", elapsed_s=8.0, exit_code=0),
        StageResult(stage="RT", status="ok", elapsed_s=30.0, exit_code=0),
        StageResult(stage="finish", status="ok", elapsed_s=5.0, exit_code=0),
    ]
    mgr.update(t1)
    t1_loaded2 = mgr.get(t1.trial_id)
    check(t1_loaded2.status == "ok", "update: status -> ok")
    check(t1_loaded2.end_time is not None, "update: end_time set")
    check(t1_loaded2.elapsed_s > 60, "update: elapsed computed from stages")
    check(t1_loaded2.is_complete, "update: is_complete=True")

    # -- Create a failed trial --
    t2 = mgr.create(experiment_id="smoke-gcd-v1", parent_trial_id=t1.trial_id,
                    branch_stage="CTS")
    t2.status = "failed"
    t2.failure = FailureClass.TOOL_CRASH
    t2.error_message = "OpenROAD segfault during routing"
    t2.stage_results = [
        StageResult(stage="CTS", status="ok", elapsed_s=8.0, exit_code=0),
        StageResult(stage="RT", status="failed", elapsed_s=12.0, exit_code=-11,
                    failure=FailureClass.TOOL_CRASH, error_message="SIGSEGV"),
    ]
    mgr.update(t2)
    t2_loaded = mgr.get(t2.trial_id)
    check(t2_loaded.status == "failed", "failed trial: status")
    check(t2_loaded.failure == FailureClass.TOOL_CRASH, "failed trial: failure class")
    check(t2_loaded.failed_stage == "RT", "failed trial: failed_stage")
    check(t2_loaded.parent_trial_id == t1.trial_id, "failed trial: parent")
    check(t2_loaded.branch_stage == "CTS", "failed trial: branch_stage")
    check(not t2_loaded.is_complete, "failed trial: is_complete=False")
    check(t2_loaded.elapsed_s == 20.0, "failed trial: elapsed from stages")

    # -- List --
    all_trials = mgr.list_all()
    check(len(all_trials) >= 2, f"list_all: >=2 trials (got {len(all_trials)})")
    check(len(mgr.list_by_experiment("smoke-gcd-v1")) == 2, "list_by_experiment: 2")
    check(len(mgr.list_by_status("ok")) == 1, "list_by_status ok: 1")
    check(len(mgr.list_by_status("failed")) == 1, "list_by_status failed: 1")
    check(mgr.latest().trial_id == t2.trial_id, "latest: t2")

    # -- Re-read from index after restart (simulate) --
    mgr2 = TrialManager(runs_dir)  # new manager, same dir
    reloaded = mgr2.list_all()
    check(len(reloaded) == 2, "reload from index: 2 trials")

    # -- Non-existent trial --
    check(mgr.get("deadbeef") is None, "get non-existent -> None")

    # Clean up
    shutil.rmtree(tmpdir)

    total = ok + fail
    print(f"\n{'='*50}")
    print(f"  {ok}/{total} passed" + (f", {fail} FAILED" if fail else " — ALL OK"))
    print(f"{'='*50}")
    sys.exit(1 if fail else 0)
