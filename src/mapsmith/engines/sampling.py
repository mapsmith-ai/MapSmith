"""Reading a surface where somebody stands, walks, or looks.

Three operations that all come down to the same question — what does the raster
say at this place — and differ in what "this place" means: a set of points, a
line walked at a fixed step, or the sight line between two positions.

They exist because the discovery benchmark asked for them in the words of people
who had the problem: *"my total station elevations along the centerline are
consistently two tenths higher than the city's surface"*, *"elevation at every 20
metres along the centreline so I can plot the profile"*, *"line of sight check,
this site to that site, is the ridge blocking it"*. Both independent labellers
marked all three `none`: the catalogue could not serve them.

The recurring hazard here is the silent null. A point outside the raster, or on a
nodata cell, samples to nothing — and a table with nulls in it looks exactly like
a table without them until somebody averages it. So every operation in this
module counts what it could not read and puts that count in the manifest as a
check, not a footnote.
"""

from __future__ import annotations

import math
from itertools import pairwise
from typing import Any

import geopandas as gpd

from .. import readers, verify
from ..provenance import InputRecord, ProvenanceRecord

#: How a value is read between cell centres.
#:
#: `nearest` is the value of the cell the position falls in — right for class
#: codes, land cover, anything where an average of 3 and 5 is not a 4.
#: `bilinear` interpolates the four surrounding cell centres, which is right for
#: a continuous surface and is what a survey comparison needs: a total station
#: shot does not land on a cell centre, and snapping it to one introduces up to
#: half a cell of horizontal error before the vertical difference is computed.
SAMPLING_METHODS = ("nearest", "bilinear")

#: Mean Earth radius, metres (IUGG). Used only for the curvature drop in
#: `line_of_sight`, where the alternative is silently pretending the planet is
#: flat.
EARTH_RADIUS_M = 6_371_008.8

#: Standard atmospheric refraction coefficient. Light bends downward, so a
#: target is visible slightly further than geometry alone would allow; 0.13 is
#: the value the surveying literature uses for ordinary daytime conditions and
#: the one every GIS viewshed implementation defaults to.
REFRACTION_COEFFICIENT = 0.13


def _require_rasterio():
    try:
        import rasterio
    except ImportError as exc:  # pragma: no cover - exercised by the extra guard
        raise ImportError(
            "This operation needs rasterio: pip install mapsmith[raster]"
        ) from exc
    return rasterio


def _engine_info() -> dict[str, str]:
    rasterio = _require_rasterio()
    return {"name": "rasterio", "version": rasterio.__version__}


