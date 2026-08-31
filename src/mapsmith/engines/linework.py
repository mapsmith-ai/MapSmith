"""Operations on the geometry of lines, and the one that puts a survey where it belongs.

Three of these were written inside other operations before they existed here.
`network` snaps endpoints together to decide whether two street segments are one
junction; `elevation_profile` walks a line at a fixed spacing to sample a
surface. Both are useful on their own, both were reachable only as a side effect
of asking for something else, and an operation that exists only inside another
one is an operation an agent cannot find.

The fourth is different. *"My boundary traverse from this morning is sitting way
out in the middle of the river on your map"* was one of twenty-one requests in
the discovery benchmark that neither labeller could place, and it has a real
answer: a survey in a local or assumed grid is put onto real ground by fitting a
transform from points whose coordinates are known in both. What makes it worth
building HERE rather than anywhere is the residual. The fit always succeeds —
two control points and a similarity transform match exactly, always, whatever
the points are — so the number that says whether the answer is any good is the
one nobody looks at. It is in the manifest, per point, with the worst named.
"""

from __future__ import annotations

import math
from typing import Any

import geopandas as gpd
import numpy as np
from shapely.geometry import MultiPoint, Point
from shapely.ops import transform as shapely_transform

from .. import readers, verify
from ..provenance import InputRecord, ProvenanceRecord

#: The transforms that can be fitted, and how many control points each needs.
#: Named for what they preserve, because that is the choice the caller is making.
TRANSFORMS = {
    # Rotation, uniform scale and shift. Preserves shape and angles: a square
    # stays square. What a total-station traverse on an assumed grid needs.
    "similarity": 2,
    # Adds independent scale on each axis and shear. A square can become a
    # parallelogram — which is right for a scanned map that stretched, and wrong
    # for a survey, where it will absorb a blunder instead of revealing it.
    "affine": 3,
}


def _engine_info() -> dict[str, str]:
    import shapely

    from .. import __version__

    return {
        "name": "mapsmith-linework",
        "version": __version__,
        "geometry_library": f"shapely {shapely.__version__}",
    }


def _write(gdf: gpd.GeoDataFrame, output_path: str) -> None:
    if str(output_path).endswith(".parquet"):
        gdf.to_parquet(output_path)
    else:
        gdf.to_file(output_path)


def _projected(gdf: gpd.GeoDataFrame, path: str, operation: str) -> None:
    """Refuse degrees where the argument is a distance.

    Not a warning. A tolerance of 5 in a geographic CRS is five degrees, which
    is about 550 km at the equator, and the operation would succeed and return
    something catastrophic without a single error.
    """
    if gdf.crs is None:
        raise ValueError(readers.no_crs_message(gdf, f"{path} has no CRS."))
    if gdf.crs.is_geographic:
        raise ValueError(
            f"{operation} takes a distance in the layer's own unit, and {path} is in "
            f"a geographic CRS ({verify.crs_label(gdf.crs)}) whose unit is the degree. "
            "A tolerance of 5 would mean five degrees, roughly 550 km. Reproject to a "
            "projected CRS first (reproject_layer)."
        )


def _lines_of(gdf: gpd.GeoDataFrame, path: str, operation: str) -> None:
    kinds = set(gdf.geometry.geom_type)
    if not kinds <= {"LineString", "MultiLineString"}:
        raise ValueError(
            f"{operation} works on lines, and {path} holds {sorted(kinds)}. "
            "Keep the line features first: run_operation(operation="
            "'select_features', arguments={'input_path': ..., 'output_path': ..., "
            "'by': 'geometry_type', 'value': 'line'}). Or convert the geometry, if "
            "the shapes themselves are the wrong ones."
        )


