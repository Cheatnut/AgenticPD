# -*- coding: utf-8 -*-
"""core/qor.py — QoR dataclass and timing-first quality comparator."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import config

# QoR dataclass and parsing
# ---------------------------------------------------------------------------

@dataclass
class QoR:
    """Four QoR metrics. Timing in ps (negative = violation), area in µm²,
    power in W.

    wns_ps semantics: taken from ORFS worst slack (can be positive; positive
    means timing is met with margin), compatible with the paper's "worst
    negative slack" concept — the comparator treats >= 0 as timing converged.
    """

    wns_ps: Optional[float] = None
    tns_ps: Optional[float] = None
    area_um2: Optional[float] = None
    power_w: Optional[float] = None

    def is_complete(self) -> bool:
        """Whether all four metrics were successfully parsed (incomplete QoR
        does not participate in best comparison)"""
        return all(v is not None for v in
                   (self.wns_ps, self.tns_ps, self.area_um2, self.power_w))

    def to_dict(self) -> Dict[str, Optional[float]]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[Dict[str, Any]]) -> Optional["QoR"]:
        if not d:
            return None
        return cls(wns_ps=d.get("wns_ps"), tns_ps=d.get("tns_ps"),
                   area_um2=d.get("area_um2"), power_w=d.get("power_w"))

    def pretty(self) -> str:
        """Human-readable single-line summary (also used for history
        serialization in prompts)"""
        def fmt(v: Optional[float], spec: str, unit: str) -> str:
            return "N/A" if v is None else format(v, spec) + unit
        return (f"WNS={fmt(self.wns_ps, '.1f', 'ps')} "
                f"TNS={fmt(self.tns_ps, '.1f', 'ps')} "
                f"Area={fmt(self.area_um2, '.1f', 'um2')} "
                f"Power={fmt((self.power_w or 0) * 1e3 if self.power_w is not None else None, '.4f', 'mW')}")

    # ------------------------------------------------------------------
    # Parse entry 1: JSON metrics (preferred, most robust)
    # ------------------------------------------------------------------
    @classmethod
    def from_report_json(cls, report_json: Path) -> "QoR":
        """Parse QoR from the finish-stage 6_report.json.

        Key keys (timing unit ns, ×1000 → ps):
          finish__timing__setup__ws / finish__timing__setup__tns
          finish__power__total / finish__design__instance__area

        Note: finish__design__instance__area appears twice in the file
        (the first occurrence includes fill cells; the second is pure standard
        cell area and is the correct value). CPython's stdlib json.load uses
        "last-wins" for duplicate keys, which coincidentally gives the correct
        value — this behavior is a prerequisite for correctness; do NOT switch
        to a parser that errors on or takes the first value for duplicate keys.
        """
        data = json.loads(report_json.read_text(encoding="utf-8"))
        scale = config.TIMING_UNIT_TO_PS

        def get(key: str) -> Optional[float]:
            v = data.get(key)
            return float(v) if isinstance(v, (int, float)) else None

        ws = get("finish__timing__setup__ws")
        tns = get("finish__timing__setup__tns")
        return cls(
            wns_ps=ws * scale if ws is not None else None,
            tns_ps=tns * scale if tns is not None else None,
            area_um2=get("finish__design__instance__area"),
            power_w=get("finish__power__total"),
        )

    # ------------------------------------------------------------------
    # Parse entry 2: rpt/log regex fallback (used when JSON is missing)
    # ------------------------------------------------------------------
    # Timing summary lines in 6_finish.rpt (re.MULTILINE, match line-by-line):
    #   "worst slack max 0.02"  — use worst slack as WNS (consistent with JSON
    #                             ws semantics; the "wns max" line in rpt is
    #                             clamped to 0.00 when timing is met, losing
    #                             positive margin, so we don't use it)
    #   "tns max 0.00"
    _RE_WORST_SLACK = re.compile(r"^worst slack max\s+(-?[\d.]+)", re.MULTILINE)
    _RE_TNS = re.compile(r"^tns max\s+(-?[\d.]+)", re.MULTILINE)
    # Total row in report_power table; 4th numeric column = total power (W):
    #   "Total                  1.41e-03   1.19e-03   1.70e-05   2.62e-03 100.0%"
    _RE_POWER_TOTAL = re.compile(
        r"^Total\s+([\d.eE+-]+)\s+([\d.eE+-]+)\s+([\d.eE+-]+)\s+([\d.eE+-]+)",
        re.MULTILINE)
    # Area line only exists in 6_report.log (NOT in 6_finish.rpt!):
    #   "Design area 716 um^2 62% utilization."
    _RE_DESIGN_AREA = re.compile(r"^Design area\s+([\d.]+)\s+um\^2", re.MULTILINE)

    @classmethod
    def from_reports_fallback(cls, finish_rpt: Path, report_log: Path) -> "QoR":
        """Regex-based fallback: parse 6_finish.rpt (timing/power) and
        6_report.log (area)"""
        qor = cls()
        scale = config.TIMING_UNIT_TO_PS

        if finish_rpt.is_file():
            text = finish_rpt.read_text(encoding="utf-8", errors="replace")
            m = cls._RE_WORST_SLACK.search(text)
            if m:
                qor.wns_ps = float(m.group(1)) * scale
            m = cls._RE_TNS.search(text)
            if m:
                qor.tns_ps = float(m.group(1)) * scale
            m = cls._RE_POWER_TOTAL.search(text)
            if m:
                qor.power_w = float(m.group(4))

        if report_log.is_file():
            text = report_log.read_text(encoding="utf-8", errors="replace")
            m = cls._RE_DESIGN_AREA.search(text)
            if m:
                qor.area_um2 = float(m.group(1))

        return qor


# ---------------------------------------------------------------------------
# QoR quality comparator
# ---------------------------------------------------------------------------

def qor_is_better(new: Optional[QoR], old: Optional[QoR],
                  wns_tol_ps: float, tns_tol_ps: float) -> bool:
    """Whether `new` is strictly better than `old` (used for best-result updates;
    ties keep the old result).

    Comparison priority (consistent with paper, tolerances avoid noise dominating):
    1. Failed/incomplete QoR always loses;
    2. Both WNS >= 0 (timing converged on both) → excess positive margin has no
       value, skip directly to power/area comparison;
    3. |ΔWNS| > wns_tol_ps → larger WNS (less violation) wins;
    4. |ΔTNS| > tns_tol_ps → larger TNS wins;
    5. Lower power wins; if still tied, lower area wins; exact tie → new loses
       (conservative, keep old best).
    """
    if new is None or not new.is_complete():
        return False
    if old is None or not old.is_complete():
        return True

    both_met = new.wns_ps >= 0 and old.wns_ps >= 0
    if not both_met:
        if abs(new.wns_ps - old.wns_ps) > wns_tol_ps:
            return new.wns_ps > old.wns_ps
        if abs(new.tns_ps - old.tns_ps) > tns_tol_ps:
            return new.tns_ps > old.tns_ps

    if new.power_w != old.power_w:
        return new.power_w < old.power_w
    if new.area_um2 != old.area_um2:
        return new.area_um2 < old.area_um2
    return False


# ---------------------------------------------------------------------------
# History persistence
# ---------------------------------------------------------------------------