def _read_at(dataset: Any, band: int, xs, ys, method: str) -> list[float | None]:
    """Values at map coordinates, or None where there is nothing to read.

    Written here rather than taken from `dataset.sample` because that helper
    returns the nodata VALUE rather than a null, and a nodata value of -9999
    averaged into a profile is the silent error this module exists to avoid.
    """
    import numpy as np

    from .. import grid

    array = dataset.read(band, masked=True)
    # `array.mask` is the scalar `np.ma.nomask` when nothing is masked, and
    # indexing a scalar raises — so a raster with no nodata cells at all would
    # crash the reader that exists to handle nodata. `getmaskarray` always
    # returns a full boolean array.
    mask = np.ma.getmaskarray(array)
    inverse = ~dataset.transform
    # Where the values sit inside their cells. 0.5 on an ordinary file, 0.0 on
    # one that declares its values are samples at grid nodes — and asking here
    # rather than assuming is the difference between a profile along a USGS DEM
    # and the same profile fifteen metres to the north-west.
    shift = grid.offset(dataset)
    height, width = array.shape
    out: list[float | None] = []

    for x, y in zip(xs, ys, strict=True):
        column, row = inverse * (x, y)
        if method == "nearest":
            # Which sample is nearest, which is `floor` when samples are cell
            # centres and `round` when they are nodes.
            r, c = grid.sample_index(dataset, x, y)
            if not (0 <= r < height and 0 <= c < width) or mask[r, c]:
                out.append(None)
                continue
            out.append(float(array[r, c]))
            continue

        # Bilinear over the four surrounding SAMPLES. On an ordinary file
        # those are the cell centres, at (column - 0.5, row - 0.5) in array
        # space; on a point-registered one they are the nodes, at (column, row).
        # The offset is the part that is easy to drop, and dropping it shifts
        # every value by half a pixel — which is what `grid` is for.
        #
        # Two different "outside" here, and conflating them was a bug. A
        # position outside the raster's EXTENT has no value and returns None.
        # A position inside the extent but within the outer half-cell has a
        # value and an incomplete stencil: its outer neighbours do not exist.
        # Refusing those would make the whole boundary ring of every raster
        # unreadable — a sight line to a target near the edge came back "outside
        # the raster" — so the stencil is clamped to the edge cell, which is what
        # GDAL does and what "the surface continues to the edge of the data"
        # means. Nodata inside the stencil still yields None: that is missing
        # data, not a boundary.
        if not (0 <= column < width and 0 <= row < height):
            out.append(None)
            continue
        cx, cy = column - shift, row - shift
        c0, r0 = math.floor(cx), math.floor(cy)
        fx, fy = cx - c0, cy - r0
        corners = []
        for dr, dc, weight in (
            (0, 0, (1 - fx) * (1 - fy)),
            (0, 1, fx * (1 - fy)),
            (1, 0, (1 - fx) * fy),
            (1, 1, fx * fy),
        ):
            if weight == 0.0:
                # A corner with no weight contributes nothing, so whether it is
                # nodata is not this sample's problem. Checking the mask first
                # threw away exact values: a point sitting on a cell centre has
                # three corners at weight zero, and one of them being nodata
                # returned None for a position whose value was right there — the
                # opposite of the loss this module exists to prevent, and it
                # inflated the very counter the check reads.
                continue
            r = min(max(r0 + dr, 0), height - 1)
            c = min(max(c0 + dc, 0), width - 1)
            if mask[r, c]:
                corners = []
                break
            corners.append(weight * float(array[r, c]))
        out.append(float(np.sum(corners)) if corners else None)

    return out


def _unreadable_check(values: list[float | None], total: int) -> verify.Check:
    """A null is data, and it has to be counted somewhere a reader will look.

    Not critical: sampling outside the raster is a legitimate thing to do on
    purpose, and refusing it would make the operation useless for exactly the
    case it is best at — comparing a survey against a surface that does not
    cover all of it. But a table with silent nulls averages to a number nobody
    can defend, so the count travels with the result.
    """
    missing = sum(1 for v in values if v is None)
    return verify.Check(
        "x-mapsmith:every_position_had_a_value",
        missing == 0,
        f"{missing} of {total} positions fell outside the raster or on nodata",
        critical=False,
    )


