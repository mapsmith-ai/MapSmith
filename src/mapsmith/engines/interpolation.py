"""Surfaces from scattered points: what both interpolators check, and the triangulated one.

`tin_interpolation` lives here and runs on GEOS and GDAL — a Delaunay
triangulation from shapely, the triangles burned onto the grid by rasterio, one
plane per triangle — and not on whitebox, where `idw_interpolation` is. It was
written on whitebox first, and three things measured on whitebox-workflows
2.0.6 while reviewing it are the reason it is not there now:

* its triangulation takes 11 s on 6 000 points lying on one circle, where it
  takes 0.6 s on 20 000 scattered ones, and did not finish 10 000 in 40 s. A
  few kilobytes of input, minutes of work, and no count that tells that file
  from a survey. GEOS triangulates the same 6 000 in 0.09 s;
* a field it cannot use — missing, text, or null in one row — is replaced by
  the feature's position in the file, without a word (`sample_values` below);
* it writes -32768 where a cell has no value, so a sample of -32768 could not
  be told from an empty cell.

The second is still true of `idw_interpolation`, which is why the check is
shared and lives beside the operation that no longer needs it for that reason.
Optional extra: ``pip install mapsmith[raster]``.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd
import shapely

from .. import grid, limits, readers, verify
from ..provenance import InputRecord, ProvenanceRecord

#: How many of a layer's field names a refusal quotes. They are the file's, not
#: ours: a layer with 1 500 columns made one message 104 000 characters long.
_FIELDS_QUOTED = 20

#: The same tolerance `resample_raster` uses, for the same reason: an extent
#: that divides evenly must not gain a column to floating-point noise.
_GRID_EPSILON = 1e-9

#: Rows evaluated at a time when the planes are read off: bounds the
#: temporaries to a few megabytes whatever the grid.
_BLOCK_ROWS = 512


def _require():
    try:
        import rasterio
        from rasterio import features
    except ImportError as exc:
        raise ImportError(
            "tin_interpolation requires the raster extra: pip install mapsmith[raster]"
        ) from exc
    return rasterio, features


def _engine_info(rasterio: Any) -> dict[str, str]:
    return {
        "name": "shapely+rasterio",
        "version": (
            f"shapely {shapely.__version__} (GEOS {shapely.geos_version_string}), "
            f"rasterio {rasterio.__version__} (GDAL {rasterio.__gdal_version__})"
        ),
    }


def sample_values(
    layer: Any, field_name: str, points_path: str, cell_size: float, operation: str
) -> tuple[np.ndarray, int]:
    """The values an interpolator will read, as floats, or a refusal that says why.

    Returns them with the number of cells the grid will have, by the limit's
    own count.

    Every refusal here is a case where whitebox answers instead of failing, and
    answers with ROW NUMBERS. Measured on whitebox-workflows 2.0.6:

        a field name that is not in the layer   -> the surface of FID
        a text field, even one holding digits   -> the surface of FID
        a null or NaN in one row                -> that row's FID in its place

    The third is the quiet one: three gauges reading 800, 1000 and 1100 mm and a
    fourth with no reading produce a surface that dips to 1 at the fourth gauge,
    because 1 is where it sits in the file. Nothing raises and the raster
    renders. Until 0.12.2 `idw_interpolation` checked only that the name was not
    blank, which is the one mistake of the four that cannot be made by accident.

    The geometry is checked here too, for the same reason and with the same
    measurement: a polygon or a row without a geometry in a point layer is
    DROPPED by that library, value and all, and the surface is built from the
    rest (three rows holding 1, 2 and 3 and a polygon holding 900: a surface
    spanning 1 to 3, verified). A value belongs to a location, and which
    location a polygon stands for is the caller's to say.

    `tin_interpolation` has no library to fall back on a row number, and is
    refused the same inputs because none of them is a measurement at a place.
    """
    geometry = layer.geometry
    if len(layer) and (geometry.isna().any() or geometry.is_empty.any()):
        raise ValueError(
            f"{points_path} has rows with no point geometry; {operation} refuses "
            "rather than leave their values out. Drop them with select_features first."
        )
    other = sorted(set(geometry.geom_type) - {"Point", "MultiPoint"})
    if other:
        raise ValueError(
            f"{operation} reads point layers, and {points_path} holds {other}. "
            "A value belongs to a location: take centroid_layer or point_on_surface "
            "first, and say which."
        )
    if len(layer) and not np.isfinite(shapely.get_coordinates(geometry.values)).all():
        raise ValueError(
            f"{points_path} has points whose coordinates are not finite numbers."
        )
    geometry_name = layer.geometry.name
    if field_name not in layer.columns or field_name == geometry_name:
        numeric = sorted(
            str(column)
            for column in layer.columns
            if column != geometry_name
            and pd.api.types.is_numeric_dtype(layer[column])
            and not pd.api.types.is_bool_dtype(layer[column])
        )
        more = len(numeric) - _FIELDS_QUOTED
        listed = f"{numeric[:_FIELDS_QUOTED]}" + (f" and {more} more" if more > 0 else "")
        raise ValueError(
            f"field {field_name!r} is not present in {points_path}; its numeric "
            f"fields are {listed}. {operation} refuses rather than interpolate "
            "something else in its place."
        )
    series = layer[field_name]
    if pd.api.types.is_bool_dtype(series) or not pd.api.types.is_numeric_dtype(series):
        raise ValueError(
            f"field {field_name!r} of {points_path} is {series.dtype}, not a number, "
            f"and {operation} interpolates numbers. Convert it with calculate_field first."
        )
    values = series.to_numpy(dtype="float64", na_value=np.nan)
    if len(values) == 0:
        raise ValueError(f"{points_path} has no points to interpolate")
    missing = int((~np.isfinite(values)).sum())
    if missing:
        raise ValueError(
            f"{missing} of {len(values)} points in {points_path} have no finite value "
            f"in {field_name!r}. {operation} refuses rather than leave them out or "
            "put something in their place. Drop them with select_features first, or "
            "fill them in, and say which."
        )
    minx, miny, maxx, maxy = (float(bound) for bound in layer.total_bounds)
    cells = limits.refuse_too_many_cells(maxx - minx, maxy - miny, cell_size, operation)
    return values, cells


def _distinct_samples(
    coordinates: np.ndarray, values: np.ndarray, points_path: str, field_name: str
) -> tuple[np.ndarray, np.ndarray]:
    """One location, one value: the distinct x, y pairs and the value at each.

    Two rows at one location holding the same value are one sample. Holding
    different values they are a question a triangulation cannot answer — which
    is the truth — and whitebox answered it by keeping the first, so the refusal
    is older than this engine.
    """
    distinct, inverse = np.unique(coordinates, axis=0, return_inverse=True)
    inverse = np.asarray(inverse).reshape(-1)
    lowest = np.full(len(distinct), np.inf)
    highest = np.full(len(distinct), -np.inf)
    np.minimum.at(lowest, inverse, values)
    np.maximum.at(highest, inverse, values)
    conflicting = np.flatnonzero(lowest != highest)
    if conflicting.size:
        x, y = (float(c) for c in distinct[conflicting[0]])
        raise ValueError(
            f"{conflicting.size} location(s) in {points_path} hold more than one value "
            f"of {field_name!r} (the first at {x:g}, {y:g}: {lowest[conflicting[0]]:g} "
            f"and {highest[conflicting[0]]:g}). tin_interpolation refuses rather than "
            "choose between them. A location is its x and y here; a z coordinate is "
            "not read. Aggregate them first, and say how."
        )
    if len(distinct) < 3 or shapely.MultiPoint(distinct).convex_hull.area == 0.0:
        raise ValueError(
            f"{points_path} has {len(distinct)} distinct location(s)"
            + (", all on one line" if len(distinct) >= 3 else "")
            + ": a triangulation needs at least three that are not collinear."
        )
    return distinct, lowest


def _triangles(
    distinct: np.ndarray, heights: np.ndarray, max_edge: float | None
) -> tuple[np.ndarray, np.ndarray]:
    """Corners (n, 3, 2) and the value at each corner (n, 3) of the Delaunay triangles."""
    parts = shapely.get_parts(shapely.delaunay_triangles(shapely.MultiPoint(distinct)))
    corners = shapely.get_coordinates(parts).reshape(-1, 4, 2)[:, :3, :]
    known = pd.MultiIndex.from_arrays([distinct[:, 0], distinct[:, 1]])
    asked = pd.MultiIndex.from_arrays([corners[:, :, 0].ravel(), corners[:, :, 1].ravel()])
    position = known.get_indexer(asked)
    if (position < 0).any():
        # The triangulation is asked for with no tolerance, so its vertices are
        # the samples, coordinate for coordinate. If a GEOS ever returns one
        # that is not, the value at it is not ours to invent.
        raise RuntimeError(
            "the triangulation returned a vertex that is not one of the samples; "
            "tin_interpolation cannot say what value it holds"
        )
    corner_values = heights[position].reshape(-1, 3)
    if max_edge is not None:
        sides = np.linalg.norm(corners - np.roll(corners, -1, axis=1), axis=2)
        keep = (sides <= max_edge).all(axis=1)
        corners, corner_values = corners[keep], corner_values[keep]
    return corners, corner_values


def _surface(
    corners: np.ndarray,
    corner_values: np.ndarray,
    burned: np.ndarray,
    west: float,
    north: float,
    cell_size: float,
) -> np.ndarray:
    """Each burned cell's value: the plane of its triangle, read at the cell's centre.

    Written from the first corner of each triangle rather than from the origin,
    z = z1 + a (x - x1) + b (y - y1): at UTM magnitudes the origin form
    subtracts numbers of 1e12 to get one of 1e2.
    """
    x1, y1 = corners[:, 0, 0], corners[:, 0, 1]
    dx2, dy2 = corners[:, 1, 0] - x1, corners[:, 1, 1] - y1
    dx3, dy3 = corners[:, 2, 0] - x1, corners[:, 2, 1] - y1
    dz2 = corner_values[:, 1] - corner_values[:, 0]
    dz3 = corner_values[:, 2] - corner_values[:, 0]
    det = dx2 * dy3 - dx3 * dy2
    # A triangle of no area holds no cell centre and is never burned; dividing
    # by its zero would only make noise.
    with np.errstate(divide="ignore", invalid="ignore"):
        slope_x = (dz2 * dy3 - dz3 * dy2) / det
        slope_y = (dx2 * dz3 - dx3 * dz2) / det
    rows, columns = burned.shape
    surface = np.full((rows, columns), np.nan, dtype="float64")
    # `grid.SAMPLE_OFFSET` and not a literal half: where a cell's value sits is
    # decided in one module.
    xs = west + (np.arange(columns) + grid.SAMPLE_OFFSET) * cell_size
    for start in range(0, rows, _BLOCK_ROWS):
        block = burned[start : start + _BLOCK_ROWS]
        hit = block > 0
        if not hit.any():
            continue
        triangle = block[hit] - 1
        row, column = np.nonzero(hit)
        ys = north - (start + row + grid.SAMPLE_OFFSET) * cell_size
        surface[start : start + _BLOCK_ROWS][hit] = (
            corner_values[triangle, 0]
            + slope_x[triangle] * (xs[column] - x1[triangle])
            + slope_y[triangle] * (ys - y1[triangle])
        )
    return surface


def tin_interpolation(
    points_path: str,
    output_path: str,
    field_name: str,
    cell_size: float,
    max_triangle_edge_length: float | None = None,
) -> dict[str, Any]:
    """Linear surface on the Delaunay triangulation of a point layer.

    Each cell takes the value of the plane through the three samples whose
    triangle contains its centre. Three consequences, each of which is the
    reason to choose this over `idw_interpolation` or to choose that instead:

    - **It honours the samples and adds nothing.** The surface passes through
      every measurement and never leaves the range of the data, and no
      parameter shapes it: two people given the same points get the same raster.
      An IDW surface depends on an exponent somebody picked. A cell holds the
      surface at its CENTRE, so a cell whose centre is not on a sample does not
      hold that sample's value.
    - **It does not extrapolate.** Cells whose centre is outside the convex hull
      of the points are nodata (NaN), where IDW fills the whole rectangle with
      numbers that look as measured as the rest. Inside the hull every gap is
      bridged unless `max_triangle_edge_length` says otherwise. `valid_cells`
      in the result says how much of the grid the triangulation covered.
    - **A trend is reproduced, not flattened.** Samples lying on a plane give
      back that plane; IDW bends it into a bullseye around each point.

    The grid is anchored at the north-west corner of the points' extent, with
    cells of exactly `cell_size`, and grows outward to the next whole cell.

    A geographic CRS is refused for the reason `idw_interpolation` gives and
    one more: which three samples make a triangle is decided by distances, and
    in degrees those are not distances, so the triangulation itself comes out
    different.

    `max_triangle_edge_length` leaves nodata in any triangle with a longer
    side: the way to keep the surface from bridging a gap in the sampling. It
    is in the layer's CRS units and it is recorded.
    """
    rasterio, features = _require()
    from rasterio.transform import from_origin

    if not (cell_size > 0 and math.isfinite(cell_size)):
        raise ValueError(f"cell_size must be positive and finite, got {cell_size}")
    if not field_name or not str(field_name).strip():
        raise ValueError("field_name is required: name the attribute to interpolate.")
    if max_triangle_edge_length is not None and not (
        max_triangle_edge_length > 0 and math.isfinite(max_triangle_edge_length)
    ):
        raise ValueError(
            "max_triangle_edge_length must be positive and finite, got "
            f"{max_triangle_edge_length}"
        )
    layer = readers.read_vector(points_path)
    if layer.crs is None:
        raise ValueError(
            readers.no_crs_message(
                layer,
                f"{points_path} declares no CRS, so neither the cell size nor the "
                "triangulation means anything.",
            )
        )
    crs = layer.crs
    if crs.is_geographic:
        raise ValueError(
            "tin_interpolation on a geographic CRS would choose its triangles by "
            "distances in degrees, which are not distances: a degree of longitude "
            "is 111 km at the equator and 83 km in Rome, so different samples end "
            "up joined and the surface changes with nothing to show for it. "
            "Reproject to a projected CRS first."
        )
    geometry = layer.geometry
    values, _cells = sample_values(layer, field_name, points_path, cell_size, "tin_interpolation")
    # One row per coordinate, with its value: a MultiPoint lends its value to
    # each of its parts.
    coordinates, owner = shapely.get_coordinates(geometry.values, return_index=True)
    sample = values[owner]
    distinct, heights = _distinct_samples(coordinates, sample, points_path, field_name)
    limits.refuse_too_many_points(len(distinct), "tin_interpolation")
    pre = verify.verify_loaded_inputs("tin_interpolation", points_path=layer)

    record = ProvenanceRecord(
        operation="tin_interpolation",
        parameters={
            "field_name": field_name,
            "cell_size": cell_size,
            "max_triangle_edge_length": max_triangle_edge_length,
        },
        inputs=[InputRecord.from_path(points_path, crs=verify.crs_label(crs))],
        engine=_engine_info(rasterio),
    )
    # Decided before the write, so a record written on failure carries it.
    record.crs_decisions = {
        "analysis_crs": verify.crs_label(crs),
        "reason": "the surface is built in the point layer's own CRS, which is "
        "projected — refused otherwise — so the cell size and the distances that "
        "decide the triangulation are both in that CRS's linear unit",
    }
    repeated = len(coordinates) - len(distinct)
    if repeated:
        record.notes.append(
            f"{repeated} point(s) repeat a location already present with the same "
            "value; they add nothing to the triangulation"
        )
    multipart = int((geometry.geom_type == "MultiPoint").sum())
    if multipart:
        record.notes.append(
            f"{multipart} row(s) are MultiPoint: each of their parts was given the "
            "row's value, as a sample of its own"
        )

    west, south = (float(v) for v in distinct.min(axis=0))
    east, north = (float(v) for v in distinct.max(axis=0))
    columns = max(1, math.ceil((east - west) / cell_size - _GRID_EPSILON))
    rows = max(1, math.ceil((north - south) / cell_size - _GRID_EPSILON))
    transform = from_origin(west, north, cell_size, cell_size)
    low, high = float(heights.min()), float(heights.max())

    with verify.audit_on_failure(record, output_path, pre):
        corners, corner_values = _triangles(distinct, heights, max_triangle_edge_length)
        if len(corners):
            # Plain mappings and not geometry objects: handed 200 000 shapely
            # polygons this call took 38 s, handed the same rings as lists, 1 s.
            rings = np.concatenate([corners, corners[:, :1, :]], axis=1).tolist()
            burned = features.rasterize(
                (
                    ({"type": "Polygon", "coordinates": [ring]}, number)
                    for number, ring in enumerate(rings, start=1)
                ),
                out_shape=(rows, columns),
                transform=transform,
                fill=0,
                dtype="int32",
            )
        else:
            burned = np.zeros((rows, columns), dtype="int32")
        surface = _surface(corners, corner_values, burned, west, north, cell_size)
        del burned
        with rasterio.open(
            output_path,
            "w",
            driver="GTiff",
            height=rows,
            width=columns,
            count=1,
            dtype="float64",
            crs=crs.to_wkt(),
            transform=transform,
            nodata=float("nan"),
        ) as destination:
            destination.write(surface, 1)
        del surface

    counts: dict[str, int] = {}
    # A linear interpolant cannot leave the range of its samples. The margin is
    # rounding and nothing else.
    margin = 1e-9 * max(1.0, abs(low), abs(high))

    def checks() -> list[verify.Check]:
        with rasterio.open(output_path) as written:
            found_crs = written.crs
            shape = (written.height, written.width)
            found_transform = written.transform
            band = written.read(1, masked=True)
        total = int(band.size)
        valid = np.ma.compressed(band)
        valid = valid[~np.isnan(valid)]
        counts["valid"], counts["total"] = int(valid.size), total
        record.notes.append(
            f"{valid.size} of {total} cells hold a value: a cell whose centre is "
            "outside the convex hull of the points"
            + (
                ", or inside a triangle with a side longer than "
                f"{max_triangle_edge_length:g},"
                if max_triangle_edge_length is not None
                else ""
            )
            + " is nodata, because a triangulation does not extrapolate"
        )
        label = verify.crs_label(found_crs) if found_crs is not None else "output has no CRS"
        found = [
            verify.Check("crs_present", found_crs is not None, label),
            verify.Check(
                "crs_matches",
                found_crs is not None and verify.same_crs(found_crs, crs),
                f"expected {verify.crs_label(crs)}, got {label}",
            ),
            verify.Check(
                "shape_preserved",
                shape == (rows, columns) and found_transform == transform,
                f"expected {(rows, columns)} at {tuple(transform)[:6]}, got {shape} "
                f"at {tuple(found_transform)[:6]}",
            ),
            verify.Check(
                "result_not_empty", valid.size > 0, f"{valid.size}/{total} valid cells"
            ),
        ]
        if valid.size:
            vmin, vmax = float(valid.min()), float(valid.max())
            found.append(
                verify.Check(
                    "values_in_expected_range",
                    low - margin <= vmin and vmax <= high + margin,
                    f"valid cells in [{vmin:.4g}, {vmax:.4g}], expected "
                    f"[{low:.4g}, {high:.4g}]: the range of the samples",
                )
            )
        return found

    manifest, extras = verify.audited(
        record, output_path, operation="tin_interpolation",
        preconditions=pre, checks_fn=checks, repair=False,
    )
    return {
        "output": str(output_path),
        "field_name": field_name,
        "cell_size": cell_size,
        "max_triangle_edge_length": max_triangle_edge_length,
        "shape": [rows, columns],
        "valid_cells": counts["valid"],
        "total_cells": counts["total"],
        "provenance": manifest,
        "verified": True,
        **extras,
    }
