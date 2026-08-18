"""Vector operations on the permissive GeoPandas/Shapely stack.

Design rules:
- Metric operations on geographic CRS are never silent: we estimate a UTM CRS,
  record the decision in provenance, and reproject back.
- Every writer emits a provenance manifest next to the output.
"""

from __future__ import annotations

from typing import Any

import geopandas as gpd

from ..provenance import InputRecord, ProvenanceRecord


def _engine_info() -> dict[str, str]:
    return {"name": "geopandas", "version": gpd.__version__}


def describe(path: str) -> dict[str, Any]:
    gdf = gpd.read_file(path)
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
    gdf = gpd.read_file(input_path)
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
    buffered.to_file(output_path)
    manifest = record.finish().write_for(output_path)
    return {
        "output": str(output_path),
        "feature_count": len(buffered),
        "provenance": str(manifest),
    }


def clip(input_path: str, mask_path: str, output_path: str) -> dict[str, Any]:
    gdf = gpd.read_file(input_path)
    mask = gpd.read_file(mask_path)
    record = ProvenanceRecord(
        operation="clip_layer",
        parameters={},
        inputs=[
            InputRecord.from_path(input_path, crs=str(gdf.crs)),
            InputRecord.from_path(mask_path, crs=str(mask.crs)),
        ],
        engine=_engine_info(),
    )
    if gdf.crs != mask.crs:
        mask = mask.to_crs(gdf.crs)
        record.crs_decisions = {
            "analysis_crs": str(gdf.crs),
            "reason": "mask reprojected to the input layer CRS before clipping",
        }
    clipped = gpd.clip(gdf, mask)
    clipped.to_file(output_path)
    manifest = record.finish().write_for(output_path)
    return {
        "output": str(output_path),
        "feature_count": len(clipped),
        "provenance": str(manifest),
    }


def reproject(input_path: str, target_crs: str, output_path: str) -> dict[str, Any]:
    gdf = gpd.read_file(input_path)
    record = ProvenanceRecord(
        operation="reproject_layer",
        parameters={"target_crs": target_crs},
        inputs=[InputRecord.from_path(input_path, crs=str(gdf.crs))],
        engine=_engine_info(),
    )
    reprojected = gdf.to_crs(target_crs)
    reprojected.to_file(output_path)
    manifest = record.finish().write_for(output_path)
    return {
        "output": str(output_path),
        "crs": str(reprojected.crs),
        "provenance": str(manifest),
    }


def spatial_join(
    left_path: str, right_path: str, output_path: str, predicate: str = "intersects"
) -> dict[str, Any]:
    allowed = {"intersects", "within", "contains"}
    if predicate not in allowed:
        raise ValueError(f"predicate must be one of {sorted(allowed)}, got {predicate!r}")
    left = gpd.read_file(left_path)
    right = gpd.read_file(right_path)
    record = ProvenanceRecord(
        operation="spatial_join",
        parameters={"predicate": predicate},
        inputs=[
            InputRecord.from_path(left_path, crs=str(left.crs)),
            InputRecord.from_path(right_path, crs=str(right.crs)),
        ],
        engine=_engine_info(),
    )
    if left.crs != right.crs:
        right = right.to_crs(left.crs)
        record.crs_decisions = {
            "analysis_crs": str(left.crs),
            "reason": "right layer reprojected to the left layer CRS before joining",
        }
    joined = gpd.sjoin(left, right, predicate=predicate, how="inner")
    joined = joined.drop(columns=[c for c in ("index_right",) if c in joined.columns])
    joined.to_file(output_path)
    manifest = record.finish().write_for(output_path)
    return {
        "output": str(output_path),
        "feature_count": len(joined),
        "provenance": str(manifest),
    }