def sample_raster_at_points(
    raster_path: str,
    points_path: str,
    output_path: str,
    method: str,
    band: int = 1,
    column_name: str = "value",
) -> dict[str, Any]:
    """The raster's value at each point, with the ones it could not read counted.

    `method` has no default because the two are right for different data and the
    wrong one fails quietly: `bilinear` on land-cover codes invents classes that
    do not exist, and `nearest` on a survey comparison snaps each shot to a cell
    centre, adding up to half a cell of horizontal error to a vertical
    difference somebody is about to call a datum offset.

    Points that fall outside the raster, or on a nodata cell, come back with a
    null in `column_name` rather than the nodata value — and the count of them
    is a check in the manifest.
    """
    rasterio = _require_rasterio()
    # Two georeferencings and nobody chose: refuse rather than compute
    # from a file the caller did not name (D-059). After the extra guard,
    # never before it: a caller without `[raster]` has to hear about the
    # missing extra, not about a sidecar.
    from .. import grid

    grid.refuse_ambiguous_georeferencing(raster_path, "sample_raster_at_points")
    if method not in SAMPLING_METHODS:
        raise ValueError(
            f"method must be one of {list(SAMPLING_METHODS)}, got {method!r}. "
            "'nearest' for class codes, 'bilinear' for a continuous surface."
        )

    points = readers.read_vector(points_path)
    if points.crs is None:
        raise ValueError(
            readers.no_crs_message(
                points, f"{points_path} has no CRS, so its points cannot be placed "
                "on the raster."
            )
        )
    kinds = set(points.geom_type.dropna().unique())
    if not kinds <= {"Point", "MultiPoint"}:
        raise ValueError(
            f"sample_raster_at_points needs a point layer; {points_path} holds "
            f"{sorted(kinds)}. For polygons use zonal_statistics, which weights "
            "partial pixels instead of reading one."
        )
    if column_name in points.columns:
        raise ValueError(
            f"the layer already has a column called {column_name!r}; pass a "
            "different column_name rather than overwriting it silently."
        )

    with rasterio.open(raster_path) as dataset:
        if band < 1 or band > dataset.count:
            raise ValueError(
                f"band {band} does not exist: {raster_path} has {dataset.count}. "
                "Bands are 1-based."
            )
        raster_crs = dataset.crs
        if raster_crs is None:
            raise ValueError(
                f"{raster_path} declares no CRS, so points cannot be placed on it. "
                "Assign one first."
            )
        record = ProvenanceRecord(
            operation="sample_raster_at_points",
            parameters={"method": method, "band": band, "column_name": column_name},
            inputs=[
                InputRecord.from_path(raster_path, crs=verify.crs_label(raster_crs)),
                InputRecord.from_path(points_path, crs=verify.crs_label(points.crs)),
            ],
            engine=_engine_info(),
        )
        if not verify.same_crs(points.crs, raster_crs):
            points = points.to_crs(raster_crs)
            record.crs_decisions = {
                "analysis_crs": verify.crs_label(raster_crs),
                "reason": "points reprojected to the raster's CRS so each one lands "
                "on the cell it actually falls in; the output keeps that CRS",
            }
        else:
            record.crs_decisions = {
                "analysis_crs": verify.crs_label(raster_crs),
                "reason": "points and raster share the same CRS",
            }
        centroids = points.geometry.representative_point()
        values = _read_at(dataset, band, centroids.x, centroids.y, method)

    out = points.copy()
    out[column_name] = values
    _write_vector(out, output_path)

    read = sum(1 for v in values if v is not None)
    manifest, extras = verify.audited(
        record,
        output_path,
        operation="sample_raster_at_points",
        preconditions=verify.verify_loaded_inputs(
            "sample_raster_at_points", points_path=points
        ),
        checks_fn=lambda: [
            *verify.verify_vector_output(
                output_path,
                expect_crs=verify.crs_label(points.crs),
                expect_count=len(points),
            ),
            _unreadable_check(values, len(points)),
        ],
    )
    return {
        "output": str(output_path),
        "points": len(points),
        "sampled": read,
        "unreadable": len(points) - read,
        "column": column_name,
        "method": method,
        "provenance": str(manifest),
        **extras,
    }


def _write_vector(gdf: gpd.GeoDataFrame, output_path: str) -> None:
    if str(output_path).endswith(".parquet"):
        gdf.to_parquet(output_path)
    else:
        gdf.to_file(output_path)


