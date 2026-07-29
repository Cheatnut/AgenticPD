# -*- coding: utf-8 -*-
"""
config.py — AgenticPD global configuration module

This module is the single source of truth for all parameters/paths in the framework.
All other modules read configuration from here; hardcoding any path, parameter name,
or value range elsewhere is forbidden.

Three sections:
1. ParamSpec / PARAM_SPACE: Data-driven definition of tunable parameter space.
   Adding/removing params only requires changes here; agents.py / orfs_interface.py
   will auto-adapt.
2. BASELINE_PARAMS: Parameters used for the baseline (iteration #0) run,
   consistent with ORFS base flow.
3. FrameworkConfig: Runtime configuration (paths, timeout, LLM settings, QoR
   comparison tolerances, etc.).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Base path derivation (all derived from this file's location, no hardcoded
# absolute paths). Assumed directory layout: <ORFS root>/flow/agenticpd/config.py
# ---------------------------------------------------------------------------
AGENTICPD_DIR: Path = Path(__file__).resolve().parent          # flow/agenticpd/
FLOW_DIR: Path = AGENTICPD_DIR.parent                          # flow/
RUNS_DIR_NAME: str = "runs"       # agenticpd/runs/ directory name
ENV_FILENAME: str = ".env"        # environment variable filename
RUNS_DIR: Path = AGENTICPD_DIR / RUNS_DIR_NAME  # working directory for all runs


def get_design_runs_dir(platform: str, design: str) -> Path:
    """Return the per-design runs directory: ``runs/<platform>_<design>/``."""
    d = RUNS_DIR / f"{platform}_{design}"
    d.mkdir(parents=True, exist_ok=True)
    return d

# ORFS four artifact directory names (results/logs/reports/objects),
# consistent with FrameworkConfig path logic
ORFS_CATEGORIES: List[str] = ["results", "logs", "reports", "objects"]

# ---------------------------------------------------------------------------
# Timing unit conversion: timing values in ORFS JSON metrics / reports are in ns
# (determined by the PDK; both sky130hd and nangate45 use ns). This framework
# uniformly uses ps externally, so multiply by 1000.
# If switching to a PDK with a different timing unit, just change this constant.
# ---------------------------------------------------------------------------
TIMING_UNIT_TO_PS: float = 1000.0

# ---------------------------------------------------------------------------
# Parameter space definition
# ---------------------------------------------------------------------------

# Canonical names for the four physical design stages (order = flow order,
# also used for Judge round-robin fallback)
STAGES: List[str] = ["FP", "PL", "CTS", "RT"]

# Parameter "delivery kind":
#   make_var             — directly appended to make command as NAME=value
#   fastroute_adjustment — pseudo-param: generate custom fastroute.tcl,
#                          then pass FASTROUTE_TCL=<path>
#                          (sky130hd's FASTROUTE_TCL hardcodes layer capacity 0.2,
#                           bypassing the ROUTING_LAYER_ADJUSTMENT env var,
#                           so we must use the official AutoTuner approach of
#                           generating a custom tcl)
#   global_route_args    — pseudo-param: rendered into GLOBAL_ROUTE_ARGS
#                          as -congestion_iterations
KIND_MAKE_VAR = "make_var"
KIND_FASTROUTE_ADJ = "fastroute_adjustment"
KIND_GRT_ARGS = "global_route_args"


@dataclass(frozen=True)
class ParamSpec:
    """Complete specification for a single tunable parameter
    (data-driven, used for prompt rendering and validation)"""

    name: str            # parameter name (make variable or pseudo-param name)
    stage: str           # owning stage: FP / PL / CTS / RT
    ptype: str           # type: "int" or "float"
    vmin: float          # lower bound (inclusive)
    vmax: float          # upper bound (inclusive)
    default: Optional[float]  # baseline default; None = not explicitly passed
                              # at baseline (use ORFS default)
    description: str     # human-readable description (rendered into stage agent
                         # system prompts)
    kind: str = KIND_MAKE_VAR  # delivery kind, see comments above

    # Stages affected when this parameter changes (in order: FP, PL, CTS, RT).
    # e.g. ("CTS","RT") = changing this param invalidates CTS+RT checkpoints
    # but not FP or PL.  Default for FP params is all stages.
    affects: tuple = ()

    # Stages affected when this parameter changes (FP,PL,CTS,RT order).
    # e.g. ("CTS","RT") = invalidates CTS+RT checkpoints only.
    affects: tuple = ()

    def cast(self, value: Any) -> float:
        """Coerce raw LLM output to this param's type and clamp to [vmin, vmax]"""
        v = float(value)
        v = max(self.vmin, min(self.vmax, v))
        if self.ptype == "int":
            return int(round(v))
        return round(v, 4)  # keep 4 decimal places for floats to avoid
                            # excessively long values in make commands


