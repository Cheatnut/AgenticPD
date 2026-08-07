# -*- coding: utf-8 -*-
"""gwtw/config.py — experiment configuration for the Doomed/GWTW demo."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml

from config import FrameworkConfig


_DEFAULT_DOOMED_VERSION = "1.0.0"
_DEFAULT_SCHEDULER_VERSION = "1.0.0"
_DEFAULT_PLANNER_VERSION = "1.0.0"


@dataclass
class MultiAgentGWTWConfig:
    """Doomed/GWTW experiment configuration — YAML is sole authority."""

    experiment_id: str
    platform: str
    design: str
    population_size: int
    seed: int
    max_trials: int
    wall_clock_budget_s: Optional[float] = None
    pl_survivor_count: int = 2
    pl_audit_quota: int = 0
    pl_max_children_per_parent: int = 2
    cts_survivor_count: int = 1
    cts_audit_quota: int = 1
    cts_max_children_per_parent: int = 2
    doomed_rule_version: str = _DEFAULT_DOOMED_VERSION
    scheduler_version: str = _DEFAULT_SCHEDULER_VERSION
    planner_version: str = _DEFAULT_PLANNER_VERSION
    decision_stages: List[str] = field(default_factory=lambda: ["PL", "CTS"])
    evaluator: str = "ORFS post-route QoR"
    runs_dir: Optional[Path] = None

    def __post_init__(self) -> None:
        if self.population_size < 1:
            raise ValueError("population.size must be >= 1")
        if self.max_trials < 1:
            raise ValueError("budget.max_trials must be >= 1")
        if not self.experiment_id:
            raise ValueError("experiment_id is required")
        if self.seed is None:
            raise ValueError("seed is required")
        if self.pl_survivor_count > self.population_size:
            raise ValueError("PL survivor_count exceeds population_size")
        if self.cts_survivor_count > self.population_size:
            raise ValueError("CTS survivor_count exceeds population_size")

    @classmethod
    def from_yaml(cls, path: Path) -> "MultiAgentGWTWConfig":
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if data is None:
            raise ValueError(f"empty YAML: {path}")
        pop = data.get("population", {})
        pl = data.get("decisions", {}).get("PL", {})
        cts = data.get("decisions", {}).get("CTS", {})
        budget = data.get("budget", {})
        versions = data.get("versions", {})
        design = data.get("design", {})
        evaluator = data.get("evaluator", {})
        decision_stages = data.get("decision_stages", ["PL", "CTS"])
        return cls(
            experiment_id=data["experiment_id"],
            platform=design["platform"], design=design["design"],
            population_size=pop["size"], seed=data["seed"],
            max_trials=budget["max_trials"],
            wall_clock_budget_s=budget.get("wall_clock_s"),
            pl_survivor_count=pl.get("survivor_count", 2),
            pl_audit_quota=pl.get("audit_quota", 0),
            pl_max_children_per_parent=pl.get("max_children_per_parent", 2),
            cts_survivor_count=cts.get("survivor_count", 1),
            cts_audit_quota=cts.get("audit_quota", 1),
            cts_max_children_per_parent=cts.get("max_children_per_parent", 2),
            doomed_rule_version=versions.get(
                "doomed_rule", _DEFAULT_DOOMED_VERSION),
            scheduler_version=versions.get(
                "scheduler", _DEFAULT_SCHEDULER_VERSION),
            planner_version=versions.get(
                "planner", _DEFAULT_PLANNER_VERSION),
            decision_stages=decision_stages,
            evaluator=evaluator.get("type", "ORFS post-route QoR"),
        )

    def to_framework_config(self) -> FrameworkConfig:
        return FrameworkConfig(platform=self.platform, design=self.design)

    @property
    def _pl_cohort_cfg(self):
        return (self.pl_survivor_count, self.pl_audit_quota,
                self.population_size, self.pl_max_children_per_parent,
                self.doomed_rule_version, self.scheduler_version,
                self.planner_version)

    @property
    def _cts_cohort_cfg(self):
        return (self.cts_survivor_count, self.cts_audit_quota,
                self.population_size, self.cts_max_children_per_parent,
                self.doomed_rule_version, self.scheduler_version,
                self.planner_version)


# =============================================================================