def elevation_profile(
    raster_path: str,
    line_path: str,
    output_path: str,
    spacing: float,
    method: str = "bilinear",
    band: int = 1,
) -> dict[str, Any]:
    """One point every `spacing` along each line, carrying the surface value.

    `spacing` is a length in the raster's own linear unit, so a geographic CRS is
    refused: 20 of a degree is not 20 metres, and the profile would come back
    with a distance axis that means nothing — plausibly, at a plausible-looking
    scale.

    Output is a point layer with `distance` (along the line, from its start),
    `value`, and `point_index`, ordered. Lines are handled one at a time and
    `line_index` says which one a point came from, so a network of centrelines
    profiles in a single call without the segments running together.
    """
    rasterio = _require_rasterio()
    # Two georeferencings and nobody chose: refuse rather than compute
    # from a file the caller did not name (D-059). After the extra guard,
    # never before it: a caller without `[raster]` has to hear about the
    # missing extra, not about a sidecar.
    from .. import grid

    grid.refuse_ambiguous_georeferencing(raster_path, "elevation_profile")
    if spacing <= 0:
        raise ValueError(f"spacing must be positive, got {spacing}")
    if method not in SAMPLING_METHODS:
        raise ValueError(f"method must be one of {list(SAMPLING_METHODS)}, got {method!r}")

    lines = readers.read_vector(line_path)
    if lines.crs is None:
        raise ValueError(
            readers.no_crs_message(
                lines, f"{line_path} has no CRS, so a spacing in its units means nothing."
            )
        )
    kinds = set(lines.geom_type.dropna().unique())
    if not kinds <= {"LineString", "MultiLineString"}:
        raise ValueError(
            f"elevation_profile needs a line layer; {line_path} holds {sorted(kinds)}."
        )
    if lines.crs.is_geographic:
        raise ValueError(
            f"{line_path} is in a geographic CRS, so a spacing of {spacing} would be "
            f"{spacing} degrees — about {spacing * 111_000:,.0f} m of latitude, and a "
            "different distance at every longitude. Reproject to a projected CRS "
            "first, or the distance axis of the profile is meaningless."
        )

    with rasterio.open(raster_path) as dataset:
        raster_crs = dataset.crs
        if raster_crs is None:
            raise ValueError(f"{raster_path} declares no CRS.")
        if band < 1 or band > dataset.count:
            raise ValueError(
                f"band {band} does not exist: {raster_path} has {dataset.count}."
            )
        record = ProvenanceRecord(
            operation="elevation_profile",
            parameters={"spacing": spacing, "method": method, "band": band},
            inputs=[
                InputRecord.from_path(raster_path, crs=verify.crs_label(raster_crs)),
                InputRecord.from_path(line_path, crs=verify.crs_label(lines.crs)),
            ],
            engine=_engine_info(),
        )
        working = lines
        if not verify.same_crs(lines.crs, raster_crs):
            working = lines.to_crs(raster_crs)
            record.crs_decisions = {
                "analysis_crs": verify.crs_label(raster_crs),
                "reason": "lines reprojected to the raster's CRS before sampling, so "
                "the spacing is measured in the unit the values are read in",
            }
        else:
            record.crs_decisions = {
                "analysis_crs": verify.crs_label(raster_crs),
                "reason": "line and raster share the same CRS",
            }

        rows = _profile_positions(working, spacing)
        values = _read_at(
            dataset, band, [r["x"] for r in rows], [r["y"] for r in rows], method
        )

    from shapely.geometry import Point

    out = gpd.GeoDataFrame(
        {
            "line_index": [r["line_index"] for r in rows],
            "point_index": [r["point_index"] for r in rows],
            "distance": [r["distance"] for r in rows],
            "value": values,
        },
        geometry=[Point(r["x"], r["y"]) for r in rows],
        crs=working.crs,
    )
    _write_vector(out, output_path)

    manifest, extras = verify.audited(
        record,
        output_path,
        operation="elevation_profile",
        preconditions=verify.verify_loaded_inputs("elevation_profile", line_path=working),
        checks_fn=lambda: [
            *verify.verify_vector_output(
                output_path,
                expect_crs=verify.crs_label(working.crs),
                expect_count=len(rows),
            ),
            # Derived from the OUTPUT, not from the formula that produced it.
            # The first version compared `len(rows)` against
            # `floor(L/S) + 1` — the same expression the generator uses, written
            # twice — so it could not fail. What is checked now is a property of
            # the points on disk: each line starts at zero, the step between
            # consecutive points is the spacing, and the last one is the last
            # whole step that fits.
            verify.Check(
                "x-mapsmith:each_profile_starts_at_zero_and_steps_by_the_spacing",
                _stepping_is_regular(rows, spacing),
                _stepping_detail(rows, spacing),
            ),
            _unreadable_check(values, len(rows)),
        ],
    )
    lengths = working.geometry.length
    return {
        "output": str(output_path),
        "lines": len(working),
        "points": len(rows),
        "spacing": spacing,
        "total_length": float(lengths.sum()),
        "sampled": sum(1 for v in values if v is not None),
        "provenance": str(manifest),
        **extras,
    }


