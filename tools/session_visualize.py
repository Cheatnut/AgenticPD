# -*- coding: utf-8 -*-
"""session_visualize.py — Offline session visualization for AgenticPD sessions.

Reads a session directory (config_snapshot.json, trials.jsonl, tree.json,
and optionally traces/decisions.jsonl), deduplicates trials by trial_id
(last-wins), validates path containment, and generates a self-contained
``index.html`` + ``session_data.json`` under ``<session>/visualization/``.

Data is embedded directly in the HTML (``<script>`` tag) — no ``fetch()``,
no CDN, no network, no server, no new dependencies. Opens via ``file://``.

Usage:
    python3 tools/session_visualize.py runs/sky130hd_gcd/<session_name>
    python3 tools/session_visualize.py --help
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("session_visualize")

# Stage order for sorting / tree layout.
_STAGE_ORDER = ["FP", "PL", "CTS", "RT", "finish"]
_STAGE_IDX = {s: i for i, s in enumerate(_STAGE_ORDER)}

# Colour palette for stages.
_STAGE_COLORS = {
    "FP": "#4c72b0", "PL": "#55a868", "CTS": "#c44e52",
    "RT": "#8172b2", "finish": "#ccb974", "root": "#64b5cd",
}
# Colour palette for risk classes.
_RISK_COLORS = {
    "survivor": "#55a868", "soft_bad": "#f0ad4e", "hard_dead": "#d9534f",
}
# Trial-status colours (for tree nodes).
_STATUS_COLORS = {
    "ok": "#55a868", "paused": "#f0ad4e", "failed": "#d9534f",
    "running": "#64b5cd",
}

# Sentinel for unknown / missing metadata.
_UNKNOWN = "Unknown"


# =============================================================================
# Path containment
# =============================================================================


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


def _build_timeline(
    traces: List[Dict[str, Any]],
    trials: List[Dict[str, Any]],
    has_traces: bool,
    session_dir: Path,
) -> List[Dict[str, Any]]:
    """Build a chronological timeline of events from static trace evidence.

    When traces are available each ``agent_proposal``, ``observation``,
    ``doomed_decision``, ``gwtw_decision``, ``parent_selection``,
    ``judge_parent_selection``, ``fork``, ``execution_resolution``,
    ``cohort_complete`` entry becomes a timeline item with its original
    timestamp.

    When traces are absent, falls back to trial-level aggregates labeled
    ``Not recorded``.
    """
    events: List[Dict[str, Any]] = []

    if not has_traces:
        # Degraded: one bootstrap aggregate + per-trial summary.
        _add_degraded_timeline(events, trials)
        return events

    # Map trial_id → short id for display.
    def _sid(tid: str) -> str:
        return tid[:8] if tid else "?"

    for te in traces:
        etype = te.get("entry_type", "")
        ts = te.get("timestamp", "")
        tid = te.get("trial_id", "")
        cstage = te.get("cohort_stage", "")
        data = te.get("data", {}) if isinstance(te.get("data"), dict) else {}

        if etype == "agent_proposal":
            role = te.get("proposal_role", "")
            label = (f"Agent Proposal — {role}"
                     if role else "Agent Proposal")
            jr = te.get("judge_reason")
            events.append({
                "entry_type": etype,
                "stage": cstage or "bootstrap",
                "label": label,
                "trial_id": tid,
                "trial_ids": [tid],
                "timestamp": ts,
                "meta": _sid(tid),
                "detail": {
                    "candidate_index": te.get("candidate_index"),
                    "judge_branch_node": te.get("judge_branch_node"),
                    "judge_branch_stage": te.get("judge_branch_stage"),
                    "judge_reason": (jr or "")[:120] if jr else "",
                    "is_fallback": te.get("is_fallback"),
                    "stage_proposals": te.get("stage_proposals"),
                },
            })

        elif etype == "observation":
            obs_wns = data.get("stage_wns_ps")
            obs_tns = data.get("stage_tns_ps")
            rank = data.get("rank")
            status = data.get("status", "")
            info_parts = [f"rank={rank}"] if rank is not None else []
            if obs_wns is not None:
                info_parts.append(f"WNS={obs_wns:.1f}ps")
            if obs_tns is not None:
                info_parts.append(f"TNS={obs_tns:.1f}ps")
            events.append({
                "entry_type": etype,
                "stage": cstage,
                "label": f"Observation — {cstage}",
                "trial_id": tid,
                "trial_ids": [tid],
                "timestamp": ts,
                "meta": f"{_sid(tid)} {', '.join(info_parts)} {status}",
                "detail": {
                    "stage_wns_ps": obs_wns,
                    "stage_tns_ps": obs_tns,
                    "rank": rank,
                    "status": status,
                },
            })

        elif etype == "doomed_decision":
            rc = data.get("risk_class", "")
            score = data.get("risk_score")
            reasons = data.get("reason_codes", [])
            events.append({
                "entry_type": etype,
                "stage": cstage,
                "label": f"Doomed — {cstage} — {rc}",
                "trial_id": tid,
                "trial_ids": [tid],
                "timestamp": ts,
                "meta": (f"{_sid(tid)} risk={rc} "
                         f"score={score:.3f}" if score is not None
                         else f"{_sid(tid)} risk={rc}"),
                "detail": {
                    "risk_class": rc,
                    "risk_score": score,
                    "reason_codes": reasons,
                    "rule_version": te.get("rule_version", ""),
                },
            })

        elif etype == "gwtw_decision":
            action = data.get("action", "")
            is_audit = data.get("is_audit_pass", False)
            events.append({
                "entry_type": etype,
                "stage": cstage,
                "label": (f"GWTW — {cstage} — {action}"
                          + (" (audit pass)" if is_audit else "")),
                "trial_id": tid,
                "trial_ids": [tid],
                "timestamp": ts,
                "meta": f"{_sid(tid)} action={action}",
                "detail": {
                    "action": action,
                    "is_audit_pass": is_audit,
                    "scheduler_version": te.get("scheduler_version", ""),
                },
            })

        elif etype == "parent_selection":
            accepted = data.get("accepted", False)
            effective = data.get("effective_parent", "")
            events.append({
                "entry_type": etype,
                "stage": cstage,
                "label": f"Parent Selection — {cstage}"
                         + (" — accepted" if accepted else " — REJECTED"),
                "trial_id": tid,
                "trial_ids": [tid],
                "timestamp": ts,
                "meta": (f"effective={_sid(effective)}"
                         if effective else ""),
                "detail": {
                    "requested_parent": data.get("requested_parent", ""),
                    "effective_parent": effective,
                    "accepted": accepted,
                    "whitelist": data.get("whitelist", []),
                    "fallback_reason": data.get("fallback_reason", ""),
                },
            })

        elif etype == "judge_parent_selection":
            jd = data if data else {}
            resolved = jd.get("resolved_parent", "")
            gwtk = jd.get("gwtk_parent", "")
            judge_failed = jd.get("judge_failed", False)
            fallback = jd.get("judge_fallback_reason", False)
            events.append({
                "entry_type": etype,
                "stage": cstage,
                "label": ("Judge Parent Selection — " +
                          ("fallback" if (judge_failed or fallback) else "ok")),
                "trial_id": resolved or gwtk or tid,
                "trial_ids": [resolved] if resolved else [],
                "timestamp": ts,
                "meta": (f"resolved={_sid(resolved)} gwtk={_sid(gwtk)}"
                         if resolved else f"gwtk={_sid(gwtk)}"),
                "detail": {
                    "resolved_parent": resolved,
                    "gwtk_parent": gwtk,
                    "judge_failed": judge_failed,
                    "judge_fallback_reason": fallback,
                    "judge_reason": jd.get("judge_reason", ""),
                },
            })

        elif etype == "fork":
            parent = te.get("parent_trial_id", "")
            checkpoint_id = data.get("checkpoint_id", "") if data else ""
            events.append({
                "entry_type": etype,
                "stage": cstage,
                "label": f"Fork — {cstage}",
                "trial_id": tid,
                "trial_ids": [tid],
                "timestamp": ts,
                "meta": (f"child={_sid(tid)} ← parent={_sid(parent)}"
                         if parent else f"child={_sid(tid)}"),
                "detail": {
                    "parent_trial_id": parent,
                    "checkpoint_id": checkpoint_id,
                    "agent_params_provided": data.get("agent_params_provided") if data else None,
                    "agent_is_fallback": data.get("agent_is_fallback") if data else None,
                },
            })

        elif etype == "execution_resolution":
            er_data = data if data else {}
            mode = er_data.get("execution_mode", "")
            eff = er_data.get("effective_start_stage", "")
            consumed = er_data.get("consumed_checkpoint", "")
            parent = te.get("parent_trial_id", "")
            events.append({
                "entry_type": etype,
                "stage": cstage,
                "label": f"Execution Resolution — {mode}",
                "trial_id": tid,
                "trial_ids": [tid],
                "timestamp": ts,
                "meta": (f"child={_sid(tid)} mode={mode}"
                         + (f" start={eff}" if eff else "")
                         + (f" cp={consumed[:20]}" if consumed else "")),
                "detail": {
                    "execution_mode": mode,
                    "effective_start_stage": eff,
                    "consumed_checkpoint": consumed,
                    "parent_trial_id": parent,
                },
            })

        elif etype == "cohort_complete":
            c_tids = te.get("trial_ids", [])
            events.append({
                "entry_type": etype,
                "stage": cstage,
                "label": f"Cohort Complete — {cstage}",
                "trial_id": "",
                "trial_ids": c_tids,
                "timestamp": ts,
                "meta": f"{len(c_tids)} trials",
                "detail": {},
            })

    # Finally add finish events for each trial that reached finish.
    for t in trials:
        if t["status"] != "ok" or not t["final_qor"]:
            continue
        finish_sr = None
        for sr in t.get("stage_results", []):
            if sr["stage"] == "finish":
                finish_sr = sr
                break
        if finish_sr is None:
            continue
        events.append({
            "entry_type": "finish",
            "stage": "finish",
            "label": "Finish",
            "trial_id": t["trial_id"],
            "trial_ids": [t["trial_id"]],
            "timestamp": t.get("end_time") or t.get("start_time") or "",
            "meta": _sid(t["trial_id"]),
            "detail": {
                "wns_ps": t["final_qor"].get("wns_ps"),
                "tns_ps": t["final_qor"].get("tns_ps"),
                "area_um2": t["final_qor"].get("area_um2"),
                "power_w": t["final_qor"].get("power_w"),
                "report_path": t["report_path"],
            },
        })

    # Sort by timestamp then by stage order for ties.
    events.sort(key=_event_sort_key)
    return events


def _event_sort_key(ev: Dict[str, Any]) -> Tuple[str, int, str]:
    """Sort key: timestamp ascending, then stage index, then entry_type."""
    ts = ev.get("timestamp", "z")
    stage = ev.get("stage", "")
    si = _STAGE_IDX.get(stage, 99)
    # agent_proposal before observation before doomed before gwtw before fork
    # before execution_resolution before cohort_complete before finish.
    etype_order = {
        "agent_proposal": 0, "observation": 1, "doomed_decision": 2,
        "gwtw_decision": 3, "parent_selection": 4, "judge_parent_selection": 5,
        "fork": 6, "execution_resolution": 7, "cohort_complete": 8,
        "finish": 9,
    }
    eo = etype_order.get(ev.get("entry_type", ""), 50)
    return (ts, si, str(eo))


def _add_degraded_timeline(
    events: List[Dict[str, Any]],
    trials: List[Dict[str, Any]],
) -> None:
    """Build degraded timeline from trial aggregates when traces are absent."""
    bootstrap_tids = [t["trial_id"] for t in trials
                      if t["parent_trial_id"] is None
                      and t["branch_stage"] is None]
    if bootstrap_tids:
        ts = trials[0].get("start_time", "") if trials else ""
        events.append({
            "entry_type": "agent_proposal_degraded",
            "stage": "bootstrap",
            "label": f"Bootstrap Population ({len(bootstrap_tids)} candidates) — Not recorded",
            "trial_id": "",
            "trial_ids": bootstrap_tids,
            "timestamp": ts,
            "meta": "Not recorded",
            "detail": {"note": "traces/decisions.jsonl absent — per-trial embedded decisions used where available"},
        })

    for t in trials:
        tid = t["trial_id"]
        ts = t.get("start_time", "")
        for dd in t.get("doomed_decisions", []):
            ev = dd.get("input_evidence", {})
            stage = ev.get("stage", "")
            rc = dd.get("risk_class", "")
            events.append({
                "entry_type": "doomed_decision_degraded",
                "stage": stage,
                "label": f"Doomed — {stage} — {rc} — Not recorded",
                "trial_id": tid,
                "trial_ids": [tid],
                "timestamp": ts,
                "meta": f"{tid[:8]} risk={rc}",
                "detail": {"risk_class": rc, "source": "trial embedded"},
            })
        for gd in t.get("gwtw_decisions", []):
            stage = gd.get("decision_stage", "")
            action = gd.get("action", "")
            events.append({
                "entry_type": "gwtw_decision_degraded",
                "stage": stage,
                "label": f"GWTW — {stage} — {action} — Not recorded",
                "trial_id": tid,
                "trial_ids": [tid],
                "timestamp": ts,
                "meta": f"{tid[:8]} action={action}",
                "detail": {"action": action, "source": "trial embedded"},
            })
        if t["status"] == "ok" and t["final_qor"]:
            for sr in t.get("stage_results", []):
                if sr["stage"] == "finish":
                    events.append({
                        "entry_type": "finish",
                        "stage": "finish",
                        "label": "Finish",
                        "trial_id": tid,
                        "trial_ids": [tid],
                        "timestamp": t.get("end_time") or ts,
                        "meta": tid[:8],
                        "detail": {
                            "wns_ps": t["final_qor"].get("wns_ps"),
                            "tns_ps": t["final_qor"].get("tns_ps"),
                            "area_um2": t["final_qor"].get("area_um2"),
                            "power_w": t["final_qor"].get("power_w"),
                            "report_path": t["report_path"],
                        },
                    })

    events.sort(key=_event_sort_key)


# =============================================================================
# Cohort extraction
# =============================================================================


def _extract_cohort(
    trials: List[Dict[str, Any]],
    traces: List[Dict[str, Any]],
    decision_stage: str,
    has_traces: bool,
    session_dir: Path,
) -> Dict[str, Any]:
    """Extract PL or CTS cohort details from traces + trials."""
    obs_by_trial: Dict[str, Dict[str, Any]] = {}

    if has_traces:
        for te in traces:
            if te.get("cohort_stage") != decision_stage:
                continue
            data = te.get("data", {}) if isinstance(te.get("data"), dict) else {}
            tid = te["trial_id"]
            etype = te["entry_type"]

            if etype == "observation":
                obs_by_trial.setdefault(tid, {}).update({
                    "trial_id": tid,
                    "stage_wns_ps": data.get("stage_wns_ps"),
                    "stage_tns_ps": data.get("stage_tns_ps"),
                    "rank": data.get("rank"),
                    "status": data.get("status"),
                })
            elif etype == "doomed_decision":
                obs_by_trial.setdefault(tid, {}).update({
                    "risk_class": data.get("risk_class", ""),
                    "risk_score": data.get("risk_score"),
                    "reason_codes": data.get("reason_codes", []),
                    "rule_version": te.get("rule_version", ""),
                })
            elif etype == "gwtw_decision":
                obs_by_trial.setdefault(tid, {}).update({
                    "gwtw_action": data.get("action", ""),
                    "gwtw_is_audit_pass": data.get("is_audit_pass", False),
                })

    if not obs_by_trial:
        # Fall back to trial-embedded decisions.
        for t in trials:
            for dd in t.get("doomed_decisions", []):
                ev = dd.get("input_evidence", {})
                if ev.get("stage") == decision_stage:
                    obs_by_trial[t["trial_id"]] = {
                        "trial_id": t["trial_id"],
                        "stage_wns_ps": ev.get("stage_wns_ps"),
                        "stage_tns_ps": ev.get("stage_tns_ps"),
                        "rank": ev.get("rank"),
                        "status": ev.get("status"),
                        "risk_class": dd.get("risk_class", ""),
                        "risk_score": dd.get("risk_score"),
                        "reason_codes": dd.get("reason_codes", []),
                        "rule_version": dd.get("rule_version", ""),
                        "gwtw_action": "",
                        "gwtw_is_audit_pass": False,
                    }
            for gd in t.get("gwtw_decisions", []):
                if gd.get("decision_stage") == decision_stage:
                    if t["trial_id"] in obs_by_trial:
                        obs_by_trial[t["trial_id"]].update({
                            "gwtw_action": gd.get("action", ""),
                            "gwtw_is_audit_pass": gd.get("is_audit_pass", False),
                        })

    survivor_whitelist = [
        tid for tid, ob in obs_by_trial.items()
        if ob.get("risk_class") == "survivor"
    ]

    paused = [
        tid for tid, ob in obs_by_trial.items()
        if ob.get("gwtw_action") == "pause"
    ]

    hard_dead = [
        tid for tid, ob in obs_by_trial.items()
        if ob.get("risk_class") == "hard_dead"
    ]

    # Fork children from traces or trials.
    children: List[Dict[str, Any]] = []
    if has_traces:
        for te in traces:
            if te.get("cohort_stage") != decision_stage:
                continue
            if te["entry_type"] != "fork":
                continue
            data = te.get("data", {}) if isinstance(te.get("data"), dict) else {}
            child_tid = te["trial_id"]
            parent_tid = te.get("parent_trial_id", data.get("parent_trial_id", ""))
            child_trial = next((t for t in trials if t["trial_id"] == child_tid), {})
            children.append({
                "trial_id": child_tid,
                "parent_trial_id": parent_tid,
                "params": child_trial.get("params", {}),
                "execution_resolution": child_trial.get("execution_resolution"),
                "agent_params_provided": data.get("agent_params_provided", False),
                "agent_is_fallback": data.get("agent_is_fallback", False),
            })
    else:
        for t in trials:
            if t.get("parent_trial_id") and t.get("branch_stage") == decision_stage:
                children.append({
                    "trial_id": t["trial_id"],
                    "parent_trial_id": t["parent_trial_id"],
                    "params": t.get("params", {}),
                    "execution_resolution": t.get("execution_resolution"),
                    "agent_params_provided": False,
                    "agent_is_fallback": False,
                })

    return {
        "observations": list(obs_by_trial.values()),
        "survivor_whitelist": survivor_whitelist,
        "children": children,
        "paused": paused,
        "hard_dead": hard_dead,
        "has_traces": has_traces,
    }


# =============================================================================
# Tree augmentation
# =============================================================================


def _augment_tree(
    tree: Dict[str, Any],
    trials: List[Dict[str, Any]],
) -> None:
    """Annotate tree nodes with trial status and execution mode for rendering.

    Modifies *tree* in-place.
    """
    trial_map = {t["trial_id"]: t for t in trials}
    nodes = tree.get("nodes", {})
    for _nid, nd in nodes.items():
        stid = nd.get("source_trial_id")
        if stid and stid in trial_map:
            tr = trial_map[stid]
            nd["_trial_status"] = tr.get("status", "")
            nd["_trial_branch"] = tr.get("branch_stage")
            nd["_trial_parent"] = tr.get("parent_trial_id")
            er = tr.get("execution_resolution")
            nd["_execution_mode"] = er.get("execution_mode", "") if er else ""
        else:
            nd.setdefault("_trial_status", "")
            nd.setdefault("_execution_mode", "")


# =============================================================================
# Data serialization for safe embedding
# =============================================================================


def _json_embed_safe(data: Any) -> str:
    """Serialize *data* to JSON and escape it for safe embedding inside a
    ``<script>`` tag.

    Handles:
    - ``</script>`` → ``<\\/script>`` (prevents premature closing)
    - ``</`` sequences (defense-in-depth)
    - ``\\u2028``, ``\\u2029`` (line separators that are valid in JSON but
      break JavaScript string literals)
    """
    raw = json.dumps(data, ensure_ascii=False, indent=None, default=str,
                     separators=(",", ":"))
    # Escape </script> and </ in general.
    raw = raw.replace("</script>", "<\\/script>")
    raw = raw.replace("</", "<\\/")
    # Escape Unicode line separators.
    raw = raw.replace(" ", "\\u2028")
    raw = raw.replace(" ", "\\u2029")
    return raw


# =============================================================================
# HTML template
# =============================================================================


def _html_template() -> str:
    """Return the self-contained HTML page template with ``__DATA_PLACEHOLDER__``
    where the embedded JSON goes."""
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AgenticPD Session Visualization</title>
<style>
/* =============================================================================
   Base & Variables
   ============================================================================= */
:root {
  --bg: #f8f9fa; --card-bg: #ffffff; --text: #212529; --text-muted: #6c757d;
  --border: #dee2e6; --accent: #4c72b0; --accent-green: #55a868;
  --accent-red: #d9534f; --accent-yellow: #f0ad4e; --accent-purple: #8172b2;
  --radius: 6px; --shadow: 0 1px 3px rgba(0,0,0,.08);
  --font-mono: 'SF Mono','Fira Code','Consolas',monospace;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
  background:var(--bg);color:var(--text);line-height:1.5;padding:16px}
h1{font-size:1.4rem;margin-bottom:4px}
h2{font-size:1.15rem;margin:16px 0 8px;padding-bottom:4px;border-bottom:2px solid var(--border)}
h3{font-size:1.0rem;margin:12px 0 6px}

/* Layout */
.container{max-width:1100px;margin:0 auto}
.header{background:var(--card-bg);border-radius:var(--radius);padding:14px 18px;
  box-shadow:var(--shadow);margin-bottom:12px;display:flex;justify-content:space-between;
  align-items:center;flex-wrap:wrap;gap:8px}
.header .badges{display:flex;gap:6px;flex-wrap:wrap}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:.75rem;font-weight:600}
.badge-ok{background:#d4edda;color:#155724}
.badge-warn{background:#fff3cd;color:#856404}
.badge-degraded{background:#f8d7da;color:#721c24}
.badge-info{background:#d1ecf1;color:#0c5460}

/* Cards */
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:10px;margin-bottom:12px}
.card{background:var(--card-bg);border-radius:var(--radius);padding:10px 14px;box-shadow:var(--shadow)}
.card .label{font-size:.7rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:.05em}
.card .value{font-size:1.1rem;font-weight:600;margin-top:2px}
.card .value.small{font-size:.85rem}

/* Sections */
.section{margin-bottom:12px}
.section-header{cursor:pointer;user-select:none;display:flex;align-items:center;gap:8px}
.section-header .arrow{transition:transform .2s;font-size:.8rem}
.section-header.open .arrow{transform:rotate(90deg)}
.section-body{display:none;margin-top:8px}
.section-body.open{display:block}

/* Timeline */
.timeline{position:relative;padding-left:28px;margin:12px 0}
.timeline::before{content:'';position:absolute;left:10px;top:4px;bottom:4px;
  width:2px;background:var(--border)}
.tl-item{position:relative;margin-bottom:10px;padding:8px 12px;
  background:var(--card-bg);border-radius:var(--radius);box-shadow:var(--shadow)}
.tl-item::before{content:'';position:absolute;left:-22px;top:12px;
  width:10px;height:10px;border-radius:50%;border:2px solid var(--text-muted);
  background:var(--card-bg)}
.tl-item.agent_proposal::before,.tl-item.agent_proposal_degraded::before{background:var(--accent);border-color:var(--accent)}
.tl-item.observation::before{background:#17a2b8;border-color:#17a2b8}
.tl-item.doomed_decision::before,.tl-item.doomed_decision_degraded::before{background:var(--accent-red);border-color:var(--accent-red)}
.tl-item.gwtw_decision::before,.tl-item.gwtw_decision_degraded::before{background:var(--accent-yellow);border-color:var(--accent-yellow)}
.tl-item.parent_selection::before,.tl-item.judge_parent_selection::before{background:var(--accent-purple);border-color:var(--accent-purple)}
.tl-item.fork::before{background:#ff6b6b;border-color:#ff6b6b}
.tl-item.execution_resolution::before{background:#20c997;border-color:#20c997}
.tl-item.cohort_complete::before{background:var(--text-muted);border-color:var(--text-muted)}
.tl-item.finish::before{background:var(--accent-green);border-color:var(--accent-green)}
.tl-item .tl-label{font-weight:600;font-size:.88rem}
.tl-item .tl-meta{font-size:.73rem;color:var(--text-muted);margin-top:2px}
.tl-item .tl-etype{font-size:.65rem;color:var(--text-muted);text-transform:uppercase;letter-spacing:.04em}
.tl-item .tl-tid{font-family:var(--font-mono);font-size:.7rem;background:#e9ecef;
  padding:1px 6px;border-radius:4px;display:inline-block;margin:2px 2px 0 0}

/* Tables */
table{width:100%;border-collapse:collapse;font-size:.82rem;margin:8px 0}
th,td{padding:5px 8px;text-align:left;border-bottom:1px solid var(--border)}
th{background:#f1f3f5;font-weight:600;font-size:.75rem;color:var(--text-muted)}
tr:hover{background:#f8f9fa}
td.best{background:#d4edda} td.worst{background:#f8d7da}
.risk-survivor{color:var(--accent-green);font-weight:600}
.risk-soft_bad{color:var(--accent-yellow);font-weight:600}
.risk-hard_dead{color:var(--accent-red);font-weight:600}

/* Trials */
.trial-card{background:var(--card-bg);border-radius:var(--radius);box-shadow:var(--shadow);
  margin-bottom:8px;overflow:hidden}
.trial-header{cursor:pointer;user-select:none;padding:8px 12px;display:flex;
  justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}
.trial-header:hover{background:#f8f9fa}
.trial-header .tid{font-family:var(--font-mono);font-weight:600;font-size:.9rem}
.trial-header .status{font-size:.75rem;padding:1px 8px;border-radius:10px;font-weight:600}
.trial-header .meta{font-size:.75rem;color:var(--text-muted)}
.trial-body{display:none;padding:8px 12px;border-top:1px solid var(--border)}
.trial-body.open{display:block}
.status-ok{background:#d4edda;color:#155724}
.status-paused{background:#fff3cd;color:#856404}
.status-failed{background:#f8d7da;color:#721c24}
.status-running{background:#d1ecf1;color:#0c5460}
.stage-row{display:flex;align-items:center;gap:4px;margin:4px 0;font-size:.78rem}
.stage-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.stage-label{font-family:var(--font-mono);font-size:.72rem;min-width:24px}
.stage-elapsed{font-size:.7rem;color:var(--text-muted);margin-left:auto}
.params-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:4px;margin:6px 0}
.param-kv{font-family:var(--font-mono);font-size:.75rem;padding:2px 6px;
  background:#f1f3f5;border-radius:3px}

/* Tree */
.tree-svg{width:100%;overflow-x:auto;margin:8px 0}
.tree-svg svg{display:block;margin:0 auto}
.node-rect{cursor:pointer;transition:opacity .15s}
.node-rect:hover{opacity:.75}
.edge-line{stroke:#adb5bd;stroke-width:1.5;fill:none}
.edge-line.fork-checkpoint{stroke:#28a745;stroke-width:2.0;stroke-dasharray:6,3}
.edge-line.fork-fullrestart{stroke:#d9534f;stroke-width:2.0;stroke-dasharray:3,3}

/* Whitelist */
.whitelist{display:flex;flex-wrap:wrap;gap:4px;margin:6px 0}
.whitelist .wl-tid{font-family:var(--font-mono);font-size:.72rem;
  background:#d4edda;color:#155724;padding:2px 8px;border-radius:4px;font-weight:600}

/* Filters */
.filters{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
.filters button{padding:4px 12px;border:1px solid var(--border);border-radius:var(--radius);
  background:var(--card-bg);cursor:pointer;font-size:.78rem}
.filters button.active{background:var(--accent);color:#fff;border-color:var(--accent)}

/* Detail expander */
.detail-toggle{font-size:.7rem;color:var(--accent);cursor:pointer;margin-left:6px}
.detail-block{display:none;margin-top:4px;padding:6px 8px;background:#f8f9fa;
  border-radius:4px;font-size:.73rem}
.detail-block.open{display:block}

@media(max-width:700px){
  .cards{grid-template-columns:repeat(2,1fr)}
  .header{flex-direction:column;align-items:flex-start}
}
</style>
</head>
<body>
<div class="container">
<div class="header">
  <div><h1 id="page-title">AgenticPD Session</h1>
    <span style="font-size:.8rem;color:var(--text-muted)" id="page-subtitle"></span></div>
  <div class="badges" id="header-badges"></div>
</div>
<div class="cards" id="overview-cards"></div>

<div class="section">
  <div class="section-header open" onclick="toggleSection(this,'timeline-body')">
    <span class="arrow">&#9654;</span><h2 style="margin:0;border:none;padding:0">Timeline</h2>
    <span style="font-size:.75rem;color:var(--text-muted)" id="timeline-summary"></span>
  </div>
  <div class="section-body open" id="timeline-body">
    <div class="timeline" id="timeline"></div>
  </div>
</div>

<div class="section">
  <div class="section-header" onclick="toggleSection(this,'pl-cohort-body')">
    <span class="arrow">&#9654;</span><h2 style="margin:0;border:none;padding:0">PL Cohort</h2>
    <span style="font-size:.75rem;color:var(--text-muted)" id="pl-cohort-summary"></span>
  </div>
  <div class="section-body" id="pl-cohort-body"></div>
</div>

<div class="section">
  <div class="section-header" onclick="toggleSection(this,'cts-cohort-body')">
    <span class="arrow">&#9654;</span><h2 style="margin:0;border:none;padding:0">CTS Cohort</h2>
    <span style="font-size:.75rem;color:var(--text-muted)" id="cts-cohort-summary"></span>
  </div>
  <div class="section-body" id="cts-cohort-body"></div>
</div>

<div class="section">
  <div class="section-header open" onclick="toggleSection(this,'trials-body')">
    <span class="arrow">&#9654;</span><h2 style="margin:0;border:none;padding:0">Trials <span id="trial-count"></span></h2>
  </div>
  <div class="section-body open" id="trials-body">
    <div class="filters" id="trial-filters"></div>
    <div id="trial-list"></div>
  </div>
</div>

<div class="section">
  <div class="section-header" onclick="toggleSection(this,'tree-body')">
    <span class="arrow">&#9654;</span><h2 style="margin:0;border:none;padding:0">Optimization Tree</h2>
    <span style="font-size:.75rem;color:var(--text-muted)" id="tree-summary"></span>
  </div>
  <div class="section-body" id="tree-body">
    <div class="tree-svg" id="tree-svg"></div>
  </div>
</div>

<div class="section">
  <div class="section-header open" onclick="toggleSection(this,'qor-body')">
    <span class="arrow">&#9654;</span><h2 style="margin:0;border:none;padding:0">Finish QoR Comparison</h2>
    <span style="font-size:.75rem;color:var(--text-muted)" id="qor-summary"></span>
  </div>
  <div class="section-body open" id="qor-body">
    <div id="qor-table-container"></div>
  </div>
</div>
</div>

<script>
var DATA = __DATA_PLACEHOLDER__;

(function(){
if (!DATA || typeof DATA !== 'object') {
  document.body.innerHTML = '<div style="padding:40px;text-align:center;color:#d9534f">'
    + '<h2>Data Error</h2><p>Embedded session data is missing or corrupt.</p></div>';
  return;
}
renderAll();
})();

function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function shortId(id) { return String(id).slice(0,8); }

window.toggleSection = function(headerEl, bodyId) {
  headerEl.classList.toggle('open');
  document.getElementById(bodyId).classList.toggle('open');
};
window.toggleTrial = function(tid) {
  var body = document.getElementById('trial-body-' + tid);
  if (body) body.classList.toggle('open');
};
window.toggleDetail = function(ev, detailId) {
  ev.stopPropagation();
  var el = document.getElementById(detailId);
  if (el) el.classList.toggle('open');
};

// =============================================================================
// Render all
// =============================================================================
function renderAll() {
  renderHeader();
  renderOverview();
  renderTimeline();
  renderCohort('pl');
  renderCohort('cts');
  renderTrials();
  renderTree();
  renderQor();
}

// =============================================================================
// Header
// =============================================================================
function renderHeader() {
  var ov = DATA.overview;
  document.getElementById('page-title').textContent =
    'AgenticPD Session — ' + esc(ov.experiment_id || '?');
  document.getElementById('page-subtitle').textContent =
    esc(ov.platform || '?') + ' / ' + esc(ov.design || '?') + ' — ' + esc(ov.session_dir_name || '');

  var badges = '';
  var mode = ov.mode || '';
  if (mode === 'stage-e') badges += '<span class="badge badge-info">Stage E</span>';
  else if (mode === 'stage-d') badges += '<span class="badge badge-info">Stage D</span>';
  else badges += '<span class="badge badge-warn">Mode: ' + esc(String(mode)) + '</span>';

  if (ov.has_traces) badges += '<span class="badge badge-ok">Full Trace</span>';
  else badges += '<span class="badge badge-degraded">Not recorded</span>';

  if (ov.mock_llm === true) badges += '<span class="badge badge-warn">Mock LLM</span>';
  else if (ov.mock_llm === false) badges += '<span class="badge badge-ok">Real LLM</span>';
  else badges += '<span class="badge badge-warn">LLM: Unknown</span>';

  if (ov.mock_orfs === true) badges += '<span class="badge badge-warn">Mock ORFS</span>';
  else if (ov.mock_orfs === false) badges += '<span class="badge badge-ok">Real ORFS</span>';
  else badges += '<span class="badge badge-warn">ORFS: Unknown</span>';

  document.getElementById('header-badges').innerHTML = badges;
}

// =============================================================================
// Overview cards
// =============================================================================
function renderOverview() {
  var ov = DATA.overview;
  var items = [
    ['Experiment', ov.experiment_id],
    ['Platform / Design', ov.platform + ' / ' + ov.design],
    ['Population', ov.population_size],
    ['Seed', ov.seed],
    ['Max Trials', ov.max_trials],
    ['Wall Clock', (ov.wall_clock_budget_s != null ? ov.wall_clock_budget_s + 's' : '∞')],
    ['Decision Stages', (ov.decision_stages || []).join(', ') || '-'],
    ['PL survivor / audit', ov.pl_survivor_count + ' / ' + ov.pl_audit_quota],
    ['CTS survivor / audit', ov.cts_survivor_count + ' / ' + ov.cts_audit_quota],
  ];
  document.getElementById('overview-cards').innerHTML = items.map(function(c) {
    return '<div class="card"><div class="label">' + esc(c[0]) + '</div>'
      + '<div class="value small">' + esc(String(c[1] != null ? c[1] : '-')) + '</div></div>';
  }).join('');
}

// =============================================================================
// Timeline
// =============================================================================
function renderTimeline() {
  var tl = DATA.timeline || [];
  document.getElementById('timeline-summary').textContent = '(' + tl.length + ' events)';
  if (!tl.length) {
    document.getElementById('timeline').innerHTML = '<p style="color:var(--text-muted)">No timeline events</p>';
    return;
  }
  var html = '';
  tl.forEach(function(ev) {
    var etype = ev.entry_type || '';
    var isDegraded = etype.indexOf('_degraded') >= 0;
    var badge = isDegraded ? ' <span class="badge badge-degraded" style="font-size:.6rem">Not recorded</span>' : '';
    var ts = ev.timestamp ? ev.timestamp.replace('T',' ').slice(0,19) : '';
    html += '<div class="tl-item ' + esc(etype) + '">'
      + '<div class="tl-etype">' + esc(etype) + '</div>'
      + '<div class="tl-label">' + esc(ev.label) + badge + '</div>'
      + '<div class="tl-meta">' + esc(ts) + '  ' + esc(ev.meta || '') + '</div>';
    if (ev.detail && Object.keys(ev.detail).length) {
      var detailId = 'dtl-' + Math.random().toString(36).slice(2,8);
      html += '<span class="detail-toggle" onclick="toggleDetail(event,\'' + detailId + '\')">[+]</span>';
      html += '<div class="detail-block" id="' + detailId + '"><pre style="font-size:.7rem;white-space:pre-wrap">'
        + esc(JSON.stringify(ev.detail, null, 2)) + '</pre></div>';
    }
    if (ev.trial_ids && ev.trial_ids.length && ev.trial_ids[0]) {
      html += '<div style="margin-top:2px">'
        + ev.trial_ids.filter(Boolean).map(function(t){return '<span class="tl-tid">'+esc(shortId(t))+'</span>';}).join(' ')
        + '</div>';
    }
    html += '</div>';
  });
  document.getElementById('timeline').innerHTML = html;
}

// =============================================================================
// Cohort
// =============================================================================
function renderCohort(stage) {
  var key = stage === 'pl' ? 'pl_cohort' : 'cts_cohort';
  var cohort = DATA[key] || {};
  var obs = cohort.observations || [];
  var children = cohort.children || [];
  var whitelist = cohort.survivor_whitelist || [];
  var paused = cohort.paused || [];
  var hard = cohort.hard_dead || [];
  var hasTraces = cohort.has_traces;

  var summary = document.getElementById(key.replace('_','-') + '-summary');
  var stageLabel = stage.toUpperCase();
  var nSurv = obs.filter(function(o){return o.risk_class==='survivor'}).length;
  var nSoft = obs.filter(function(o){return o.risk_class==='soft_bad'}).length;
  var nHard = obs.filter(function(o){return o.risk_class==='hard_dead'}).length;
  summary.textContent = nSurv + ' survivor, ' + nSoft + ' soft_bad, ' + nHard
    + ' hard_dead | ' + children.length + ' forks | ' + paused.length + ' paused | ' + hard.length + ' hard_dead';
  if (!hasTraces) summary.textContent += ' (Not recorded)';

  var html = '';
  if (!hasTraces) {
    html += '<p style="color:#721c24;font-size:.78rem;margin-bottom:8px">'
      + '⚠ Not recorded — traces/decisions.jsonl absent; data from trials.jsonl embedded decisions.</p>';
  }

  if (whitelist.length) {
    html += '<h3>Survivor Whitelist</h3><div class="whitelist">'
      + whitelist.map(function(t){return '<span class="wl-tid">'+esc(shortId(t))+'</span>';}).join('')
      + '</div>';
  }

  if (obs.length) {
    html += '<h3>Observations &amp; Doomed / GWTW</h3><table><thead><tr>'
      + '<th>Trial</th><th>WNS (ps)</th><th>TNS (ps)</th><th>Rank</th>'
      + '<th>Risk Class</th><th>Risk Score</th><th>Reason Codes</th>'
      + '<th>GWTW Action</th><th>Audit?</th></tr></thead><tbody>';
    obs.forEach(function(o) {
      var rc = o.risk_class || '';
      html += '<tr>'
        + '<td style="font-family:var(--font-mono);font-size:.78rem">'+esc(shortId(o.trial_id))+'</td>'
        + '<td>'+(o.stage_wns_ps!=null?o.stage_wns_ps.toFixed(1):'-')+'</td>'
        + '<td>'+(o.stage_tns_ps!=null?o.stage_tns_ps.toFixed(1):'-')+'</td>'
        + '<td>'+(o.rank!=null?o.rank:'-')+'</td>'
        + '<td class="risk-'+rc+'">'+esc(rc||'-')+'</td>'
        + '<td>'+(o.risk_score!=null?o.risk_score.toFixed(3):'-')+'</td>'
        + '<td style="font-size:.72rem">'+esc((o.reason_codes||[]).join(', ')||'-')+'</td>'
        + '<td>'+esc(o.gwtw_action||'-')+'</td>'
        + '<td>'+(o.gwtw_is_audit_pass?'✓':'-')+'</td></tr>';
    });
    html += '</tbody></table>';
  } else {
    html += '<p style="color:var(--text-muted);font-size:.8rem">Not recorded</p>';
  }

  if (children.length) {
    html += '<h3>Fork Children (' + children.length + ')</h3><table><thead><tr>'
      + '<th>Child</th><th>Parent</th><th>Execution Mode</th>'
      + '<th>Effective Start</th><th>Agent Params?</th><th>Fallback?</th>'
      + '<th>Checkpoint</th></tr></thead><tbody>';
    children.forEach(function(c) {
      var er = c.execution_resolution || {};
      html += '<tr>'
        + '<td style="font-family:var(--font-mono);font-size:.78rem">'+esc(shortId(c.trial_id))+'</td>'
        + '<td style="font-family:var(--font-mono);font-size:.78rem">'+esc(shortId(c.parent_trial_id||''))+'</td>'
        + '<td>'+esc(er.execution_mode||'-')+'</td>'
        + '<td>'+esc(er.effective_start_stage||'-')+'</td>'
        + '<td>'+(c.agent_params_provided?'✓':'-')+'</td>'
        + '<td>'+(c.agent_is_fallback?'<span style="color:#d9534f">Yes</span>':'-')+'</td>'
        + '<td style="font-size:.7rem">'+esc((er.consumed_checkpoint||'-').slice(0,24))+'</td></tr>';
    });
    html += '</tbody></table>';
  }

  if (paused.length) {
    html += '<h3>Paused Trials</h3><div class="whitelist" style="background:#fff3cd">'
      + paused.map(function(t){return '<span class="wl-tid" style="background:#fff3cd;color:#856404">'+esc(shortId(t))+'</span>';}).join('')+'</div>';
  }
  document.getElementById(key.replace('_','-') + '-body').innerHTML = html;
}

// =============================================================================
// Trials
// =============================================================================
function renderTrials() {
  var trials = DATA.trials || [];
  document.getElementById('trial-count').textContent = '(' + trials.length + ')';

  var counts = {};
  trials.forEach(function(t){counts[t.status]=(counts[t.status]||0)+1;});
  var labels = {ok:'OK',paused:'Paused',failed:'Failed',running:'Running'};
  var html = '<button class="active" onclick="filterTrials(\'all\',this)">All ('+trials.length+')</button>';
  Object.keys(counts).forEach(function(s){
    html += '<button onclick="filterTrials(\''+s+'\',this)">'+esc(labels[s]||s)+' ('+counts[s]+')</button>';
  });
  document.getElementById('trial-filters').innerHTML = html;
  window._trialData = trials;
  renderTrialList(trials);
}

window.filterTrials = function(status, btn) {
  document.querySelectorAll('#trial-filters button').forEach(function(b){b.classList.remove('active');});
  btn.classList.add('active');
  var filtered = status === 'all' ? window._trialData
    : window._trialData.filter(function(t){return t.status === status;});
  renderTrialList(filtered);
};

function renderTrialList(trials) {
  var html = '';
  trials.forEach(function(t) {
    var hasTrace = t.has_decision_trace;
    var stHtml = '';
    var stageColors = {FP:'#4c72b0',PL:'#55a868',CTS:'#c44e52',RT:'#8172b2',finish:'#ccb974'};
    t.stage_results.forEach(function(sr) {
      var color = stageColors[sr.stage] || '#999';
      stHtml += '<div class="stage-row">'
        + '<span class="stage-dot" style="background:'+color+'"></span>'
        + '<span class="stage-label">'+esc(sr.stage)+'</span>'
        + '<span style="font-size:.7rem;color:'+(sr.status==='ok'?'#28a745':'#d9534f')+'">'+esc(sr.status)+'</span>'
        + '<span class="stage-elapsed">'+(sr.elapsed_s||0).toFixed(1)+'s</span></div>';
    });

    var paramsHtml = '';
    ['FP','PL','CTS','RT'].forEach(function(st) {
      var p = (t.params||{})[st] || {};
      Object.keys(p).forEach(function(k) {
        paramsHtml += '<span class="param-kv">'+esc(st)+'.'+esc(k)+'='+esc(String(p[k]))+'</span>';
      });
    });

    var qor = t.final_qor;
    var qorHtml = '';
    if (qor) {
      qorHtml = '<div style="font-size:.8rem;margin-top:4px"><strong>Final QoR:</strong> '
        + 'WNS='+(qor.wns_ps!=null?qor.wns_ps.toFixed(1)+'ps':'-')+' '
        + 'TNS='+(qor.tns_ps!=null?qor.tns_ps.toFixed(1)+'ps':'-')+' '
        + 'Area='+(qor.area_um2!=null?qor.area_um2.toFixed(1)+'µm²':'-')+' '
        + 'Power='+(qor.power_w!=null?(qor.power_w*1000).toFixed(3)+'mW':'-');
      if (t.report_path) qorHtml += ' <span style="font-size:.7rem;color:var(--text-muted)">[report: '+esc(t.report_path)+']</span>';
      qorHtml += '</div>';
    }

    var dgHtml = '';
    t.doomed_decisions.forEach(function(dd, i) {
      var gd = t.gwtw_decisions[i] || {};
      dgHtml += '<span style="font-size:.72rem;display:inline-block;margin:2px 4px;padding:1px 6px;'
        + 'background:#f1f3f5;border-radius:3px">'+esc(dd.risk_class||'?')+' → '+esc(gd.action||'?')+'</span>';
    });

    var branchInfo = '';
    if (t.parent_trial_id) branchInfo += '<span style="font-size:.7rem;color:var(--text-muted)">← '+esc(shortId(t.parent_trial_id))+'</span>';
    if (t.branch_stage) branchInfo += '<span style="font-size:.7rem;color:var(--text-muted)"> fork@'+esc(t.branch_stage)+'</span>';

    html += '<div class="trial-card" id="trial-'+esc(t.trial_id)+'">'
      + '<div class="trial-header" onclick="toggleTrial(\''+esc(t.trial_id)+'\')">'
        + '<span class="tid">'+esc(shortId(t.trial_id))+'</span>'
        + branchInfo
        + '<span style="flex:1"></span>'
        + (hasTrace?'':'<span class="badge badge-degraded" style="font-size:.65rem">Not recorded</span>')
        + dgHtml
        + '<span class="status status-'+esc(t.status)+'">'+esc(t.status)+'</span>'
      + '</div>'
      + '<div class="trial-body" id="trial-body-'+esc(t.trial_id)+'">'
        + '<div style="display:flex;gap:16px;flex-wrap:wrap">'
          + '<div style="flex:1;min-width:180px"><h3>Stages</h3>'+stHtml+'</div>'
          + '<div style="flex:2;min-width:300px"><h3>Parameters</h3><div class="params-grid">'+paramsHtml+'</div></div>'
        + '</div>'
        + qorHtml
        + (t.execution_resolution ? '<div style="font-size:.78rem;margin-top:4px"><strong>Execution:</strong> '
          + esc(t.execution_resolution.execution_mode||'')+' → '+esc(t.execution_resolution.effective_start_stage||'')+'</div>' : '')
      + '</div></div>';
  });
  document.getElementById('trial-list').innerHTML = html || '<p style="color:var(--text-muted)">No trials</p>';
}

// =============================================================================
// Tree (SVG)
// =============================================================================
function renderTree() {
  var tree = DATA.tree || {};
  var nodes = tree.nodes || {};
  var nodeList = Object.values(nodes);
  if (!nodeList.length) {
    document.getElementById('tree-summary').textContent = '(empty)';
    return;
  }
  document.getElementById('tree-summary').textContent = nodeList.length + ' nodes';

  var layers = {root:[],FP:[],PL:[],CTS:[],RT:[]};
  nodeList.forEach(function(n) {
    var s = n.stage || 'root';
    if (layers[s]) layers[s].push(n);
  });
  var layerOrder = ['root','FP','PL','CTS','RT'];
  layerOrder.forEach(function(s) {
    if (layers[s]) layers[s].sort(function(a,b){return (a.iteration||0)-(b.iteration||0);});
  });

  var hSpacing = 140, vSpacing = 80, nodeW = 110, nodeH = 36;
  var margin = {top:20,right:40,bottom:20,left:40};
  var maxN = Math.max.apply(null, layerOrder.map(function(s){return (layers[s]||[]).length;}));
  var svgW = Math.max(600, maxN * hSpacing + margin.left + margin.right);
  var svgH = layerOrder.length * vSpacing + margin.top + margin.bottom;

  // Trial-id -> status map for colouring
  var trialStatus = {};
  (DATA.trials||[]).forEach(function(t){trialStatus[t.trial_id]=t.status;});

  // Node positions
  var positions = {};
  layerOrder.forEach(function(stage, li) {
    var ln = layers[stage] || [];
    var y = margin.top + li * vSpacing + vSpacing/2;
    var totalW = (ln.length - 1) * hSpacing;
    var startX = (svgW - totalW) / 2;
    ln.forEach(function(n, i) { positions[n.node_id] = {x: startX + i * hSpacing, y: y}; });
  });

  // Edges — distinguish checkpoint fork vs full restart
  var edges = '';
  nodeList.forEach(function(n) {
    if (!n.parent_id || !positions[n.parent_id] || !positions[n.node_id]) return;
    var p = positions[n.parent_id], c = positions[n.node_id];
    var mode = n._execution_mode || '';
    var edgeClass = 'edge-line';
    if (mode === 'checkpoint_fork') edgeClass += ' fork-checkpoint';
    else if (mode === 'full_restart') edgeClass += ' fork-fullrestart';
    edges += '<line class="'+edgeClass+'" x1="'+p.x+'" y1="'+(p.y+nodeH/2)+'" x2="'+c.x+'" y2="'+(c.y-nodeH/2)+'" />';
  });

  // Nodes — coloured by trial status
  var rects = '';
  var statusColors = {ok:'#55a868',paused:'#f0ad4e',failed:'#d9534f',running:'#64b5cd'};
  var stageColors = {root:'#64b5cd',FP:'#4c72b0',PL:'#55a868',CTS:'#c44e52',RT:'#8172b2'};
  nodeList.forEach(function(n) {
    var pos = positions[n.node_id];
    if (!pos) return;
    var stage = n.stage || 'root';
    var tid = n.source_trial_id || '';
    var status = n._trial_status || (tid ? (trialStatus[tid]||'') : '');
    var fill = (status && statusColors[status]) ? statusColors[status] : (stageColors[stage] || '#999');
    var label = stage==='root' ? 'ROOT' : stage+'|'+esc(tid.slice(0,6));

    rects += '<g class="node-group" data-nid="'+esc(n.node_id)+'" data-tid="'+esc(tid)+'">'
      + '<rect class="node-rect" x="'+(pos.x-nodeW/2)+'" y="'+(pos.y-nodeH/2)
      + '" width="'+nodeW+'" height="'+nodeH+'" rx="4" fill="'+fill+'" '
      + 'onclick="highlightTrial(\''+esc(tid)+'\')">'
      + '<title>'+esc(n.node_id)+'\nStage: '+esc(stage)+'\nTrial: '+esc(tid)+'\nStatus: '+esc(status)+'\nIteration: '+(n.iteration||'')+'</title>'
      + '</rect>'
      + '<text x="'+pos.x+'" y="'+pos.y+'" text-anchor="middle" dominant-baseline="central" '
      + 'fill="white" font-size="10" font-family="monospace" style="pointer-events:none">'+esc(label)+'</text>'
      + '</g>';
  });

  var svg = '<svg width="'+svgW+'" height="'+svgH+'" viewBox="0 0 '+svgW+' '+svgH+'">' + edges + rects + '</svg>';
  document.getElementById('tree-svg').innerHTML = svg;

  // Legend
  var legendHtml = '<div style="font-size:.72rem;margin-top:6px;display:flex;gap:12px;flex-wrap:wrap">'
    + '<span><span style="display:inline-block;width:12px;height:12px;background:#55a868;border-radius:2px;vertical-align:middle"></span> OK</span>'
    + '<span><span style="display:inline-block;width:12px;height:12px;background:#f0ad4e;border-radius:2px;vertical-align:middle"></span> Paused</span>'
    + '<span><span style="display:inline-block;width:12px;height:12px;background:#d9534f;border-radius:2px;vertical-align:middle"></span> Failed</span>'
    + '<span><span style="display:inline-block;width:12px;height:12px;background:#64b5cd;border-radius:2px;vertical-align:middle"></span> Running</span>'
    + '<span style="margin-left:8px">|</span>'
    + '<span style="color:#28a745">── checkpoint fork</span>'
    + '<span style="color:#d9534f">── full restart</span>'
    + '</div>';
  document.getElementById('tree-svg').insertAdjacentHTML('beforeend', legendHtml);

  window.highlightTrial = function(tid) {
    var el = document.getElementById('trial-'+tid);
    if (el) {
      el.scrollIntoView({behavior:'smooth',block:'center'});
      el.style.boxShadow = '0 0 0 3px var(--accent)';
      setTimeout(function(){el.style.boxShadow='';}, 2000);
    }
  };
}

// =============================================================================
// Finish QoR table
// =============================================================================
function renderQor() {
  var qors = DATA.finish_qors || [];
  document.getElementById('qor-summary').textContent = '(' + qors.length + ' finished trials)';
  if (!qors.length) {
    document.getElementById('qor-table-container').innerHTML = '<p style="color:var(--text-muted)">No finish QoR data</p>';
    return;
  }

  function best(arr, key, lowerBetter) {
    var vals = arr.map(function(r){return r[key];}).filter(function(v){return v!=null;});
    return vals.length ? (lowerBetter ? Math.min.apply(null,vals) : Math.max.apply(null,vals)) : null;
  }
  function worst(arr, key, lowerBetter) {
    var vals = arr.map(function(r){return r[key];}).filter(function(v){return v!=null;});
    return vals.length ? (lowerBetter ? Math.max.apply(null,vals) : Math.min.apply(null,vals)) : null;
  }
  var bestWns=best(qors,'wns_ps',false),worstWns=worst(qors,'wns_ps',false);
  var bestTns=best(qors,'tns_ps',false),worstTns=worst(qors,'tns_ps',false);
  var bestArea=best(qors,'area_um2',true),worstArea=worst(qors,'area_um2',true);
  var bestPower=best(qors,'power_w',true),worstPower=worst(qors,'power_w',true);

  var html = '<table><thead><tr>'
    + '<th>Trial</th><th>Parent</th><th>WNS (ps)</th><th>TNS (ps)</th>'
    + '<th>Area (µm²)</th><th>Power (W)</th><th>Report</th></tr></thead><tbody>';
  qors.forEach(function(q) {
    function cellClass(val,bestV,worstV) {
      if (val==null||bestV==null) return '';
      if (val===bestV) return 'best';
      if (val===worstV) return 'worst';
      return '';
    }
    html += '<tr>'
      + '<td style="font-family:var(--font-mono);font-size:.78rem">'+esc(shortId(q.trial_id))+'</td>'
      + '<td style="font-family:var(--font-mono);font-size:.72rem">'+esc(shortId(q.parent_trial_id||''))+'</td>'
      + '<td class="'+cellClass(q.wns_ps,bestWns,worstWns)+'">'+(q.wns_ps!=null?q.wns_ps.toFixed(1):'-')+'</td>'
      + '<td class="'+cellClass(q.tns_ps,bestTns,worstTns)+'">'+(q.tns_ps!=null?q.tns_ps.toFixed(1):'-')+'</td>'
      + '<td class="'+cellClass(q.area_um2,bestArea,worstArea)+'">'+(q.area_um2!=null?q.area_um2.toFixed(1):'-')+'</td>'
      + '<td class="'+cellClass(q.power_w,bestPower,worstPower)+'">'+(q.power_w!=null?(q.power_w*1000).toFixed(3)+'m':'-')+'</td>'
      + '<td style="font-size:.7rem;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="'+esc(q.report_path||'')+'">'+esc(q.report_path||'-')+'</td></tr>';
  });
  html += '</tbody></table>';
  document.getElementById('qor-table-container').innerHTML = html;
}
</script>
</body>
</html>"""


