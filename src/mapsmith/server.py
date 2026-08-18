"""MapSmith MCP server.

Transports:
- stdio (default): ``mapsmith`` — zero infrastructure, for Claude Desktop & co.
- Streamable HTTP: set ``MAPSMITH_TRANSPORT=http`` (env: MAPSMITH_HOST,
  MAPSMITH_PORT). Stateless: any request can hit any replica.
"""

from __future__ import annotations

import os
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import __version__, catalog, jobs
from .engines import dispatch, duckdb_engine, vector
from .provenance import read_provenance

_HTTP = os.environ.get("MAPSMITH_TRANSPORT", "stdio").lower() in {"http", "streamable-http"}

mcp = FastMCP(
    "mapsmith",
    instructions=(
        "MapSmith is a deterministic geoprocessing toolbox. Geometry and numbers always "
        "come from tool executions, never from the model. Every output dataset has a "
        "lineage manifest (<output>.provenance.json) retrievable with get_provenance. "
        "Use list_operations to discover capabilities before improvising; if an operation "
        "is 'planned', say so instead of approximating it with the wrong tool. "
        "Datasets are file paths; GeoParquet is the fast path, GeoPackage also works."
    ),
    host=os.environ.get("MAPSMITH_HOST", "127.0.0.1"),
    port=int(os.environ.get("MAPSMITH_PORT", "8000")),
    stateless_http=_HTTP,
    json_response=_HTTP,
)


# Standard MCP tool annotations (readOnlyHint etc. — hints for clients, spec 2025-06-18).
# Writers are marked non-destructive: they only (re)write the declared output_path and are
# deterministic, so re-running with the same arguments reproduces the same dataset.
_READONLY = ToolAnnotations(readOnlyHint=True, idempotentHint=True, openWorldHint=False)
_WRITER = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
# run_sql executes arbitrary SQL (COPY, DDL on attached DBs), so it keeps the honest default.
_SQL = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)


def _run(operation: str, params: dict[str, Any], fn, *args) -> dict[str, Any]:
    """Execute an engine call as a durable job (no-op ledger without DATABASE_URL)."""
    with jobs.job(operation, params) as (job_id, res):
        result = fn(*args)
        res.update(result)
    result["job_id"] = job_id
    return result


@mcp.tool(annotations=_READONLY)
def describe_dataset(path: str) -> dict[str, Any]:
    """Inspect a vector dataset: CRS, geometry types, schema, extent, feature count.

    Call this before any analysis on a dataset you have not inspected yet.
    """
    return vector.describe(path)


@mcp.tool(annotations=_WRITER)
def buffer_layer(input_path: str, distance_meters: float, output_path: str) -> dict[str, Any]:
    """Buffer all features by a distance in meters.

    Geographic-CRS inputs are reprojected to an estimated UTM zone for the metric
    operation and back; the decision is recorded in the provenance manifest.
    """
    return _run(
        "buffer_layer",
        {"input": input_path, "distance_meters": distance_meters, "output": output_path},
        vector.buffer,
        input_path,
        distance_meters,
        output_path,
    )


@mcp.tool(annotations=_WRITER)
def clip_layer(input_path: str, mask_path: str, output_path: str) -> dict[str, Any]:
    """Clip a layer to the area of a mask layer. CRS are aligned automatically."""
    return _run(
        "clip_layer",
        {"input": input_path, "mask": mask_path, "output": output_path},
        vector.clip,
        input_path,
        mask_path,
        output_path,
    )


@mcp.tool(annotations=_WRITER)
def reproject_layer(input_path: str, target_crs: str, output_path: str) -> dict[str, Any]:
    """Reproject a layer to a target CRS, e.g. 'EPSG:32632' or a WKT string."""
    return _run(
        "reproject_layer",
        {"input": input_path, "target_crs": target_crs, "output": output_path},
        vector.reproject,
        input_path,
        target_crs,
        output_path,
    )


@mcp.tool(annotations=_WRITER)
def spatial_join(
    left_path: str,
    right_path: str,
    output_path: str,
    predicate: str = "intersects",
    engine: str = "auto",
) -> dict[str, Any]:
    """Join by spatial predicate (intersects/within/contains).

    engine='auto' routes to the fastest available engine for the inputs:
    SedonaDB (heavy joins, 10-180x) > DuckDB (GeoParquet fast path) > GeoPandas.
    """
    chosen = dispatch.pick(dispatch.Workload.HEAVY_JOIN, engine)
    if chosen == "sedonadb":
        from .engines import sedona_engine

        fn = sedona_engine.spatial_join
    elif chosen == "duckdb" and duckdb_engine.supports_inputs(left_path, right_path):
        fn = duckdb_engine.spatial_join
    else:
        fn = vector.spatial_join
    return _run(
        "spatial_join",
        {
            "left": left_path,
            "right": right_path,
            "output": output_path,
            "predicate": predicate,
            "engine": chosen,
        },
        fn,
        left_path,
        right_path,
        output_path,
        predicate,
    )


