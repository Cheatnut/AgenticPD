# -*- coding: utf-8 -*-
"""
main.py — AgenticPD entry point

Run from flow/ directory:
    python3 agenticpd/main.py [options]

Common modes:
    # Full optimization (requires DEEPSEEK_API_KEY)
    python3 agenticpd/main.py --iterations 10

    # Parse QoR for an existing variant (zero token, zero EDA)
    python3 agenticpd/main.py --parse-only base

    # Run baseline ORFS once (validate make chain, zero token)
    python3 agenticpd/main.py --baseline-only

    # Full mock debugging (zero token, zero EDA, finishes in seconds)
    python3 agenticpd/main.py --mock-llm --mock-orfs --iterations 5

    # MockLLM + real ORFS (zero token end-to-end)
    python3 agenticpd/main.py --mock-llm --iterations 2

    # Resume from a previous run
    python3 agenticpd/main.py --resume [run_dir, defaults to latest]
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import config
from config import AGENTICPD_DIR, ENV_FILENAME, RUNS_DIR as CFG_RUNS_DIR, FrameworkConfig, get_design_runs_dir
from optimizer import Optimizer
from orfs_interface import MockORFSRunner, ORFSRunner
from orfs.parser import parse_qor, parse_stage_qor, detect_failed_stage
from utils import load_dotenv_file, setup_logging

log = logging.getLogger("main")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="AgenticPD: LLM multi-agent driven physical design QoR optimization",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--iterations", type=int, default=None,
                        help="Number of optimization iterations (excluding baseline #0; default from config.py)")
    parser.add_argument("--platform", type=str, default=None,
                        help="Target platform (default from config.py)")
    parser.add_argument("--design", type=str, default=None,
                        help="Target design name (default from config.py)")
    parser.add_argument("--timeout", type=int, default=None,
                        help="Single flow timeout in seconds (default from config.py)")
    parser.add_argument("--wns-tol", type=float, default=None,
                        help="WNS tolerance in ps for QoR comparison (default from config.py)")
    parser.add_argument("--tns-tol", type=float, default=None,
                        help="TNS tolerance in ps for QoR comparison (default from config.py)")
    parser.add_argument("--mock-llm", action="store_true",
                        help="Use MockLLMClient (zero token, deterministic decisions)")
    parser.add_argument("--mock-orfs", action="store_true",
                        help="Use MockORFSRunner (no real EDA, synthesize QoR in seconds)")
    parser.add_argument("--baseline-only", action="store_true",
                        help="Run baseline flow once, parse QoR, then exit (zero token)")
    parser.add_argument("--parse-only", type=str, metavar="VARIANT",
                        help="Parse QoR for a given variant (e.g. 'base') and exit")
    parser.add_argument("--resume", nargs="?", const="latest", default=None,
                        metavar="RUN_DIR",
                        help="Resume from a previous run directory (default: latest)")
    parser.add_argument("--stage-d", type=str, default=None,
                        metavar="YAML",
                        help="Run Stage D GWTW orchestration from experiment YAML")
    parser.add_argument("--log-level", type=str, default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Log level (DEBUG prints full prompts)")
    return parser.parse_args()


def _run_stage_d(args, yaml_path: Path) -> None:
    """Stage D dedicated entry: YAML is sole authority.

    Diverges from the generic main() path BEFORE run_dir / FrameworkConfig
    / logging / runner creation.  Everything — run_dir, snapshot, runner,
    managers — is derived from the YAML-backed StageDConfig.
    """
    from orchestrator import StageDConfig, StageDOrchestrator
    from managers import TrialManager, CheckpointManager

    # 1) Parse and validate YAML first — nothing else exists yet.
    sd_cfg = StageDConfig.from_yaml(yaml_path)

    # 2) Derive FrameworkConfig from YAML (design/platform authority).
    fw = sd_cfg.to_framework_config()
    if not fw.flow_dir:
        fw.flow_dir = Path("flow")

    # 3) Create run_dir from YAML experiment_id + timestamp.
    design_dir = get_design_runs_dir(fw.platform, fw.design)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = design_dir / f"{sd_cfg.experiment_id}_{ts}"
    run_dir.mkdir(parents=True, exist_ok=True)
    sd_cfg.runs_dir = run_dir
    fw.run_dir = run_dir

    # 4) Setup logging to the Stage D run_dir.
    log_file = run_dir / "agenticpd.log"
    setup_logging(log_file, level=getattr(logging, args.log_level))
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    log.info("[MAIN] Stage D start: experiment=%s platform=%s design=%s "
             "run_dir=%s", sd_cfg.experiment_id, fw.platform, fw.design,
             run_dir)

    # 5) Write config snapshot — unique to this Stage D run.
    snapshot = {
        "mode": "stage-d",
        "experiment_id": sd_cfg.experiment_id,
        "platform": fw.platform,
        "design": fw.design,
        "population_size": sd_cfg.population_size,
        "seed": sd_cfg.seed,
        "max_trials": sd_cfg.max_trials,
        "wall_clock_budget_s": sd_cfg.wall_clock_budget_s,
        "evaluator": sd_cfg.evaluator,
        "pl_survivor_count": sd_cfg.pl_survivor_count,
        "pl_audit_quota": sd_cfg.pl_audit_quota,
        "cts_survivor_count": sd_cfg.cts_survivor_count,
        "cts_audit_quota": sd_cfg.cts_audit_quota,
        "doomed_rule_version": sd_cfg.doomed_rule_version,
        "scheduler_version": sd_cfg.scheduler_version,
        "planner_version": sd_cfg.planner_version,
        "framework_config": fw.to_dict(),
        "yaml_path": str(yaml_path.resolve()),
    }
    (run_dir / "config_snapshot.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2),
        encoding="utf-8")

    # 6) Create runner from YAML-derived FrameworkConfig.
    if args.mock_orfs:
        from orfs.interface import MockORFSRunner
        runner = MockORFSRunner(fw)
    else:
        from orfs.interface import ORFSRunner
        runner = ORFSRunner(fw)

    # 7) Create managers and orchestrator, then run.
    trial_mgr = TrialManager(run_dir)
    checkpoint_mgr = CheckpointManager(fw.flow_dir)
    orch = StageDOrchestrator(sd_cfg, trial_mgr, checkpoint_mgr, runner)
    result = orch.run()

    log.info("[MAIN] Stage D complete: total_trials=%d budget_remaining=%d "
             "errors=%s resumed=%s",
             result.total_trials, result.budget_remaining,
             result.errors, result.resumed)
    if result.errors:
        for err in result.errors:
            log.error("[MAIN] Stage D error: %s", err)
        sys.exit(1)


def resolve_run_dir(resume: Optional[str], platform: str, design: str) -> Path:
    """Resolve the run working directory.

    Session directories are organised as::

        runs/<platform>_<design>/<YYYYMMDD_HHMMSS>/

    TrialManager creates per-trial subdirectories within the session.
    Multiple invocations never share files.
    """
    design_dir = get_design_runs_dir(platform, design)

    if resume is None:
        # Auto-number sessions: 001_, 002_, ...
        # Only match names with a 3-digit numeric prefix (NNN_YYYYMMDD_HHMMSS)
        seq = 1
        for d in design_dir.iterdir():
            if not d.is_dir() or d.name.startswith("."):
                continue
            m = re.match(r"^(\d{3})_", d.name)
            if m:
                seq = max(seq, int(m.group(1)) + 1)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = design_dir / f"{seq:03d}_{ts}"
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    if resume == "latest":
        # Sort by session number (001_, 002_, …), fallback to name
        def _session_key(d):
            m = re.match(r"^(\d+)_", d.name)
            return (int(m.group(1)), d.name) if m else (10**9, d.name)
        candidates = sorted((d for d in design_dir.iterdir()
                             if d.is_dir() and not d.name.startswith(".")),
                            key=_session_key)
        if not candidates:
            sys.exit(f"Error: --resume found no historical run directories ({design_dir})")
        return candidates[-1]

    run_dir = Path(resume)
    if not run_dir.is_dir():
        sys.exit(f"Error: --resume directory not found: {run_dir}")
    return run_dir


def build_config(args: argparse.Namespace, run_dir: Path) -> FrameworkConfig:
    """Build FrameworkConfig: CLI args override config.py defaults.

    argparse defaults are None — only explicitly passed args override.
    This keeps config.py as the single source of truth for defaults.
    """
    cfg = FrameworkConfig(run_dir=run_dir)
    overrides = {
        "platform": args.platform,
        "design": args.design,
        "iterations": args.iterations,
        "timeout_s": args.timeout,
        "wns_tol_ps": args.wns_tol,
        "tns_tol_ps": args.tns_tol,
    }
    for field_name, value in overrides.items():
        if value is not None:
            setattr(cfg, field_name, value)
    # Archive run config for reproducibility
    (run_dir / "config_snapshot.json").write_text(
        json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8")
    return cfg


def mode_parse_only(cfg: FrameworkConfig, variant: str) -> None:
    """Parse QoR for an existing variant (validate parsing logic, zero EDA).

    Uses orfs.parser functions directly — does not instantiate ORFSRunner,
    so this mode works without a valid ORFS environment (zero subprocess).
    """
    qor = parse_qor(cfg, variant)
    if qor is None:
        log.error("[MAIN] variant=%s: no parseable report found (6_report.json / "
                  "6_finish.rpt / 6_report.log)", variant)
        sys.exit(1)
    log.info("[MAIN] variant=%s final QoR: %s", variant, qor.pretty())
    log.info("[MAIN] raw values: %s", qor.to_dict())
    stage_qor = parse_stage_qor(cfg, variant)
    log.info("[MAIN] stage intermediate timing: %s",
             json.dumps(stage_qor, ensure_ascii=False, indent=2))
    failed = detect_failed_stage(cfg, variant)
    if failed:
        log.warning("[MAIN] Flow stopped at stage per JSON check: %s (expected if variant only ran partial flow)", failed)


def main() -> None:
    args = parse_args()

    # 1) Load .env (API key via env var only, never hardcoded)
    load_dotenv_file(AGENTICPD_DIR / ENV_FILENAME)

    # ------------------------------------------------------------------
    # Stage D: YAML is sole authority.  Divert BEFORE generic run_dir /
    # FrameworkConfig / logging creation so no stale session directory
    # is left on disk.
    # ------------------------------------------------------------------
    if args.stage_d:
        yaml_path = Path(args.stage_d)
        if not yaml_path.is_file():
            log.error("Stage D YAML not found: %s", yaml_path)
            sys.exit(1)
        _run_stage_d(args, yaml_path)
        return

    # 2) Working directory & logging
    # Resolve platform/design early (before run_dir) so the session
    # directory can be placed under runs/<platform>_<design>/.
    _platform = args.platform or FrameworkConfig.platform
    _design = args.design or FrameworkConfig.design
    run_dir = resolve_run_dir(args.resume, _platform, _design)
    cfg = build_config(args, run_dir)
    setup_logging(cfg.log_file, level=getattr(logging, args.log_level))
    # Suppress httpx/openai HTTP request logs (too noisy)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    log.info("[MAIN] AgenticPD start: platform=%s design=%s run_dir=%s",
             cfg.platform, cfg.design, run_dir)

    # 3) Special modes
    if args.parse_only:
        mode_parse_only(cfg, args.parse_only)
        return

    runner = MockORFSRunner(cfg) if args.mock_orfs else ORFSRunner(cfg)

    # Wipe stale variants only for fresh runs (not resume).
    if not args.resume:
        n_wiped = runner.wipe_all_variants()
        if n_wiped:
            log.debug('[MAIN] pre-run wiped %d stale variant directories', n_wiped)

    if args.baseline_only:
        # Baseline only: no LLM needed, run iteration #0 directly
        optimizer = Optimizer(cfg, llm=None, runner=runner)
        result = optimizer.run_baseline()
        sys.exit(0 if result.ok else 1)

    # 4) Full optimization: choose LLM client (real / mock)
    if args.mock_llm:
        from llm_interface import MockLLMClient
        llm = MockLLMClient(cfg)
        log.info("[MAIN] --mock-llm: using MockLLMClient (zero token)")
    else:
        from llm_interface import LLMClient, LLMError
        try:
            llm = LLMClient(cfg)
        except LLMError as e:
            sys.exit(f"Error: {e}")

    optimizer = Optimizer(cfg, llm=llm, runner=runner)
    optimizer.run(resume=args.resume is not None)

    # Generate optimization tree visualization
    try:
        # tools/ subdirectory
        import sys as _sys
        _tools_dir = str(AGENTICPD_DIR / "tools")
        if _tools_dir not in _sys.path:
            _sys.path.insert(0, _tools_dir)
        from visualize import visualize_tree
        vis_path = visualize_tree(cfg.run_dir)
        if vis_path:
            log.info("[MAIN] Optimization tree visualization saved to %s", vis_path)
    except Exception as e:
        log.warning("[MAIN] Tree visualization failed (non-fatal): %s", e)


if __name__ == "__main__":
    main()
