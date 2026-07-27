#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
clean.py — Clean AgenticPD run artifacts for a given design.

Deletes all AgenticPD artifacts under flow/ for the specified <platform> <design>:
- results/<platform>/<design>/  (all variants except "base")
- logs/<platform>/<design>/     (all variants except "base")
- reports/<platform>/<design>/  (all variants except "base")
- objects/<platform>/<design>/  (all variants except "base")
- agenticpd/runs/ directories matching the design

The "base" baseline variant is strictly protected and never deleted.

Usage:
    python3 agenticpd/clean.py <platform> <design>
    python3 agenticpd/clean.py --target <platform> <design>
    python3 agenticpd/clean.py --target <platform> <design> --dry-run
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import List, Tuple

# When running from tools/ subdirectory, add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config import FLOW_DIR, RUNS_DIR, ORFS_CATEGORIES

# Protected variant — NEVER deleted
PROTECTED_VARIANT = "base"


# ---------------------------------------------------------------------------
# Path construction (consistent with config.py)
# ---------------------------------------------------------------------------

def _variant_dirs(platform: str, design: str, category: str) -> Path:
    """Return flow/<category>/<platform>/<design>/."""
    return FLOW_DIR / category / platform / design


def _find_matching_runs(platform: str, design: str) -> List[Path]:
    """Scan agenticpd/runs/ for directories matching (platform, design).

    Matches against config_snapshot.json in each run dir.
    Skips directories without a snapshot (empty or legacy format).
    """
    if not RUNS_DIR.is_dir():
        return []
    matched: List[Path] = []
    for run_dir in sorted(RUNS_DIR.iterdir()):
        if not run_dir.is_dir():
            continue
        snapshot = run_dir / "config_snapshot.json"
        if not snapshot.is_file():
            continue
        try:
            cfg = json.loads(snapshot.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if cfg.get("platform") == platform and cfg.get("design") == design:
            matched.append(run_dir)
    return matched


# ---------------------------------------------------------------------------
# Scan: collect directories to be deleted
# ---------------------------------------------------------------------------

def collect_targets(platform: str, design: str) -> List[Tuple[Path, str]]:
    """Collect all directories to clean, returning [(path, description), ...].

    Descriptions are used for dry-run display and final confirmation.
    The "base" variant is always excluded.
    """
    targets: List[Tuple[Path, str]] = []

    for cat in ORFS_CATEGORIES:
        variant_root = _variant_dirs(platform, design, cat)
        if not variant_root.is_dir():
            continue
        for variant_dir in sorted(variant_root.iterdir()):
            if not variant_dir.is_dir():
                continue
            if variant_dir.name == PROTECTED_VARIANT:
                continue  # base is protected
            targets.append((variant_dir, f"{cat}/{platform}/{design}/{variant_dir.name}"))

    # agenticpd runs
    for run_dir in _find_matching_runs(platform, design):
        targets.append((run_dir, f"agenticpd/runs/{run_dir.name}"))

    return targets


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Clean AgenticPD run artifacts for a design (base variant protected)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  python3 agenticpd/clean.py nangate45 gcd\n"
               "  python3 agenticpd/clean.py --target sky130hd ibex --dry-run",
    )
    parser.add_argument(
        "platform", nargs="?", default=None,
        help="Target platform (e.g. nangate45, sky130hd)",
    )
    parser.add_argument(
        "design", nargs="?", default=None,
        help="Target design name (e.g. gcd, ibex)",
    )
    parser.add_argument(
        "--target", nargs=2, metavar=("PLATFORM", "DESIGN"), default=None,
        help="Target platform and design (alternative to positional args)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List directories to be deleted without actually removing them",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Skip confirmation prompt (use with caution)",
    )

    args = parser.parse_args()

    # Resolve platform / design
    if args.target is not None:
        platform, design = args.target
    elif args.platform is not None and args.design is not None:
        platform, design = args.platform, args.design
    else:
        parser.print_help()
        sys.exit(1)

    # Validation: no wildcards
    for val, name in [(platform, "platform"), (design, "design")]:
        if not val or any(c in val for c in "*?[]"):
            print(f"Error: invalid {name} '{val}' (wildcards not allowed)")
            sys.exit(1)

    # Collect
    targets = collect_targets(platform, design)

    if not targets:
        print(f"No artifacts found for {platform}/{design} (already clean).")
        return

    # List
    total_items = len(targets)
    # Estimate total size
    total_size = 0
    for path, _desc in targets:
        try:
            total_size += sum(
                f.stat().st_size for f in path.rglob("*") if f.is_file()
            )
        except OSError:
            pass

    print(f"Will clean {total_items} directories under {platform}/{design}"
          f" (~{total_size / 1024 / 1024:.1f} MB):")
    print("-" * 60)
    for path, desc in targets:
        try:
            file_count = sum(1 for _ in path.rglob("*") if _.is_file())
            dir_count = sum(1 for _ in path.rglob("*") if _.is_dir())
        except OSError:
            file_count = dir_count = 0
        size_mb = 0.0
        try:
            size_mb = sum(
                f.stat().st_size for f in path.rglob("*") if f.is_file()
            ) / 1024 / 1024
        except OSError:
            pass
        print(f"  {desc}  ({file_count} files, {dir_count} dirs, {size_mb:.1f} MB)")
    print("-" * 60)
    print(f"  base directory will NOT be affected.")

    if args.dry_run:
        print("\n[dry-run] No directories were actually deleted.")
        return

    # Confirm
    if not args.yes:
        try:
            resp = input(f"\nDelete {total_items} directories? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")
            sys.exit(0)
        if resp not in ("y", "yes"):
            print("Cancelled.")
            sys.exit(0)

    # Execute
    failed: List[str] = []
    for path, desc in targets:
        try:
            shutil.rmtree(path)
            print(f"  [OK] deleted {desc}")
        except OSError as e:
            failed.append(f"  [FAIL] {desc}: {e}")

    if failed:
        print(f"\n{len(failed)} directories failed to delete:")
        for line in failed:
            print(line)
        sys.exit(1)
    else:
        print(f"\nCleanup complete: {total_items} directories deleted.")


if __name__ == "__main__":
    main()
