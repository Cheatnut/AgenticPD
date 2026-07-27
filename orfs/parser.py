# -*- coding: utf-8 -*-
"""orfs.parser — Stage C1: ORFS report parsing.

Extracted from orfs_interface.py.  Reads 6_report.json, stage JSON logs,
and make logs to extract QoR metrics and detect failure stages.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import config
from config import FrameworkConfig
from utils import QoR

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ORFS stage file markers (ordered from first to last)
# ---------------------------------------------------------------------------

# Each entry: (expected_json_filename, stage_name)
# Used by detect_failed_stage() to find the first missing file.
STEP_JSON_SEQUENCE: List[Tuple[str, str]] = [
    ("1_synth.json", "synth"),
    ("2_1_floorplan.json", "floorplan"),
    ("2_2_floorplan_macro.json", "floorplan"),
    ("2_3_floorplan_tapcell.json", "floorplan"),
    ("2_4_floorplan_pdn.json", "floorplan"),
    ("3_1_place_gp_skip_io.json", "place"),
    ("3_2_place_iop.json", "place"),
    ("3_3_place_gp.json", "place"),
    ("3_4_place_resized.json", "place"),
    ("3_5_place_dp.json", "place"),
    ("4_1_cts.json", "cts"),
    ("5_1_grt.json", "globalroute"),
    ("5_2_route.json", "detailedroute"),
    ("5_3_fillcell.json", "route"),
    ("6_report.json", "finish"),
]

# Representative intermediate QoR sources per stage (fed to StageAgents):
#   FP uses floorplan-end, PL uses detailed-placement-end,
#   CTS uses CTS-end, RT uses both global and detail route.
STAGE_QOR_SOURCES: Dict[str, List[str]] = {
    "FP":  ["2_1_floorplan.json"],
    "PL":  ["3_5_place_dp.json"],
    "CTS": ["4_1_cts.json"],
    "RT":  ["5_1_grt.json", "5_2_route.json"],
}

# Stage name -> ORFS single-stage make target
STAGE_MAKE_TARGET: Dict[str, str] = {
    "FP":  "floorplan",
    "PL":  "place",
    "CTS": "cts",
    "RT":  "route",
}

# Clean targets for branch-from-stage re-runs
CLEAN_TARGETS: Dict[str, str] = {
    "FP":  "clean_floorplan",
    "PL":  "clean_place",
    "CTS": "clean_cts",
    "RT":  "clean_route",
}


# ---------------------------------------------------------------------------
# QoR parsing
# ---------------------------------------------------------------------------

def parse_qor(cfg: FrameworkConfig, variant: str) -> Optional[QoR]:
    """Parse final post-route QoR for a variant.

    6_report.json is preferred.  Falls back to rpt/log regex if JSON is
    missing or incomplete.
    """
    report_json = cfg.logs_dir(variant) / "6_report.json"
    if report_json.is_file():
        try:
            qor = QoR.from_report_json(report_json)
            if qor.is_complete():
                return qor
            log.warning(
                "[ORFS] 6_report.json incomplete (%s), trying rpt fallback",
                qor.to_dict(),
            )
        except (json.JSONDecodeError, OSError) as e:
            log.warning(
                "[ORFS] Failed to parse 6_report.json (%s), trying rpt fallback", e,
            )

    finish_rpt = cfg.reports_dir(variant) / "6_finish.rpt"
    report_log = cfg.logs_dir(variant) / "6_report.log"
    qor = QoR.from_reports_fallback(finish_rpt, report_log)
    if qor.is_complete():
        log.info("[ORFS] QoR extracted via rpt/log fallback")
        return qor
    return None


def parse_stage_qor(cfg: FrameworkConfig,
                    variant: str) -> Dict[str, Dict[str, float]]:
    """Parse intermediate QoR from stage JSON logs.

    Uses STAGE_QOR_SOURCES to know which JSON files to read per stage.
    Returns {stage: {metric_name: value_ps}}.
    """
    logs_dir = cfg.logs_dir(variant)
    stage_qor: Dict[str, Dict[str, float]] = {}
    for stage, json_names in STAGE_QOR_SOURCES.items():
        merged: Dict[str, float] = {}
        for json_name in json_names:
            path = logs_dir / json_name
            if not path.is_file():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            tag = json_name.split(".")[0]  # e.g. "5_1_grt"
            for key, value in data.items():
                if not isinstance(value, (int, float)):
                    continue
                if key.endswith("__timing__setup__ws"):
                    merged[f"{tag}_ws_ps"] = round(
                        float(value) * config.TIMING_UNIT_TO_PS, 1)
                elif key.endswith("__timing__setup__tns"):
                    merged[f"{tag}_tns_ps"] = round(
                        float(value) * config.TIMING_UNIT_TO_PS, 1)
        if merged:
            stage_qor[stage] = merged
    return stage_qor


def detect_failed_stage(cfg: FrameworkConfig, variant: str) -> Optional[str]:
    """Heuristically identify the failed stage.

    Walks STEP_JSON_SEQUENCE in order; the first expected JSON file that is
    missing indicates the likely crash point.
    """
    logs_dir = cfg.logs_dir(variant)
    for json_name, stage in STEP_JSON_SEQUENCE:
        if not (logs_dir / json_name).is_file():
            return stage
    return None
