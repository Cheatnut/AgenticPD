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
import logging
import os
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


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

    # Execution metadata (Stage C contract: command, wall time bounds, report path)
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
# 3c. MinimalObservation — stage-level observation for Doomed/GWTW input
# =============================================================================


@dataclass
class MinimalObservation:
    """Minimal per-trial observation at a decision stage (PL or CTS).

    Captures just enough data for the rule-based DoomedPredictor and
    GWTWScheduler to make a decision.  This is intentionally minimal —
    full trajectory features belong to Stage E.
    """

    trial_id: str                              # which trial this observation belongs to
    stage: str                                 # "PL" | "CTS" — the decision stage
    status: str                                # "ok" | "failed" | "running" | "paused"
    stage_wns_ps: Optional[float] = None       # WNS at this stage (ps); None if unavailable
    stage_tns_ps: Optional[float] = None       # TNS at this stage (ps); None if unavailable
    stage_elapsed_s: float = 0.0               # wall-clock seconds up to this stage
    failure_type: Optional[str] = None         # FailureClass value if failed; None otherwise
    checkpoint_id: Optional[str] = None        # checkpoint produced at this stage
    parent_trial_id: Optional[str] = None      # lineage

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> Optional["MinimalObservation"]:
        """Deserialize from dict; returns None for null/missing input."""
        if not d:
            return None
        return cls(**{k: v for k, v in d.items()
                      if k in cls.__dataclass_fields__})


# =============================================================================
# 3d. DoomedDecision — rule-based risk assessment result
# =============================================================================


@dataclass
class DoomedDecision:
    """Output of the rule-based DoomedPredictor for one trial at one stage.

    ``risk_score`` is a relative ranking within the same stage cohort, NOT a
    calibrated probability.  ``reason_codes`` are machine-readable slugs
    suitable for filtering and auditing.
    """

    risk_class: str                # "hard_dead" | "soft_bad" | "survivor"
    risk_score: float = 0.0        # relative rank within cohort (lower = worse)
    reason_codes: List[str] = field(default_factory=list)  # e.g. ["timing_negative", "stage_failed"]
    rule_version: str = "0.0.0"    # predictor rule-set version
    # Snapshot of the MinimalObservation + cohort context fed to the predictor
    input_evidence: Dict[str, Any] = field(default_factory=dict)

    _VALID_RISK_CLASSES = frozenset({"hard_dead", "soft_bad", "survivor"})

    def __post_init__(self) -> None:
        if self.risk_class not in self._VALID_RISK_CLASSES:
            raise ValueError(
                f"Invalid risk_class {self.risk_class!r}; "
                f"must be one of {sorted(self._VALID_RISK_CLASSES)}")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> Optional["DoomedDecision"]:
        """Deserialize from dict; returns None for null/missing input."""
        if not d:
            return None
        return cls(**{k: v for k, v in d.items()
                      if k in cls.__dataclass_fields__})


# =============================================================================
# 3e. GWTWDecision — scheduler action for one trial
# =============================================================================


@dataclass
class GWTWDecision:
    """Output of the serial async GWTWScheduler for one trial at one decision
    stage.

    The action determines what happens to the trial after the cohort reaches a
    decision stage (PL or CTS).
    """

    action: str                       # "continue" | "pause" | "audit_continue" | "fork" | "finish"
    decision_stage: str               # "PL" | "CTS" — which stage triggered this decision
    rank: int = 0                     # cohort rank at decision time
    parent_trial_id: Optional[str] = None   # survivor parent (for fork actions)
    child_trial_id: Optional[str] = None    # new trial spawned by fork action
    is_audit_pass: bool = False       # True when audit quota overrides soft_bad
    scheduler_version: str = "0.0.0"  # scheduler rule-set version

    _VALID_ACTIONS = frozenset({"continue", "pause", "audit_continue", "fork", "finish"})
    _VALID_DECISION_STAGES = frozenset({"PL", "CTS"})

    def __post_init__(self) -> None:
        if self.action not in self._VALID_ACTIONS:
            raise ValueError(
                f"Invalid action {self.action!r}; "
                f"must be one of {sorted(self._VALID_ACTIONS)}")
        if self.decision_stage not in self._VALID_DECISION_STAGES:
            raise ValueError(
                f"Invalid decision_stage {self.decision_stage!r}; "
                f"must be one of {sorted(self._VALID_DECISION_STAGES)}")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> Optional["GWTWDecision"]:
        """Deserialize from dict; returns None for null/missing input."""
        if not d:
            return None
        return cls(**{k: v for k, v in d.items()
                      if k in cls.__dataclass_fields__})


# =============================================================================
# 3f. DecisionTraceRef — stable reference to an append-only decision trace entry
# =============================================================================


