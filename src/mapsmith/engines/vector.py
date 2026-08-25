"""Vector operations on the permissive GeoPandas/Shapely stack.

Design rules:
- Metric operations on geographic CRS are never silent: we estimate a UTM CRS,
  record the decision in provenance, and reproject back.
- Every writer emits a provenance manifest next to the output.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import geopandas as gpd
import pandas as pd
import shapely

from .. import readers, verify
from ..provenance import InputRecord, ProvenanceRecord


def _engine_info() -> dict[str, str]:
    return {"name": "geopandas", "version": gpd.__version__}


_read = readers.read_vector


def _write(gdf: gpd.GeoDataFrame, output_path: str) -> None:
    """Write GeoParquet natively (canonical format), other formats via GDAL."""
    if str(output_path).lower().endswith(".parquet"):
        gdf.to_parquet(output_path)
    else:
        gdf.to_file(output_path)


def describe(path: str) -> dict[str, Any]:
    layers = readers.ambiguous_layers(path)
    if layers:
        # Inspection is the one place a multi-layer container is NOT refused:
        # describing every layer is exactly what lets the caller choose one,
        # which is what every other operation now requires (#29).
        import pyogrio

        described = []
        for name in layers:
            info = pyogrio.read_info(path, layer=name)
            described.append({
                "layer": name,
                "feature_count": int(info.get("features") or -1),
                "geometry_type": info.get("geometry_type"),
                "crs": str(info["crs"]) if info.get("crs") else None,
            })
        return {
            "path": str(path),
            "kind": "vector-container",
            "layer_count": len(described),
            "layers": described,
            "hint": (
                "this container holds more than one layer, and operations need a "
                "single-layer dataset: extract the one you mean first — e.g. "
                "run_sql: SELECT * FROM ST_Read(path, layer='<name>') with an "
                "output_path"
            ),
        }
    gdf = _read(path)
    bounds = gdf.total_bounds
    return {
        "path": str(path),
        "kind": "vector",
        "crs": verify.crs_label(gdf.crs) if gdf.crs else None,
        "feature_count": len(gdf),
        "geometry_types": sorted(gdf.geom_type.dropna().unique().tolist()),
        "fields": {c: str(t) for c, t in gdf.dtypes.items() if c != gdf.geometry.name},
        "extent": {
            "minx": float(bounds[0]),
            "miny": float(bounds[1]),
            "maxx": float(bounds[2]),
            "maxy": float(bounds[3]),
        },
    }


def buffer(input_path: str, distance_meters: float, output_path: str) -> dict[str, Any]:
    gdf = _read(input_path)
    if gdf.crs is None:
        raise ValueError(readers.no_crs_message(
            gdf,
            f"{input_path} has no CRS. Refusing to buffer without knowing the units — "
            "assign a CRS first (see reproject_layer).",
        ))
    record = ProvenanceRecord(
        operation="buffer_layer",
        parameters={"distance_meters": distance_meters},
        inputs=[InputRecord.from_path(input_path, crs=verify.crs_label(gdf.crs))],
        engine=_engine_info(),
    )
    pre = verify.verify_loaded_inputs("buffer_layer", input_path=gdf)
    original_crs = gdf.crs
    if original_crs.is_geographic:
        analysis_crs = gdf.estimate_utm_crs()
        record.crs_decisions = {
            "analysis_crs": str(analysis_crs),
            "reason": "estimated UTM zone for metric buffering on a geographic CRS",
        }
        buffered = gdf.to_crs(analysis_crs)
        buffered["geometry"] = buffered.geometry.buffer(distance_meters)
        buffered = buffered.to_crs(original_crs)
    else:
        record.crs_decisions = {
            "analysis_crs": verify.crs_label(original_crs),
            "reason": "input CRS is already projected; distance interpreted in its units",
        }
        buffered = gdf.copy()
        buffered["geometry"] = buffered.geometry.buffer(distance_meters)
    _write(buffered, output_path)
    manifest, extras = verify.audited(
        record,
        output_path,
        operation="buffer_layer",
        preconditions=pre,
        checks_fn=lambda: verify.verify_vector_output(
            output_path,
            expect_crs=original_crs,
            expect_count=len(gdf),
            expect_geometry={"Polygon", "MultiPolygon"},
            # a buffer of a non-empty layer cannot be empty: that is a bug
            on_empty="fail" if len(gdf) else "ignore",
        ),
    )
    return {
        "output": str(output_path),
        "feature_count": len(buffered),
        "provenance": manifest,
        "verified": True,
        **extras,
    }


def clip(input_path: str, mask_path: str, output_path: str) -> dict[str, Any]:
    gdf = _read(input_path)
    mask = _read(mask_path)
    record = ProvenanceRecord(
        operation="clip_layer",
        parameters={},
        inputs=[
            InputRecord.from_path(input_path, crs=verify.crs_label(gdf.crs)),
            InputRecord.from_path(mask_path, crs=verify.crs_label(mask.crs)),
        ],
        engine=_engine_info(),
    )
    # the CRS gate comes first: aligning CRS is what would raise a raw pyproj
    # error on a CRS-less input, before our own check could explain it
    pre = verify.verify_loaded_inputs("clip_layer", input_path=gdf, mask_path=mask)
    if verify.has_critical_failure(pre):
        record.add_verification(pre).finish().write_for(output_path)
        verify.enforce(pre, "clip_layer")
    if gdf.crs != mask.crs:
        mask = mask.to_crs(gdf.crs)
        record.crs_decisions = {
            "analysis_crs": verify.crs_label(gdf.crs),
            "reason": "mask reprojected to the input layer CRS before clipping",
        }
    # extents can only be compared once both layers share a CRS
    pre += verify.verify_input_pairs("clip_layer", input_path=gdf, mask_path=mask)
    with verify.audit_on_failure(record, output_path, pre):
        clipped = gpd.clip(gdf, mask)
        _write(clipped, output_path)
    mask_bounds = tuple(float(v) for v in mask.total_bounds)
    manifest, extras = verify.audited(
        record,
        output_path,
        operation="clip_layer",
        preconditions=pre,
        checks_fn=lambda: verify.verify_vector_output(
            output_path,
            expect_crs=gdf.crs,
            max_count=len(gdf),
            within_bounds=mask_bounds,
            bounds_margin=1e-6,
            # an empty clip is legitimate (extents can overlap while geometries
            # miss), so warn loudly instead of failing a valid analysis
            on_empty="warn" if len(gdf) and len(mask) else "ignore",
        ),
        # gpd.clip goes through GEOS overlay, which always returns valid
        # geometry: there is nothing here for make_valid to fix
        repair=False,
    )
    return {
        "output": str(output_path),
        "feature_count": len(clipped),
        "provenance": manifest,
        "verified": True,
        **extras,
    }


def reproject(input_path: str, target_crs: str, output_path: str) -> dict[str, Any]:
    gdf = _read(input_path)
    record = ProvenanceRecord(
        operation="reproject_layer",
        parameters={"target_crs": target_crs},
        inputs=[InputRecord.from_path(input_path, crs=verify.crs_label(gdf.crs))],
        engine=_engine_info(),
    )
    pre = verify.verify_loaded_inputs("reproject_layer", input_path=gdf)
    if verify.has_critical_failure(pre):
        record.add_verification(pre).finish().write_for(output_path)
        verify.enforce(pre, "reproject_layer")
    with verify.audit_on_failure(record, output_path, pre):
        reprojected = gdf.to_crs(target_crs)
        _write(reprojected, output_path)
    # reprojection carries geometry through verbatim, so an invalid input gives
    # an invalid output: this is where mechanical repair actually earns its keep
    manifest, extras = verify.audited(
        record,
        output_path,
        operation="reproject_layer",
        preconditions=pre,
        checks_fn=lambda: verify.verify_vector_output(
            output_path,
            expect_crs=target_crs,
            expect_count=len(gdf),
            on_empty="fail" if len(gdf) else "ignore",
        ),
    )
    return {
        "output": str(output_path),
        "crs": verify.crs_label(reprojected.crs),
        "provenance": manifest,
        "verified": True,
        **extras,
    }


OVERLAY_HOWS = {"intersection", "union", "identity", "symmetric_difference", "difference"}
DISSOLVE_AGGFUNCS = {"first", "last", "sum", "mean", "median", "min", "max", "count"}


def overlay(
    input_path: str, overlay_path: str, output_path: str, how: str = "intersection"
) -> dict[str, Any]:
    if how not in OVERLAY_HOWS:
        raise ValueError(f"how must be one of {sorted(OVERLAY_HOWS)}, got {how!r}")
    left = _read(input_path)
    right = _read(overlay_path)
    record = ProvenanceRecord(
        operation="overlay_layers",
        parameters={"how": how, "keep_geom_type": True},
        inputs=[
            InputRecord.from_path(input_path, crs=verify.crs_label(left.crs)),
            InputRecord.from_path(overlay_path, crs=verify.crs_label(right.crs)),
        ],
        engine=_engine_info(),
    )
    pre = verify.verify_loaded_inputs("overlay_layers", input_path=left, overlay_path=right)
    if verify.has_critical_failure(pre):
        record.add_verification(pre).finish().write_for(output_path)
        verify.enforce(pre, "overlay_layers")
    if left.crs != right.crs:
        right = right.to_crs(left.crs)
        record.crs_decisions = {
            "analysis_crs": verify.crs_label(left.crs),
            "reason": "overlay layer reprojected to the input CRS before overlaying",
        }
    else:
        record.crs_decisions = {
            "analysis_crs": verify.crs_label(left.crs),
            "reason": "both layers share the same CRS; no reprojection needed",
        }
    # Two polygons that merely TOUCH intersect in a line, and a corner contact
    # in a point. keep_geom_type=True drops those lower-dimension pieces — a
    # semantic choice a reader of the result cannot see, so it is stated here
    # and in the manifest rather than made silently.
    record.notes.append(
        "keep_geom_type=true: overlay pieces of lower dimension than the inputs "
        "(shared edges, corner contacts) are dropped from the result"
    )
    pre += verify.verify_input_pairs("overlay_layers", input_path=left, overlay_path=right)
    with verify.audit_on_failure(record, output_path, pre):
        combined = gpd.overlay(left, right, how=how, keep_geom_type=True)
        _write(combined, output_path)
    manifest, extras = verify.audited(
        record,
        output_path,
        operation="overlay_layers",
        preconditions=pre,
        checks_fn=lambda: verify.verify_vector_output(
            output_path,
            expect_crs=left.crs,
            # An empty intersection or difference is legitimate but suspicious,
            # exactly like an empty clip: it comes back flagged, never silent.
            on_empty="warn" if len(left) and len(right) else "ignore",
        ),
    )
    return {
        "output": str(output_path),
        "how": how,
        "feature_count": len(combined),
        "provenance": manifest,
        "verified": True,
        **extras,
    }


def dissolve(
    input_path: str, output_path: str, by: str | None = None, aggfunc: str = "first"
) -> dict[str, Any]:
    if aggfunc not in DISSOLVE_AGGFUNCS:
        raise ValueError(
            f"aggfunc must be one of {sorted(DISSOLVE_AGGFUNCS)}, got {aggfunc!r}. "
            "The aggregation is recorded in the provenance manifest: a sum reported "
            "where a mean was meant is a plausible wrong number nobody can see."
        )
    gdf = _read(input_path)
    if by is not None and by not in gdf.columns:
        columns = [c for c in gdf.columns if c != gdf.geometry.name]
        raise ValueError(
            f"column {by!r} does not exist in {input_path}. Available columns: {columns}"
        )
    record = ProvenanceRecord(
        operation="dissolve_layer",
        parameters={"by": by, "aggfunc": aggfunc},
        inputs=[InputRecord.from_path(input_path, crs=verify.crs_label(gdf.crs))],
        engine=_engine_info(),
    )
    pre = verify.verify_loaded_inputs("dissolve_layer", input_path=gdf)
    if verify.has_critical_failure(pre):
        record.add_verification(pre).finish().write_for(output_path)
        verify.enforce(pre, "dissolve_layer")
    record.crs_decisions = {
        "analysis_crs": verify.crs_label(gdf.crs),
        "reason": "dissolve is a topological union; computed in the layer's native CRS",
    }
    # The group count is knowable BEFORE the engine runs: one output feature
    # per distinct non-null key (or exactly one with no key). Declaring it here
    # turns the row count into a closed-form postcondition instead of a report.
    if by is None:
        expected = 1 if len(gdf) else 0
    else:
        expected = int(gdf[by].nunique())
        dropped = int(gdf[by].isna().sum())
        if dropped:
            record.notes.append(
                f"{dropped} features have a null {by!r} key and are dropped by the "
                "grouping (geopandas dissolve default) — they are not merged into "
                "any group"
            )
    with verify.audit_on_failure(record, output_path, pre):
        merged = gdf.dissolve(by=by, aggfunc=aggfunc, as_index=False)
        _write(merged, output_path)
    manifest, extras = verify.audited(
        record,
        output_path,
        operation="dissolve_layer",
        preconditions=pre,
        checks_fn=lambda: verify.verify_vector_output(
            output_path,
            expect_crs=gdf.crs,
            expect_count=expected,
            on_empty="ignore" if not len(gdf) else "warn",
        ),
    )
    return {
        "output": str(output_path),
        "by": by,
        "aggfunc": aggfunc,
        "feature_count": len(merged),
        "provenance": manifest,
        "verified": True,
        **extras,
    }


def nearest_join(
    left_path: str,
    right_path: str,
    output_path: str,
    max_distance_meters: float | None = None,
    distance_column: str = "nearest_distance_m",
) -> dict[str, Any]:
    if max_distance_meters is not None and max_distance_meters <= 0:
        raise ValueError(f"max_distance_meters must be positive, got {max_distance_meters}")
    left = _read(left_path)
    right = _read(right_path)
    record = ProvenanceRecord(
        operation="nearest_join",
        parameters={
            "max_distance_meters": max_distance_meters,
            "distance_column": distance_column,
        },
        inputs=[
            InputRecord.from_path(left_path, crs=verify.crs_label(left.crs)),
            InputRecord.from_path(right_path, crs=verify.crs_label(right.crs)),
        ],
        engine=_engine_info(),
    )
    pre = verify.verify_loaded_inputs("nearest_join", left_path=left, right_path=right)
    if verify.has_critical_failure(pre):
        record.add_verification(pre).finish().write_for(output_path)
        verify.enforce(pre, "nearest_join")

    original_crs = left.crs
    if right.crs != left.crs:
        right = right.to_crs(left.crs)
    if original_crs.is_geographic:
        # The distance column is in METERS, always: nearest-in-degrees is the
        # classic silent killer of this operation (a degree of longitude is not
        # a degree of latitude, and neither is a metre).
        analysis_crs = left.estimate_utm_crs()
        record.crs_decisions = {
            "analysis_crs": str(analysis_crs),
            "reason": (
                "estimated UTM zone for metric nearest-distance on a geographic CRS; "
                "output geometries are returned in the input CRS"
            ),
        }
        left_m, right_m = left.to_crs(analysis_crs), right.to_crs(analysis_crs)
    else:
        record.crs_decisions = {
            "analysis_crs": verify.crs_label(original_crs),
            "reason": "nearest distances measured in the layers' native projected CRS",
        }
        left_m, right_m = left, right
    pre += verify.verify_input_pairs("nearest_join", left_path=left_m, right_path=right_m)
    with verify.audit_on_failure(record, output_path, pre):
        joined = gpd.sjoin_nearest(
            left_m, right_m, max_distance=max_distance_meters, distance_col=distance_column
        )
        joined = joined.drop(columns=[c for c in ("index_right",) if c in joined.columns])
        if original_crs.is_geographic:
            joined = joined.to_crs(original_crs)
        _write(joined, output_path)
    manifest, extras = verify.audited(
        record,
        output_path,
        operation="nearest_join",
        preconditions=pre,
        checks_fn=lambda: verify.verify_vector_output(
            output_path,
            expect_crs=original_crs,
            # max_distance can legitimately empty the result; it comes back
            # flagged rather than silent, like every suspicious emptiness.
            on_empty="warn" if len(left) and len(right) else "ignore",
        ),
    )
    return {
        "output": str(output_path),
        "feature_count": len(joined),
        "distance_column": distance_column,
        "provenance": manifest,
        "verified": True,
        **extras,
    }


def explode(input_path: str, output_path: str) -> dict[str, Any]:
    gdf = _read(input_path)
    record = ProvenanceRecord(
        operation="explode_layer",
        parameters={},
        inputs=[InputRecord.from_path(input_path, crs=verify.crs_label(gdf.crs))],
        engine=_engine_info(),
    )
    pre = verify.verify_loaded_inputs("explode_layer", input_path=gdf)
    if verify.has_critical_failure(pre):
        record.add_verification(pre).finish().write_for(output_path)
        verify.enforce(pre, "explode_layer")
    record.crs_decisions = {
        "analysis_crs": verify.crs_label(gdf.crs),
        "reason": "explode changes structure, not coordinates; computed in the native CRS",
    }
    # The output size is knowable before the engine runs: one feature per
    # part. Declaring it turns the count into a closed-form postcondition.
    expected = int(
        gdf.geometry.apply(
            lambda g: len(g.geoms) if hasattr(g, "geoms") else 1
        ).sum()
    )
    with verify.audit_on_failure(record, output_path, pre):
        parts = gdf.explode(index_parts=False, ignore_index=True)
        _write(parts, output_path)
    manifest, extras = verify.audited(
        record,
        output_path,
        operation="explode_layer",
        preconditions=pre,
        checks_fn=lambda: verify.verify_vector_output(
            output_path,
            expect_crs=gdf.crs,
            expect_count=expected,
            on_empty="ignore" if not len(gdf) else "warn",
        ),
    )
    return {
        "output": str(output_path),
        "feature_count": len(parts),
        "provenance": manifest,
        "verified": True,
        **extras,
    }


# Dimension class per geometry type: merging polygons with more polygons is
# routine; merging polygons with points is almost always a mistake upstream.
_GEOMETRY_CLASS = {
    "Point": "point",
    "MultiPoint": "point",
    "LineString": "line",
    "MultiLineString": "line",
    "LinearRing": "line",
    "Polygon": "polygon",
    "MultiPolygon": "polygon",
    "GeometryCollection": "collection",
}


def merge(input_paths: list[str], output_path: str) -> dict[str, Any]:
    if len(input_paths) < 2:
        raise ValueError(
            f"merge_layers needs at least two input layers, got {len(input_paths)}"
        )
    frames = [_read(p) for p in input_paths]
    record = ProvenanceRecord(
        operation="merge_layers",
        parameters={"layer_count": len(frames)},
        inputs=[
            InputRecord.from_path(path, crs=verify.crs_label(frame.crs))
            for path, frame in zip(input_paths, frames)
        ],
        engine=_engine_info(),
    )
    named = {f"input_{i}": frame for i, frame in enumerate(frames, start=1)}
    pre = verify.verify_loaded_inputs("merge_layers", **named)
    if verify.has_critical_failure(pre):
        record.add_verification(pre).finish().write_for(output_path)
        verify.enforce(pre, "merge_layers")
    target = frames[0].crs
    moved = sum(1 for frame in frames[1:] if not verify.same_crs(frame.crs, target))
    if moved:
        frames = [
            frame if verify.same_crs(frame.crs, target) else frame.to_crs(target)
            for frame in frames
        ]
        record.crs_decisions = {
            "analysis_crs": verify.crs_label(target),
            "reason": f"{moved} of {len(frames)} layers reprojected to the first "
            "layer's CRS before merging",
        }
    else:
        record.crs_decisions = {
            "analysis_crs": verify.crs_label(target),
            "reason": "all layers share the first layer's CRS; no reprojection needed",
        }
    # A column missing from one input becomes nulls in its rows — data that
    # looks measured and is actually absent. The manifest names those columns.
    per_layer = [set(frame.columns) - {frame.geometry.name} for frame in frames]
    partial = sorted(set.union(*per_layer) - set.intersection(*per_layer))
    if partial:
        record.notes.append(
            "columns present in only some inputs are null-filled in the rows of "
            f"the others: {partial}"
        )
    classes = sorted({
        _GEOMETRY_CLASS.get(t, "other")
        for frame in frames
        for t in frame.geom_type.dropna().unique()
    })
    if len(classes) > 1:
        record.notes.append(
            f"the merged layer mixes geometry classes {classes}: many formats and "
            "operations reject mixed layers — merge like with like unless this is "
            "deliberate"
        )
    geometry_name = frames[0].geometry.name
    frames = [
        frame if frame.geometry.name == geometry_name
        else frame.rename_geometry(geometry_name)
        for frame in frames
    ]
    # One output row per input row: the count is knowable before the engine runs.
    expected = sum(len(frame) for frame in frames)
    with verify.audit_on_failure(record, output_path, pre):
        merged = pd.concat(frames, ignore_index=True)
        _write(merged, output_path)
    manifest, extras = verify.audited(
        record,
        output_path,
        operation="merge_layers",
        preconditions=pre,
        checks_fn=lambda: verify.verify_vector_output(
            output_path,
            expect_crs=target,
            expect_count=expected,
            on_empty="fail" if expected else "ignore",
        ),
    )
    return {
        "output": str(output_path),
        "layer_count": len(input_paths),
        "feature_count": len(merged),
        "provenance": manifest,
        "verified": True,
        **extras,
    }


def simplify(input_path: str, tolerance_meters: float, output_path: str) -> dict[str, Any]:
    if tolerance_meters <= 0:
        raise ValueError(f"tolerance_meters must be positive, got {tolerance_meters}")
    gdf = _read(input_path)
    if gdf.crs is None:
        raise ValueError(readers.no_crs_message(
            gdf,
            f"{input_path} has no CRS. Refusing to simplify without knowing the "
            "units — assign a CRS first (see reproject_layer).",
        ))
    record = ProvenanceRecord(
        operation="simplify_layer",
        parameters={"tolerance_meters": tolerance_meters, "preserve_topology": True},
        inputs=[InputRecord.from_path(input_path, crs=verify.crs_label(gdf.crs))],
        engine=_engine_info(),
    )
    pre = verify.verify_loaded_inputs("simplify_layer", input_path=gdf)
    original_crs = gdf.crs
    # An empty layer has nothing to estimate a UTM zone from (estimate_utm_crs
    # raises a raw pyproj error), and nothing to simplify: it passes through in
    # its own CRS with the reason recorded, instead of crashing without a manifest.
    if original_crs.is_geographic and len(gdf):
        analysis_crs = gdf.estimate_utm_crs()
        record.crs_decisions = {
            "analysis_crs": str(analysis_crs),
            "reason": "estimated UTM zone for metric simplification on a geographic "
            "CRS; output geometries are returned in the input CRS",
        }
        work = gdf.to_crs(analysis_crs)
        restore = True
    else:
        record.crs_decisions = {
            "analysis_crs": verify.crs_label(original_crs),
            "reason": "empty input layer: no UTM zone to estimate, nothing to simplify"
            if original_crs.is_geographic
            else "input CRS is already projected; tolerance interpreted in its units",
        }
        work = gdf.copy()
        restore = False
    vertices_before = int(shapely.get_num_coordinates(work.geometry.values).sum())
    area_before = float(work.geometry.area.sum())
    length_before = float(work.geometry.length.sum())
    with verify.audit_on_failure(record, output_path, pre):
        work[work.geometry.name] = work.geometry.simplify(
            tolerance_meters, preserve_topology=True
        )
        vertices_after = int(shapely.get_num_coordinates(work.geometry.values).sum())
        # Simplification moves boundaries: the drift is measured and recorded,
        # never assumed away. Zero drift is a statement too.
        if area_before > 0:
            area_after = float(work.geometry.area.sum())
            drift = (area_after - area_before) / area_before * 100
            record.notes.append(
                f"total area {area_before:.6g} -> {area_after:.6g} square CRS units "
                f"({drift:+.4f}%) in the analysis CRS"
            )
        if length_before > 0:
            length_after = float(work.geometry.length.sum())
            drift = (length_after - length_before) / length_before * 100
            record.notes.append(
                f"total length {length_before:.6g} -> {length_after:.6g} CRS units "
                f"({drift:+.4f}%) in the analysis CRS"
            )
        record.notes.append(
            f"vertices {vertices_before} -> {vertices_after} "
            f"(tolerance {tolerance_meters} in analysis-CRS units)"
        )
        if restore:
            work = work.to_crs(original_crs)
        _write(work, output_path)
    manifest, extras = verify.audited(
        record,
        output_path,
        operation="simplify_layer",
        preconditions=pre,
        checks_fn=lambda: verify.verify_vector_output(
            output_path,
            expect_crs=original_crs,
            expect_count=len(gdf),
            on_empty="fail" if len(gdf) else "ignore",
        ),
    )
    return {
        "output": str(output_path),
        "feature_count": len(work),
        "vertices_before": vertices_before,
        "vertices_after": vertices_after,
        "provenance": manifest,
        "verified": True,
        **extras,
    }


def centroid(input_path: str, output_path: str) -> dict[str, Any]:
    gdf = _read(input_path)
    if gdf.crs is None:
        raise ValueError(readers.no_crs_message(
            gdf,
            f"{input_path} has no CRS. Refusing to compute centroids without knowing "
            "the units — assign a CRS first (see reproject_layer).",
        ))
    record = ProvenanceRecord(
        operation="centroid_layer",
        parameters={},
        inputs=[InputRecord.from_path(input_path, crs=verify.crs_label(gdf.crs))],
        engine=_engine_info(),
    )
    pre = verify.verify_loaded_inputs("centroid_layer", input_path=gdf)
    original_crs = gdf.crs
    # Same guard as simplify: estimate_utm_crs on an empty layer raises a raw
    # pyproj error before any manifest exists, and there is nothing to measure.
    if original_crs.is_geographic and len(gdf):
        # A planar centroid of degree coordinates lands in the wrong place —
        # quietly, and by more the farther from the equator the data sits.
        analysis_crs = gdf.estimate_utm_crs()
        record.crs_decisions = {
            "analysis_crs": str(analysis_crs),
            "reason": "estimated UTM zone for planar centroids on a geographic CRS; "
            "output points are returned in the input CRS",
        }
        work = gdf.to_crs(analysis_crs)
        restore = True
    else:
        record.crs_decisions = {
            "analysis_crs": verify.crs_label(original_crs),
            "reason": "empty input layer: no UTM zone to estimate, nothing to measure"
            if original_crs.is_geographic
            else "centroids computed in the layer's native projected CRS",
        }
        work = gdf.copy()
        restore = False
    record.notes.append(
        "the geometric centroid of a concave or multi-part feature can fall outside "
        "the feature itself; a point guaranteed inside is a different operation "
        "(representative point), not a tighter centroid"
    )
    with verify.audit_on_failure(record, output_path, pre):
        work[work.geometry.name] = work.geometry.centroid
        if restore:
            work = work.to_crs(original_crs)
        _write(work, output_path)
    manifest, extras = verify.audited(
        record,
        output_path,
        operation="centroid_layer",
        preconditions=pre,
        checks_fn=lambda: verify.verify_vector_output(
            output_path,
            expect_crs=original_crs,
            expect_count=len(gdf),
            expect_geometry={"Point"},
            on_empty="fail" if len(gdf) else "ignore",
        ),
    )
    return {
        "output": str(output_path),
        "feature_count": len(work),
        "provenance": manifest,
        "verified": True,
        **extras,
    }


CONVERT_FORMATS = {".parquet": "GeoParquet", ".gpkg": "GeoPackage", ".geojson": "GeoJSON"}


def convert(input_path: str, output_path: str) -> dict[str, Any]:
    suffix = Path(str(output_path)).suffix.lower()
    if suffix == ".shp":
        raise ValueError(
            "refusing to write a shapefile: field names are truncated to 10 characters "
            "and dtypes are coerced, silently — a conversion that quietly renames "
            "columns is a silent error. Write GeoPackage (.gpkg) instead; if a legacy "
            "tool truly needs a shapefile, export from the GeoPackage in that tool, "
            "where the renaming is visible."
        )
    if suffix not in CONVERT_FORMATS:
        raise ValueError(
            f"output format {suffix!r} is not supported: use one of "
            f"{sorted(CONVERT_FORMATS)}"
        )
    gdf = _read(input_path)
    # RFC 7946 prescribes WGS84 lon-lat, which the authorities spell two ways:
    # OGC:CRS84 (the GeoParquet default) and EPSG:4326 (whose formal axis order
    # GeoDataFrames ignore anyway). Both are the same coordinates here.
    wgs84 = gdf.crs is not None and any(
        verify.same_crs(gdf.crs, crs) for crs in ("EPSG:4326", "OGC:CRS84")
    )
    if suffix == ".geojson" and gdf.crs is not None and not wgs84:
        raise ValueError(
            f"GeoJSON (RFC 7946) is WGS84 by definition and this layer is in "
            f"{verify.crs_label(gdf.crs)}: writing it would produce a file whose CRS "
            "some readers honour and others ignore. Reproject to EPSG:4326 first "
            "(reproject_layer), or convert to .gpkg/.parquet, which carry any CRS."
        )
    record = ProvenanceRecord(
        operation="convert_format",
        parameters={"target_format": CONVERT_FORMATS[suffix]},
        inputs=[InputRecord.from_path(input_path, crs=verify.crs_label(gdf.crs))],
        engine=_engine_info(),
    )
    pre = verify.verify_loaded_inputs("convert_format", input_path=gdf)
    if verify.has_critical_failure(pre):
        record.add_verification(pre).finish().write_for(output_path)
        verify.enforce(pre, "convert_format")
    record.crs_decisions = {
        "analysis_crs": verify.crs_label(gdf.crs),
        "reason": "no CRS change: format conversion does not transform coordinates",
    }
    with verify.audit_on_failure(record, output_path, pre):
        _write(gdf, output_path)
    # Geometry is carried through verbatim, so an invalid input gives an invalid
    # output: as in reproject, this is where mechanical repair earns its keep,
    # and every repair lands in the manifest and the result.
    manifest, extras = verify.audited(
        record,
        output_path,
        operation="convert_format",
        preconditions=pre,
        checks_fn=lambda: verify.verify_vector_output(
            output_path,
            expect_crs=gdf.crs,
            expect_count=len(gdf),
            on_empty="ignore",
        ),
    )
    return {
        "output": str(output_path),
        "format": CONVERT_FORMATS[suffix],
        "feature_count": len(gdf),
        "provenance": manifest,
        "verified": True,
        **extras,
    }


def spatial_join(
    left_path: str, right_path: str, output_path: str, predicate: str = "intersects"
) -> dict[str, Any]:
    allowed = {"intersects", "within", "contains"}
    if predicate not in allowed:
        raise ValueError(f"predicate must be one of {sorted(allowed)}, got {predicate!r}")
    left = _read(left_path)
    right = _read(right_path)
    record = ProvenanceRecord(
        operation="spatial_join",
        parameters={"predicate": predicate},
        inputs=[
            InputRecord.from_path(left_path, crs=verify.crs_label(left.crs)),
            InputRecord.from_path(right_path, crs=verify.crs_label(right.crs)),
        ],
        engine=_engine_info(),
    )
    pre = verify.verify_loaded_inputs("spatial_join", left_path=left, right_path=right)
    if verify.has_critical_failure(pre):
        record.add_verification(pre).finish().write_for(output_path)
        verify.enforce(pre, "spatial_join")
    if left.crs != right.crs:
        right = right.to_crs(left.crs)
        record.crs_decisions = {
            "analysis_crs": verify.crs_label(left.crs),
            "reason": "right layer reprojected to the left layer CRS before joining",
        }
    pre += verify.verify_input_pairs("spatial_join", left_path=left, right_path=right)
    with verify.audit_on_failure(record, output_path, pre):
        joined = gpd.sjoin(left, right, predicate=predicate, how="inner")
        joined = joined.drop(columns=[c for c in ("index_right",) if c in joined.columns])
        _write(joined, output_path)
    manifest, extras = verify.audited(
        record,
        output_path,
        operation="spatial_join",
        preconditions=pre,
        checks_fn=lambda: verify.verify_vector_output(
            output_path,
            expect_crs=left.crs,
            on_empty="warn" if len(left) and len(right) else "ignore",
        ),
    )
    return {
        "output": str(output_path),
        "feature_count": len(joined),
        "provenance": manifest,
        "verified": True,
        **extras,
    }
