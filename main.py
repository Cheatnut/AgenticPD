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
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import config
from config import AGENTICPD_DIR, ENV_FILENAME, RUNS_DIR as CFG_RUNS_DIR, FrameworkConfig, get_design_runs_dir
from optimizer import Optimizer
from orfs_interface import MockORFSRunner, ORFSRunner
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
    parser.add_argument("--log-level", type=str, default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Log level (DEBUG prints full prompts)")
    return parser.parse_args()


def resolve_run_dir(resume: Optional[str], platform: str, design: str) -> Path:
    """Resolve the run working directory.

    Session directories are organised as::

        runs/<platform>_<design>/<YYYYMMDD_HHMMSS>/

    TrialManager creates per-trial subdirectories within the session.
    Multiple invocations never share files.
    """
    design_dir = get_design_runs_dir(platform, design)

    if resume is None:
        run_dir = design_dir / datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    if resume == "latest":
        candidates = sorted((d for d in design_dir.iterdir() if d.is_dir()),
                            key=lambda d: d.name)
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
    """Parse QoR for an existing variant (validate parsing logic, zero EDA)."""
    runner = ORFSRunner(cfg)
    qor = runner.parse_qor(variant)
    if qor is None:
        log.error("[MAIN] variant=%s: no parseable report found (6_report.json / "
                  "6_finish.rpt / 6_report.log)", variant)
        sys.exit(1)
    log.info("[MAIN] variant=%s final QoR: %s", variant, qor.pretty())
    log.info("[MAIN] raw values: %s", qor.to_dict())
    stage_qor = runner.parse_stage_qor(variant)
    log.info("[MAIN] stage intermediate timing: %s",
             json.dumps(stage_qor, ensure_ascii=False, indent=2))
    failed = runner.detect_failed_stage(variant)
    if failed:
        log.warning("[MAIN] Flow stopped at stage per JSON check: %s (expected if variant only ran partial flow)", failed)


def main() -> None:
    args = parse_args()

    # 1) Load .env (API key via env var only, never hardcoded)
    load_dotenv_file(AGENTICPD_DIR / ENV_FILENAME)

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

    # Wipe ALL stale agenticpd variants from previous runs before starting.
    # Without this, a new run with fewer iterations leaves behind old higher-
    # numbered variant directories (e.g. old run had 10 iters → iter4..9 persist).
    n_wiped = runner.wipe_all_variants()
    if n_wiped:
        log.info('[MAIN] pre-run wiped %d stale variant directories from previous run', n_wiped)

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
