#!/usr/bin/env python3
"""Standalone hardware diagnostic.

    python3 scripts/diagnose_i2c.py [--config config.yaml]

Equivalent to ``python3 -m oxy.cli diag`` but works without setting PYTHONPATH.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from oxy.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["diag"] + sys.argv[1:]))