# =============================================================================
# Main entry
# =============================================================================


def generate_visualization(session_dir: Path) -> Path:
    """Generate visualization/index.html and visualization/session_data.json.

    Returns the path to the generated index.html.
    Only overwrites the two generated files; all other session artifacts
    are untouched.
    """
    session_dir = _validate_dir(session_dir)

    # Validate visualization output is within session_dir.
    viz_rel = "visualization"
    viz_dir = _validate_contained(session_dir, viz_rel)
    viz_dir.mkdir(parents=True, exist_ok=True)

    # 1. Build and write session_data.json (audit copy).
    log.info("Extracting session data from %s", session_dir)
    data = extract_session_data(session_dir)

    data_path = viz_dir / "session_data.json"
    _validate_contained(session_dir, f"{viz_rel}/session_data.json")
    tmp_data = data_path.with_suffix(".tmp")
    tmp_data.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    tmp_data.replace(data_path)
    log.info("Wrote session_data.json (%d bytes)", data_path.stat().st_size)

    # 2. Build HTML with embedded data.
    html_template = _html_template()
    embedded_json = _json_embed_safe(data)
    html = html_template.replace("__DATA_PLACEHOLDER__", embedded_json)

    html_path = viz_dir / "index.html"
    _validate_contained(session_dir, f"{viz_rel}/index.html")
    tmp_html = html_path.with_suffix(".tmp")
    tmp_html.write_text(html, encoding="utf-8")
    tmp_html.replace(html_path)
    log.info("Wrote index.html (%d bytes)", html_path.stat().st_size)

    # 3. Summary.
    n_trials = len(data["trials"])
    n_finish = len(data["finish_qors"])
    n_paused = sum(1 for t in data["trials"] if t["status"] == "paused")
    n_audit = sum(
        1 for dec in (list(data["pl_cohort"].get("observations", []))
                      + list(data["cts_cohort"].get("observations", [])))
        if dec.get("gwtw_action") == "audit_continue")
    n_forks = (len(data["pl_cohort"].get("children", []))
               + len(data["cts_cohort"].get("children", [])))
    n_timeline = len(data["timeline"])

    log.info("Visualization summary: %d trials, %d finish, %d paused, "
             "%d audit, %d forks, %d timeline events",
             n_trials, n_finish, n_paused, n_audit, n_forks, n_timeline)

    return html_path


# =============================================================================
# CLI
# =============================================================================


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="session_visualize.py",
        description="Generate a self-contained offline HTML visualization "
                    "for an AgenticPD session directory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Example:\n"
               "  python3 tools/session_visualize.py "
               "runs/sky130hd_gcd/multi-agent-gwtw-demo_20260731_061927")
    p.add_argument(
        "session_dir", type=str,
        help="Path to the session directory containing config_snapshot.json, "
             "trials.jsonl, tree.json, and optionally traces/decisions.jsonl.")
    p.add_argument(
        "--log-level", type=str, default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO).")
    return p


if __name__ == "__main__":
    parser = _build_argparser()
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)s: %(message)s")

    session = Path(args.session_dir)
    try:
        out = generate_visualization(session)
        print(f"\nVisualization generated: {out}")
        print(f"Open file://{out} in a browser to view.")
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        raise
