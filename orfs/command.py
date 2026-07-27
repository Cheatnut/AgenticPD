# -*- coding: utf-8 -*-
"""orfs.command — Stage C1: ORFS make command construction.

Extracted from orfs_interface.py.  Builds the make command line and
supporting files (fastroute.tcl) for a given set of stage parameters.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config
from config import FrameworkConfig

log = logging.getLogger(__name__)


def build_make_cmd(
    cfg: FrameworkConfig,
    stage_params: Dict[str, Dict[str, Any]],
    variant: str,
    iteration: int,
    target: Optional[str] = None,
) -> Tuple[List[str], Path]:
    """Construct the ``make ...`` command for a full flow or single stage.

    Handles three kinds of parameters:
      - KIND_MAKE_VAR      — passed as ``NAME=value`` on the command line
      - KIND_FASTROUTE_ADJ — writes a custom fastroute.tcl, passes FASTROUTE_TCL
      - KIND_GRT_ARGS      — renders into GLOBAL_ROUTE_ARGS template

    Args:
        cfg:          FrameworkConfig instance.
        stage_params: per-stage parameter dict {stage: {param: value}}.
        variant:      FLOW_VARIANT name (e.g. "agenticpd_iter3").
        iteration:    iteration number (for naming fastroute.tcl and log).
        target:       make target override (None = cfg.make_target, "all").

    Returns:
        (cmd_list, make_log_path): the command to pass to subprocess, and the
        path where stdout/stderr will be written.
    """
    make_vars: Dict[str, str] = {}
    log_suffix = ""
    if target is not None and target != cfg.make_target:
        log_suffix = f"_{target}"

    for stage in config.STAGES:
        for name, value in stage_params.get(stage, {}).items():
            spec = config.get_param_spec(name)
            if spec is None:
                log.warning(
                    "[ORFS] Unknown param %s=%s (not in PARAM_SPACE), ignored",
                    name, value,
                )
                continue
            if spec.kind == config.KIND_MAKE_VAR:
                make_vars[name] = str(value)
            elif spec.kind == config.KIND_FASTROUTE_ADJ:
                tcl_path = _write_fastroute_tcl(cfg, float(value), iteration)
                make_vars["FASTROUTE_TCL"] = str(tcl_path)
            elif spec.kind == config.KIND_GRT_ARGS:
                make_vars["GLOBAL_ROUTE_ARGS"] = (
                    config.GLOBAL_ROUTE_ARGS_TEMPLATE.format(iters=int(value))
                )

    cmd = [
        "make", "-C", str(cfg.flow_dir),
        f"DESIGN_CONFIG={cfg.design_config}",
        f"FLOW_VARIANT={variant}",
    ]
    cmd += [f"{k}={v}" for k, v in sorted(make_vars.items())]
    cmd.append(target or cfg.make_target)

    assert cfg.run_dir is not None
    log_name = f"iter{iteration}{log_suffix}.make.log"
    make_log_path = cfg.run_dir / log_name
    return cmd, make_log_path


def _write_fastroute_tcl(cfg: FrameworkConfig, adjustment: float,
                         iteration: int) -> Path:
    """Generate a custom fastroute.tcl for this iteration.

    Written to ``cfg.run_dir`` (not objects/) because the objects variant
    directory does not exist yet when the command is being built.
    """
    assert cfg.run_dir is not None
    tcl_path = cfg.run_dir / f"fastroute_iter{iteration}.tcl"
    tcl_path.write_text(
        config.FASTROUTE_TCL_TEMPLATE.format(adjustment=f"{adjustment:.2f}"),
        encoding="utf-8",
    )
    return tcl_path.resolve()
