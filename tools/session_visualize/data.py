# -*- coding: utf-8 -*-
"""session_visualize/data.py — session data loading, validation and extraction."""
from __future__ import annotations

import json
import logging
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional

from tools.session_visualize.render import (
    _augment_tree,
    _build_timeline,
    _extract_cohort,
)

log = logging.getLogger("session_visualize")

_UNKNOWN = "Unknown"

# Stage order for sorting / tree layout.
_STAGE_ORDER = ["FP", "PL", "CTS", "RT", "finish"]
_STAGE_IDX = {s: i for i, s in enumerate(_STAGE_ORDER)}

def _validate_dir(path: Path) -> Path:
    """Resolve *path* and require it to be an existing directory."""
    p = path.resolve()
    if not p.is_dir():
        raise FileNotFoundError(f"not a directory: {p}")
    return p


def _validate_contained(session_dir: Path, rel: str, *,
                        must_exist: bool = False) -> Path:
    """Reject *rel* if it escapes *session_dir* and return the resolved absolute path.

    Raises ValueError for empty, absolute, ``..``, or symlink-escape paths.
    If *must_exist* is True also raises FileNotFoundError when the path is
    missing.
    """
    if not rel:
        raise ValueError("relative path must not be empty")
    if rel.startswith("/"):
        raise ValueError(f"relative path must not be absolute: {rel!r}")
    if ".." in Path(rel).parts:
        raise ValueError(f"relative path must not contain '..': {rel!r}")

    session_dir = session_dir.resolve()
    full = (session_dir / rel).resolve()
    try:
        full.relative_to(session_dir)
    except ValueError:
        raise ValueError(
            f"path {rel!r} resolves to {full} which is outside "
            f"session_dir {session_dir}"
        ) from None

    if must_exist and not full.exists():
        raise FileNotFoundError(f"required file not found: {full}")
    return full


def _safe_relpath(session_dir: Path, value: Any) -> str:
    """Convert *value* to a path relative to *session_dir*.
    Returns the original string if relativizing fails, and never emits
    a path outside *session_dir*.
    """
    if not isinstance(value, str) or not value:
        return str(value) if value is not None else ""
    try:
        p = Path(value)
        if p.is_absolute():
            rel = str(p.resolve().relative_to(session_dir.resolve()))
            # Double-check containment.
            _validate_contained(session_dir, rel)
            return rel
        # Already relative — verify it doesn't escape.
        _validate_contained(session_dir, str(p))
        return str(p)
    except (ValueError, OSError, FileNotFoundError):
        # Path is outside session_dir — do not leak it.
        return "[redacted — path outside session]"


# =============================================================================
# Data loading
# =============================================================================


def load_config(session_dir: Path) -> Dict[str, Any]:
    """Read config_snapshot.json; return empty dict if missing."""
    try:
        cp = _validate_contained(session_dir, "config_snapshot.json",
                                 must_exist=True)
    except (ValueError, FileNotFoundError):
        log.warning("config_snapshot.json not found or rejected")
        return {}
    return json.loads(cp.read_text(encoding="utf-8"))