def snap_layer(
    input_path: str,
    reference_path: str,
    output_path: str,
    tolerance: float,
) -> dict[str, Any]:
    """Move vertices onto a reference layer's vertices when they are nearly there.

    The repair for data that is a millimetre from lining up: two datasets from
    two surveys, a parcel edge that should sit on the road centreline, endpoints
    that should be one junction. Every vertex within `tolerance` of a reference
    vertex is moved onto it exactly; everything else is left alone.

    **`tolerance` has no default and cannot have one.** Too small and nothing
    lines up while the output looks fixed; too large and features are pulled
    onto neighbours they were never related to, which is worse because it makes
    plausible geometry. What the manifest carries is what actually happened: how
    many vertices moved, the largest move, and whether any geometry became
    invalid in the process.

    Snapping is destructive and the original is not modified — the output is a
    new layer, and the count of moved vertices is the receipt.
    """
    if tolerance <= 0:
        raise ValueError(f"tolerance must be positive, got {tolerance}")

    gdf = readers.read_vector(input_path)
    _projected(gdf, input_path, "snap_layer")
    reference = readers.read_vector(reference_path)
    if reference.crs is None:
        raise ValueError(
            readers.no_crs_message(reference, f"{reference_path} has no CRS.")
        )
    crs_decisions: dict[str, Any] = {}
    if not verify.same_crs(gdf.crs, reference.crs):
        crs_decisions["reference_reprojected"] = (
            f"{verify.crs_label(reference.crs)} -> {verify.crs_label(gdf.crs)}"
        )
        crs_decisions["reason"] = (
            "the tolerance is in the input layer's unit, so the reference is brought "
            "into that CRS rather than the other way round"
        )
        reference = reference.to_crs(gdf.crs)

    targets = []
    for geometry in reference.geometry:
        if geometry is None or geometry.is_empty:
            continue
        targets.extend(geometry.coords if hasattr(geometry, "coords")
                       else [c for part in geometry.geoms for c in part.coords])
    if not targets:
        raise ValueError(
            f"{reference_path} has no vertices to snap to. An empty reference would "
            "return the input unchanged and call it snapped."
        )
    target_points = np.array([[x, y] for x, y, *_ in targets])
    index = gpd.GeoSeries([Point(x, y) for x, y in target_points], crs=gdf.crs).sindex

    moves: list[float] = []

    def snap_coordinates(x, y, z=None):
        point = Point(float(x), float(y))
        # `nearest` over a spatial index rather than a scan: the reference is
        # often the bigger of the two layers.
        candidates = index.query(point.buffer(tolerance), predicate="intersects")
        best, best_distance = None, tolerance
        for candidate in candidates:
            cx, cy = target_points[int(candidate)]
            distance = math.hypot(cx - float(x), cy - float(y))
            if distance <= best_distance:
                best, best_distance = (cx, cy), distance
        if best is None:
            return (x, y) if z is None else (x, y, z)
        if best_distance > 0:
            moves.append(best_distance)
        return best if z is None else (best[0], best[1], z)

    out = gdf.copy()
    out["geometry"] = [
        None if geometry is None else shapely_transform(snap_coordinates, geometry)
        for geometry in gdf.geometry
    ]
    invalid_before = int((~gdf.geometry.is_valid).sum())
    invalid_after = int((~out.geometry.is_valid).sum())
    _write(out, output_path)

    record = ProvenanceRecord(
        operation="snap_layer",
        parameters={
            "tolerance": tolerance,
            "unit": _unit_of(gdf),
            "rule": "each vertex moves to the nearest reference vertex within the "
            "tolerance; ties go to the first in reference order",
        },
        inputs=[
            InputRecord.from_path(input_path, crs=verify.crs_label(gdf.crs)),
            InputRecord.from_path(reference_path, crs=verify.crs_label(reference.crs)),
        ],
        engine=_engine_info(),
    )
    if crs_decisions:
        record.crs_decisions = crs_decisions
    largest = max(moves) if moves else 0.0
    if moves:
        record.notes.append(
            f"{len(moves)} vertex/vertices moved, the largest by {largest:.6g} "
            f"{_unit_of(gdf)}. Snapping edits coordinates: this layer no longer "
            "agrees with its source to the last decimal, on purpose."
        )
    else:
        record.notes.append(
            "no vertex was within the tolerance, so the geometry is unchanged. The "
            "tolerance is too small for this pair, or they already line up."
        )

    manifest, extras = verify.audited(
        record,
        output_path,
        operation="snap_layer",
        preconditions=verify.verify_loaded_inputs(
            "snap_layer", input_path=gdf, reference_path=reference
        ),
        checks_fn=lambda: [
            *verify.verify_vector_output(
                output_path,
                expect_crs=verify.crs_label(gdf.crs),
                expect_count=len(gdf),
            ),
            # The tolerance is the contract. A vertex that moved further than it
            # means the search is wrong, and the output would be geometry nobody
            # asked for.
            verify.Check(
                "x-mapsmith:no_vertex_moved_further_than_the_tolerance",
                largest <= tolerance + 1e-9,
                f"largest move {largest:.6g} against a tolerance of {tolerance}",
            ),
            # Snapping can fold a thin sliver onto itself. Valid in, invalid out
            # is a defect of this operation and not of the data.
            verify.Check(
                "x-mapsmith:snapping_did_not_break_a_geometry",
                invalid_after <= invalid_before,
                f"{invalid_before} invalid before, {invalid_after} after",
                hint="a tolerance wider than a feature's own width collapses it; "
                "lower the tolerance or repair with validate_geometry",
            ),
        ],
    )
    return {
        "output": str(output_path),
        "features": len(gdf),
        "vertices_moved": len(moves),
        "largest_move": round(largest, 9),
        "unit": _unit_of(gdf),
        "invalid_before": invalid_before,
        "invalid_after": invalid_after,
        "provenance": str(manifest),
        **extras,
    }