# Tunable parameter space per stage (ranges primarily based on the official
# AutoTuner settings for sky130hd/gcd, see
# flow/designs/sky130hd/gcd/autotuner.json)
PARAM_SPACE: Dict[str, List[ParamSpec]] = {
    "FP": [
        ParamSpec(
            name="CORE_UTILIZATION", stage="FP", ptype="int",
            vmin=20, vmax=50, default=38,
            description=(
                "Core utilization (percent, 20–50). Higher = smaller chip area "
                "but more routing congestion; lower = easier timing/routing but "
                "larger area. Base design currently uses 38."
            ),
            affects=('FP', 'PL', 'CTS', 'RT'),
        ),
        ParamSpec(
            name="CORE_ASPECT_RATIO", stage="FP", ptype="float",
            vmin=0.5, vmax=2.0, default=1.0,
            description=(
                "Core aspect ratio (height/width, 0.5–2.0). Affects floorplan "
                "shape and clock/signal wirelength distribution. 1.0 = square."
            ),
            affects=('FP', 'PL', 'CTS', 'RT'),
        ),
    ],
    "PL": [
        ParamSpec(
            name="PLACE_DENSITY_LB_ADDON", stage="PL", ptype="float",
            vmin=0.0, vmax=0.2, default=None,
            description=(
                "Placement density margin (0.0–0.2). When set, actual density "
                "= feasible lower bound + addon, preventing placer errors from "
                "excessively low density. Smaller addon = cells more spread out "
                "(better for timing/routability); larger = more compact. "
                "Note: baseline does not set this parameter, using the platform's "
                "fixed density 0.60; once set, it switches to 'lower bound + addon' mode."
            ),
            affects=('PL', 'CTS', 'RT'),
        ),
        ParamSpec(
            name="CELL_PAD_IN_SITES_GLOBAL_PLACEMENT", stage="PL", ptype="int",
            vmin=0, vmax=3, default=0,
            description=(
                "Cell padding in sites on both sides during global placement "
                "(0–3). Larger values relieve local congestion and improve "
                "routability, but effectively increase density pressure."
            ),
            affects=('PL', 'CTS', 'RT'),
        ),
    ],
    "CTS": [
        ParamSpec(
            name="CTS_CLUSTER_SIZE", stage="CTS", ptype="int",
            vmin=10, vmax=200, default=None,
            description=(
                "Maximum sinks per cluster for clock tree sink clustering "
                "(10–200). Smaller = lower local skew but more buffers and "
                "higher power; baseline not set (uses tool default)."
            ),
            affects=('CTS', 'RT'),
        ),
        ParamSpec(
            name="CTS_CLUSTER_DIAMETER", stage="CTS", ptype="int",
            vmin=20, vmax=400, default=None,
            description=(
                "Maximum cluster diameter for clock tree sink clustering "
                "(microns, 20–400). Smaller = more balanced clock tree but "
                "more buffers inserted; baseline not set (uses tool default)."
            ),
            affects=('CTS', 'RT'),
        ),
        ParamSpec(
            name="SETUP_SLACK_MARGIN", stage="CTS", ptype="float",
            vmin=0.0, vmax=0.2,
            default=0.0,
            description=(
                "Setup slack margin target for repair_timing (ns, 0–0.2). "
                "Larger = tool repairs timing more aggressively (may increase "
                "area/power). Note: this variable also affects repair_timing "
                "in FP/GRT stages; it is managed under CTS for convenience."
            ),
            affects=('FP', 'PL', 'CTS', 'RT'),
        ),
    ],
    "RT": [
        ParamSpec(
            name="FASTROUTE_LAYER_ADJUSTMENT", stage="RT", ptype="float",
            vmin=0.1, vmax=0.3, default=0.2,
            description=(
                "Global routing layer capacity reduction factor (0.1–0.3, "
                "pseudo-param). Larger = more conservative global routing "
                "(more margin, more detours), smaller = more aggressive "
                "(shorter wirelength but detailed routing may be harder to "
                "converge). Platform default is 0.2. "
                "Implementation: generates a custom fastroute.tcl and passes "
                "it via FASTROUTE_TCL."
            ),
            kind=KIND_FASTROUTE_ADJ,
            affects=('FP', 'PL', 'CTS', 'RT'),
        ),
        ParamSpec(
            name="GRT_CONGESTION_ITERATIONS", stage="RT", ptype="int",
            vmin=10, vmax=50, default=30,
            description=(
                "Max congestion elimination iterations for global routing "
                "(10–50, pseudo-param, rendered into GLOBAL_ROUTE_ARGS "
                "-congestion_iterations). Larger = router gets more chances "
                "to resolve congestion (slower), smaller = faster but may "
                "leave residual congestion. Flow default is 30."
            ),
            kind=KIND_GRT_ARGS,
            affects=('RT',),
        ),
    ],
}