@mcp.tool(annotations=_SQL)
def run_sql(query: str, output_path: str = "") -> dict[str, Any]:
    """Run spatial SQL (DuckDB dialect, ST_* functions, read_parquet/ST_Read for files).

    Without output_path: returns up to 50 preview rows. With output_path (.parquet):
    materializes the full result as GeoParquet with a provenance manifest.
    """
    out = output_path or None
    return _run("run_sql", {"query": query, "output": out}, duckdb_engine.run_sql, query, out)


@mcp.tool(annotations=_WRITER)
def zonal_statistics(
    raster_path: str,
    zones_path: str,
    output_path: str,
    stats: list[str] | None = None,
) -> dict[str, Any]:
    """Statistics of a raster within each vector zone (exact fractional pixel coverage).

    stats: subset of count/sum/mean/median/min/max/stdev/variance/majority/minority/
    variety (default: count, mean, min, max). Zones are aligned to the raster CRS
    automatically; the decision is recorded in the provenance manifest.
    Requires the [raster] extra.
    """
    from .engines import raster

    return _run(
        "zonal_statistics",
        {"raster": raster_path, "zones": zones_path, "output": output_path, "stats": stats},
        raster.zonal_statistics,
        raster_path,
        zones_path,
        output_path,
        stats,
    )


@mcp.tool(annotations=_WRITER)
def hillshade(
    dem_path: str,
    output_path: str,
    azimuth: float = 315.0,
    altitude: float = 30.0,
    z_factor: float = 1.0,
) -> dict[str, Any]:
    """Shaded relief from a DEM: GeoTIFF in, GeoTIFF out (values scaled 0-32767).

    azimuth = sun direction in degrees (default 315, NW); altitude = sun angle
    above the horizon (default 30). DEMs without a CRS are rejected.
    Requires the [whitebox] extra.
    """
    from .engines import whitebox_engine

    return _run(
        "hillshade",
        {"dem": dem_path, "output": output_path, "azimuth": azimuth, "altitude": altitude},
        whitebox_engine.hillshade,
        dem_path,
        output_path,
        azimuth,
        altitude,
        z_factor,
    )


@mcp.tool(annotations=_WRITER)
def flow_accumulation(
    dem_path: str,
    output_path: str,
    out_type: str = "cells",
    log_transform: bool = False,
) -> dict[str, Any]:
    """D8 flow accumulation from a DEM (GeoTIFF in/out). Depressions are filled first.

    out_type: 'cells' (upslope cell count, includes the cell itself) or 'sca'
    (specific catchment area). log_transform=True for visualization-friendly
    values. Requires the [whitebox] extra.
    """
    from .engines import whitebox_engine

    return _run(
        "flow_accumulation",
        {"dem": dem_path, "output": output_path, "out_type": out_type},
        whitebox_engine.flow_accumulation,
        dem_path,
        output_path,
        out_type,
        log_transform,
    )


@mcp.tool(annotations=_WRITER)
def watershed(dem_path: str, pour_points_path: str, output_path: str) -> dict[str, Any]:
    """Watershed of each pour point: DEM + points in, basin raster out (GeoTIFF).

    Basins get 1-based IDs following the pour-point feature order; cells not
    draining to any point stay nodata. Points are aligned to the DEM CRS
    automatically (decision recorded). Requires the [whitebox] extra.
    """
    from .engines import whitebox_engine

    return _run(
        "watershed",
        {"dem": dem_path, "pour_points": pour_points_path, "output": output_path},
        whitebox_engine.watershed,
        dem_path,
        pour_points_path,
        output_path,
    )


@mcp.tool(annotations=_READONLY)
def get_provenance(output_path: str) -> dict[str, Any]:
    """Return the full lineage manifest of a MapSmith output dataset."""
    return read_provenance(output_path)


@mcp.tool(annotations=_READONLY)
def list_operations(query: str = "", detail: bool = False, limit: int = 10) -> list[dict[str, Any]]:
    """Search the catalog of available and planned operations (progressive discovery).

    Describe what you need in plain words (e.g. 'statistics of a raster inside
    polygons') and results come back ranked by relevance (BM25). Compact entries
    by default; detail=True adds parameters and worked example calls — use it on
    the exact operation name before calling an unfamiliar tool. Empty query
    lists the whole catalog including planned (not yet available) operations.
    """
    return catalog.search(query, limit=limit, detail=detail)


@mcp.tool(annotations=_READONLY)
def server_info() -> dict[str, Any]:
    """MapSmith version, licensing, and available engines."""
    return {
        "name": "mapsmith",
        "version": __version__,
        "license": "AGPL-3.0-or-later",
        "homepage": "https://github.com/mapsmith-ai/MapSmith",
        "engines": dispatch.available_engines(),
    }


def main() -> None:
    mcp.run(transport="streamable-http" if _HTTP else "stdio")


if __name__ == "__main__":
    main()
