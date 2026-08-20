"""Where the GABench checkout and our results live.

The harness never assumes a fixed location: set GABENCH_ROOT to your GABench
clone (and optionally AB_RESULTS to a results directory), or keep GABench as a
sibling of this directory and both defaults just work.
"""

from __future__ import annotations

import os
from pathlib import Path

HARNESS_DIR = Path(__file__).resolve().parent


def gabench_root(required: bool = True) -> Path:
    """The GABench checkout. required=False just resolves the path, for code
    that only needs the pure functions (see test_gate.py)."""
    env = os.environ.get("GABENCH_ROOT", "").strip()
    root = Path(env).resolve() if env else HARNESS_DIR.parent / "GABench"
    if required and not (root / "benchmark" / "benchmark.csv").exists():
        raise SystemExit(
            f"GABench not found at {root}. Clone https://github.com/GeoX-Lab/GABench "
            "and point GABENCH_ROOT at it."
        )
    return root


def results_root() -> Path:
    env = os.environ.get("AB_RESULTS", "").strip()
    return Path(env).resolve() if env else HARNESS_DIR / "results"
