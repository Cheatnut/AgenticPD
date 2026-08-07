# -*- coding: utf-8 -*-
"""core/models.py — core data models (trial/stage/checkpoint records).

Defines four immutable-record-style dataclasses that replace the flat
history.json dict with typed, queryable, self-describing trial records.

Models:
    FailureClass  — enum of 5 failure categories (tool crash, timeout, ...)
    StageResult   — per-stage timing, exit code, intermediate QoR, failure info
    CheckpointRef — artifact manifest for resumable stage snapshots
    TrialRecord   — complete record of one RTL-to-GDS run
    (decision models live in core/decisions.py)

All models support:
    - to_dict() / from_dict() round-trip
    - JSONL append (one trial per line in trials.jsonl)
    - self-validation via __post_init__ invariants
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

from core.decisions import (
    DecisionTraceRef,
    DoomedDecision,
    GWTWDecision,
    MinimalObservation,
)


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
    ORFSRunner.run_stage(). Every stage — success or failure —
    records elapsed time, exit code, and log path so budget accounting
    is always accurate.
    """

    stage: str                          # "FP" | "PL" | "CTS" | "RT" | "finish"
    status: str                         # "ok" | "failed" | "skipped"
    elapsed_s: float                    # wall-clock seconds (always >= 0)
    exit_code: Optional[int] = None     # process return code; None if stage was skipped
    log_path: Optional[str] = None      # path to stage make log (relative to artifact_dir)

    # Execution metadata (contract: command, wall time bounds, report path)
    command: Optional[str] = None       # make command line executed (for audit/replay)
    start_time: Optional[str] = None    # ISO 8601 timestamp when stage started
    end_time: Optional[str] = None      # ISO 8601 timestamp when stage ended
    report_path: Optional[str] = None   # path to stage report JSON (e.g. 2_floorplan.json)

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
            command=d.get("command"),
            start_time=d.get("start_time"),
            end_time=d.get("end_time"),
            report_path=d.get("report_path"),
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
# 3a. CheckpointAuditEntry — per-checkpoint audit record during resolution
# =============================================================================


@dataclass
class CheckpointAuditEntry:
    """Immutable audit record for one checkpoint examined during resolution.

    Each entry captures the full verification and compatibility result for a
    single checkpoint, preserving the decision trail so fallback decisions
    remain traceable after the fact.

    When the entry represents a consumed checkpoint, ``rejection_reason`` is
    None and ``is_compatible`` is True.  When the entry represents a rejected
    checkpoint, ``rejection_reason`` explains why it was skipped.
    """

    checkpoint_id: str                        # "cp-<trial_id>-<stage>"
    stage: str                                # "FP" | "PL" | "CTS"
    source_trial_id: str                      # trial that produced this checkpoint
    manifest_verified: bool = False           # did artifact files pass existence + hash checks?
    manifest_errors: List[str] = field(default_factory=list)  # human-readable manifest issues
    compatibility_checked: bool = False       # was param compatibility checked?
    is_compatible: bool = False               # are params compatible with this checkpoint?
    invalidating_parameters: List[str] = field(default_factory=list)  # params that caused rejection
    rejection_reason: Optional[str] = None    # None if consumed; else why rejected

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CheckpointAuditEntry":
        return cls(**{k: v for k, v in d.items()
                      if k in cls.__dataclass_fields__})


# =============================================================================
# 3b. ExecutionResolution — how the optimizer resolved a branch request
# =============================================================================


