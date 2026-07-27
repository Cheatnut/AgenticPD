# -*- coding: utf-8 -*-
"""orfs_interface.py — backward-compatible re-export layer.

Stage C1: the original orfs_interface.py has been split into:
    orfs/command.py   — make command construction
    orfs/parser.py    — report / QoR parsing
    orfs/runner.py    — subprocess execution
    orfs/interface.py — ORFSRunner, MockORFSRunner, RunResult

This file re-exports the public API so existing imports continue to work:
    from orfs_interface import ORFSRunner, RunResult, MockORFSRunner
"""

from orfs.interface import ORFSRunner, MockORFSRunner, RunResult

__all__ = ["ORFSRunner", "MockORFSRunner", "RunResult"]