def _unit_of(gdf: gpd.GeoDataFrame) -> str:
    try:
        return gdf.crs.axis_info[0].unit_name
    except (AttributeError, IndexError):  # pragma: no cover - exotic CRS
        return "unit"


def points_along_lines(
    input_path: str,
    output_path: str,
    spacing: float,
    include_endpoint: bool = True,
) -> dict[str, Any]:
    """A point every `spacing` along each line, carrying its distance along it.

    Chainage, stationing, sampling positions — the same operation under three
    names, and the one `elevation_profile` performs internally before it reads a
    surface. Each output point carries the id of its line and `distance_along`,
    so the result can be joined back or plotted against anything sampled at it.

    Measured along the line in the CRS's own unit, so a projected CRS is
    required: a spacing of 20 in degrees is not 20 metres, it is about two
    thousand kilometres, and the operation would happily return four points for
    a continent.

    `include_endpoint` adds the line's end even when it does not fall on the
    spacing. On by default because a profile that stops 3 m short of the summit
    is a profile of the wrong thing, and the flag is recorded so a reader knows
    whether the last interval is short.
    """
    if spacing <= 0:
        raise ValueError(f"spacing must be positive, got {spacing}")

    gdf = readers.read_vector(input_path)
    _projected(gdf, input_path, "points_along_lines")
    _lines_of(gdf, input_path, "points_along_lines")

    rows: list[dict[str, Any]] = []
    short_lines = 0
    for position, (index, feature) in enumerate(gdf.iterrows()):
        geometry = feature.geometry
        if geometry is None or geometry.is_empty:
            continue
        parts = (
            [geometry] if geometry.geom_type == "LineString" else list(geometry.geoms)
        )
        for part_number, part in enumerate(parts):
            length = part.length
            if length < spacing:
                short_lines += 1
            steps = int(length // spacing)
            distances = [step * spacing for step in range(steps + 1)]
            if include_endpoint and (not distances or distances[-1] < length):
                distances.append(length)
            for point_number, distance in enumerate(distances):
                point = part.interpolate(distance)
                rows.append(
                    {
                        "line_index": index if isinstance(index, int | str) else position,
                        "part": part_number,
                        "point_number": point_number,
                        "distance_along": round(float(distance), 9),
                        "geometry": point,
                    }
                )

    if not rows:
        raise ValueError(
            f"{input_path} produced no points: every geometry is empty. Nothing to "
            "space out."
        )
    out = gpd.GeoDataFrame(rows, geometry="geometry", crs=gdf.crs)
    _write(out, output_path)

    total_length = float(gdf.geometry.length.sum())
    record = ProvenanceRecord(
        operation="points_along_lines",
        parameters={
            "spacing": spacing,
            "unit": _unit_of(gdf),
            "include_endpoint": include_endpoint,
            "rule": "distance is measured along the line from its start vertex; "
            "multi-part geometries are walked part by part, each restarting at zero",
        },
        inputs=[InputRecord.from_path(input_path, crs=verify.crs_label(gdf.crs))],
        engine=_engine_info(),
    )
    record.crs_decisions = {
        "measurement_crs": verify.crs_label(gdf.crs),
        "reason": "spacing and distance_along are in this CRS's linear unit; the "
        "geometry is not reprojected, so the numbers are the layer's own",
    }
    if short_lines:
        record.notes.append(
            f"{short_lines} line part(s) are shorter than the spacing, so they "
            "contribute their start point and (with include_endpoint) their end."
        )

    manifest, extras = verify.audited(
        record,
        output_path,
        operation="points_along_lines",
        preconditions=verify.verify_loaded_inputs("points_along_lines", input_path=gdf),
        checks_fn=lambda: [
            *verify.verify_vector_output(
                output_path,
                expect_crs=verify.crs_label(gdf.crs),
                expect_count=len(rows),
                expect_geometry={"Point"},
            ),
            # The count is arithmetic, so it can be predicted rather than
            # reported. A generator that drifts by one is the classic
            # off-by-one, and it is invisible in a map of dots.
            verify.Check(
                "x-mapsmith:the_point_count_is_what_the_spacing_implies",
                len(rows) == _expected_points(gdf, spacing, include_endpoint),
                f"{len(rows)} points against "
                f"{_expected_points(gdf, spacing, include_endpoint)} implied by "
                f"a spacing of {spacing} over {total_length:.6g} {_unit_of(gdf)}",
            ),
            # Every point must sit ON its line. `interpolate` guarantees it, but
            # the guarantee is worth an assertion: this is the check that would
            # catch a future version that offsets or rounds.
            verify.Check(
                "x-mapsmith:every_point_lies_on_its_line",
                _all_on_line(gdf, out),
                "each point is within a micrometre of the line it came from",
            ),
        ],
    )
    return {
        "output": str(output_path),
        "lines": len(gdf),
        "points": len(rows),
        "spacing": spacing,
        "unit": _unit_of(gdf),
        "total_length": round(total_length, 6),
        "provenance": str(manifest),
        **extras,
    }


def _expected_points(gdf: gpd.GeoDataFrame, spacing: float, include_endpoint: bool) -> int:
    total = 0
    for geometry in gdf.geometry:
        if geometry is None or geometry.is_empty:
            continue
        parts = [geometry] if geometry.geom_type == "LineString" else list(geometry.geoms)
        for part in parts:
            steps = int(part.length // spacing)
            count = steps + 1
            if include_endpoint and steps * spacing < part.length:
                count += 1
            total += count
    return total


def _all_on_line(lines: gpd.GeoDataFrame, points: gpd.GeoDataFrame) -> bool:
    joined = lines.geometry.union_all()
    return bool(points.geometry.distance(joined).max() < 1e-6)


def _is_shared_endpoint(place: Any, left: Any, right: Any) -> bool:
    """Whether this meeting point is an endpoint of BOTH lines.

    Two segments joined end to end meet at a point that is on the boundary of
    each: an ordinary junction, and not what a caller looking for crossings
    wants back. A T junction — one line ending on the middle of another — is on
    the boundary of one and the interior of the other, and it IS what they want,
    because it is the node somebody forgot to split.

    `left.touches(right)` cannot tell the two apart: it is true for both. The
    first version used it and reported no crossings for a plain T, then wrote
    into the manifest that the network might already be noded — a confident,
    well-formed, false statement about the data, which is the failure class this
    project exists to measure in other software.
    """
    tolerance = 1e-9
    return (
        left.boundary.distance(place) <= tolerance
        and right.boundary.distance(place) <= tolerance
    )


def line_intersections(
    input_path: str,
    other_path: str | None,
    output_path: str,
) -> dict[str, Any]:
    """Where lines cross, as points, with the pair that made each crossing.

    Two uses, one operation. With `other_path`, every crossing between the two
    layers — where the pipeline meets the road, which is the question that
    precedes every permit. Without it, every crossing *within* one layer, which
    is how a network is checked for the junctions somebody forgot to node.

    A crossing where the lines merely touch end-to-end is not reported: two
    segments meeting at a shared endpoint are a junction, not a crossing, and
    reporting them would bury the real ones. Overlapping collinear stretches are
    reported by their endpoints, and counted separately, because a line lying on
    top of another is a digitising error with a different fix.
    """
    gdf = readers.read_vector(input_path)
    if gdf.crs is None:
        raise ValueError(readers.no_crs_message(gdf, f"{input_path} has no CRS."))
    _lines_of(gdf, input_path, "line_intersections")

    crs_decisions: dict[str, Any] = {}
    inputs = [InputRecord.from_path(input_path, crs=verify.crs_label(gdf.crs))]
    if other_path is not None:
        other = readers.read_vector(other_path)
        if other.crs is None:
            raise ValueError(readers.no_crs_message(other, f"{other_path} has no CRS."))
        _lines_of(other, other_path, "line_intersections")
        inputs.append(InputRecord.from_path(other_path, crs=verify.crs_label(other.crs)))
        if not verify.same_crs(gdf.crs, other.crs):
            crs_decisions["second_layer_reprojected"] = (
                f"{verify.crs_label(other.crs)} -> {verify.crs_label(gdf.crs)}"
            )
            crs_decisions["reason"] = (
                "crossings are computed in the first layer's CRS, so the output "
                "coordinates are in it"
            )
            other = other.to_crs(gdf.crs)
        pairs = [
            (i, j)
            for i in range(len(gdf))
            for j in other.sindex.query(gdf.geometry.iloc[i], predicate="intersects")
        ]
        second = other
        within_one_layer = False
    else:
        second = gdf
        within_one_layer = True
        pairs = []
        for i in range(len(gdf)):
            for j in gdf.sindex.query(gdf.geometry.iloc[i], predicate="intersects"):
                if int(j) > i:  # each pair once, and never a line with itself
                    pairs.append((i, int(j)))

    rows: list[dict[str, Any]] = []
    collinear = 0
    for i, j in pairs:
        left, right = gdf.geometry.iloc[i], second.geometry.iloc[int(j)]
        if left is None or right is None:
            continue
        meeting = left.intersection(right)
        if meeting.is_empty:
            continue
        if meeting.geom_type in ("LineString", "MultiLineString"):
            collinear += 1
            places = [Point(c) for part in
                      ([meeting] if meeting.geom_type == "LineString" else meeting.geoms)
                      for c in (part.coords[0], part.coords[-1])]
            kind = "overlap"
        else:
            places = list(meeting.geoms) if isinstance(meeting, MultiPoint) else [meeting]
            kind = "crossing"
        for place in places:
            if not isinstance(place, Point):
                continue
            # End-to-end contact is a junction, not a crossing — but `touches`
            # is True whenever the interiors are disjoint and the boundaries
            # meet, which includes the case where ONE line's endpoint lands on
            # the OTHER's interior. That is a T junction, i.e. exactly the
            # forgotten node this operation exists to find, and skipping it
            # meant `(0,0)-(10,0)` against `(5,0)-(5,10)` reported no crossings
            # and a note saying the network may already be noded.
            #
            # The contact to skip is the one where the meeting point is on the
            # boundary of BOTH lines: two segments joined end to end. Checked at
            # the point rather than on the pair, because one pair can have a
            # proper junction at one end and a T at the other.
            if kind == "crossing" and _is_shared_endpoint(place, left, right):
                continue
            rows.append(
                {
                    "kind": kind,
                    "first_index": i,
                    "second_index": int(j),
                    "x": round(place.x, 9),
                    "y": round(place.y, 9),
                    "geometry": place,
                }
            )

    out = gpd.GeoDataFrame(
        rows or [], geometry="geometry", crs=gdf.crs
    ) if rows else gpd.GeoDataFrame(
        {"kind": [], "first_index": [], "second_index": [], "x": [], "y": []},
        geometry=gpd.GeoSeries([], crs=gdf.crs),
    )
    _write(out, output_path)

    record = ProvenanceRecord(
        operation="line_intersections",
        parameters={
            "scope": "within one layer" if within_one_layer else "between two layers",
            "rule": "lines meeting only at a shared endpoint are junctions and are "
            "not reported; collinear overlaps are reported by their endpoints and "
            "counted as overlaps",
        },
        inputs=inputs,
        engine=_engine_info(),
    )
    if crs_decisions:
        record.crs_decisions = crs_decisions
    if collinear:
        record.notes.append(
            f"{collinear} pair(s) overlap along a stretch rather than crossing at a "
            "point. That is usually a digitising error — one line drawn twice, or a "
            "shared boundary captured in both layers."
        )
    if not rows:
        record.notes.append(
            "no crossings at all: no pair of features meets anywhere except where "
            "both lines end, which includes the T junctions where one line ends on "
            "another's middle. For a network that is consistent with the lines "
            "being noded; between two layers it usually means they do not meet. "
            "Self-intersections within a single feature are not part of this "
            "count, so this is not a claim that every geometry is simple."
        )

    manifest, extras = verify.audited(
        record,
        output_path,
        operation="line_intersections",
        preconditions=verify.verify_loaded_inputs("line_intersections", input_path=gdf),
        checks_fn=lambda: [
            *verify.verify_vector_output(
                output_path,
                expect_crs=verify.crs_label(gdf.crs),
                expect_count=len(rows),
                # Legitimate and rarely intended: two layers that do not meet
                # look exactly like two layers somebody failed to align.
                on_empty="warn",
                expect_geometry={"Point"} if rows else None,
            ),
            # Every reported crossing must actually be on both lines. Cheap, and
            # it is the assertion that a future speed-up would break first.
            verify.Check(
                "x-mapsmith:every_crossing_lies_on_both_lines",
                all(
                    gdf.geometry.iloc[row["first_index"]].distance(row["geometry"]) < 1e-9
                    and second.geometry.iloc[row["second_index"]].distance(
                        row["geometry"]
                    ) < 1e-9
                    for row in rows
                ),
                f"{len(rows)} crossing(s) checked against both parents",
            ),
        ],
    )
    return {
        "output": str(output_path),
        "crossings": sum(1 for row in rows if row["kind"] == "crossing"),
        "overlap_points": sum(1 for row in rows if row["kind"] == "overlap"),
        "overlapping_pairs": collinear,
        "pairs_tested": len(pairs),
        "provenance": str(manifest),
        **extras,
    }


def _fit(source: np.ndarray, target: np.ndarray, kind: str) -> tuple[np.ndarray, str]:
    """Least-squares fit of a plane transform, returned as a 2x3 matrix.

    Similarity is solved in its four-parameter form (a, b, tx, ty) so that the
    scale is the same on both axes and angles survive; affine is the full six.
    Both by normal equations on an over-determined system, which is what "least
    squares" means and is worth writing rather than importing: the residuals
    below are only interesting if this is the fit they came from.
    """
    n = len(source)
    if kind == "similarity":
        # [x -y 1 0] [a]   [X]
        # [y  x 0 1] [b] = [Y]
        rows = np.zeros((2 * n, 4))
        rhs = np.zeros(2 * n)
        for i, ((x, y), (X, Y)) in enumerate(zip(source, target, strict=True)):
            rows[2 * i] = [x, -y, 1, 0]
            rows[2 * i + 1] = [y, x, 0, 1]
            rhs[2 * i], rhs[2 * i + 1] = X, Y
        solution, *_ = np.linalg.lstsq(rows, rhs, rcond=None)
        a, b, tx, ty = solution
        matrix = np.array([[a, -b, tx], [b, a, ty]])
        scale = math.hypot(a, b)
        rotation = math.degrees(math.atan2(b, a))
        summary = f"scale {scale:.9g}, rotation {rotation:.6f}°"
    else:
        rows = np.zeros((2 * n, 6))
        rhs = np.zeros(2 * n)
        for i, ((x, y), (X, Y)) in enumerate(zip(source, target, strict=True)):
            rows[2 * i] = [x, y, 1, 0, 0, 0]
            rows[2 * i + 1] = [0, 0, 0, x, y, 1]
            rhs[2 * i], rhs[2 * i + 1] = X, Y
        solution, *_ = np.linalg.lstsq(rows, rhs, rcond=None)
        matrix = solution.reshape(2, 3)
        summary = (
            f"determinant {matrix[0, 0] * matrix[1, 1] - matrix[0, 1] * matrix[1, 0]:.9g}"
        )
    return matrix, summary


def transform_by_control_points(
    input_path: str,
    control_path: str,
    output_path: str,
    target_crs: str,
    source_x: str = "source_x",
    source_y: str = "source_y",
    kind: str = "similarity",
) -> dict[str, Any]:
    """Put a survey on real ground by fitting a transform from known points.

    For data in a local, assumed or shifted grid: a traverse that plots in the
    river, a scanned plan, an old site grid nobody has the parameters for. The
    control layer holds points whose *target* coordinates are their geometry and
    whose *source* coordinates are two columns, one pair per control point. The
    fit is least squares, applied to every vertex of the input.

    **The fit always succeeds, and that is the danger.** Two control points and
    a similarity transform reproduce both exactly, whatever they are — including
    when one of them was typed wrong. So the residual per control point is the
    output that matters: it is in the result, in the manifest, and the worst one
    is named. A run with two control points reports a residual of zero and says
    why that is not evidence of anything.

    `kind='similarity'` (default) preserves shape and angles — right for a
    survey, where a stretched square means a blunder. `kind='affine'` allows
    independent scale and shear, which is right for a scanned map that
    distorted and wrong for a survey, because it will quietly absorb the blunder
    a similarity fit would have exposed.
    """
    if kind not in TRANSFORMS:
        raise ValueError(f"kind must be one of {sorted(TRANSFORMS)}, got {kind!r}")

    gdf = readers.read_vector(input_path)
    control = readers.read_vector(control_path)
    if control.crs is None:
        raise ValueError(
            readers.no_crs_message(
                control,
                f"{control_path} has no CRS, and its geometry is the KNOWN side of "
                "the fit — without a CRS there is nothing to fit onto.",
            )
        )
    for column in (source_x, source_y):
        if column not in control.columns:
            raise ValueError(
                f"{control_path} has no column {column!r}, which should hold the "
                "control point's coordinate in the input's own (local) system. "
                f"Columns: {sorted(c for c in control.columns if c != control.geometry.name)}"
            )
    if set(control.geometry.geom_type) != {"Point"}:
        raise ValueError(
            f"{control_path} must hold points: each one is a place whose position is "
            f"known in both systems. It holds {sorted(set(control.geometry.geom_type))}."
        )

    from pyproj import CRS as _CRS

    target = _CRS.from_user_input(target_crs)
    if not verify.same_crs(control.crs, target):
        control = control.to_crs(target)

    needed = TRANSFORMS[kind]
    if len(control) < needed:
        raise ValueError(
            f"a {kind} fit needs at least {needed} control points; {control_path} has "
            f"{len(control)}."
        )

    source = np.array(
        [[float(row[source_x]), float(row[source_y])] for _, row in control.iterrows()]
    )
    destination = np.array([[point.x, point.y] for point in control.geometry])
    matrix, summary = _fit(source, destination, kind)

    fitted = (matrix[:, :2] @ source.T).T + matrix[:, 2]
    residuals = np.sqrt(((fitted - destination) ** 2).sum(axis=1))
    worst = int(residuals.argmax())
    rms = float(math.sqrt(float((residuals**2).mean())))
    exactly_determined = len(control) == needed

    def apply(x, y, z=None):
        vector = np.array([float(x), float(y)])
        moved = matrix[:, :2] @ vector + matrix[:, 2]
        return (moved[0], moved[1]) if z is None else (moved[0], moved[1], z)

    out = gdf.copy()
    out["geometry"] = [
        None if geometry is None else shapely_transform(apply, geometry)
        for geometry in gdf.geometry
    ]
    out = out.set_crs(target, allow_override=True)
    _write(out, output_path)

    record = ProvenanceRecord(
        operation="transform_by_control_points",
        parameters={
            "kind": kind,
            "control_points": len(control),
            "matrix": [[round(v, 12) for v in row] for row in matrix.tolist()],
            "fit": summary,
            "method": "least squares (normal equations)",
        },
        inputs=[
            InputRecord.from_path(input_path, crs=verify.crs_label(gdf.crs)),
            InputRecord.from_path(control_path, crs=verify.crs_label(control.crs)),
        ],
        engine=_engine_info(),
    )
    record.crs_decisions = {
        "declared_output_crs": verify.crs_label(target),
        "reason": "the input carried a local or assumed system; the fit onto the "
        "control points IS the georeferencing, so the output is declared in the "
        "control points' CRS and the input's own declaration (if any) is discarded",
        "input_crs_before": verify.crs_label(gdf.crs),
    }
    record.notes.append(
        "residual per control point, in the target CRS's unit: "
        + ", ".join(f"{value:.6g}" for value in residuals)
    )
    if exactly_determined:
        record.notes.append(
            f"{len(control)} control points for a {kind} fit is exactly determined: "
            "the transform reproduces them perfectly by construction, so the "
            "residuals below are zero whatever the points are and say nothing about "
            "whether the fit is right. Add one more point to learn anything."
        )

    manifest, extras = verify.audited(
        record,
        output_path,
        operation="transform_by_control_points",
        # The control points must have a CRS — they are the georeferencing. The
        # input must NOT be required to: a layer in a local, assumed or shifted
        # grid is the whole reason this operation exists, and "assign the correct
        # CRS first" is advice to do the thing it replaces. It was checked, so
        # the operation wrote a correct output and a correct manifest and then
        # raised on the one input it was built for.
        preconditions=verify.verify_loaded_inputs(
            "transform_by_control_points", control_path=control
        ),
        checks_fn=lambda: [
            *verify.verify_vector_output(
                output_path,
                expect_crs=verify.crs_label(target),
                expect_count=len(gdf),
            ),
            # The fit reproducing its own control points is the one thing that
            # must be true of any correct implementation, and it is exactly what
            # a sign error or a transposed matrix breaks.
            verify.Check(
                "x-mapsmith:the_fit_reproduces_its_control_points",
                bool(residuals.max() < 1e-6) if exactly_determined else True,
                f"largest residual {residuals.max():.6g} on an exactly determined fit"
                if exactly_determined
                else f"over-determined fit, RMS residual {rms:.6g}",
            ),
            # Not critical, because a big residual can be honest data — but it
            # is the number the caller came for and it must be impossible to
            # miss.
            verify.Check(
                "x-mapsmith:the_residuals_are_small_enough_to_trust",
                exactly_determined or rms < 1.0,
                f"RMS residual {rms:.6g}, worst {residuals.max():.6g} at control "
                f"point {worst}",
                critical=False,
                hint="a large residual means a control point is wrong, the two "
                "systems differ by more than this transform can express, or "
                "kind='similarity' is being asked to absorb a real distortion",
            ),
        ],
    )
    return {
        "output": str(output_path),
        "features": len(gdf),
        "kind": kind,
        "control_points": len(control),
        "fit": summary,
        "rms_residual": round(rms, 9),
        "largest_residual": round(float(residuals.max()), 9),
        "worst_control_point": worst,
        "residuals": [round(float(value), 9) for value in residuals],
        "exactly_determined": exactly_determined,
        "provenance": str(manifest),
        **extras,
    }
