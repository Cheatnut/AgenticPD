# -*- coding: utf-8 -*-
"""core/decisions.py — Doomed/GWTW decision data models.

MinimalObservation, DoomedDecision, GWTWDecision and DecisionTraceRef
are self-contained dataclasses with to_dict/from_dict round-trips.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# 3c. MinimalObservation — stage-level observation for Doomed/GWTW input
# =============================================================================


@dataclass
class MinimalObservation:
    """Minimal per-trial observation at a decision stage (PL or CTS).

    Captures just enough data for the rule-based DoomedPredictor and
    GWTWScheduler to make a decision.  This is intentionally minimal —
    full trajectory features belong to a future learned predictor.
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
