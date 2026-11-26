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

#: A 3162 x 3162 surface: 31 km at 10 m. Measured THROUGH the two interpolators,
#: not on a library alone, from four points in a 12 kB file: 463 MB at peak for
#: `idw_interpolation` (949 MB at twenty-five million cells) and 377 MB for
#: `tin_interpolation`, in two to fourteen seconds. The first figure written
#: here was whitebox's own (493 MB at twenty-five million), which left out the
#: write and the read-back; the audit that found the difference measured
#: 1.8 GB through the operation as it then was.
DEFAULT_MAX_GRID_CELLS = 10_000_000


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


MAX_TRIANGULATION_POINTS_ENV = "MAPSMITH_MAX_TRIANGULATION_POINTS"

#: Set by the worst arrangement measured, not by the ordinary one. On GEOS 3.13,
#: triangulation alone: 20 000 points scattered at random take a fraction of a
#: second and 250 000 take six — but 20 000 points on one line with the last
#: one lifted off it take 10 s and 30 000 take 39 s, cubic in the points, and
#: 50 000 on one circle take half a minute. Both are arrangements a Delaunay
#: triangulation has no good answer for, and neither needs a large file. An
#: operator whose callers are trusted raises it.
DEFAULT_MAX_TRIANGULATION_POINTS = 20_000

#: How many cell-by-sample evaluations a search radius may ask of an
#: interpolator, as a multiple of `MAPSMITH_MAX_SAMPLES`: 100 million by
#: default, about ten seconds at the measured rate of eleven million a second.
SEARCH_WORK_PER_SAMPLE = 100


def max_triangulation_points() -> int:
    return _whole_number_setting(
        MAX_TRIANGULATION_POINTS_ENV, DEFAULT_MAX_TRIANGULATION_POINTS
    )


MAX_INPUT_FEATURES_ENV = "MAPSMITH_MAX_INPUT_FEATURES"

#: The rows a file may hold however small it is on disk. Measured through
#: `readers.read_vector`, points with two attributes: about 450 MB of memory
#: per million rows, the same from GeoParquet, GeoPackage, shapefile and
#: GeoJSON. What differs is the size on disk that buys them: 28 MB for a
#: million scattered points in a shapefile, 30 MB in GeoParquet — and 20 kB in
#: a GeoParquet whose rows are all alike, because Parquet stores a repeated
#: value once. Fifty million such rows are a 0.77 MB file.
DEFAULT_MAX_INPUT_FEATURES = 1_000_000

#: Bytes on disk that entitle a file to one row beyond the limit, so that a
#: large dataset is not refused for being large. An honest file is bounded by
#: its own size, which is the operator's to bound; at this rate an entitled
#: file costs at most about twice the memory an ordinary one of its size does
#: (28 MB per megabyte on disk, against the 15 measured on scattered points).
INPUT_BYTES_PER_FEATURE = 16


def max_input_features() -> int:
    return _whole_number_setting(MAX_INPUT_FEATURES_ENV, DEFAULT_MAX_INPUT_FEATURES)


