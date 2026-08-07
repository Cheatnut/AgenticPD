# -*- coding: utf-8 -*-
"""session_visualize/render.py — HTML/timeline/cohort/tree rendering."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

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
def _html_template() -> str:
    """Return the self-contained HTML page template with ``__DATA_PLACEHOLDER__``
    where the embedded JSON goes."""
    template_path = Path(__file__).resolve().parent / "template.html"
    return template_path.read_text(encoding="utf-8")


# =============================================================================
# Main entry
# =============================================================================