def _usable(lines: gpd.GeoDataFrame) -> list[Any]:
    return [g for g in lines.geometry if g is not None and not g.is_empty]


def _stepping_is_regular(rows: list[dict[str, Any]], spacing: float) -> bool:
    """Every line starts at 0 and advances by exactly `spacing`."""
    by_line: dict[int, list[float]] = {}
    for row in rows:
        by_line.setdefault(row["line_index"], []).append(row["distance"])
    for distances in by_line.values():
        if distances[0] != 0.0:
            return False
        for previous, current in pairwise(distances):
            if not math.isclose(current - previous, spacing, rel_tol=1e-9):
                return False
    return True


def _stepping_detail(rows: list[dict[str, Any]], spacing: float) -> str:
    lines = len({row["line_index"] for row in rows})
    return f"{len(rows)} point(s) over {lines} line(s) at a step of {spacing}"


def _profile_positions(lines: gpd.GeoDataFrame, spacing: float) -> list[dict[str, Any]]:
    """Positions along each line at a fixed step, both ends included."""
    rows: list[dict[str, Any]] = []
    for line_index, geometry in enumerate(lines.geometry):
        # `network._build` guards these and this did not: a null geometry died on
        # `'NoneType' object has no attribute 'length'`, and an empty LineString
        # was counted as one point and then died in `interpolate`. Skipped rather
        # than refused, because a layer with a few empty rows is ordinary — and
        # counted, because a profile missing a line should not look complete.
        if geometry is None or geometry.is_empty:
            continue
        length = geometry.length
        steps = math.floor(length / spacing)
        for point_index in range(steps + 1):
            distance = min(point_index * spacing, length)
            position = geometry.interpolate(distance)
            rows.append(
                {
                    "line_index": line_index,
                    "point_index": point_index,
                    "distance": float(distance),
                    "x": position.x,
                    "y": position.y,
                }
            )
    return rows


