"""Operation catalog for progressive discovery.

Agent accuracy collapses when hundreds of raw tools are exposed. Mapsmith keeps
a small set of semantic MCP tools and lets agents *search* this catalog to find
what exists. Entries marked ``planned`` document the roadmap so the agent can
say "not yet" instead of hallucinating a capability.
"""

from __future__ import annotations

OPERATIONS: list[dict[str, str]] = [
    {
        "name": "describe_dataset",
        "status": "available",
        "category": "inspection",
        "summary": "CRS, geometry types, schema, extent and feature count of a vector dataset",
    },
    {
        "name": "buffer_layer",
        "status": "available",
        "category": "vector",
        "summary": "Metric buffer with automatic UTM estimation on geographic CRS",
    },
    {
        "name": "clip_layer",
        "status": "available",
        "category": "vector",
        "summary": "Clip a layer with a mask layer (CRS-aligned automatically)",
    },
    {
        "name": "reproject_layer",
        "status": "available",
        "category": "vector",
        "summary": "Reproject a layer to a target CRS (EPSG code or WKT)",
    },
    {
        "name": "spatial_join",
        "status": "available",
        "category": "vector",
        "summary": "Join by spatial predicate (intersects/within/contains); auto-routed to "
        "SedonaDB or DuckDB for speed, GeoPandas fallback",
    },
    {
        "name": "run_sql",
        "status": "available",
        "category": "sql",
        "summary": "Spatial SQL (DuckDB dialect, ST_* functions) over GeoParquet and GDAL "
        "formats; materializes GeoParquet outputs with provenance",
    },
    {
        "name": "get_provenance",
        "status": "available",
        "category": "provenance",
        "summary": "Full lineage manifest of any Mapsmith output",
    },
    {
        "name": "zonal_statistics",
        "status": "planned",
        "category": "raster",
        "summary": "Statistics of a raster within vector zones (Rasterio engine)",
    },
    {
        "name": "hillshade",
        "status": "planned",
        "category": "raster",
        "summary": "Terrain hillshading from a DEM",
    },
    {
        "name": "watershed",
        "status": "planned",
        "category": "hydrology",
        "summary": "Watershed delineation (WhiteboxTools engine)",
    },
    {
        "name": "isochrone",
        "status": "planned",
        "category": "network",
        "summary": "Travel-time polygons (Valhalla engine)",
    },
    {
        "name": "qgis_processing",
        "status": "planned",
        "category": "bridge",
        "summary": "~900 QGIS/GRASS/SAGA algorithms via GPL-isolated subprocess sidecar",
    },
]


def search(query: str = "") -> list[dict[str, str]]:
    """Case-insensitive substring search across name, category and summary."""
    q = query.strip().lower()
    if not q:
        return OPERATIONS
    return [
        op
        for op in OPERATIONS
        if q in op["name"].lower() or q in op["category"].lower() or q in op["summary"].lower()
    ]