@dataclass
class DecisionTraceRef:
    """Lightweight reference to one entry in the append-only decision trace
    JSONL file.  The actual decision object (DoomedDecision or GWTWDecision)
    lives in the trace file; this ref lets a Trial point to its entries
    without embedding full nested objects.

    ``trace_path`` must be a relative path (no absolute paths, no ``..``
    traversal) pointing to the JSONL file from the session runs directory.
    """

    decision_id: str          # unique identifier of the decision entry
    trace_path: str           # relative path to the trace JSONL file

    def __post_init__(self) -> None:
        if not self.trace_path:
            raise ValueError("trace_path must not be empty")
        if self.trace_path.startswith("/"):
            raise ValueError(
                f"trace_path must be relative, got absolute: {self.trace_path!r}")
        if ".." in Path(self.trace_path).parts:
            raise ValueError(
                f"trace_path must not contain '..': {self.trace_path!r}")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> Optional["DecisionTraceRef"]:
        """Deserialize from dict; returns None for null/missing input."""
        if not d:
            return None
        return cls(**{k: v for k, v in d.items()
                      if k in cls.__dataclass_fields__})


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

    # ---- Stage D decision trace (Doomed + GWTW) ----
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

    # Stage C contract: command, start/end timestamps, report_path
    sr_full = StageResult(
        stage="CTS", status="ok", elapsed_s=8.0, exit_code=0,
        command="make -C <flow_dir> DESIGN_CONFIG=... CTS ...", start_time="2026-07-28T15:00:00+00:00",
        end_time="2026-07-28T15:00:08+00:00",
        report_path="reports/sky130hd/gcd/iter0/4_cts.json",
        stage_qor={"4_1_cts_ws_ps": -1200.0},
    )
    check(sr_full.command == "make -C <flow_dir> DESIGN_CONFIG=... CTS ...", "StageResult command recorded")
    check(sr_full.start_time == "2026-07-28T15:00:00+00:00", "StageResult start_time")
    check(sr_full.end_time is not None, "StageResult end_time set")
    check(sr_full.report_path is not None, "StageResult report_path set")
    sr_full_rt = StageResult.from_dict(sr_full.to_dict())
    check(sr_full_rt.command == sr_full.command, "StageResult roundtrip command")
    check(sr_full_rt.report_path == sr_full.report_path, "StageResult roundtrip report_path")

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

    # -- MinimalObservation --
    obs = MinimalObservation(
        trial_id="test001", stage="PL", status="ok",
        stage_wns_ps=-1200.0, stage_tns_ps=-5000.0,
        stage_elapsed_s=45.0, checkpoint_id="cp-test001-PL",
        parent_trial_id=None,
    )
    check(obs.trial_id == "test001", "MinimalObservation create")
    check(obs.stage_wns_ps == -1200.0, "MinimalObservation wns")
    obs2 = MinimalObservation.from_dict(obs.to_dict())
    check(obs2.stage == "PL", "MinimalObservation roundtrip")
    check(obs2.stage_elapsed_s == 45.0, "MinimalObservation roundtrip elapsed")
    check(MinimalObservation.from_dict(None) is None, "MinimalObservation from_dict(None) -> None")
    check(MinimalObservation.from_dict({}) is None, "MinimalObservation from_dict({}) -> None")

    # -- DoomedDecision --
    dd = DoomedDecision(
        risk_class="soft_bad", risk_score=0.4,
        reason_codes=["timing_negative", "stage_slow"],
        rule_version="1.0.0",
        input_evidence={"obs": obs.to_dict()},
    )
    check(dd.risk_class == "soft_bad", "DoomedDecision risk_class")
    check(len(dd.reason_codes) == 2, "DoomedDecision reason_codes")
    dd2 = DoomedDecision.from_dict(dd.to_dict())
    check(dd2.risk_score == 0.4, "DoomedDecision roundtrip")
    check(dd2.input_evidence == dd.input_evidence, "DoomedDecision roundtrip evidence")
    check(DoomedDecision.from_dict(None) is None, "DoomedDecision from_dict(None) -> None")
    check(DoomedDecision.from_dict({}) is None, "DoomedDecision from_dict({}) -> None")

    # -- GWTWDecision --
    gd = GWTWDecision(
        action="pause", decision_stage="PL", rank=3,
        parent_trial_id=None, child_trial_id=None,
        is_audit_pass=False, scheduler_version="1.0.0",
    )
    check(gd.action == "pause", "GWTWDecision action")
    check(gd.decision_stage == "PL", "GWTWDecision decision_stage")
    gd2 = GWTWDecision.from_dict(gd.to_dict())
    check(gd2.rank == 3, "GWTWDecision roundtrip")
    check(gd2.scheduler_version == "1.0.0", "GWTWDecision roundtrip version")
    check(GWTWDecision.from_dict(None) is None, "GWTWDecision from_dict(None) -> None")
    check(GWTWDecision.from_dict({}) is None, "GWTWDecision from_dict({}) -> None")

    # -- TrialRecord paused lifecycle --
    tr_paused = TrialRecord(
        trial_id="test_paused",
        experiment_id="smoke-gcd-v1",
        status="paused",
        params={"FP": {"CORE_UTILIZATION": 38}, "PL": {}, "CTS": {}, "RT": {}},
        stage_results=[StageResult(stage="FP", status="ok", elapsed_s=12.0),
                       StageResult(stage="PL", status="ok", elapsed_s=45.0)],
        checkpoint=CheckpointRef(
            checkpoint_id="cp-test_paused-PL", source_trial_id="test_paused",
            stage="PL", param_hash="sha256:deadbeef", orfs_commit="unresolved",
        ),
        final_qor=None,  # paused trials have no final QoR
    )
    check(tr_paused.status == "paused", "TrialRecord paused status accepted")
    check(tr_paused.checkpoint is not None, "TrialRecord paused preserves checkpoint")
    check(tr_paused.final_qor is None, "TrialRecord paused no final_qor")
    check(tr_paused.is_complete == False, "TrialRecord paused is_complete=False")
    check(tr_paused.failed_stage is None, "TrialRecord paused no failed stage")
    # Roundtrip paused trial
    tr_paused2 = TrialRecord.from_dict(tr_paused.to_dict())
    check(tr_paused2.status == "paused", "TrialRecord paused roundtrip status")
    check(tr_paused2.checkpoint.checkpoint_id == "cp-test_paused-PL", "TrialRecord paused roundtrip checkpoint")

    # -- TrialRecord with multi-stage decision trace roundtrip --
    dd_pl = DoomedDecision(
        risk_class="soft_bad", risk_score=0.4,
        reason_codes=["timing_negative"],
        rule_version="1.0.0",
        input_evidence={"stage": "PL"},
    )
    dd_cts = DoomedDecision(
        risk_class="survivor", risk_score=0.9,
        reason_codes=["timing_ok"],
        rule_version="1.0.0",
        input_evidence={"stage": "CTS"},
    )
    gd_pl = GWTWDecision(
        action="continue", decision_stage="PL", rank=2,
        scheduler_version="1.0.0",
    )
    gd_cts = GWTWDecision(
        action="finish", decision_stage="CTS", rank=1,
        scheduler_version="1.0.0",
    )
    tr_dt = TrialRecord(
        trial_id="test_decision",
        experiment_id="smoke-gcd-v1",
        status="ok",
        params={"FP": {"CORE_UTILIZATION": 38}},
        doomed_decisions=[dd_pl, dd_cts],
        gwtw_decisions=[gd_pl, gd_cts],
    )
    check(len(tr_dt.doomed_decisions) == 2, "TrialRecord doomed_decisions count")
    check(tr_dt.doomed_decisions[0].risk_class == "soft_bad", "TrialRecord doomed_decisions PL entry")
    check(tr_dt.doomed_decisions[1].risk_class == "survivor", "TrialRecord doomed_decisions CTS entry")
    check(len(tr_dt.gwtw_decisions) == 2, "TrialRecord gwtw_decisions count")
    check(tr_dt.gwtw_decisions[0].action == "continue", "TrialRecord gwtw_decisions PL entry")
    check(tr_dt.gwtw_decisions[1].action == "finish", "TrialRecord gwtw_decisions CTS entry")
    # Roundtrip through dict
    tr_dt2 = TrialRecord.from_dict(tr_dt.to_dict())
    check(len(tr_dt2.doomed_decisions) == 2, "TrialRecord decision trace roundtrip count doomed")
    check(tr_dt2.doomed_decisions[1].risk_class == "survivor", "TrialRecord decision trace roundtrip CTS class")
    check(len(tr_dt2.gwtw_decisions) == 2, "TrialRecord decision trace roundtrip count gwtw")
    check(tr_dt2.gwtw_decisions[1].action == "finish", "TrialRecord decision trace roundtrip CTS action")

    # -- Old Trial JSON backward compat (no doomed/gwtw keys) --
    old_dict = {
        "trial_id": "old001",
        "experiment_id": "old-test",
        "status": "ok",
        "params": {"FP": {}, "PL": {}, "CTS": {}, "RT": {}},
        "final_qor": {"wns_ps": -100.0, "tns_ps": -200.0, "area_um2": 500.0, "power_w": 0.01},
        "stage_results": [],
    }
    tr_old = TrialRecord.from_dict(old_dict)
    check(len(tr_old.doomed_decisions) == 0, "Old Trial JSON doomed_decisions=[]")
    check(len(tr_old.gwtw_decisions) == 0, "Old Trial JSON gwtw_decisions=[]")
    check(tr_old.execution_resolution is None, "Old Trial JSON execution_resolution=None")
    # Roundtrip old dict → Trial → dict → Trial preserves empties
    tr_old2 = TrialRecord.from_dict(tr_old.to_dict())
    check(len(tr_old2.doomed_decisions) == 0, "Old Trial JSON roundtrip doomed_decisions=[]")
    check(len(tr_old2.gwtw_decisions) == 0, "Old Trial JSON roundtrip gwtw_decisions=[]")

    # -- Legacy singular key backward compat --
    legacy_dict = {
        "trial_id": "legacy001",
        "experiment_id": "legacy-test",
        "status": "ok",
        "params": {"FP": {}},
        "doomed_decision": {"risk_class": "survivor", "reason_codes": []},
        "gwtw_decision": {"action": "continue", "decision_stage": "PL"},
        "stage_results": [],
    }
    tr_legacy = TrialRecord.from_dict(legacy_dict)
    check(len(tr_legacy.doomed_decisions) == 1, "Legacy singular doomed_decision → list of 1")
    check(tr_legacy.doomed_decisions[0].risk_class == "survivor", "Legacy doomed_decision class")
    check(len(tr_legacy.gwtw_decisions) == 1, "Legacy singular gwtw_decision → list of 1")
    check(tr_legacy.gwtw_decisions[0].action == "continue", "Legacy gwtw_decision action")

    # -- Enum validation --
    try:
        DoomedDecision(risk_class="invalid_class")
        check(False, "DoomedDecision invalid risk_class should raise ValueError")
    except ValueError as e:
        check("invalid_class" in str(e), f"DoomedDecision ValueError message: {e}")

    try:
        GWTWDecision(action="invalid_action", decision_stage="PL")
        check(False, "GWTWDecision invalid action should raise ValueError")
    except ValueError as e:
        check("invalid_action" in str(e), f"GWTWDecision action ValueError: {e}")

    try:
        GWTWDecision(action="continue", decision_stage="RT")
        check(False, "GWTWDecision invalid decision_stage should raise ValueError")
    except ValueError as e:
        check("RT" in str(e), f"GWTWDecision decision_stage ValueError: {e}")

    # -- DecisionTraceRef --
    ref = DecisionTraceRef(
        decision_id="dtr-001",
        trace_path="traces/decisions.jsonl",
    )
    check(ref.decision_id == "dtr-001", "DecisionTraceRef create")
    check(ref.trace_path == "traces/decisions.jsonl", "DecisionTraceRef trace_path")
    ref2 = DecisionTraceRef.from_dict(ref.to_dict())
    check(ref2.decision_id == "dtr-001", "DecisionTraceRef roundtrip")
    check(DecisionTraceRef.from_dict(None) is None, "DecisionTraceRef from_dict(None)")
    check(DecisionTraceRef.from_dict({}) is None, "DecisionTraceRef from_dict({})")
    # Reject absolute path
    try:
        DecisionTraceRef(decision_id="x", trace_path="/absolute/path/trace.jsonl")
        check(False, "absolute trace_path should raise ValueError")
    except ValueError as e:
        check("absolute" in str(e).lower(), f"absolute path message: {e}")
    # Reject ".." traversal
    try:
        DecisionTraceRef(decision_id="x", trace_path="../escape/trace.jsonl")
        check(False, "'..' in trace_path should raise ValueError")
    except ValueError as e:
        check(".." in str(e), f"'..' path message: {e}")
    # Reject empty
    try:
        DecisionTraceRef(decision_id="x", trace_path="")
        check(False, "empty trace_path should raise ValueError")
    except ValueError as e:
        check("empty" in str(e).lower(), f"empty path message: {e}")

    # -- TrialRecord decision_trace_refs roundtrip --
    tr_refs = TrialRecord(
        trial_id="test_refs",
        experiment_id="smoke",
        status="ok",
        decision_trace_refs=[
            DecisionTraceRef(decision_id="dtr-pl", trace_path="traces/decisions.jsonl"),
            DecisionTraceRef(decision_id="dtr-cts", trace_path="traces/decisions.jsonl"),
        ],
    )
    check(len(tr_refs.decision_trace_refs) == 2, "TrialRecord decision_trace_refs count")
    tr_refs2 = TrialRecord.from_dict(tr_refs.to_dict())
    check(len(tr_refs2.decision_trace_refs) == 2, "TrialRecord decision_trace_refs roundtrip count")
    check(tr_refs2.decision_trace_refs[0].decision_id == "dtr-pl", "roundtrip ref[0] id")
    check(tr_refs2.decision_trace_refs[1].decision_id == "dtr-cts", "roundtrip ref[1] id")

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