def load_trials(session_dir: Path) -> List[Dict[str, Any]]:
    """Read trials.jsonl with last-wins dedup by trial_id.

    Corrupt JSON lines are skipped with ``warnings.warn``.
    """
    try:
        tp = _validate_contained(session_dir, "trials.jsonl", must_exist=True)
    except (ValueError, FileNotFoundError):
        log.warning("trials.jsonl not found or rejected")
        return []

    trials_map: Dict[str, Dict[str, Any]] = {}
    trial_order: List[str] = []
    with open(tp, "r", encoding="utf-8") as fh:
        for lineno, raw_line in enumerate(fh, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                msg = (f"[session_visualize] skipping corrupt line {lineno} "
                       f"in trials.jsonl: {exc}")
                warnings.warn(msg)
                log.warning(msg)
                continue
            tid = entry.get("trial_id")
            if not tid:
                continue
            if tid not in trial_order:
                trial_order.append(tid)
            trials_map[tid] = entry  # last-wins

    return [trials_map[tid] for tid in trial_order]


def load_tree(session_dir: Path) -> Dict[str, Any]:
    """Read tree.json; return empty dict if missing or rejected."""
    try:
        tp = _validate_contained(session_dir, "tree.json", must_exist=True)
    except (ValueError, FileNotFoundError):
        log.warning("tree.json not found or rejected")
        return {}
    return json.loads(tp.read_text(encoding="utf-8"))


def load_traces(session_dir: Path) -> List[Dict[str, Any]]:
    """Read traces/decisions.jsonl; return ``[]`` if missing or rejected.

    Corrupt JSON lines are skipped with ``warnings.warn``.
    """
    try:
        tp = _validate_contained(session_dir, "traces/decisions.jsonl",
                                 must_exist=True)
    except (ValueError, FileNotFoundError):
        log.info("traces/decisions.jsonl not found — degraded mode")
        return []

    entries: List[Dict[str, Any]] = []
    with open(tp, "r", encoding="utf-8") as fh:
        for lineno, raw_line in enumerate(fh, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                msg = (f"[session_visualize] skipping corrupt line {lineno} "
                       f"in traces/decisions.jsonl: {exc}")
                warnings.warn(msg)
                log.warning(msg)
                continue
            entries.append(entry)
    return entries


# =============================================================================
# Data extraction
# =============================================================================


def _sanitize_er(er: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Keep only the display-relevant ExecutionResolution fields."""
    if er is None:
        return None
    return {
        "execution_mode": er.get("execution_mode", ""),
        "requested_start_stage": er.get("requested_start_stage"),
        "effective_start_stage": er.get("effective_start_stage"),
        "consumed_checkpoint": er.get("consumed_checkpoint"),
        "is_compatible": er.get("is_compatible"),
        "fallback_reason": er.get("fallback_reason"),
    }


def extract_session_data(session_dir: Path) -> Dict[str, Any]:
    """Assemble the ``session_data.json`` payload from raw inputs."""
    session_dir = Path(session_dir).resolve()
    cfg = load_config(session_dir)
    raw_trials = load_trials(session_dir)
    tree = load_tree(session_dir)
    traces = load_traces(session_dir)

    has_traces = len(traces) > 0

    # ---- Determine run mode ----
    mode = cfg.get("mode", "")
    if not mode:
        mode = _UNKNOWN
    mock_llm = cfg.get("mock_llm")
    mock_orfs = cfg.get("mock_orfs")

    # ---- Overview ----
    overview = {
        "experiment_id": cfg.get("experiment_id", _UNKNOWN),
        "platform": cfg.get("platform", _UNKNOWN),
        "design": cfg.get("design", _UNKNOWN),
        "population_size": cfg.get("population_size", 0),
        "seed": cfg.get("seed", 0),
        "max_trials": cfg.get("max_trials", 0),
        "wall_clock_budget_s": cfg.get("wall_clock_budget_s"),
        "decision_stages": cfg.get("decision_stages", []),
        "pl_survivor_count": cfg.get("pl_survivor_count", 0),
        "pl_audit_quota": cfg.get("pl_audit_quota", 0),
        "cts_survivor_count": cfg.get("cts_survivor_count", 0),
        "cts_audit_quota": cfg.get("cts_audit_quota", 0),
        "mode": mode,
        "mock_llm": mock_llm if mock_llm is not None else _UNKNOWN,
        "mock_orfs": mock_orfs if mock_orfs is not None else _UNKNOWN,
        "has_traces": has_traces,
        "session_dir_name": session_dir.name,
    }

    # ---- Trials (sanitized for JSON) ----
    trials_out: List[Dict[str, Any]] = []
    for t in raw_trials:
        report_path = ""
        for sr in t.get("stage_results", []):
            if sr.get("stage") == "finish" and sr.get("report_path"):
                report_path = _safe_relpath(session_dir, sr["report_path"])
                break
        trials_out.append({
            "trial_id": t.get("trial_id", ""),
            "parent_trial_id": t.get("parent_trial_id"),
            "branch_stage": t.get("branch_stage"),
            "status": t.get("status", ""),
            "params": t.get("params", {}),
            "stage_results": [
                {
                    "stage": sr.get("stage", ""),
                    "status": sr.get("status", ""),
                    "elapsed_s": sr.get("elapsed_s", 0),
                    "stage_qor": sr.get("stage_qor", {}),
                    "report_path": _safe_relpath(session_dir,
                                                 sr.get("report_path", "")),
                    "log_path": _safe_relpath(session_dir,
                                             sr.get("log_path", "")),
                }
                for sr in t.get("stage_results", [])
            ],
            "final_qor": t.get("final_qor"),
            "doomed_decisions": [
                {
                    "risk_class": dd.get("risk_class", ""),
                    "risk_score": dd.get("risk_score"),
                    "reason_codes": dd.get("reason_codes", []),
                    "input_evidence": dd.get("input_evidence", {}),
                }
                for dd in t.get("doomed_decisions", [])
            ],
            "gwtw_decisions": [
                {
                    "action": gd.get("action", ""),
                    "decision_stage": gd.get("decision_stage", ""),
                    "is_audit_pass": gd.get("is_audit_pass", False),
                    "rank": gd.get("rank"),
                }
                for gd in t.get("gwtw_decisions", [])
            ],
            "execution_resolution": _sanitize_er(t.get("execution_resolution")),
            "start_time": t.get("start_time"),
            "end_time": t.get("end_time"),
            "has_decision_trace": bool(t.get("decision_trace_refs")),
            "report_path": report_path,
        })

    # ---- Traces sanitized ----
    traces_out: List[Dict[str, Any]] = []
    for te in traces:
        traces_out.append({
            "entry_type": te.get("entry_type", ""),
            "trial_id": te.get("trial_id", ""),
            "cohort_stage": te.get("cohort_stage", ""),
            "cohort_id": te.get("cohort_id", ""),
            "decision_id": te.get("decision_id", ""),
            "timestamp": te.get("timestamp", ""),
            "data": te.get("data", {}),
            "judge_branch_node": te.get("judge_branch_node"),
            "judge_branch_stage": te.get("judge_branch_stage"),
            "judge_hints": te.get("judge_hints"),
            "judge_reason": te.get("judge_reason"),
            "is_fallback": te.get("is_fallback", False),
            "proposal_role": te.get("proposal_role"),
            "stage_proposals": te.get("stage_proposals"),
            "stage_fallbacks": te.get("stage_fallbacks"),
            "candidate_index": te.get("candidate_index"),
            "parent_trial_id": te.get("parent_trial_id"),
            "trial_ids": te.get("trial_ids", []),
            "population_size": te.get("population_size"),
            "survivor_count": te.get("survivor_count"),
            "rule_version": te.get("rule_version"),
            "scheduler_version": te.get("scheduler_version"),
            "planner_version": te.get("planner_version"),
        })

    # ---- Timeline: static evidence per decision event ----
    timeline = _build_timeline(traces_out, trials_out, has_traces, session_dir)

    # ---- Finish QoRs ----
    finish_qors: List[Dict[str, Any]] = []
    for t in trials_out:
        if t["status"] == "ok" and t["final_qor"]:
            finish_qors.append({
                "trial_id": t["trial_id"],
                "wns_ps": t["final_qor"].get("wns_ps"),
                "tns_ps": t["final_qor"].get("tns_ps"),
                "area_um2": t["final_qor"].get("area_um2"),
                "power_w": t["final_qor"].get("power_w"),
                "report_path": t["report_path"],
                "parent_trial_id": t["parent_trial_id"],
            })

    # ---- PL / CTS cohort extraction ----
    pl_cohort = _extract_cohort(trials_out, traces_out, "PL", has_traces, session_dir)
    cts_cohort = _extract_cohort(trials_out, traces_out, "CTS", has_traces, session_dir)

    # ---- Tree augmentation: add status and execution_mode to nodes ----
    _augment_tree(tree, trials_out)

    return {
        "overview": overview,
        "trials": trials_out,
        "tree": tree,
        "traces": traces_out,
        "timeline": timeline,
        "finish_qors": finish_qors,
        "pl_cohort": pl_cohort,
        "cts_cohort": cts_cohort,
    }


# =============================================================================
# Timeline: static evidence
# =============================================================================