def refuse_too_many_features(rows: int, bytes_on_disk: int, path: str) -> None:
    """Refuse to read a file that holds far more rows than its size on disk pays for.

    The rows come from the file's own footer, before any of them is read. A
    file within the limit is read whatever its size; above it, the file must
    occupy `INPUT_BYTES_PER_FEATURE` bytes for each row, which every ordinary
    dataset does and a file of repeated rows does not.
    """
    cap = max(max_input_features(), bytes_on_disk // INPUT_BYTES_PER_FEATURE)
    if rows > cap:
        raise ValueError(
            f"{path} declares {rows:,} rows in {bytes_on_disk:,} bytes: more rows "
            f"than this server reads from a file that small (its limit is {cap:,} "
            f"for this file). Reading costs memory by the row, not by the byte on "
            f"disk, and rows that repeat take almost none there. If the data are "
            f"what they should be, ask the operator to raise "
            f"{MAX_INPUT_FEATURES_ENV}: it is a setting of the server, not an "
            "argument of the tool."
        )


def refuse_too_many_points(count: int, operation: str) -> None:
    """Refuse a triangulation over more locations than the operator allows."""
    cap = max_triangulation_points()
    if count > cap:
        raise ValueError(
            f"{operation} over {count:,} distinct locations is over this server's "
            f"limit of {cap:,}: triangulating grows faster than the points do. Thin "
            f"them with thin_points first, or ask the operator to raise "
            f"{MAX_TRIANGULATION_POINTS_ENV}: it is a setting of the server, not an "
            "argument of the tool."
        )


def refuse_too_many_coordinates(count: int, operation: str) -> None:
    """Refuse a point layer with more coordinates than `MAPSMITH_MAX_SAMPLES`.

    Counted from the geometries' headers, before a coordinate array exists:
    one row holding a MultiPoint of four million parts is a 69 MB file that
    cost 2.8 GB to sort before the limit on distinct locations was consulted.
    """
    cap = max_samples()
    if count > cap:
        raise ValueError(
            f"{operation} was given {count:,} coordinates, over this server's limit "
            f"of {cap:,}. Thin the points first, or ask the operator to raise "
            f"{MAX_SAMPLES_ENV}: it is a setting of the server, not an argument of "
            "the tool."
        )


def refuse_too_much_search(
    cells: int,
    points: int,
    cell_size: float,
    radius: float,
    min_points: int,
    operation: str,
    coincident: int = 1,
) -> None:
    """Refuse a search radius or a neighbour count that turns a grid into cells x points.

    With neither, the library looks near each cell and the cost follows the
    grid. A radius makes every cell weigh every sample inside it, and a minimum
    count makes every cell go and find that many: measured, 250 000 cells and
    1 000 samples take 0.4 s as they are, 22.5 s with a radius wider than the
    layer, 23.4 s with `min_points=1000`. A radius smaller than the layer costs
    in proportion to the cells it covers — the same 0.4 s at a twentieth of the
    extent — so the count below is the samples times the cells one radius
    reaches, whichever way the samples are clustered.

    `coincident` is the largest number of samples at one location. "Near each
    cell" stops meaning few when thousands of rows share a point: measured by
    the audit, 20 000 identical points make a 250 000-cell grid take 11 s with
    no radius and no minimum, and 100 000 did not finish two million cells in
    two minutes. Every cell weighs all of them, so that product is counted too.
    """
    reached = 0.0
    if radius > 0:
        reached = points * min(float(cells), math.pi * (radius / cell_size) ** 2)
    wanted = float(cells) * min(min_points, points) if min_points > 0 else 0.0
    stacked = float(cells) * coincident if coincident > 1 else 0.0
    work = max(reached, wanted, stacked)
    cap = SEARCH_WORK_PER_SAMPLE * max_samples()
    if not math.isfinite(work) or work > cap:
        raise ValueError(
            f"{operation} with radius={radius} and min_points={min_points} over "
            f"{cells:,} cells and {points:,} points"
            + (f", {coincident:,} of them at one location," if coincident > 1 else "")
            + " would weigh about "
            f"{min(work, 1e15):,.0f} cell-sample pairs, over this server's limit of "
            f"{cap:,}. Use a smaller radius, a smaller min_points or a larger cell "
            f"size, or ask the operator to raise {MAX_SAMPLES_ENV}: it is a setting "
            "of the server, not an argument of the tool."
        )


def refuse_too_many_cells(
    width: float, height: float, cell_size: float, operation: str, entitled: int = 0
) -> int:
    """Count the cells a cell size will lay over an extent, and refuse before allocating.

    For operations whose grid comes from an extent and a cell size the caller
    chose: nothing bounds that product except this. A reference raster bounds
    itself, so operations that take one do not come here.

    `entitled` is a count the caller may have whatever the limit says — for an
    operation that re-grids an existing raster, a multiple of the cells that
    raster already has, so that a large input is not refused for being large.

    Returns the count, one more row and column than the extent strictly needs:
    the safe side for a limit, and the grid may round outwards.
    """
    cap = max(max_grid_cells(), entitled)
    # In floating point first: a cell size of 1e-320 makes the quotient
    # infinite, and `math.floor(inf)` raises an OverflowError nobody asked for;
    # one of 1e-300 would print a count a hundred digits long.
    across, down = width / cell_size, height / cell_size
    if not (math.isfinite(across) and math.isfinite(down)) or (across + 1) * (down + 1) > 1e15:
        raise ValueError(
            f"{operation} with a cell size of {cell_size} over an extent of "
            f"{width:g} x {height:g} would produce more than a million billion cells, "
            f"over this server's limit of {cap:,}. Use a larger cell size."
        )
    columns = math.floor(across) + 1
    rows = math.floor(down) + 1
    expected = columns * rows
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
