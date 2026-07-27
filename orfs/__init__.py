# -*- coding: utf-8 -*-
"""orfs — Stage C: ORFS adapter split into command, parser, runner, interface.

Re-exports everything from the original orfs_interface.py so existing
imports (e.g. ``from orfs_interface import ORFSRunner, RunResult``)
continue to work without modification.
"""

from orfs.interface import ORFSRunner, MockORFSRunner, RunResult