# Baseline parameters (used for iteration #0, kept as close as possible to the
# ORFS base run): only includes parameters that need to be explicitly passed to
# make; unlisted params use ORFS/design config defaults.
# Note: PL/CTS stages are intentionally empty at baseline —
# PLACE_DENSITY_LB_ADDON has different semantics when set (even to 0.0) vs.
# unset (see ParamSpec description); CTS params use tool internal defaults
# when not set.
BASELINE_PARAMS: Dict[str, Dict[str, Any]] = {
    "FP": {"CORE_UTILIZATION": 38, "CORE_ASPECT_RATIO": 1.0},
    "PL": {},
    "CTS": {},
    "RT": {"FASTROUTE_LAYER_ADJUSTMENT": 0.2, "GRT_CONGESTION_ITERATIONS": 30},
}

# GLOBAL_ROUTE_ARGS template: the first part is the ORFS default (must be
# included when overriding this variable, otherwise default congestion report
# params are lost), {iters} is the pseudo-param GRT_CONGESTION_ITERATIONS
GLOBAL_ROUTE_ARGS_TEMPLATE = (
    "-congestion_report_iter_step 5 -verbose -congestion_iterations {iters}"
)

# Custom fastroute.tcl template: content copied from
# flow/platforms/sky130hd/fastroute.tcl, with the hardcoded layer capacity
# adjustment factor 0.2 parameterized as {adjustment}
# (same approach as the official AutoTuner)
FASTROUTE_TCL_TEMPLATE = """\
set_global_routing_layer_adjustment $::env(MIN_ROUTING_LAYER)-$::env(MAX_ROUTING_LAYER) {adjustment}

set_routing_layers -clock $::env(MIN_CLK_ROUTING_LAYER)-$::env(MAX_ROUTING_LAYER)
set_routing_layers -signal $::env(MIN_ROUTING_LAYER)-$::env(MAX_ROUTING_LAYER)
"""


def get_param_spec(name: str) -> Optional[ParamSpec]:
    """Look up a ParamSpec by parameter name (returns None if not found)"""
    for specs in PARAM_SPACE.values():
        for spec in specs:
            if spec.name == name:
                return spec
    return None


# ---------------------------------------------------------------------------
# Framework runtime configuration
# ---------------------------------------------------------------------------