def line_of_sight(
    raster_path: str,
    observer_x: float,
    observer_y: float,
    target_x: float,
    target_y: float,
    earth_curvature: bool,
    observer_height: float = 0.0,
    target_height: float = 0.0,
    samples: int | None = None,
    band: int = 1,
) -> dict[str, Any]:
    """Whether the terrain blocks the view, and where it first does.

    `earth_curvature` has no default, and that is deliberate. Over 5 km the
    planet drops about 1.7 m below the tangent plane and over 30 km about 62 m
    net of refraction, so a flat-Earth answer is right for a rooftop survey and
    badly wrong for a radio link — and there is no way to guess which one the
    caller has. Say it, and the manifest records what was assumed. When true,
    the standard refraction coefficient of 0.13 is applied with it, because
    curvature without refraction over-corrects.

    Answers rather than writes: the result is a verdict, the distance at which
    the ground first rises above the sight line, and how far below the terrain
    the line passes there. Use `elevation_profile` for the shape of the ground.

    **A profile with holes in it does not produce a verdict.** Samples that fall
    on nodata are counted in `unreadable_samples`, and above a twentieth of the
    line missing `visible` comes back `None` with `verdict_withheld` set. The
    first version skipped unreadable samples and said nothing: a 100 m ridge
    buried in a nodata gap returned `visible: True`. "Cannot say" is an answer a
    caller can act on; "yes" computed over the parts that happened to be there
    is not.
    """
    rasterio = _require_rasterio()
    # Two georeferencings and nobody chose: refuse rather than compute
    # from a file the caller did not name (D-059). After the extra guard,
    # never before it: a caller without `[raster]` has to hear about the
    # missing extra, not about a sidecar.
    from .. import grid

    grid.refuse_ambiguous_georeferencing(raster_path, "line_of_sight")
    with rasterio.open(raster_path) as dataset:
        crs = dataset.crs
        if crs is None:
            raise ValueError(f"{raster_path} declares no CRS.")
        if crs.is_geographic:
            raise ValueError(
                f"{raster_path} is in a geographic CRS, so the distance between the "
                "two positions would be in degrees and the sight line would compare "
                "a height in metres against a run in degrees. Reproject first."
            )
        if band < 1 or band > dataset.count:
            raise ValueError(
                f"band {band} does not exist: {raster_path} has {dataset.count}."
            )

        run = math.hypot(target_x - observer_x, target_y - observer_y)
        if run == 0:
            raise ValueError(
                "the observer and the target are at the same position, so there is "
                "no sight line to check."
            )
        # One sample per cell along the line by default: sampling coarser than
        # the data can step over the ridge that blocks the view, and a viewshed
        # that misses a ridge is the confident wrong answer this whole product
        # is about.
        cell = min(abs(dataset.transform.a), abs(dataset.transform.e))
        steps = samples if samples is not None else max(2, math.ceil(run / cell) + 1)
        if steps < 2:
            raise ValueError(f"samples must be at least 2, got {samples}")

        xs = [observer_x + (target_x - observer_x) * i / (steps - 1) for i in range(steps)]
        ys = [observer_y + (target_y - observer_y) * i / (steps - 1) for i in range(steps)]
        ground = _read_at(dataset, band, xs, ys, "bilinear")

    if ground[0] is None or ground[-1] is None:
        raise ValueError(
            "the observer or the target is outside the raster or on nodata, so "
            "there is no ground elevation to stand on."
        )

    observer_z = ground[0] + observer_height
    target_z = ground[-1] + target_height
    blocked_at: float | None = None
    clearance = math.inf
    unreadable = sum(1 for value in ground if value is None)
    for index in range(1, steps - 1):
        if ground[index] is None:
            continue
        distance = run * index / (steps - 1)
        drop = _curvature_drop(distance, run) if earth_curvature else 0.0
        sight_z = observer_z + (target_z - observer_z) * index / (steps - 1) - drop
        gap = sight_z - ground[index]
        clearance = min(clearance, gap)
        if gap < 0 and blocked_at is None:
            blocked_at = distance

    # A verdict computed over a profile with holes in it is not a verdict. This
    # module's docstring promises that every operation counts what it could not
    # read; this one is the only one that answers yes-or-no instead of returning
    # a table, so a silent null costs the most here — a 100 m ridge buried in a
    # nodata gap came back `visible: True` with nothing to show for it. Above a
    # twentieth of the line missing, the answer is None: "cannot say" is an
    # answer a caller can act on and "yes" is not.
    too_holey = unreadable > max(1, steps // 20)
    return {
        "visible": None if too_holey else blocked_at is None,
        "unreadable_samples": unreadable,
        "samples_read": steps - unreadable,
        "verdict_withheld": too_holey,
        "distance": run,
        "first_obstruction_at": blocked_at,
        "minimum_clearance": None if clearance is math.inf else clearance,
        "observer_elevation": observer_z,
        "target_elevation": target_z,
        "earth_curvature": earth_curvature,
        "refraction_coefficient": REFRACTION_COEFFICIENT if earth_curvature else None,
        "samples": steps,
        "crs": verify.crs_label(crs),
    }


def _curvature_drop(distance: float, total: float) -> float:
    """How far the Earth falls away under a chord, net of refraction.

    `d * (total - d) / (2R)` is the sagitta of the arc at distance `d` along a
    chord of length `total` — zero at both ends, greatest in the middle, which
    is what curvature actually does to a sight line. The refraction coefficient
    reduces it, because the atmosphere bends light back down.
    """
    return (
        (1 - REFRACTION_COEFFICIENT) * distance * (total - distance) / (2 * EARTH_RADIUS_M)
    )
