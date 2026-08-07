# -*- coding: utf-8 -*-
"""session_visualize — offline HTML visualization for AgenticPD sessions."""
from __future__ import annotations

from tools.session_visualize.data import (
    _validate_contained,
    _validate_dir,
    extract_session_data,
    load_config,
    load_traces,
    load_trials,
    load_tree,
)
from tools.session_visualize.render import _json_embed_safe
from tools.session_visualize.cli import _build_argparser, generate_visualization

__all__ = [
    "_build_argparser", "_json_embed_safe", "_validate_contained",
    "_validate_dir", "extract_session_data", "generate_visualization",
    "load_config", "load_traces", "load_trials", "load_tree",
]
