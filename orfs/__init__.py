# -*- coding: utf-8 -*-
"""orfs — ORFS adapter split into command, parser, runner, interface.

Re-exports the ORFS public API so existing
imports (e.g. ``from orfs.interface import ORFSRunner, RunResult``)
continue to work without modification.
"""

from orfs.interface import ORFSRunner, MockORFSRunner, RunResult
