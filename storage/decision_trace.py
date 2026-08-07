# -*- coding: utf-8 -*-
"""decision_trace.py — append-only decision trace JSONL writer / reader.

Pure Python, no ORFS, no LLM, no network.  All paths are validated to stay
within the session runs directory — absolute paths, ``..`` traversal, and
symlink escapes are rejected at construction / read time.

Usage:
    writer = DecisionTraceWriter(runs_dir)
    ref = writer.append({
        "entry_type": "doomed_decision",
        "trial_id": "abc12345",
        "data": doomed_decision.to_dict(),
        "cohort_stage": "PL",
        "cohort_seed": 42,
    })
    # ref.trace_path == "traces/decisions.jsonl"

    entries = read_trace(runs_dir, "traces/decisions.jsonl")

    # Idempotency guard: has this cohort already been executed?
    if cohort_already_executed(runs_dir, trace_path, "PL", seed=42,
                               trial_ids=["a","b"]):
        ...
"""

from __future__ import annotations

import io
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from core.models import DecisionTraceRef

log = logging.getLogger(__name__)

DEFAULT_TRACE_PATH: str = "traces/decisions.jsonl"


# =============================================================================
# Path containment
# =============================================================================


def _validate_trace_path(runs_dir: Path, trace_path: str) -> Path:
    """Reject trace paths that escape *runs_dir* and return the resolved
    absolute ``full_path``.

    Raises:
        ValueError: *trace_path* is empty, absolute, contains ``..``, or
            resolves outside *runs_dir*.
    """
    if not trace_path:
        raise ValueError("trace_path must not be empty")
    if trace_path.startswith("/"):
        raise ValueError(
            f"trace_path must be relative, got absolute: {trace_path!r}")
    if ".." in Path(trace_path).parts:
        raise ValueError(
            f"trace_path must not contain '..': {trace_path!r}")

    runs_dir = Path(runs_dir).resolve()
    full = (runs_dir / trace_path).resolve()

    # Containment: the resolved path must be inside runs_dir.
    try:
        full.relative_to(runs_dir)
    except ValueError:
        raise ValueError(
            f"trace_path {trace_path!r} resolves to {full} which is "
            f"outside runs_dir {runs_dir}"
        ) from None

    return full


# =============================================================================
# Writer
# =============================================================================


class DecisionTraceWriter:
    """Append-only JSONL writer for decision trace entries.

    Validates *trace_path* on construction so the file is always within
    *runs_dir*.
    """

    def __init__(
        self,
        runs_dir: Path,
        trace_path: str = DEFAULT_TRACE_PATH,
    ) -> None:
        self._runs_dir = Path(runs_dir).resolve()
        self._trace_path = trace_path
        self._full_path = _validate_trace_path(self._runs_dir, trace_path)

    @property
    def trace_path(self) -> str:
        return self._trace_path

    @property
    def full_path(self) -> Path:
        return self._full_path

    def append(self, entry: Dict[str, Any]) -> DecisionTraceRef:
        """Append one decision entry as a JSON line.

        Injects ``decision_id`` (``dtr-`` + 10 hex) and ``timestamp``
        (ISO-8601 UTC) when missing.  Returns a :class:`DecisionTraceRef`.
        """
        entry = dict(entry)
        decision_id: str = entry.setdefault(
            "decision_id", f"dtr-{uuid.uuid4().hex[:10]}")
        entry.setdefault("timestamp", datetime.now(timezone.utc).isoformat())

        self._full_path.parent.mkdir(parents=True, exist_ok=True)

        line = json.dumps(entry, ensure_ascii=False, sort_keys=True)
        with open(self._full_path, "a+", encoding="utf-8") as fh:
            _ensure_trailing_newline(fh)
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

        return DecisionTraceRef(
            decision_id=decision_id,
            trace_path=self._trace_path,
        )


# =============================================================================
# Reader
# =============================================================================


def read_trace(
    runs_dir: Path,
    trace_path: str,
) -> List[Dict[str, Any]]:
    """Read all valid entries from a decision trace JSONL file.

    Corrupt JSON, blank lines, and trailing partial lines are silently
    skipped.  Returns ``[]`` when the file does not exist.
    """
    full_path = _validate_trace_path(Path(runs_dir), trace_path)
    if not full_path.is_file():
        return []

    entries: List[Dict[str, Any]] = []
    with open(full_path, "r", encoding="utf-8") as fh:
        for lineno, raw_line in enumerate(fh, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                log.debug(
                    "[decision_trace] skipping corrupt line %d in %s: %s",
                    lineno, trace_path, exc,
                )
                continue
            entries.append(entry)

    return entries


# =============================================================================
# Cohort identity helpers (stable across runs)
# =============================================================================


def make_cohort_id(
    decision_stage: str,
    seed: int,
    trial_ids: List[str],
    survivor_count: int = 0,
    audit_quota: int = 0,
    population_size: int = 0,
    max_children_per_parent: int = 0,
    doomed_rule_version: str = "",
    scheduler_version: str = "",
    planner_version: str = "",
) -> str:
    """Return a collision-resistant, deterministic cohort identifier.

    The id is ``<stage>-s<seed>-<sha256_prefix>`` where the hash covers:
    trial IDs, planning config, and version strings — so different rules
    or scheduler versions on the same trial set produce different ids.
    """
    import hashlib
    canonical = json.dumps({
        "stage": decision_stage,
        "seed": seed,
        "trial_ids": sorted(trial_ids),
        "survivor_count": survivor_count,
        "audit_quota": audit_quota,
        "population_size": population_size,
        "max_children_per_parent": max_children_per_parent,
        "doomed_rule_version": doomed_rule_version,
        "scheduler_version": scheduler_version,
        "planner_version": planner_version,
    }, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode()).hexdigest()[:12]
    return f"{decision_stage}-s{seed}-{digest}"


# =============================================================================
# Idempotency guard (sentinel-based, not per-trial)
# =============================================================================


def _ensure_trailing_newline(fh) -> None:
    """If the open file ends without ``\\n``, write one so the next append
    starts on a clean line (crash-recovery measure)."""
    try:
        pos = fh.tell()
        if pos == 0:
            return
        fh.seek(max(0, pos - 1))
        last_byte = fh.read(1)
        fh.seek(pos)
        if last_byte and last_byte != "\n":
            fh.write("\n")
    except (OSError, io.UnsupportedOperation):
        pass


# =============================================================================
# Self-test
# =============================================================================
