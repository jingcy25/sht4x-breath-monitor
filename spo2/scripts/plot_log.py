#!/usr/bin/env python3
"""Render a 1 Hz summary CSV (written by ``oxy monitor`` / ``oxy simulate``) to PNG.

    python3 scripts/plot_log.py data/session.csv data/session.png
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from oxy.plotting import plot_summary  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="plot an oxy summary CSV")
    parser.add_argument("csv", help="summary CSV path")
    parser.add_argument("out", help="output PNG path")
    parser.add_argument("--title", default=None)
    args = parser.parse_args()

    path = plot_summary(args.csv, args.out, title=args.title)
    print("wrote %s" % path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
