# -*- coding: utf-8 -*-
"""session_visualize/__main__.py — CLI entry (python3 -m tools.session_visualize)."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from tools.session_visualize.cli import _build_argparser, generate_visualization


def main() -> None:
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


if __name__ == "__main__":
    main()
