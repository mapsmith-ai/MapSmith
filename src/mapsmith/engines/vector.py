"""Vector operations on the permissive GeoPandas/Shapely stack.

Design rules:
- Metric operations on geographic CRS are never silent: we estimate a UTM CRS,
  record the decision in provenance, and reproject back.
- Every writer emits a provenance manifest next to the output.
"""

from __future__ import annotations

from typing import Any

import geopandas as gpd

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
