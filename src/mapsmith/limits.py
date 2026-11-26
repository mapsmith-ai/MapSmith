"""Limits on how much work one call may ask for, set by the operator and not the agent.

An agent chooses the arguments, and some arguments decide how much memory and
time a call takes before anything is computed: a spacing of 1e-6 along a 90 m
line is ninety million points, materialised as Python objects, for a call that
looks as ordinary as any other. Over stdio that hurts the person running the
server; over HTTP it is a denial of service within reach of whoever can call a
tool. Measured in the 0.7.0 audit: 100 000 profile points take 10.5 s.

The limit is an environment variable for the same reason
`MAPSMITH_ALLOW_EXTENSIONS` is: the operator sets it, and no tool argument can
move it.
"""

from __future__ import annotations

import math
import os
from collections.abc import Iterable

MAX_SAMPLES_ENV = "MAPSMITH_MAX_SAMPLES"

#: About two minutes and a few hundred megabytes of sampling, from the measured
#: rate. Enough for a 1000 km line at 1 m, which is past any profile a person
#: reads; an operator who needs more says so.
DEFAULT_MAX_SAMPLES = 1_000_000


MAX_GRID_CELLS_ENV = "MAPSMITH_MAX_GRID_CELLS"

#: A 5000 x 5000 surface. Measured on whitebox-workflows 2.0.6 from four points
#: in a 12 kB file: 493 MB at peak for either interpolator, 4.8 s for IDW. It is
#: 50 km at 10 m, past what a surface built from scattered points usually
#: covers; an operator who needs more says so.
DEFAULT_MAX_GRID_CELLS = 25_000_000


def _whole_number_setting(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a whole number, got {raw!r}") from exc
    if value < 1:
        raise ValueError(f"{name} must be at least 1, got {value}")
    return value


def max_samples() -> int:
    return _whole_number_setting(MAX_SAMPLES_ENV, DEFAULT_MAX_SAMPLES)


def max_grid_cells() -> int:
    return _whole_number_setting(MAX_GRID_CELLS_ENV, DEFAULT_MAX_GRID_CELLS)


def refuse_too_many_cells(
    width: float, height: float, cell_size: float, operation: str
) -> int:
    """Count the cells a cell size will lay over an extent, and refuse before allocating.

    For operations that INVENT their grid from a vector extent and a cell size the
    caller chose: nothing bounds that product except this. A reference raster
    bounds itself, so operations that take one do not come here.

    Returns the count, one more row and column than the extent strictly needs:
    the safe side for a limit, and the grid may round outwards.
    """
    columns = math.floor(width / cell_size) + 1
    rows = math.floor(height / cell_size) + 1
    expected = columns * rows
    cap = max_grid_cells()
    if expected > cap:
        raise ValueError(
            f"{operation} with a cell size of {cell_size} over an extent of "
            f"{width:g} x {height:g} would produce about {expected:,} cells "
            f"({columns:,} x {rows:,}), over this server's limit of {cap:,}. Use a "
            f"larger cell size, or ask the operator to raise {MAX_GRID_CELLS_ENV}: "
            "it is a setting of the server, not an argument of the tool."
        )
    return expected


def refuse_too_many_samples(lengths: Iterable[float], spacing: float, operation: str) -> int:
    """Count the points a spacing will produce, and refuse before allocating any.

    Returns the count. One per whole step plus the two ends of each line: the
    count the generators produce, or one more, which is the safe side for a
    limit.
    """
    expected = sum(math.floor(length / spacing) + 2 for length in lengths if length > 0)
    cap = max_samples()
    if expected > cap:
        raise ValueError(
            f"{operation} with a spacing of {spacing} would produce about {expected:,} "
            f"points, over this server's limit of {cap:,}. Use a larger spacing, or ask "
            f"the operator to raise {MAX_SAMPLES_ENV}: it is a setting of the server, "
            "not an argument of the tool."
        )
    return expected
