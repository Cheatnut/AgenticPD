# -*- coding: utf-8 -*-
"""multi_agent_gwtw.py — Multi-Agent + Doomed/GWTW demo entry point.

Thin CLI that reads a YAML experiment config, selects real or mock LLM/ORFS
runner, creates managers, and launches MultiAgentGWTWOrchestrator.

Usage:
    # Mock mode (zero token, zero EDA)
    python3 multi_agent_gwtw.py \\
      --config configs/experiments/multi-agent-gwtw-demo.yml \\
      --mock-llm --mock-orfs

    # Real LLM + real ORFS
    python3 multi_agent_gwtw.py \\
      --config configs/experiments/multi-agent-gwtw-demo.yml

    # MockLLM + real ORFS
    python3 multi_agent_gwtw.py \\
      --config configs/experiments/multi-agent-gwtw-demo.yml \\
      --mock-llm
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
import config as cfg_mod
from config import AGENTICPD_DIR, ENV_FILENAME, FrameworkConfig, get_design_runs_dir
from gwtw.orchestrator import (
    MultiAgentGWTWConfig,
    MultiAgentGWTWOrchestrator,
)
from storage import TrialManager, CheckpointManager
from core.utils import load_dotenv_file, setup_logging

log = logging.getLogger("multi_agent_gwtw")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Multi-Agent + Doomed/GWTW optimization demo",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--config", type=str, required=True,
                        help="Path to experiment YAML config")
    parser.add_argument("--mock-llm", action="store_true",
                        help="Use MockLLMClient (zero token, deterministic)")
    parser.add_argument("--mock-orfs", action="store_true",
                        help="Use MockORFSRunner (zero EDA, synthetic QoR)")
    parser.add_argument("--log-level", type=str, default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Log level")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    # 1) Load .env for API key (real LLM only).
    load_dotenv_file(AGENTICPD_DIR / ENV_FILENAME)

    # 2) Parse YAML config — sole authority.
    yaml_path = Path(args.config)
    if not yaml_path.is_file():
        log.error("Config YAML not found: %s", yaml_path)
        sys.exit(1)
    cfg = MultiAgentGWTWConfig.from_yaml(yaml_path)

    # 3) Derive FrameworkConfig from YAML.
    fw = cfg.to_framework_config()
    if not fw.flow_dir:
        fw.flow_dir = Path("flow")

    # 4) Create session run_dir.
    design_dir = get_design_runs_dir(fw.platform, fw.design)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = design_dir / f"{cfg.experiment_id}_{ts}"
    run_dir.mkdir(parents=True, exist_ok=True)
    cfg.runs_dir = run_dir
    fw.run_dir = run_dir

    # 5) Setup logging.
    log_file = run_dir / "agenticpd.log"
    setup_logging(log_file, level=getattr(logging, args.log_level))
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    log.info("[GWTW-DEMO] start: experiment=%s platform=%s design=%s "
             "run_dir=%s", cfg.experiment_id, fw.platform, fw.design, run_dir)

    # 6) Write config snapshot.
    snapshot = {
        "mode": "demo-gwtw",
        "experiment_id": cfg.experiment_id,
        "platform": fw.platform,
        "design": fw.design,
        "population_size": cfg.population_size,
        "seed": cfg.seed,
        "max_trials": cfg.max_trials,
        "wall_clock_budget_s": cfg.wall_clock_budget_s,
        "evaluator": cfg.evaluator,
        "decision_stages": cfg.decision_stages,
        "pl_survivor_count": cfg.pl_survivor_count,
        "pl_audit_quota": cfg.pl_audit_quota,
        "cts_survivor_count": cfg.cts_survivor_count,
        "cts_audit_quota": cfg.cts_audit_quota,
        "doomed_rule_version": cfg.doomed_rule_version,
        "scheduler_version": cfg.scheduler_version,
        "planner_version": cfg.planner_version,
        "framework_config": fw.to_dict(),
        "yaml_path": str(yaml_path.resolve()),
        "mock_llm": args.mock_llm,
        "mock_orfs": args.mock_orfs,
    }
    (run_dir / "config_snapshot.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    # 7) Create runner.
    if args.mock_orfs:
        # Stage-E-aware mock runner: MockORFSRunner._mock_stage_qor only
        # produces _ws_ps keys, but the Doomed/GWTW observation builder requires
        # BOTH _ws_ps and _tns_ps keys with the same tag.  Fix by using the
        # StageERecordingFakeRunner which produces complete two-key QoR.
        from gwtw.orchestrator import StageERecordingFakeRunner
        runner = StageERecordingFakeRunner(fw.flow_dir)
    else:
        from orfs.interface import ORFSRunner
        runner = ORFSRunner(fw)

    # 8) Create LLM client + Agents.
    judge_agent = None
    stage_agents = {}
    if args.mock_llm:
        from agents.llm import MockLLMClient
        llm = MockLLMClient(fw)
    else:
        from agents.llm import LLMClient, LLMError
        try:
            llm = LLMClient(fw)
        except LLMError as e:
            sys.exit(f"Error: {e}")

    # Create agents regardless of mock/real — they share the same interface.
    from agents.judge import JudgeAgent
    from agents.stage import build_stage_agents
    judge_agent = JudgeAgent(llm, fw)
    stage_agents = build_stage_agents(llm, fw)
    log.info("[GWTW-DEMO] Agents ready: Judge + %d StageAgents",
             len(stage_agents))

    # 9) Create managers and orchestrator, then run.
    trial_mgr = TrialManager(run_dir)
    checkpoint_mgr = CheckpointManager(fw.flow_dir)
    orch = MultiAgentGWTWOrchestrator(
        cfg, trial_mgr, checkpoint_mgr, runner,
        judge_agent=judge_agent, stage_agents=stage_agents)
    result = orch.run()

    log.info("[GWTW-DEMO] complete: total_trials=%d budget_remaining=%d "
             "errors=%s resumed=%s finish_trials=%d",
             result.total_trials, result.budget_remaining,
             result.errors, result.resumed, len(result.finish_trial_ids))
    if result.errors:
        for err in result.errors:
            log.error("[GWTW-DEMO] error: %s", err)
        sys.exit(1)


if __name__ == "__main__":
    main()
