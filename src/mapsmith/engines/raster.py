"""Raster zonal statistics on the exactextract engine.

exactextract computes exact fractional pixel coverage (no all-in/all-out pixel
approximation) with bounded memory — 10-100x faster than rasterstats-class
implementations. Optional extra: ``pip install mapsmith[raster]``.

CRS discipline: zones are reprojected to the raster CRS before extraction
(mismatched CRS is the single most common silent error in GIS analysis), and
the decision is recorded in the provenance manifest. Output stays in the
raster CRS.
"""

from __future__ import annotations

from typing import Any

import geopandas as gpd
import pandas as pd

from .. import readers, verify
from ..provenance import InputRecord, ProvenanceRecord

VALID_STATS = {
    "count",
    "sum",
    "mean",
    "median",
    "min",
    "max",
    "stdev",
    "variance",
    "majority",
    "minority",
    "variety",
}


def _require():
    try:
        import exactextract
        import rasterio
    except ImportError as exc:
        raise ImportError(
            "zonal_statistics requires the raster extra: pip install mapsmith[raster]"
        ) from exc
    return exactextract, rasterio


def _engine_info() -> dict[str, str]:
    from importlib.metadata import version

    return {"name": "exactextract", "version": version("exactextract")}


def _require_rasterio():
    try:
        import rasterio
    except ImportError as exc:
        raise ImportError(
            "raster inspection requires the raster extra: pip install mapsmith[raster]"
        ) from exc
    return rasterio


def describe(path: str) -> dict[str, Any]:
    """CRS, grid, bands, nodata and per-band statistics of a raster (read-only).

    Statistics are computed on the masked read, so nodata cells are excluded
    from min/max/mean and counted separately — most silent raster errors start
    with metadata nobody looked at, and nodata treated as elevation is the
    canonical one.
    """
    rasterio = _require_rasterio()
    with rasterio.open(path) as ds:
        bands = []
        for index in range(1, ds.count + 1):
            data = ds.read(index, masked=True)
            valid = int(data.count())
            bands.append({
                "band": index,
                "dtype": ds.dtypes[index - 1],
                "nodata": ds.nodatavals[index - 1],
                "valid_cells": valid,
                "nodata_cells": int(data.size - valid),
                "min": float(data.min()) if valid else None,
                "max": float(data.max()) if valid else None,
                "mean": float(data.mean()) if valid else None,
            })
        left, bottom, right, top = ds.bounds
        return {
            "path": str(path),
            "kind": "raster",
            "crs": str(ds.crs) if ds.crs else None,
            "width": ds.width,
            "height": ds.height,
            "band_count": ds.count,
            "resolution": {"x": abs(float(ds.res[0])), "y": abs(float(ds.res[1]))},
            "extent": {
                "minx": float(left),
                "miny": float(bottom),
                "maxx": float(right),
                "maxy": float(top),
            },
            "bands": bands,
        }


def zonal_statistics(
    raster_path: str,
    zones_path: str,
    output_path: str,
    stats: list[str] | None = None,
) -> dict[str, Any]:
    """Statistics of a single-band raster within each vector zone."""
    exactextract, rasterio = _require()
    ops = stats or ["count", "mean", "min", "max"]
    unknown = [s for s in ops if s not in VALID_STATS]
    if unknown:
        raise ValueError(
            f"Unknown statistics {unknown}. Valid: {sorted(VALID_STATS)} "
            "(note: 'stdev', not 'std')"
        )

    zones = readers.read_vector(zones_path)
    if zones.crs is None:
        raise ValueError(readers.no_crs_message(
            zones, f"{zones_path} has no CRS — cannot align zones to the raster."
        ))

    with rasterio.open(raster_path) as ds:
        raster_crs = ds.crs
        record = ProvenanceRecord(
            operation="zonal_statistics",
            parameters={"stats": ops, "bands": ds.count},
            inputs=[
                InputRecord.from_path(raster_path, crs=verify.crs_label(raster_crs)),
                InputRecord.from_path(zones_path, crs=verify.crs_label(zones.crs)),
            ],
            engine=_engine_info(),
        )
        if raster_crs is not None and not verify.same_crs(zones.crs, raster_crs):
            zones = zones.to_crs(raster_crs)
            record.crs_decisions = {
                "analysis_crs": verify.crs_label(raster_crs),
                "reason": "zones reprojected to the raster CRS for exact pixel "
                "alignment; output kept in the raster CRS",
            }
        else:
            record.crs_decisions = {
                "analysis_crs": str(raster_crs),
                "reason": "zones and raster share the same CRS",
            }
        stats_df = exactextract.exact_extract(ds, zones, ops, output="pandas")

    out = gpd.GeoDataFrame(
        pd.concat(
            [zones.reset_index(drop=True), stats_df.reset_index(drop=True)], axis=1
        ),
        geometry=zones.geometry.name,
        crs=zones.crs,
    )
    if str(output_path).endswith(".parquet"):
        out.to_parquet(output_path)
    else:
        out.to_file(output_path)

    # the zone geometries are carried through verbatim, so an invalid input
    # yields an invalid output: mechanical repair applies here
    manifest, extras = verify.audited(
        record,
        output_path,
        operation="zonal_statistics",
        preconditions=verify.verify_loaded_inputs("zonal_statistics", zones_path=zones),
        checks_fn=lambda: verify.verify_vector_output(
            output_path,
            expect_crs=zones.crs,
            expect_count=len(zones),
        ),
    )
    return {
        "output": str(output_path),
        "feature_count": len(out),
        "statistics": ops,
        "provenance": manifest,
        "verified": True,
        **extras,
    }
