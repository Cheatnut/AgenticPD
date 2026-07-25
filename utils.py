# -*- coding: utf-8 -*-
"""
utils.py — AgenticPD utility module

Contains:
1. Logging initialization (dual channel: console + file)
2. .env file parsing (hand-rolled implementation, avoids hard python-dotenv dependency)
3. Robust JSON extraction from LLM output (strip markdown fences, extract first/last braces)
4. QoR dataclass and its parsing (JSON metrics primary, rpt/log regex fallback)
5. QoR quality comparator (WNS-first → TNS → power → area, with tolerances)
6. Atomic history file persistence (crash-safe)

Run self-tests directly: python3 agenticpd/utils.py
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import config


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(log_file: Optional[Path] = None,
                  level: int = logging.INFO) -> logging.Logger:
    """Initialize root logger: console + optional file dual-channel output.

    Repeated calls are safe (existing handlers are cleared first), making
    this convenient for test scenarios that need re-initialization.
    """
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    fmt = logging.Formatter("%(message)s")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)

    return root


# ---------------------------------------------------------------------------
# .env file parsing
# ---------------------------------------------------------------------------

def load_dotenv_file(path: Path) -> None:
    """Parse a KEY=VALUE .env file and write entries into os.environ.

    Rules: skip blank lines and # comments; never override existing env vars
    (environment takes priority over file); surrounding single/double quotes
    on values are stripped. Silently returns if file doesn't exist (key may
    be provided directly from the environment).
    """
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


# ---------------------------------------------------------------------------
# Robust JSON extraction
# ---------------------------------------------------------------------------

class JsonParseError(Exception):
    """LLM output could not be parsed as JSON; the `raw` field preserves the
    original text for feeding back to the model"""

    def __init__(self, reason: str, raw: str):
        super().__init__(reason)
        self.reason = reason
        self.raw = raw


def extract_json(text: str) -> Dict[str, Any]:
    """Robustly extract a JSON object from LLM output text.

    Processing strategy (progressive degradation):
    1. Strip ```json ... ``` or ``` ... ``` markdown code fences;
    2. Try direct json.loads;
    3. If that fails, extract substring from first '{' to last '}' and retry;
    4. If still failing, raise JsonParseError (caller feeds back to LLM).

    """
    if not isinstance(text, str) or not text.strip():
        raise JsonParseError("LLM returned empty content", raw=str(text))

    cleaned = text.strip()
    # Strip markdown code fences (```json or ```), allowing surrounding
    # conversational text before/after the fence
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL | re.IGNORECASE)
    if fence:
        cleaned = fence.group(1).strip()

    for candidate in (cleaned,):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    # Extract content between first '{' and last '}' (handles text with
    # explanatory prose before/after the JSON)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(cleaned[start:end + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError as e:
            raise JsonParseError(f"JSON syntax error: {e}", raw=text)

    raise JsonParseError("No JSON object found in text", raw=text)


# ---------------------------------------------------------------------------
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

def save_history_atomic(path: Path, history: List[Dict[str, Any]]) -> None:
    """Atomic history write: write to .tmp then os.replace; crash mid-write
    won't corrupt the old file"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(history, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)


def load_history(path: Path) -> List[Dict[str, Any]]:
    """Load history; if file is corrupted, rename to .corrupt and return empty
    list (fresh start)"""
    log = logging.getLogger("utils")
    if not path.is_file():
        return []
    try:
        history = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(history, list):
            return history
        raise ValueError("history top-level is not a list")
    except (json.JSONDecodeError, ValueError) as e:
        corrupt = path.with_suffix(path.suffix + ".corrupt")
        os.replace(path, corrupt)
        log.warning("[utils] History corrupted (%s), renamed to %s, restarting", e, corrupt)
        return []


def save_tree_atomic(path: Path, tree) -> None:
    """Atomic tree JSON write. This is a stub to avoid circular imports;
    the actual call delegates to optimization_tree.save_tree_atomic.
    """
    from optimization_tree import save_tree_atomic as _impl
    _impl(path, tree)


def load_tree(path: Path):
    """Load tree JSON. Actual call delegates to optimization_tree.load_tree."""
    from optimization_tree import load_tree as _impl
    return _impl(path)


# ---------------------------------------------------------------------------
# Built-in self-test: python3 agenticpd/utils.py
# ---------------------------------------------------------------------------

def _self_test() -> None:
    """Quick self-test of extract_json with dirty samples + qor_is_better
    with hand-written test cases"""
    # --- extract_json ---
    cases_ok = [
        '{"a": 1}',
        '```json\n{"a": 1}\n```',
        'OK, parameters are:\n```\n{"a": 1}\n```\nThat\'s it.',
        'prefix text {"a": 1} suffix text',
    ]
    for c in cases_ok:
        assert extract_json(c) == {"a": 1}, f"extract_json failed: {c!r}"
    for bad in ("", "no json at all", '{"a": }'):
        try:
            extract_json(bad)
            raise AssertionError(f"Should have raised JsonParseError: {bad!r}")
        except JsonParseError:
            pass

    # --- qor_is_better (tolerances: wns=10ps, tns=50ps) ---
    q = lambda w, t, a, p: QoR(wns_ps=w, tns_ps=t, area_um2=a, power_w=p)
    table = [
        # (new, old, expected, description)
        (q(-50, -500, 700, 2e-3), None, True, "new always wins when old is None"),
        (None, q(-50, -500, 700, 2e-3), False, "new always loses when None"),
        (q(-30, -500, 700, 2e-3), q(-50, -500, 700, 2e-3), True, "WNS diff > tol, larger wins"),
        (q(-45, -300, 700, 2e-3), q(-50, -500, 700, 2e-3), True, "WNS tie, compare TNS"),
        (q(-45, -480, 700, 1e-3), q(-50, -500, 700, 2e-3), True, "WNS/TNS tie, compare power"),
        (q(-45, -480, 600, 2e-3), q(-50, -500, 700, 2e-3), True, "power tie, compare area"),
        (q(5, 0, 700, 2e-3), q(20, 0, 700, 1e-3), False, "both converged, compare power, new worse"),
        (q(5, 0, 700, 1e-3), q(20, 0, 700, 2e-3), True, "both converged, new saves power"),
        (q(-50, -500, 700, 2e-3), q(-50, -500, 700, 2e-3), False, "exact tie, keep old"),
        (q(None, -500, 700, 2e-3), q(-50, -500, 700, 2e-3), False, "incomplete QoR always loses"),
    ]
    for new, old, expect, desc in table:
        got = qor_is_better(new, old, wns_tol_ps=10.0, tns_tol_ps=50.0)
        assert got == expect, f"qor_is_better test failed: {desc} (got={got})"

    print("utils.py self-test all passed ✓")


if __name__ == "__main__":
    _self_test()
