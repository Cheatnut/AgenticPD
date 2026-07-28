# -*- coding: utf-8 -*-
"""trial.py — Stage B core data models.

Defines four immutable-record-style dataclasses that replace the flat
history.json dict with typed, queryable, self-describing trial records.

Models:
    FailureClass  — enum of 5 failure categories (tool crash, timeout, ...)
    StageResult   — per-stage timing, exit code, intermediate QoR, failure info
    CheckpointRef — artifact manifest for resumable stage snapshots
    TrialRecord   — complete record of one RTL-to-GDS run

All models support:
    - to_dict() / from_dict() round-trip
    - JSONL append (one trial per line in trials.jsonl)
    - self-validation via __post_init__ invariants
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional


# =============================================================================
# 1. FailureClass — why did this trial (or stage) fail?
# =============================================================================

class FailureClass(str, Enum):
    """Machine-readable failure category.

    String-valued so it serialises cleanly to JSON without a custom encoder.
    """
    NONE = "none"               # no failure (success)
    TOOL_CRASH = "tool_crash"   # OpenROAD / Yosys segfault, assert, abort
    TIMEOUT = "timeout"         # wall-clock or CPU timeout, process group killed
    QOR_INCOMPLETE = "qor_incomplete"   # flow exited 0 but 6_report.json missing/incomplete
    PARSE_ERROR = "parse_error"         # report exists but parser could not extract metrics
    LEGALITY_FAIL = "legality_fail"     # placement illegal, DRC overflow, etc.

    @classmethod
    def from_exit_code(cls, code: int, timed_out: bool = False) -> "FailureClass":
        """Heuristic classifier from process return code."""
        if timed_out:
            return cls.TIMEOUT
        if code == 0:
            return cls.NONE
        # negative = killed by signal (SIGSEGV=11, SIGABRT=6, SIGKILL=9, ...)
        if code < 0:
            return cls.TOOL_CRASH
        # positive non-zero = tool reported error
        return cls.TOOL_CRASH


# =============================================================================
# 2. StageResult — what happened during one flow stage
# =============================================================================

@dataclass
class StageResult:
    """Immutable record of a single flow stage execution.

    Replaces the current ad-hoc (ok, stage_qor) tuple returned by
    orfs_interface.run_stage().  Every stage — success or failure —
    records elapsed time, exit code, and log path so budget accounting
    is always accurate.
    """

    stage: str                          # "FP" | "PL" | "CTS" | "RT" | "finish"
    status: str                         # "ok" | "failed" | "skipped"
    elapsed_s: float                    # wall-clock seconds (always >= 0)
    exit_code: Optional[int] = None     # process return code; None if stage was skipped
    log_path: Optional[str] = None      # path to stage make log (relative to artifact_dir)

    # Intermediate QoR extracted immediately after this stage.
    # Keys follow ORFS convention, e.g. "2_1_floorplan_ws_ps", "3_5_place_dp_ws_ps".
    stage_qor: Dict[str, float] = field(default_factory=dict)

    # Only populated when status == "failed"
    failure: Optional[FailureClass] = None
    error_message: Optional[str] = None

    def __post_init__(self) -> None:
        if self.stage not in ("FP", "PL", "CTS", "RT", "finish", "synth"):
            raise ValueError(f"Unknown stage: {self.stage}")
        if self.status not in ("ok", "failed", "skipped"):
            raise ValueError(f"Unknown status: {self.status}")
        if self.elapsed_s < 0:
            raise ValueError(f"elapsed_s must be >= 0, got {self.elapsed_s}")

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if self.failure is not None:
            d["failure"] = self.failure.value
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "StageResult":
        failure = d.get("failure")
        if failure is not None and failure != "none":
            failure = FailureClass(failure)
        else:
            failure = None
        return cls(
            stage=d["stage"],
            status=d["status"],
            elapsed_s=d["elapsed_s"],
            exit_code=d.get("exit_code"),
            log_path=d.get("log_path"),
            stage_qor=d.get("stage_qor", {}),
            failure=failure,
            error_message=d.get("error_message"),
        )


# =============================================================================
# 3. CheckpointRef — resumable stage snapshot
# =============================================================================

@dataclass
class CheckpointRef:
    """Metadata for one checkpoint that can resume a downstream re-run.

    A checkpoint is valid when (a) the source trial completed up to the
    checkpoint stage successfully, and (b) the downstream parameters are
    compatible with the upstream artifacts (checked via param_hash).
    """

    checkpoint_id: str                             # "cp-<trial_id>-<stage>"
    source_trial_id: str                           # which trial produced this checkpoint
    stage: str                                     # which stage was the last completed one
    param_hash: str                                # sha256 of resolved upstream params
    orfs_commit: str                               # ORFS commit (or "unresolved")
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    # Manifest: list of artifact files with size and hash for integrity checks.
    # Each entry: {"file": "results/.../2_floorplan.odb", "size_bytes": 12345, "sha256": "..."}
    artifact_manifest: List[Dict[str, Any]] = field(default_factory=list)

    # Path to the variant directory holding these artifacts (relative to flow/)
    artifact_dir: Optional[str] = None

    @classmethod
    def make_id(cls, trial_id: str, stage: str) -> str:
        return f"cp-{trial_id}-{stage}"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CheckpointRef":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# =============================================================================
# 4. TrialRecord — complete record of one RTL-to-GDS run
# =============================================================================

def _new_trial_id() -> str:
    """Generate a short unique trial ID: 8 hex chars from UUID4."""
    return uuid.uuid4().hex[:8]


@dataclass
class TrialRecord:
    """Complete, immutable record of one backend execution.

    This is the replacement for the current history.json entry dict.
    Each trial gets its own directory under runs/, and every observation
    (stage results, final QoR, failure reason, checkpoint) is stored here.
    """

    # ---- Identity ----
    trial_id: str = field(default_factory=_new_trial_id)
    experiment_id: str = "unknown"

    # ---- Lineage (tree position) ----
    parent_trial_id: Optional[str] = None       # which trial this one branches from
    branch_stage: Optional[str] = None          # "FP" | "PL" | "CTS" | "RT" | None (full restart)

    # ---- Lifecycle ----
    status: str = "running"                     # "running" | "ok" | "failed"
    start_time: Optional[str] = None
    end_time: Optional[str] = None

    # ---- Parameters ----
    # Resolved per-stage params for this trial (inherited + new).
    params: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Diff from parent: {stage: {param_name: {"from": old, "to": new}}}
    param_diff: Dict[str, Any] = field(default_factory=dict)

    # ---- QoR ----
    # Final post-route WNS/TNS/Area/Power (None until finish completes).
    final_qor: Optional[Dict[str, Optional[float]]] = None

    # ---- Stage-by-stage log ----
    stage_results: List[StageResult] = field(default_factory=list)

    # ---- Failure (only when status == "failed") ----
    failure: Optional[FailureClass] = None
    error_message: Optional[str] = None

    # ---- Checkpoint (produced by this trial, if successful) ----
    checkpoint: Optional[CheckpointRef] = None

    # ---- Reproducibility ----
    config_hash: Optional[str] = None           # sha256 of resolved config
    env_hash: Optional[str] = None              # sha256 of environment_manifest.json

    # ---- Artifact location ----
    artifact_dir: Optional[str] = None           # runs/<trial_id>/

    # ---- Computed convenience fields ----
    @property
    def elapsed_s(self) -> float:
        """Total wall-clock time. Prefers sum of per-stage elapsed_s
        (which reflects actual tool runtime); falls back to start-end span."""
        stage_sum = sum(sr.elapsed_s for sr in self.stage_results)
        if stage_sum > 0:
            return stage_sum
        if self.start_time and self.end_time:
            try:
                start = datetime.fromisoformat(self.start_time)
                end = datetime.fromisoformat(self.end_time)
                return (end - start).total_seconds()
            except (ValueError, TypeError):
                pass
        return 0.0

    @property
    def failed_stage(self) -> Optional[str]:
        """First stage that failed, or None."""
        for sr in self.stage_results:
            if sr.status == "failed":
                return sr.stage
        return None

    @property
    def is_complete(self) -> bool:
        return self.status == "ok" and self.failure is None and self.final_qor is not None

    # ---- Serialisation ----

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {}
        # Explicit fields to control order and exclude computed properties
        d["trial_id"] = self.trial_id
        d["experiment_id"] = self.experiment_id
        d["parent_trial_id"] = self.parent_trial_id
        d["branch_stage"] = self.branch_stage
        d["status"] = self.status
        d["start_time"] = self.start_time
        d["end_time"] = self.end_time
        d["params"] = self.params
        d["param_diff"] = self.param_diff
        d["final_qor"] = self.final_qor
        d["stage_results"] = [sr.to_dict() for sr in self.stage_results]
        d["failure"] = self.failure.value if self.failure else None
        d["error_message"] = self.error_message
        d["checkpoint"] = self.checkpoint.to_dict() if self.checkpoint else None
        d["config_hash"] = self.config_hash
        d["env_hash"] = self.env_hash
        d["artifact_dir"] = self.artifact_dir
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TrialRecord":
        failure = d.get("failure")
        if failure and failure != "none":
            failure = FailureClass(failure)
        else:
            failure = None
        checkpoint = d.get("checkpoint")
        if checkpoint:
            checkpoint = CheckpointRef.from_dict(checkpoint)
        return cls(
            trial_id=d["trial_id"],
            experiment_id=d.get("experiment_id", "unknown"),
            parent_trial_id=d.get("parent_trial_id"),
            branch_stage=d.get("branch_stage"),
            status=d.get("status", "failed"),
            start_time=d.get("start_time"),
            end_time=d.get("end_time"),
            params=d.get("params", {}),
            param_diff=d.get("param_diff", {}),
            final_qor=d.get("final_qor"),
            stage_results=[StageResult.from_dict(sr) for sr in d.get("stage_results", [])],
            failure=failure,
            error_message=d.get("error_message"),
            checkpoint=checkpoint,
            config_hash=d.get("config_hash"),
            env_hash=d.get("env_hash"),
            artifact_dir=d.get("artifact_dir"),
        )

    # ---- Validation ----

    def __post_init__(self) -> None:
        if self.status not in ("running", "ok", "failed"):
            raise ValueError(f"Unknown status: {self.status}")
        if self.status == "failed" and self.failure is None:
            # Auto-classify if possible
            if self.stage_results:
                for sr in self.stage_results:
                    if sr.status == "failed" and sr.failure:
                        self.failure = sr.failure
                        break


# =============================================================================
# 5. JSONL store helpers (append-only index of all trials)
# =============================================================================

def append_trial_to_jsonl(trial: TrialRecord, jsonl_path: Path) -> None:
    """Append one trial as a single JSON line to trials.jsonl.

    Uses atomic write: write to .tmp then os.replace, so a crash mid-write
    never corrupts the existing file.
    """
    line = json.dumps(trial.to_dict(), ensure_ascii=False, sort_keys=True)
    tmp = jsonl_path.with_suffix(jsonl_path.suffix + ".tmp")
    existing = ""
    if jsonl_path.exists():
        existing = jsonl_path.read_text(encoding="utf-8")
    tmp.write_text(existing + line + "\n", encoding="utf-8")
    os.replace(tmp, jsonl_path)


def load_trials_from_jsonl(jsonl_path: Path) -> List[TrialRecord]:
    """Load all trials from a JSONL file (one JSON object per line).

    Duplicate trial_ids (from multiple update() calls) are deduplicated;
    only the last occurrence of each trial_id is kept.
    """
    if not jsonl_path.exists():
        return []
    seen: dict = {}
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                trial = TrialRecord.from_dict(json.loads(line))
                seen[trial.trial_id] = trial  # last-wins
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                print(f"[WARN] Skipping corrupt JSONL line: {e}")
    return list(seen.values())


# =============================================================================
# Self-test (run with: python3 schemas/trial.py)
# =============================================================================

if __name__ == "__main__":
    import sys

    ok = 0
    fail = 0

    def check(cond, msg):
        global ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  FAIL: {msg}")

    # -- FailureClass --
    check(FailureClass.from_exit_code(0) == FailureClass.NONE, "exit 0 -> NONE")
    check(FailureClass.from_exit_code(1) == FailureClass.TOOL_CRASH, "exit 1 -> TOOL_CRASH")
    check(FailureClass.from_exit_code(-11) == FailureClass.TOOL_CRASH, "SIGSEGV -> TOOL_CRASH")
    check(FailureClass.from_exit_code(0, timed_out=True) == FailureClass.TIMEOUT, "timed_out -> TIMEOUT")

    # -- StageResult --
    sr_ok = StageResult(stage="FP", status="ok", elapsed_s=12.5, exit_code=0,
                        stage_qor={"2_1_floorplan_ws_ps": -1154.1})
    check(sr_ok.to_dict()["stage"] == "FP", "StageResult to_dict stage")
    check(StageResult.from_dict(sr_ok.to_dict()).elapsed_s == 12.5, "StageResult roundtrip")

    sr_fail = StageResult(stage="PL", status="failed", elapsed_s=45.0, exit_code=1,
                          failure=FailureClass.TOOL_CRASH, error_message="openroad segfault")
    check(sr_fail.failure == FailureClass.TOOL_CRASH, "StageResult failure enum")

    # elapsed_s must be >= 0
    try:
        StageResult(stage="FP", status="ok", elapsed_s=-1.0)
        check(False, "negative elapsed_s should raise")
    except ValueError:
        check(True, "negative elapsed_s raises ValueError")

    # -- CheckpointRef --
    cp = CheckpointRef(
        checkpoint_id=CheckpointRef.make_id("abc12345", "CTS"),
        source_trial_id="abc12345",
        stage="CTS",
        param_hash="sha256:deadbeef",
        orfs_commit="unresolved",
        artifact_manifest=[{"file": "results/.../4_cts.odb", "size_bytes": 12345, "sha256": "abc"}],
    )
    check(cp.checkpoint_id == "cp-abc12345-CTS", "CheckpointRef id format")
    check(CheckpointRef.from_dict(cp.to_dict()).stage == "CTS", "CheckpointRef roundtrip")

    # -- TrialRecord --
    tr = TrialRecord(
        trial_id="test001",
        experiment_id="smoke-gcd-v1",
        parent_trial_id=None,
        branch_stage=None,
        status="ok",
        start_time="2026-07-27T15:00:00+00:00",
        end_time="2026-07-27T15:02:30+00:00",
        params={"FP": {"CORE_UTILIZATION": 38}, "PL": {}, "CTS": {}, "RT": {}},
        final_qor={"wns_ps": -1460.3, "tns_ps": -61747.6, "area_um2": 5400.2, "power_w": 0.00938},
        stage_results=[sr_ok, sr_fail,
                       StageResult(stage="CTS", status="skipped", elapsed_s=0.0),
                       StageResult(stage="RT", status="skipped", elapsed_s=0.0),
                       StageResult(stage="finish", status="skipped", elapsed_s=0.0)],
        failure=FailureClass.TOOL_CRASH,
        error_message="PL stage crashed",
    )
    check(tr.trial_id == "test001", "TrialRecord create")
    check(tr.failed_stage == "PL", "TrialRecord failed_stage")
    check(tr.is_complete == False, "TrialRecord is_complete=False when failed")
    # Roundtrip
    tr2 = TrialRecord.from_dict(tr.to_dict())
    check(tr2.trial_id == tr.trial_id, "TrialRecord roundtrip id")
    check(tr2.failed_stage == "PL", "TrialRecord roundtrip failed_stage")
    check(tr2.failure == FailureClass.TOOL_CRASH, "TrialRecord roundtrip failure")
    check(tr2.stage_results[1].elapsed_s == 45.0, "TrialRecord roundtrip stage elapsed")
    check(tr2.elapsed_s > 0, "TrialRecord elapsed_s computed")

    # -- JSONL --
    import tempfile
    tmpdir = tempfile.mkdtemp()
    jl_path = Path(tmpdir) / "trials.jsonl"

    # Empty
    check(load_trials_from_jsonl(jl_path) == [], "JSONL empty load")
    # Append
    append_trial_to_jsonl(tr, jl_path)
    loaded = load_trials_from_jsonl(jl_path)
    check(len(loaded) == 1, "JSONL one trial")
    check(loaded[0].trial_id == "test001", "JSONL load id")
    # Append second
    tr3 = TrialRecord(trial_id="test002", experiment_id="smoke-gcd-v1", status="ok")
    append_trial_to_jsonl(tr3, jl_path)
    check(len(load_trials_from_jsonl(jl_path)) == 2, "JSONL two trials")

    import shutil
    shutil.rmtree(tmpdir)

    # -- Summary --
    total = ok + fail
    print(f"\n{'='*50}")
    print(f"  {ok}/{total} passed" + (f", {fail} FAILED" if fail else " — ALL OK"))
    print(f"{'='*50}")
    sys.exit(1 if fail else 0)
