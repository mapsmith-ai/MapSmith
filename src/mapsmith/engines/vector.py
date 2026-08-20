"""Vector operations on the permissive GeoPandas/Shapely stack.

Design rules:
- Metric operations on geographic CRS are never silent: we estimate a UTM CRS,
  record the decision in provenance, and reproject back.
- Every writer emits a provenance manifest next to the output.
"""

from __future__ import annotations

from typing import Any

import geopandas as gpd

from .. import verify
from ..provenance import InputRecord, ProvenanceRecord


def _engine_info() -> dict[str, str]:
    return {"name": "geopandas", "version": gpd.__version__}


def _read(path: str) -> gpd.GeoDataFrame:
    """Read GeoParquet natively, everything else via GDAL (GDAL's Parquet driver
    is not bundled in the wheels, so routing .parquet through read_file breaks)."""
    if str(path).lower().endswith(".parquet"):
        return gpd.read_parquet(path)
    return gpd.read_file(path)


def _write(gdf: gpd.GeoDataFrame, output_path: str) -> None:
    """Write GeoParquet natively (canonical format), other formats via GDAL."""
    if str(output_path).lower().endswith(".parquet"):
        gdf.to_parquet(output_path)
    else:
        gdf.to_file(output_path)


def describe(path: str) -> dict[str, Any]:
    gdf = _read(path)
    bounds = gdf.total_bounds
    return {
        "path": str(path),
        "crs": str(gdf.crs) if gdf.crs else None,
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
        raise ValueError(
            f"{input_path} has no CRS. Refusing to buffer without knowing the units — "
            "assign a CRS first (see reproject_layer)."
        )
    record = ProvenanceRecord(
        operation="buffer_layer",
        parameters={"distance_meters": distance_meters},
        inputs=[InputRecord.from_path(input_path, crs=str(gdf.crs))],
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
            "analysis_crs": str(original_crs),
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
            expect_crs=str(original_crs),
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
            InputRecord.from_path(input_path, crs=str(gdf.crs)),
            InputRecord.from_path(mask_path, crs=str(mask.crs)),
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
            "analysis_crs": str(gdf.crs),
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
            expect_crs=str(gdf.crs),
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
        inputs=[InputRecord.from_path(input_path, crs=str(gdf.crs))],
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
        "crs": str(reprojected.crs),
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
            InputRecord.from_path(left_path, crs=str(left.crs)),
            InputRecord.from_path(right_path, crs=str(right.crs)),
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
            "analysis_crs": str(left.crs),
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
            expect_crs=str(left.crs),
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