@dataclass
class ExecutionResolution:
    """Immutable record of checkpoint resolution for one trial.

    Records the gap between what the Judge/Policy requested and what was
    actually executed after checkpoint manifest verification and parameter
    compatibility checking.

    This is stored as an optional ``execution_resolution`` field on
    TrialRecord — separate from the ``checkpoint`` field which records
    the checkpoint *produced* by this trial.
    """

    # ---- What was requested ----
    requested_parent_node_id: str          # tree node the Judge/Policy chose
    requested_start_stage: str             # "FP" | "PL" | "CTS" | "RT"

    # ---- What was actually executed ----
    effective_start_stage: str             # "FP" | "PL" | "CTS" | "RT"
    execution_mode: str                    # "checkpoint_fork" | "full_restart"

    # ---- Checkpoint consumed (null for full_restart) ----
    consumed_checkpoint: Optional[str] = None  # checkpoint_id, or None
    consumed_node_id: Optional[str] = None     # tree node id that produced the cp
    consumed_variant: Optional[str] = None     # FLOW_VARIANT of the consumed cp source

    # ---- Manifest verification ----
    manifest_verified: bool = False
    manifest_errors: List[str] = field(default_factory=list)

    # ---- Compatibility check ----
    compatibility_checked: bool = False
    is_compatible: bool = False
    invalidating_parameters: List[str] = field(default_factory=list)

    # ---- Fallback rationale ----
    fallback_reason: Optional[str] = None

    # ---- Full audit trail: every checkpoint examined, in order ----
    checkpoint_audit_trail: List[CheckpointAuditEntry] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # Convert audit entries to plain dicts
        d["checkpoint_audit_trail"] = [
            e.to_dict() for e in self.checkpoint_audit_trail
        ]
        return d

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> Optional["ExecutionResolution"]:
        """Deserialize from dict; returns None for null/missing input
        (backward compat: old Trial JSON has no execution_resolution)."""
        if not d:
            return None
        # Parse audit trail entries (backward compat: missing key → empty list)
        audit_raw = d.get("checkpoint_audit_trail", [])
        audit_trail = [CheckpointAuditEntry.from_dict(e) for e in audit_raw]
        kwargs = {k: v for k, v in d.items()
                  if k in cls.__dataclass_fields__}
        kwargs["checkpoint_audit_trail"] = audit_trail
        return cls(**kwargs)


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
    status: str = "running"                     # "running" | "ok" | "failed" | "paused"
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

    # ---- Execution resolution (how this trial's branch request was resolved) ----
    execution_resolution: Optional[ExecutionResolution] = None

    # ---- decision trace (Doomed + GWTW) ----
    # Lists hold one entry per decision stage (PL, CTS) so PL decisions
    # are never overwritten by later CTS decisions.  When the append-only
    # JSONL trace is implemented, each entry can carry a stable file
    # reference in addition to the inline data.
    doomed_decisions: List[DoomedDecision] = field(default_factory=list)
    gwtw_decisions: List[GWTWDecision] = field(default_factory=list)

    # ---- Decision trace references (append-only JSONL pointers) ----
    decision_trace_refs: List[DecisionTraceRef] = field(default_factory=list)

    # ---- Reproducibility ----
    config_hash: Optional[str] = None           # sha256 of resolved config
    env_hash: Optional[str] = None              # sha256 of environment_manifest.json

    # ---- Artifact location ----
    # Relative to session runs_dir (e.g. "iter-0-abc12345"), or absolute
    # for backward-compat / self-test tempdirs.
    artifact_dir: Optional[str] = None

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
        d["execution_resolution"] = (self.execution_resolution.to_dict()
                                     if self.execution_resolution else None)
        d["doomed_decisions"] = [dd.to_dict() for dd in self.doomed_decisions]
        d["gwtw_decisions"] = [gd.to_dict() for gd in self.gwtw_decisions]
        d["decision_trace_refs"] = [r.to_dict() for r in self.decision_trace_refs]
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
        execution_resolution = ExecutionResolution.from_dict(d.get("execution_resolution"))
        # Backward compat: old JSON may have singular "doomed_decision" /
        # "gwtw_decision" keys (null or object).  Accept both shapes.
        doomed_raw = d.get("doomed_decisions", None)
        if doomed_raw is None:
            # Fall back to legacy singular key
            legacy_dd = d.get("doomed_decision")
            doomed_raw = [legacy_dd] if legacy_dd else []
        doomed_decisions = [
            dd for dd in (DoomedDecision.from_dict(e) for e in doomed_raw)
            if dd is not None
        ]
        gwtw_raw = d.get("gwtw_decisions", None)
        if gwtw_raw is None:
            legacy_gd = d.get("gwtw_decision")
            gwtw_raw = [legacy_gd] if legacy_gd else []
        gwtw_decisions = [
            gd for gd in (GWTWDecision.from_dict(e) for e in gwtw_raw)
            if gd is not None
        ]
        decision_trace_refs = [
            r for r in (DecisionTraceRef.from_dict(e)
                        for e in d.get("decision_trace_refs", []))
            if r is not None
        ]
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
            execution_resolution=execution_resolution,
            doomed_decisions=doomed_decisions,
            gwtw_decisions=gwtw_decisions,
            decision_trace_refs=decision_trace_refs,
            config_hash=d.get("config_hash"),
            env_hash=d.get("env_hash"),
            artifact_dir=d.get("artifact_dir"),
        )

    # ---- Validation ----

    def __post_init__(self) -> None:
        if self.status not in ("running", "ok", "failed", "paused"):
            raise ValueError(f"Unknown status: {self.status}")
        if self.status == "failed" and self.failure is None:
            # Auto-classify if possible
            if self.stage_results:
                for sr in self.stage_results:
                    if sr.status == "failed" and sr.failure:
                        self.failure = sr.failure
                        break


# =============================================================================
# 4a. Artifact path resolution
# =============================================================================

def resolve_artifact_dir(artifact_dir: Optional[str],
                         runs_dir: "Path") -> Optional[Path]:
    """Resolve artifact_dir to an absolute path.

    If ``artifact_dir`` is already absolute, use as-is (backward compat).
    Otherwise resolve relative to ``runs_dir`` (the session directory).
    """
    if artifact_dir is None:
        return None
    p = Path(artifact_dir)
    if p.is_absolute():
        return p
    return runs_dir / p


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
                log.warning("Skipping corrupt JSONL line: %s", e)
    return list(seen.values())


# =============================================================================
# Self-test (run with: python3 schemas/trial.py)
# =============================================================================

