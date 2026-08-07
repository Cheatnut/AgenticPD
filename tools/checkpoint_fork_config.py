# -*- coding: utf-8 -*-
"""checkpoint_fork_config.py — YAML experiment config loading for checkpoint-fork verification."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import yaml

_REQUIRED_YAML_KEYS = ["platform", "design", "checkpoint", "search_space"]


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_experiment_config(yaml_path: str) -> dict:
    """Load all experiment parameters from YAML.  Fails on missing required keys.

    Returns a dict with keys:
        experiment_id, platform, design, checkpoint_stage, checkpoint_source,
        baseline_params, actions, qor_tolerances, acceptance
    """
    with open(yaml_path, encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}

    # Validate required top-level keys
    missing = [k for k in _REQUIRED_YAML_KEYS if k not in doc]
    if missing:
        log.error("YAML config %s missing required keys: %s", yaml_path, missing)
        sys.exit(1)

    experiment_id = doc.get("experiment_id")
    if not experiment_id:
        log.error("YAML config missing experiment_id")
        sys.exit(1)

    platform = doc.get("platform")
    design = doc.get("design")
    if not platform or not design:
        log.error("YAML config missing platform or design")
        sys.exit(1)

    # Checkpoint config
    cp_block = doc.get("checkpoint", {})
    checkpoint_stage = cp_block.get("fork_stage")
    if not checkpoint_stage:
        log.error("YAML config missing checkpoint.fork_stage")
        sys.exit(1)
    checkpoint_source = cp_block.get("source")
    if not checkpoint_source:
        log.error("YAML config missing checkpoint.source")
        sys.exit(1)

    # Baseline parameters — read from YAML, NO fallback to config.py
    baseline_raw = doc.get("search_space", {}).get("baseline", {})
    if not baseline_raw:
        log.error("YAML config missing search_space.baseline")
        sys.exit(1)
    baseline_params = _normalise_baseline(baseline_raw)

    # Acceptance block — qor_tolerances are required fields
    acceptance = doc.get("acceptance", {})
    tol_block = acceptance.get("qor_tolerances", {})
    if "wns_ps" not in tol_block or "tns_ps" not in tol_block:
        log.error("YAML config missing acceptance.qor_tolerances.wns_ps or .tns_ps")
        sys.exit(1)
    qor_tolerances = {
        "wns_ps": float(tol_block["wns_ps"]),
        "tns_ps": float(tol_block["tns_ps"]),
    }

    actions = _parse_actions(doc, checkpoint_stage)
    if not actions:
        log.error("YAML config has no actions in search_space.actions")
        sys.exit(1)

    return {
        "experiment_id": experiment_id,
        "platform": platform,
        "design": design,
        "checkpoint_stage": checkpoint_stage,
        "checkpoint_source": checkpoint_source,
        "baseline_params": baseline_params,
        "actions": actions,
        "qor_tolerances": qor_tolerances,
        "acceptance": acceptance,
    }


def _normalise_baseline(raw: dict) -> dict:
    """Convert YAML baseline block to per-stage params dict.

    YAML:  {FP: {CORE_UTILIZATION: 38}, PL: {}, CTS: {}, RT: {...}}
    Returns the same dict unchanged (already in correct format)."""
    result = {}
    for stage in _STAGE_ORDER:
        result[stage] = dict(raw.get(stage, {}))
    return result


def _parse_actions(doc: dict, checkpoint_stage: str) -> dict:
    """Extract actions from YAML and compute checkpoint compatibility.

    Compatibility rule: an action is compatible with a checkpoint at stage S
    when ALL of its ``affects`` stages are strictly downstream of S.

    No execution-semantic defaults — missing required fields cause immediate
    failure rather than silently using wrong values.
    """
    actions_block = doc.get("search_space", {}).get("actions", {})
    if not actions_block:
        return {}

    cp_idx = _STAGE_ORDER.index(checkpoint_stage) if checkpoint_stage in _STAGE_ORDER else -1
    if cp_idx < 0:
        log.error("Unknown checkpoint stage: %s", checkpoint_stage)
        sys.exit(1)

    result = {}
    for key, a in actions_block.items():
        # Required fields — fail on missing
        param_name = a.get("param")
        if not param_name:
            log.error("Action '%s' missing required field 'param'", key)
            sys.exit(1)
        if "from" not in a or "to" not in a:
            log.error("Action '%s' missing required field 'from'/'to'", key)
            sys.exit(1)
        action_stage = a.get("stage")
        if not action_stage:
            log.error("Action '%s' missing required field 'stage'", key)
            sys.exit(1)

        affects = a.get("affects", [])
        # Compatible iff every affected stage is strictly downstream of checkpoint
        expect_compatible = True
        for aff in affects:
            aff_idx = _STAGE_ORDER.index(aff) if aff in _STAGE_ORDER else 999
            if aff_idx <= cp_idx:
                expect_compatible = False
                break

        result[key] = {
            "description": a.get("description", key),
            "stage": action_stage,
            "param_name": param_name,
            "baseline_value": a["from"],
            "changed_value": a["to"],
            "expect_compatible": expect_compatible,
            "expect_qor_match": expect_compatible,
        }
    return result


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------



def build_params(action: dict, baseline_params: dict) -> dict:
    """Build per-stage params: baseline + one override."""
    params = {s: dict(baseline_params.get(s, {})) for s in STAGES}
    params[action["stage"]][action["param_name"]] = action["changed_value"]
    return params


def qor_to_dict(qor) -> dict | None:
    if qor is None:
        return None
    if hasattr(qor, "to_dict"):
        return qor.to_dict()
    if isinstance(qor, dict):
        return qor
    return {k: getattr(qor, k, None)
            for k in ("wns_ps", "tns_ps", "area_um2", "power_w")}


def qor_equal(a, b, wns_tol=1.0, tns_tol=5.0) -> tuple[bool, str]:
    if a is None and b is None:
        return True, "both None"
    if a is None or b is None:
        return False, "one is None"
    da, db = qor_to_dict(a), qor_to_dict(b)
    for key in ("wns_ps", "tns_ps", "area_um2", "power_w"):
        va, vb = da.get(key), db.get(key)
        if va is None and vb is None:
            continue
        if va is None or vb is None:
            return False, f"{key}: one is None"
        tol = (wns_tol if key == "wns_ps" else tns_tol if key == "tns_ps"
               else max(1.0, 0.001 * abs(va or vb or 1)))
        if abs(va - vb) > tol:
            return False, f"{key}: {va} vs {vb} (delta={abs(va - vb):.3g} > tol={tol:.3g})"
    return True, "all metrics within tolerance"


