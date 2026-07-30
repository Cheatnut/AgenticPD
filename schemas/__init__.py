# -*- coding: utf-8 -*-
"""schemas — Stage B data models for AgenticPD.

All models use stdlib dataclasses (no Pydantic dependency).
JSON Schema can be generated from these definitions when needed.
"""

from schemas.trial import (
    FailureClass,
    StageResult,
    CheckpointRef,
    CheckpointAuditEntry,
    ExecutionResolution,
    MinimalObservation,
    DoomedDecision,
    GWTWDecision,
    DecisionTraceRef,
    TrialRecord,
)
