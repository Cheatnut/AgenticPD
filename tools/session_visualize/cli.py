# -*- coding: utf-8 -*-
"""session_visualize/cli.py — CLI entry and visualization generation orchestration."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from tools.session_visualize.data import (
    _validate_contained,
    _validate_dir,
    extract_session_data,
)
from tools.session_visualize.render import _html_template, _json_embed_safe

log = logging.getLogger("session_visualize")



def generate_visualization(session_dir: Path) -> Path:
    """Generate visualization/index.html and visualization/session_data.json.

    Returns the path to the generated index.html.
    Only overwrites the two generated files; all other session artifacts
    are untouched.
    """
    session_dir = _validate_dir(session_dir)

    # Validate visualization output is within session_dir.
    viz_rel = "visualization"
    viz_dir = _validate_contained(session_dir, viz_rel)
    viz_dir.mkdir(parents=True, exist_ok=True)

    # 1. Build and write session_data.json (audit copy).
    log.info("Extracting session data from %s", session_dir)
    data = extract_session_data(session_dir)

    data_path = viz_dir / "session_data.json"
    _validate_contained(session_dir, f"{viz_rel}/session_data.json")
    tmp_data = data_path.with_suffix(".tmp")
    tmp_data.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    tmp_data.replace(data_path)
    log.info("Wrote session_data.json (%d bytes)", data_path.stat().st_size)

    # 2. Build HTML with embedded data.
    html_template = _html_template()
    embedded_json = _json_embed_safe(data)
    html = html_template.replace("__DATA_PLACEHOLDER__", embedded_json)

    html_path = viz_dir / "index.html"
    _validate_contained(session_dir, f"{viz_rel}/index.html")
    tmp_html = html_path.with_suffix(".tmp")
    tmp_html.write_text(html, encoding="utf-8")
    tmp_html.replace(html_path)
    log.info("Wrote index.html (%d bytes)", html_path.stat().st_size)

    # 3. Summary.
    n_trials = len(data["trials"])
    n_finish = len(data["finish_qors"])
    n_paused = sum(1 for t in data["trials"] if t["status"] == "paused")
    n_audit = sum(
        1 for dec in (list(data["pl_cohort"].get("observations", []))
                      + list(data["cts_cohort"].get("observations", [])))
        if dec.get("gwtw_action") == "audit_continue")
    n_forks = (len(data["pl_cohort"].get("children", []))
               + len(data["cts_cohort"].get("children", [])))
    n_timeline = len(data["timeline"])

    log.info("Visualization summary: %d trials, %d finish, %d paused, "
             "%d audit, %d forks, %d timeline events",
             n_trials, n_finish, n_paused, n_audit, n_forks, n_timeline)

    return html_path


# =============================================================================
# CLI
# =============================================================================


def _build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="session_visualize.py",
        description="Generate a self-contained offline HTML visualization "
                    "for an AgenticPD session directory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Example:\n"
               "  python3 -m tools.session_visualize "
               "runs/sky130hd_gcd/<session>")
    p.add_argument(
        "session_dir", type=str,
        help="Path to the session directory containing config_snapshot.json, "
             "trials.jsonl, tree.json, and optionally traces/decisions.jsonl.")
    p.add_argument(
        "--log-level", type=str, default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO).")
    return p


if __name__ == "__main__":
    parser = _build_argparser()
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)s: %(message)s")

    session = Path(args.session_dir)
    try:
        out = generate_visualization(session)
        print(f"\nVisualization generated: {out}")
        print(f"Open file://{out} in a browser to view.")
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        raise
