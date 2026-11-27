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


MAX_INPUT_MEMORY_ENV = "MAPSMITH_MAX_INPUT_MEMORY"

#: Bytes of memory one read of a vector input may be estimated to take, however
#: small the file is on disk. Chosen by the operator; 2 GB is about three
#: million points with two attributes, or fewer rows of larger geometries.
DEFAULT_MAX_INPUT_MEMORY = 2_000_000_000

#: What a row costs to hold before anything in it is counted: a geometry
#: object and a place in every column. With the next figure it reproduces what
#: was measured through `readers.read_vector` — points with two attributes,
#: 435 to 520 bytes a row, the same from GeoParquet, GeoPackage, shapefile and
#: GeoJSON, against 500 estimated.
INPUT_MEMORY_PER_ROW = 300

#: What a byte of decoded value costs on top of that where the value becomes
#: an object: a geometry, a string, anything in a list. The bytes are held as
#: an Arrow column, as Python objects and as geometries before the first two
#: are let go. Measured through the reader on 163 MB of WKB, 100 000 polygons
#: of 100 vertices: 840 MB above the idle process when they are all alike and
#: 1.0 to 1.1 GB when they differ; 25 000 parcels of 200 vertices, 80 MB of
#: WKB, 633 MB. That is 5 to 8 times, and the higher is the one a limit has to
#: assume.
INPUT_MEMORY_PER_VALUE_BYTE = 8

#: What a byte of a column of numbers costs: held as an Arrow column and as an
#: array, and copied once on the way. Measured: two hundred float columns are
#: 1.6 kB of numbers a row and 6.7 kB of memory, 8.3 estimated.
INPUT_MEMORY_PER_NUMBER_BYTE = 5

#: Memory a file is entitled to for each byte it occupies on disk, so that a
#: large dataset is not refused for being large: a million scattered points
#: with one attribute are 30 MB of GeoParquet and an estimated 540 MB to read,
#: a factor of 18.
INPUT_MEMORY_PER_BYTE_ON_DISK = 24


def max_input_memory() -> int:
    return _whole_number_setting(MAX_INPUT_MEMORY_ENV, DEFAULT_MAX_INPUT_MEMORY)


def estimated_read_memory(rows: int, value_bytes: int, number_bytes: int = 0) -> int:
    """Bytes a read is expected to peak at: a cost per row and one per decoded byte.

    `value_bytes` are those of values that become objects, `number_bytes`
    those of columns of numbers, which cost less each.
    """
    return (
        rows * INPUT_MEMORY_PER_ROW
        + value_bytes * INPUT_MEMORY_PER_VALUE_BYTE
        + number_bytes * INPUT_MEMORY_PER_NUMBER_BYTE
    )


def input_memory_allowance(bytes_on_disk: int) -> int:
    """The operator's budget, or what the file's own size on disk entitles it to."""
    return max(max_input_memory(), bytes_on_disk * INPUT_MEMORY_PER_BYTE_ON_DISK)


def refuse_too_costly_a_read(
    rows: int,
    value_bytes: int,
    bytes_on_disk: int,
    path: str,
    *,
    number_bytes: int = 0,
    complete: bool = True,
) -> None:
    """Refuse a dataset whose rows and values would take more memory than it may.

    Reading costs memory by the row and by the decoded byte, and Parquet stores
    a repeated value once: neither the size of the file nor its footer shows
    what opening it takes. `value_bytes` is what its columns decode to;
    `complete` is False when the count stopped early because the estimate had
    already passed the allowance.
    """
    allowance = input_memory_allowance(bytes_on_disk)
    estimate = estimated_read_memory(rows, value_bytes, number_bytes)
    if estimate > allowance:
        counted = value_bytes + number_bytes
        values = (
            f" and {'' if complete else 'at least '}{counted:,} bytes of values"
            if counted
            else ""
        )
        megabytes = math.ceil(estimate / 1e6)
        raise ValueError(
            f"{path} holds {rows:,} rows{values} in {bytes_on_disk:,} bytes on disk. "
            f"Reading it is estimated to take {megabytes:,} MB"
            + ("" if complete else " or more")
            + f", over this server's limit of {math.floor(allowance / 1e6):,} MB for "
            "this dataset. The estimate counts rows and what their values decode to, "
            "not the size of the file: values that repeat take almost nothing on "
            f"disk. Reduce the dataset, or ask the operator to raise "
            f"{MAX_INPUT_MEMORY_ENV}: it is a setting of the server, not an argument "
            "of the tool."
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