@dataclass
class FrameworkConfig:
    """Framework runtime configuration: paths, target design, timeout,
    LLM settings, QoR comparison tolerances, etc."""

    # ---- Target design (overridable via CLI; note PARAM_SPACE is tuned for
    #      sky130hd/gcd — gcd is the smoke design; re-evaluate parameter
    #      space and fastroute template) ----
    platform: str = "sky130hd"
    design: str = "gcd"

    # ---- Paths (all derived from FLOW_DIR) ----
    flow_dir: Path = field(default_factory=lambda: FLOW_DIR)
    # Working directory for this run (stores logs, history, generated
    # fastroute.tcl, etc.), created by main.py with a timestamp name,
    # e.g. flow/agenticpd/runs/20260718_153000/
    run_dir: Optional[Path] = None

    # ---- ORFS invocation ----
    make_target: str = "all"          # full flow: synth→floorplan→place→cts→route→finish
    timeout_s: int = 3600             # single flow timeout (seconds); gcd typically finishes in minutes
    variant_prefix: str = "agenticpd_iter"   # FLOW_VARIANT prefix for each iteration
    best_variant_name: str = "agenticpd_best"  # best result export directory (sibling to base)
    baseline_variant_name: str = "agenticpd_baseline"  # shared baseline (never wiped, cached across sessions)

    # ---- Optimization loop ----
    iterations: int = 10              # number of iterations (excluding baseline #0)
    history_window: int = 15          # history window size for prompts (most recent N entries)
    skip_non_target_agents: bool = False  # when True, non-target stages reuse best params directly (saves LLM calls)
    max_branch_count: int = 3        # max times a tree node can be branched from; exceeded nodes are excluded from branchable_nodes
    # (max depth for post-branch stages is reserved; currently only uses count to prevent over-exploration)

    # ---- LLM (DeepSeek, OpenAI-compatible API) ----
    llm_base_url: str = "https://api.deepseek.com"
    llm_model: str = "deepseek-v4-pro"
    llm_temperature: float = 0.6
    llm_api_key_env: str = "DEEPSEEK_API_KEY"  # env var name for API key (never hardcode key)
    max_json_retries: int = 3         # number of retries when LLM JSON parsing fails
    max_api_retries: int = 3          # number of retries for API network/rate-limit errors

    # ---- QoR comparison tolerances (gcd WNS is only tens of ps, these
    #      tolerances are sensitive; adjustable via CLI) ----
    wns_tol_ps: float = 10.0          # WNS diff below this = tie, proceed to TNS
    tns_tol_ps: float = 50.0          # TNS diff below this = tie, proceed to power/area

    # ------------------------------------------------------------------
    # Derived paths (defined once here; other modules must not build paths
    # independently)
    # ------------------------------------------------------------------
    @property
    def design_config(self) -> str:
        """DESIGN_CONFIG relative path (used with make -C flow_dir, relative to flow/)"""
        return f"./designs/{self.platform}/{self.design}/config.mk"

    def variant_name(self, iteration: int) -> str:
        """FLOW_VARIANT name for the given iteration"""
        return f"{self.variant_prefix}{iteration}"

    def results_dir(self, variant: str) -> Path:
        return self.flow_dir / "results" / self.platform / self.design / variant

    def logs_dir(self, variant: str) -> Path:
        return self.flow_dir / "logs" / self.platform / self.design / variant

    def reports_dir(self, variant: str) -> Path:
        return self.flow_dir / "reports" / self.platform / self.design / variant

    def objects_dir(self, variant: str) -> Path:
        return self.flow_dir / "objects" / self.platform / self.design / variant

    @property
    def tree_path(self) -> Path:
        """Optimization tree JSON file path (sibling to history.json)"""
        assert self.run_dir is not None, "run_dir must be initialized by main.py first"
        return self.run_dir / "tree.json"

    @property
    def log_file(self) -> Path:
        """Framework log file path"""
        assert self.run_dir is not None, "run_dir must be initialized by main.py first"
        return self.run_dir / "agenticpd.log"

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict (for archiving run config in run_dir, enabling
        experimental reproducibility)"""
        d = dataclasses.asdict(self)
        d["flow_dir"] = str(self.flow_dir)
        d["run_dir"] = str(self.run_dir) if self.run_dir else None
        return d
