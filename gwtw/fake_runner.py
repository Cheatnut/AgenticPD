# -*- coding: utf-8 -*-
"""gwtw/fake_runner.py — deterministic fake ORFS runner for pure-Python testing."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from core.models import StageResult
from core.qor import QoR
from storage import CheckpointManager
from orfs.interface import RunResult

_STAGE_ORDER = ["FP", "PL", "CTS", "RT", "finish"]
_STAGE_ARTIFACTS: Dict[str, List[str]] = {
    "FP": ["2_floorplan.odb", "2_floorplan.sdc"],
    "PL": ["3_place.odb", "3_place.sdc"],
    "CTS": ["4_cts.odb", "4_cts.sdc"],
    "RT": ["5_route.odb", "5_route.sdc"],
}

class StageERecordingFakeRunner:
    """Stateful fake that records calls + creates real artifact files.

    Used for pure Python testing — no ORFS, no network.
    """

    def __init__(self, flow_dir: Path) -> None:
        self.flow_dir = Path(flow_dir)
        self.calls: List[Dict[str, Any]] = []
        self._artifact_files: Dict[str, set] = {}

    def _record(self, method: str, **kw) -> None:
        self.calls.append({"method": method, **kw})

    def _ensure_artifacts(self, variant: str, stage: str) -> None:
        files = _STAGE_ARTIFACTS.get(stage, [])
        vdir = self.flow_dir / "results" / "sky130hd" / "gcd" / variant
        vdir.mkdir(parents=True, exist_ok=True)
        for fname in files:
            p = vdir / fname
            p.write_text(f"fake {stage} {variant} {fname}")
            self._artifact_files.setdefault(variant, set()).add(str(p))

    def _make_qor(self, params: Dict, stage: str) -> Dict[str, float]:
        util = params.get("FP", {}).get("CORE_UTILIZATION", 38)
        wns = -1500.0 + (util - 20) * 5.0; tns = wns * 40.0
        _map = {"FP": (1.0, "2_1_floorplan"), "PL": (1.05, "3_5_place_dp"),
                "CTS": (1.02, "4_1_cts"), "RT": (1.01, "5_1_grt")}
        scale, tag = _map.get(stage, (1.0, stage))
        return {f"{tag}_ws_ps": round(wns * scale, 1),
                f"{tag}_tns_ps": round(tns * scale, 1)}

    def run_stage(self, stage: str, params: Any, variant: str,
                  iteration: int) -> StageResult:
        self._record("run_stage", stage=stage, variant=variant)
        self._ensure_artifacts(variant, stage)
        return StageResult(
            stage=stage, status="ok", elapsed_s=0.02, exit_code=0,
            stage_qor=self._make_qor(params, stage))

    def run_finish(self, params: Any, variant: str, iteration: int) -> Any:
        self._record("run_finish", variant=variant)
        from orfs.interface import RunResult; from core.utils import QoR
        util = params.get("FP", {}).get("CORE_UTILIZATION", 38)
        wns = -1500.0 + (util - 20) * 5.0
        return RunResult(
            ok=True, variant=variant,
            qor=QoR(wns_ps=round(wns * 1.01, 1),
                    tns_ps=round(wns * 40 * 1.01, 1),
                    area_um2=5000.0, power_w=0.008),
            stage_qor={"5_2_route_ws_ps": round(wns*1.01,1),
                       "5_2_route_tns_ps": round(wns*40*1.01,1)},
            elapsed_s=0.05, command="[mock] make finish",
            report_path="[mock] reports/.../6_report.json")

    def copy_parent_results(self, parent_variant: str,
                            child_variant: str) -> None:
        self._record("copy_parent_results",
                     parent=parent_variant, child=child_variant)

    def clean_downstream(self, variant: str, effective_start: str) -> None:
        self._record("clean_downstream", variant=variant,
                     effective_start=effective_start)
        try:
            si = _STAGE_ORDER.index(effective_start)
        except ValueError:
            return
        for stage in _STAGE_ORDER[si:4]:
            for fname in _STAGE_ARTIFACTS.get(stage, []):
                p = (self.flow_dir / "results" / "sky130hd" / "gcd"
                     / variant / fname)
                if p.is_file():
                    p.unlink()
                self._artifact_files.setdefault(variant, set()).discard(
                    str(p))


# =============================================================================
# Helpers
# =============================================================================


def _resolve_trial_id(
    branch_node: str, whitelist: List[str],
) -> Optional[str]:
    """Resolve a Judge branch_node to a trial_id in *whitelist*.

    Tries exact match first, then prefix match (first 6–8 chars).
    Returns None if no match found.
    """
    if branch_node in whitelist:
        return branch_node
    # Prefix match: first N chars must uniquely match one whitelist entry.
    for n in (8, 6):
        matches = [w for w in whitelist if w.startswith(branch_node[:n])]
        if len(matches) == 1:
            return matches[0]
    return None


def _hash_params(params: Dict) -> Optional[str]:
    try: return CheckpointManager.param_hash(params)
    except Exception: return None


# =============================================================================
# Self-test
# =============================================================================
