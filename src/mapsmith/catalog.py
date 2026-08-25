"""Operation catalog with deterministic BM25 retrieval for progressive discovery.

Agent accuracy collapses when hundreds of raw tools are exposed (the ~100-tool
cliff). MapSmith keeps a small set of semantic MCP tools and lets agents
*search* this catalog to find what exists: each entry carries structured docs
(description, parameters, worked examples) so a client can defer-load tool
detail instead of holding every schema in context. Entries marked ``planned``
document the roadmap so the agent can say "not yet" instead of hallucinating.

Retrieval is pure-Python Okapi BM25: deterministic, dependency-free, and fast
at catalog scale (hundreds of entries). If lexical search ever proves
insufficient, an embedding reranker can be layered on top of :func:`rank`
without changing the public API.
"""

from __future__ import annotations

import math
import re
from typing import Any

OPERATIONS: list[dict[str, Any]] = [
    {
        "name": "describe_dataset",
        "status": "available",
        "category": "inspection",
        "summary": "Inspect a vector or raster dataset: CRS, schema/bands, extent, "
        "nodata, statistics",
        "description": (
            "Inspect a dataset before analysing it. Vector: coordinate reference system, "
            "geometry types, attribute schema, bounding extent and feature count. A "
            "multi-layer container is described per layer (name, feature count, geometry "
            "type, CRS) — operations refuse containers with no chosen layer, so this is "
            "where you find the layer to extract. Raster (.tif): CRS, grid size, "
            "resolution, bands with dtype, nodata value and masked statistics — nodata "
            "cells are excluded from min/max/mean and counted separately. Call it first "
            "on any dataset you have not seen yet: most silent GIS errors start with "
            "wrong assumptions about CRS, units, nodata or which layer you are on. "
            "Raster inspection requires the [raster] extra."
        ),
        "parameters": [
            {
                "name": "path",
                "type": "str",
                "required": True,
                "description": "Dataset path (GeoParquet, GeoPackage, any GDAL vector "
                "format, or a GeoTIFF)",
            },
        ],
        "examples": [
            {
                "goal": "Check the CRS and schema of a parcels layer before buffering it",
                "call": {"tool": "describe_dataset", "arguments": {"path": "parcels.gpkg"}},
            },
            {
                "goal": "Read nodata, resolution and band statistics of a DEM before "
                "terrain analysis",
                "call": {"tool": "describe_dataset", "arguments": {"path": "dem.tif"}},
            },
        ],
    },
    {
        "name": "buffer_layer",
        "status": "available",
        "category": "vector",
        "summary": "Metric buffer with automatic UTM estimation on geographic CRS",
        "description": (
            "Buffer every feature by a distance in meters. Inputs in a geographic CRS "
            "(degrees) are reprojected to an estimated UTM zone for the metric operation "
            "and back; the decision is recorded in the provenance manifest. Output "
            "geometries are polygons in the input CRS. A negative distance erodes "
            "features; if it erases them all, the result carries a 'warnings' entry "
            "saying so rather than reporting a clean success."
        ),
        "parameters": [
            {
                "name": "input_path",
                "type": "str",
                "required": True,
                "description": "Vector dataset to buffer",
            },
            {
                "name": "distance_meters",
                "type": "float",
                "required": True,
                "description": "Buffer distance in meters (always meters, never degrees)",
            },
            {
                "name": "output_path",
                "type": "str",
                "required": True,
                "description": "Output path (.parquet for GeoParquet, .gpkg for GeoPackage)",
            },
        ],
        "examples": [
            {
                "goal": "500 m protection zone around wells stored in WGS84",
                "call": {
                    "tool": "buffer_layer",
                    "arguments": {
                        "input_path": "wells.gpkg",
                        "distance_meters": 500,
                        "output_path": "wells_500m.parquet",
                    },
                },
            },
            {
                "goal": "1 km influence area around metro stations",
                "call": {
                    "tool": "buffer_layer",
                    "arguments": {
                        "input_path": "stations.parquet",
                        "distance_meters": 1000,
                        "output_path": "stations_1km.parquet",
                    },
                },
            },
        ],
    },
    {
        "name": "clip_layer",
        "status": "available",
        "category": "vector",
        "summary": "Clip a layer with a mask layer (CRS-aligned automatically)",
        "description": (
            "Keep only the parts of the input that fall inside the mask layer's area. "
            "The mask is reprojected to the input CRS when they differ, and the decision "
            "is recorded in the provenance manifest. Inputs without a CRS are refused. "
            "An empty result is legitimate but reported: the result then carries a "
            "'warnings' list (with hints) instead of passing silently as a success."
        ),
        "parameters": [
            {
                "name": "input_path",
                "type": "str",
                "required": True,
                "description": "Vector dataset to clip",
            },
            {
                "name": "mask_path",
                "type": "str",
                "required": True,
                "description": "Polygon layer used as the clipping area",
            },
            {
                "name": "output_path",
                "type": "str",
                "required": True,
                "description": "Output path (.parquet or .gpkg)",
            },
        ],
        "examples": [
            {
                "goal": "Cut the national road network down to one municipality",
                "call": {
                    "tool": "clip_layer",
                    "arguments": {
                        "input_path": "roads.parquet",
                        "mask_path": "municipality.gpkg",
                        "output_path": "roads_city.parquet",
                    },
                },
            },
            {
                "goal": "Extract buildings inside a flood-risk zone",
                "call": {
                    "tool": "clip_layer",
                    "arguments": {
                        "input_path": "buildings.parquet",
                        "mask_path": "flood_zone.parquet",
                        "output_path": "buildings_at_risk.parquet",
                    },
                },
            },
        ],
    },
    {
        "name": "reproject_layer",
        "status": "available",
        "category": "vector",
        "summary": "Reproject a layer to a target CRS (EPSG code or WKT)",
        "description": (
            "Transform a dataset to a target coordinate reference system. Use it before "
            "combining layers that must share a CRS, or to move data into a metric CRS "
            "for measurement. Inputs without a CRS are rejected. Geometry is carried "
            "through unchanged, so invalid input geometry is repaired deterministically "
            "and reported in a 'repairs' key (the geometry type may change)."
        ),
        "parameters": [
            {
                "name": "input_path",
                "type": "str",
                "required": True,
                "description": "Vector dataset to reproject",
            },
            {
                "name": "target_crs",
                "type": "str",
                "required": True,
                "description": "Target CRS, e.g. 'EPSG:32632' or a WKT string",
            },
            {
                "name": "output_path",
                "type": "str",
                "required": True,
                "description": "Output path (.parquet or .gpkg)",
            },
        ],
        "examples": [
            {
                "goal": "Bring a WGS84 layer into UTM 32N for metric analysis",
                "call": {
                    "tool": "reproject_layer",
                    "arguments": {
                        "input_path": "parcels.gpkg",
                        "target_crs": "EPSG:32632",
                        "output_path": "parcels_utm.parquet",
                    },
                },
            },
            {
                "goal": "Publish results back in WGS84 for a web map",
                "call": {
                    "tool": "reproject_layer",
                    "arguments": {
                        "input_path": "result_utm.parquet",
                        "target_crs": "EPSG:4326",
                        "output_path": "result_wgs84.parquet",
                    },
                },
            },
        ],
    },
    {
        "name": "spatial_join",
        "status": "available",
        "category": "vector",
        "summary": "Join by spatial predicate (intersects/within/contains); auto-routed to "
        "SedonaDB or DuckDB for speed, GeoPandas fallback",
        "description": (
            "Attach attributes from one layer to another based on a spatial relationship "
            "(intersects, within, contains). engine='auto' routes to the fastest engine "
            "available for the inputs: SedonaDB (heavy joins) > DuckDB (GeoParquet fast "
            "path) > GeoPandas. The engine that actually ran is recorded in provenance. "
            "Inputs without a CRS are refused; an empty join, or inputs whose extents do "
            "not overlap, come back in a 'warnings' list with hints."
        ),
        "parameters": [
            {
                "name": "left_path",
                "type": "str",
                "required": True,
                "description": "Left layer (keeps all its attributes)",
            },
            {
                "name": "right_path",
                "type": "str",
                "required": True,
                "description": "Right layer providing the joined attributes",
            },
            {
                "name": "output_path",
                "type": "str",
                "required": True,
                "description": "Output path (.parquet or .gpkg)",
            },
            {
                "name": "predicate",
                "type": "str",
                "required": False,
                "description": "Spatial predicate: intersects (default), within, contains",
            },
            {
                "name": "engine",
                "type": "str",
                "required": False,
                "description": "auto (default), sedonadb, duckdb or geopandas",
            },
        ],
        "examples": [
            {
                "goal": "Tag every building with the census tract it falls in",
                "call": {
                    "tool": "spatial_join",
                    "arguments": {
                        "left_path": "buildings.parquet",
                        "right_path": "census_tracts.parquet",
                        "output_path": "buildings_tracts.parquet",
                        "predicate": "within",
                    },
                },
            },
            {
                "goal": "Find which roads cross protected areas",
                "call": {
                    "tool": "spatial_join",
                    "arguments": {
                        "left_path": "roads.parquet",
                        "right_path": "protected_areas.parquet",
                        "output_path": "roads_protected.parquet",
                    },
                },
            },
            {
                "goal": "Force the GeoPandas engine on a small GeoPackage join",
                "call": {
                    "tool": "spatial_join",
                    "arguments": {
                        "left_path": "sites.gpkg",
                        "right_path": "zones.gpkg",
                        "output_path": "sites_zones.gpkg",
                        "engine": "geopandas",
                    },
                },
            },
        ],
    },
    {
        "name": "run_sql",
        "status": "available",
        "category": "sql",
        "summary": "Spatial SQL (DuckDB dialect, ST_* functions) over GeoParquet and GDAL "
        "formats; materializes GeoParquet outputs with provenance",
        "description": (
            "Run spatial SQL in the DuckDB dialect: read_parquet('file.parquet') for "
            "GeoParquet, ST_Read('file.gpkg') for GDAL formats, full ST_* function set. "
            "Without output_path it returns up to 50 preview rows; with output_path "
            "(.parquet) it materializes the full result as GeoParquet with a provenance "
            "manifest. Use it for filters, aggregations and anything without a dedicated "
            "tool."
        ),
        "parameters": [
            {
                "name": "query",
                "type": "str",
                "required": True,
                "description": "SQL query (DuckDB spatial dialect)",
            },
            {
                "name": "output_path",
                "type": "str",
                "required": False,
                "description": "If set (.parquet), materialize the full result with provenance",
            },
        ],
        "examples": [
            {
                "goal": "Preview the 10 largest parcels",
                "call": {
                    "tool": "run_sql",
                    "arguments": {
                        "query": "SELECT id, ST_Area(geometry) AS area FROM "
                        "read_parquet('parcels.parquet') ORDER BY area DESC LIMIT 10"
                    },
                },
            },
            {
                "goal": "Materialize all buildings taller than 30 m as GeoParquet",
                "call": {
                    "tool": "run_sql",
                    "arguments": {
                        "query": "SELECT * FROM read_parquet('buildings.parquet') "
                        "WHERE height > 30",
                        "output_path": "tall_buildings.parquet",
                    },
                },
            },
            {
                "goal": "Aggregate accident counts per district from a GeoPackage",
                "call": {
                    "tool": "run_sql",
                    "arguments": {
                        "query": "SELECT district, COUNT(*) AS n FROM "
                        "ST_Read('accidents.gpkg') GROUP BY district"
                    },
                },
            },
        ],
    },
    {
        "name": "zonal_statistics",
        "status": "available",
        "category": "raster",
        "summary": "Statistics of a raster within vector zones via exactextract "
        "(exact fractional pixel coverage); requires the [raster] extra",
        "description": (
            "Compute statistics of a single-band raster inside each polygon of a zones "
            "layer, with exact fractional pixel coverage (no all-in/all-out pixel "
            "approximation). Zones are aligned to the raster CRS automatically and the "
            "decision is recorded in provenance. Output is the zones layer plus one "
            "column per statistic. Requires: pip install mapsmith[raster]."
        ),
        "parameters": [
            {
                "name": "raster_path",
                "type": "str",
                "required": True,
                "description": "Single-band raster (GeoTIFF or any rasterio-readable format)",
            },
            {
                "name": "zones_path",
                "type": "str",
                "required": True,
                "description": "Polygon zones layer (must have a CRS)",
            },
            {
                "name": "output_path",
                "type": "str",
                "required": True,
                "description": "Output path (.parquet or .gpkg)",
            },
            {
                "name": "stats",
                "type": "list[str]",
                "required": False,
                "description": "Subset of count/sum/mean/median/min/max/stdev/variance/"
                "majority/minority/variety (default: count, mean, min, max). "
                "Note: 'stdev', not 'std'",
            },
        ],
        "examples": [
            {
                "goal": "Mean elevation per watershed from a DEM",
                "call": {
                    "tool": "zonal_statistics",
                    "arguments": {
                        "raster_path": "dem.tif",
                        "zones_path": "watersheds.parquet",
                        "output_path": "watershed_elevation.parquet",
                        "stats": ["mean", "min", "max"],
                    },
                },
            },
            {
                "goal": "Population sum per municipality from a population grid",
                "call": {
                    "tool": "zonal_statistics",
                    "arguments": {
                        "raster_path": "population.tif",
                        "zones_path": "municipalities.gpkg",
                        "output_path": "pop_by_muni.parquet",
                        "stats": ["sum"],
                    },
                },
            },
        ],
    },
    {
        "name": "get_provenance",
        "status": "available",
        "category": "provenance",
        "summary": "Full lineage manifest of any MapSmith output",
        "description": (
            "Return the complete lineage manifest of a dataset MapSmith wrote: inputs "
            "with sha256, exact parameters, CRS decisions with reasons, engine and "
            "version, deterministic verification results, timestamps. Use it to audit "
            "or explain any result."
        ),
        "parameters": [
            {
                "name": "output_path",
                "type": "str",
                "required": True,
                "description": "Path of a dataset previously written by MapSmith",
            },
        ],
        "examples": [
            {
                "goal": "Audit how a joined dataset was produced",
                "call": {
                    "tool": "get_provenance",
                    "arguments": {"output_path": "buildings_tracts.parquet"},
                },
            },
            {
                "goal": "Show the user which engine and CRS decisions produced a buffer",
                "call": {
                    "tool": "get_provenance",
                    "arguments": {"output_path": "wells_500m.parquet"},
                },
            },
        ],
    },
    {
        "name": "hillshade",
        "status": "available",
        "category": "terrain",
        "summary": "Shaded relief from a DEM (Whitebox engine, in-memory); "
        "requires the [whitebox] extra",
        "description": (
            "Compute a shaded-relief raster from a digital elevation model. Output "
            "values are scaled 0-32767; nodata cells are preserved. The DEM must have "
            "a CRS (rejected otherwise). Runs in memory on the Whitebox Next Gen "
            "engine. Requires: pip install mapsmith[whitebox]."
        ),
        "parameters": [
            {
                "name": "dem_path",
                "type": "str",
                "required": True,
                "description": "Digital elevation model (GeoTIFF, must have a CRS)",
            },
            {
                "name": "output_path",
                "type": "str",
                "required": True,
                "description": "Output GeoTIFF path",
            },
            {
                "name": "azimuth",
                "type": "float",
                "required": False,
                "description": "Sun direction in degrees, 0-360 (default 315 = NW)",
            },
            {
                "name": "altitude",
                "type": "float",
                "required": False,
                "description": "Sun angle above the horizon in degrees, 0-90 (default 30)",
            },
            {
                "name": "z_factor",
                "type": "float",
                "required": False,
                "description": "Vertical exaggeration (default 1.0)",
            },
        ],
        "examples": [
            {
                "goal": "Classic NW-lit hillshade for a basemap",
                "call": {
                    "tool": "hillshade",
                    "arguments": {"dem_path": "dem.tif", "output_path": "hillshade.tif"},
                },
            },
            {
                "goal": "Low morning sun from the east to emphasize subtle relief",
                "call": {
                    "tool": "hillshade",
                    "arguments": {
                        "dem_path": "dem.tif",
                        "output_path": "hillshade_east.tif",
                        "azimuth": 90,
                        "altitude": 15,
                    },
                },
            },
        ],
    },
    {
        "name": "slope",
        "status": "available",
        "category": "terrain",
        "summary": "Slope gradient from a DEM in degrees, percent or radians "
        "(Whitebox engine); requires the [whitebox] extra",
        "description": (
            "Compute the slope gradient of a digital elevation model "
            "(Zevenbergen-Thorne). Units: degrees (default), percent or radians. "
            "DEMs in a geographic CRS are refused — degree cells with meter "
            "elevations give plausible but wrong values everywhere; reproject to a "
            "projected CRS first. The CRS decision is recorded in the provenance "
            "manifest. Requires: pip install mapsmith[whitebox]."
        ),
        "parameters": [
            {
                "name": "dem_path",
                "type": "str",
                "required": True,
                "description": "Digital elevation model (GeoTIFF, projected CRS required)",
            },
            {
                "name": "output_path",
                "type": "str",
                "required": True,
                "description": "Output GeoTIFF path",
            },
            {
                "name": "units",
                "type": "str",
                "required": False,
                "description": "degrees (default), percent or radians",
            },
            {
                "name": "z_factor",
                "type": "float",
                "required": False,
                "description": "Vertical unit conversion factor (default 1.0)",
            },
        ],
        "examples": [
            {
                "goal": "Slope in degrees for a landslide-susceptibility analysis",
                "call": {
                    "tool": "slope",
                    "arguments": {"dem_path": "dem_utm.tif", "output_path": "slope.tif"},
                },
            },
            {
                "goal": "Slope in percent for road-grade screening",
                "call": {
                    "tool": "slope",
                    "arguments": {
                        "dem_path": "dem_utm.tif",
                        "output_path": "slope_pct.tif",
                        "units": "percent",
                    },
                },
            },
        ],
    },
    {
        "name": "aspect",
        "status": "available",
        "category": "terrain",
        "summary": "Aspect from a DEM: downslope azimuth in degrees, 0 = north, "
        "flat cells = -1 (Whitebox engine); requires the [whitebox] extra",
        "description": (
            "Compute the aspect of a digital elevation model: the azimuth of the "
            "downslope direction in degrees, 0 = north, 90 = east. FLAT CELLS ARE "
            "ENCODED AS -1, not as nodata — mask them before averaging aspect over "
            "an area, or the average is plausibly wrong. DEMs in a geographic CRS "
            "are refused (see slope). Requires: pip install mapsmith[whitebox]."
        ),
        "parameters": [
            {
                "name": "dem_path",
                "type": "str",
                "required": True,
                "description": "Digital elevation model (GeoTIFF, projected CRS required)",
            },
            {
                "name": "output_path",
                "type": "str",
                "required": True,
                "description": "Output GeoTIFF path",
            },
            {
                "name": "z_factor",
                "type": "float",
                "required": False,
                "description": "Vertical unit conversion factor (default 1.0)",
            },
        ],
        "examples": [
            {
                "goal": "South-facing slopes for a solar-potential study",
                "call": {
                    "tool": "aspect",
                    "arguments": {"dem_path": "dem_utm.tif", "output_path": "aspect.tif"},
                },
            },
            {
                "goal": "Exposure classes for a vegetation model",
                "call": {
                    "tool": "aspect",
                    "arguments": {"dem_path": "dem_utm.tif", "output_path": "exposure.tif"},
                },
            },
        ],
    },
    {
        "name": "flow_accumulation",
        "status": "available",
        "category": "hydrology",
        "summary": "D8 flow accumulation from a DEM with automatic depression filling; "
        "requires the [whitebox] extra",
        "description": (
            "Number of upslope cells draining through each cell (D8 routing). "
            "Depressions are filled first and the preprocessing is recorded in "
            "provenance. out_type 'cells' counts cells (each cell counts itself, so "
            "values run from 1 to the grid size); 'sca' gives specific catchment "
            "area. Requires: pip install mapsmith[whitebox]."
        ),
        "parameters": [
            {
                "name": "dem_path",
                "type": "str",
                "required": True,
                "description": "Digital elevation model (GeoTIFF, must have a CRS)",
            },
            {
                "name": "output_path",
                "type": "str",
                "required": True,
                "description": "Output GeoTIFF path",
            },
            {
                "name": "out_type",
                "type": "str",
                "required": False,
                "description": "'cells' (default) or 'sca' (specific catchment area)",
            },
            {
                "name": "log_transform",
                "type": "bool",
                "required": False,
                "description": "Natural-log transform for visualization (default false)",
            },
        ],
        "examples": [
            {
                "goal": "Find where streams concentrate on a DEM",
                "call": {
                    "tool": "flow_accumulation",
                    "arguments": {"dem_path": "dem.tif", "output_path": "flowacc.tif"},
                },
            },
            {
                "goal": "Log-scaled accumulation for a drainage-network visualization",
                "call": {
                    "tool": "flow_accumulation",
                    "arguments": {
                        "dem_path": "dem.tif",
                        "output_path": "flowacc_log.tif",
                        "log_transform": True,
                    },
                },
            },
        ],
    },
    {
        "name": "watershed",
        "status": "available",
        "category": "hydrology",
        "summary": "Watershed delineation from a DEM and pour points (Whitebox engine); "
        "requires the [whitebox] extra",
        "description": (
            "Delineate the watershed draining to each pour point. Basins get 1-based "
            "IDs following the pour-point feature order; cells not draining to any "
            "point stay nodata. Pour points (any GeoPandas-readable format) are "
            "aligned to the DEM CRS automatically with the decision recorded. "
            "Depressions are filled before flow routing. "
            "Requires: pip install mapsmith[whitebox]."
        ),
        "parameters": [
            {
                "name": "dem_path",
                "type": "str",
                "required": True,
                "description": "Digital elevation model (GeoTIFF, must have a CRS)",
            },
            {
                "name": "pour_points_path",
                "type": "str",
                "required": True,
                "description": "Point layer of outlets (must have a CRS)",
            },
            {
                "name": "output_path",
                "type": "str",
                "required": True,
                "description": "Output GeoTIFF path (basin IDs, nodata outside)",
            },
        ],
        "examples": [
            {
                "goal": "Catchment upstream of a gauging station",
                "call": {
                    "tool": "watershed",
                    "arguments": {
                        "dem_path": "dem.tif",
                        "pour_points_path": "station.gpkg",
                        "output_path": "catchment.tif",
                    },
                },
            },
            {
                "goal": "Drainage basins of several dam sites at once",
                "call": {
                    "tool": "watershed",
                    "arguments": {
                        "dem_path": "dem.tif",
                        "pour_points_path": "dam_sites.parquet",
                        "output_path": "basins.tif",
                    },
                },
            },
        ],
    },
    {
        "name": "validate_plan",
        "status": "available",
        "category": "planning",
        "summary": "Static validation of a multi-step plan before execution: operations, "
        "arguments, references, input files, simulated CRS flow",
        "description": (
            "Check a multi-step geoprocessing plan without running anything: every "
            "operation exists and is installed, arguments are complete and well-typed, "
            "'$step_id' references resolve backwards (mis-ordered steps rejected), "
            "input files exist, outputs don't collide, and the CRS of every "
            "intermediate dataset is simulated from the real inputs. Errors carry "
            "stable codes and name the exact step, so a planner can repair the plan "
            "and retry. Always validate before execute_plan."
        ),
        "parameters": [
            {
                "name": "plan",
                "type": "object",
                "required": True,
                "description": "{goal, steps: [{id, operation, arguments, comment?}]}; "
                "'$step_id' argument values consume earlier outputs",
            },
        ],
        "examples": [
            {
                "goal": "Check a buffer+clip pipeline before running it",
                "call": {
                    "tool": "validate_plan",
                    "arguments": {
                        "plan": {
                            "goal": "buildings within 300 m of rivers",
                            "steps": [
                                {
                                    "id": "buf",
                                    "operation": "buffer_layer",
                                    "arguments": {
                                        "input_path": "rivers.gpkg",
                                        "distance_meters": 300,
                                        "output_path": "rivers_300m.parquet",
                                    },
                                },
                                {
                                    "id": "cut",
                                    "operation": "clip_layer",
                                    "arguments": {
                                        "input_path": "buildings.parquet",
                                        "mask_path": "$buf",
                                        "output_path": "buildings_near_rivers.parquet",
                                    },
                                },
                            ],
                        }
                    },
                },
            },
            {
                "goal": "Verify CRS assumptions of a terrain pipeline",
                "call": {
                    "tool": "validate_plan",
                    "arguments": {
                        "plan": {
                            "steps": [
                                {
                                    "id": "acc",
                                    "operation": "flow_accumulation",
                                    "arguments": {
                                        "dem_path": "dem.tif",
                                        "output_path": "acc.tif",
                                        "out_type": "sca",
                                    },
                                }
                            ]
                        }
                    },
                },
            },
        ],
    },
    {
        "name": "execute_plan",
        "status": "available",
        "category": "planning",
        "summary": "Validate then run a multi-step plan; per-step provenance plus a "
        "plan-level manifest tying the chain together",
        "description": (
            "Run a validated plan step by step (an invalid plan runs nothing — "
            "validation is repeated internally). '$step_id' references resolve to "
            "earlier outputs. Every step writes its own provenance manifest; a "
            "plan-level manifest (<last output>.plan.json) records the plan sha256, "
            "goal, per-step outcomes and timings. Stops at the first failing step, "
            "keeping earlier outputs and manifests on disk."
        ),
        "parameters": [
            {
                "name": "plan",
                "type": "object",
                "required": True,
                "description": "Same format as validate_plan",
            },
        ],
        "examples": [
            {
                "goal": "Run a two-step buffer+clip pipeline with full lineage",
                "call": {
                    "tool": "execute_plan",
                    "arguments": {
                        "plan": {
                            "goal": "wells inside the flood zone",
                            "steps": [
                                {
                                    "id": "buf",
                                    "operation": "buffer_layer",
                                    "arguments": {
                                        "input_path": "wells.gpkg",
                                        "distance_meters": 500,
                                        "output_path": "wells_500m.parquet",
                                    },
                                },
                                {
                                    "id": "cut",
                                    "operation": "clip_layer",
                                    "arguments": {
                                        "input_path": "$buf",
                                        "mask_path": "flood_zone.parquet",
                                        "output_path": "wells_at_risk.parquet",
                                    },
                                },
                            ],
                        }
                    },
                },
            },
            {
                "goal": "DEM to watershed statistics in one verified chain",
                "call": {
                    "tool": "execute_plan",
                    "arguments": {
                        "plan": {
                            "steps": [
                                {
                                    "id": "ws",
                                    "operation": "watershed",
                                    "arguments": {
                                        "dem_path": "dem.tif",
                                        "pour_points_path": "outlets.gpkg",
                                        "output_path": "basins.tif",
                                    },
                                },
                                {
                                    "id": "stats",
                                    "operation": "zonal_statistics",
                                    "arguments": {
                                        "raster_path": "dem.tif",
                                        "zones_path": "catchments.parquet",
                                        "output_path": "basin_elevation.parquet",
                                        "stats": ["mean", "max"],
                                    },
                                },
                            ]
                        }
                    },
                },
            },
        ],
    },
    {
        "name": "preview_map",
        "status": "available",
        "category": "visualization",
        "summary": "Interactive in-chat map of one or more datasets (MCP Apps panel) "
        "with per-layer provenance and verification status",
        "description": (
            "Show vector datasets and GeoTIFFs on the interactive map panel rendered "
            "inside the chat (MCP Apps). Layers are previewed in EPSG:4326 with "
            "simplified geometries and capped feature counts sized to client limits; "
            "each layer card shows what produced it (operation, engine, verified "
            "status from the provenance manifest). Read-only preview: the datasets "
            "of record stay untouched on disk. Use it after an analysis to let the "
            "user SEE the result."
        ),
        "parameters": [
            {
                "name": "paths",
                "type": "list[str]",
                "required": True,
                "description": "Dataset paths to show (vector formats or .tif)",
            },
            {
                "name": "max_features",
                "type": "int",
                "required": False,
                "description": "Per-layer feature cap before simplification (default 2000)",
            },
        ],
        "examples": [
            {
                "goal": "Show the result of a buffer+clip analysis with its inputs",
                "call": {
                    "tool": "preview_map",
                    "arguments": {"paths": ["wells.gpkg", "wells_at_risk.parquet"]},
                },
            },
            {
                "goal": "Inspect a hillshade next to the watershed that was derived from it",
                "call": {
                    "tool": "preview_map",
                    "arguments": {"paths": ["hillshade.tif", "basins.tif"]},
                },
            },
        ],
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

# --- Okapi BM25 over the catalog (deterministic, no dependencies) ---------

_K1 = 1.5  # term-frequency saturation
_B = 0.75  # document-length normalization
_TOKEN = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def _document(op: dict[str, Any]) -> list[str]:
    """Flatten one catalog entry into a token list (name and category weighted)."""
    parts = [op["name"]] * 3 + [op["category"]] * 2 + [op["summary"]]
    parts.append(op.get("description", ""))
    for param in op.get("parameters", []):
        parts.append(param["name"])
        parts.append(param.get("description", ""))
    for example in op.get("examples", []):
        parts.append(example.get("goal", ""))
    return _tokenize(" ".join(parts))


def bm25_scores(query_tokens: list[str], documents: list[list[str]]) -> list[float]:
    """Okapi BM25 scores per document (Lucene-style ln(1+x) idf, always non-negative)."""
    n_docs = len(documents)
    if n_docs == 0:
        return []
    avg_len = sum(len(d) for d in documents) / n_docs
    doc_freq: dict[str, int] = {}
    for doc in documents:
        for term in set(doc):
            doc_freq[term] = doc_freq.get(term, 0) + 1
    scores = []
    for doc in documents:
        score = 0.0
        length_norm = _K1 * (1 - _B + _B * len(doc) / avg_len) if avg_len else _K1
        # sorted: float addition is not associative, so a hash-ordered iteration
        # could make scores differ across processes — determinism is the brand.
        for term in sorted(set(query_tokens)):
            tf = doc.count(term)
            if tf == 0:
                continue
            idf = math.log((n_docs - doc_freq[term] + 0.5) / (doc_freq[term] + 0.5) + 1)
            score += idf * tf * (_K1 + 1) / (tf + length_norm)
        scores.append(score)
    return scores


def rank(query: str, limit: int = 10) -> list[tuple[dict[str, Any], float]]:
    """Catalog entries ranked by BM25 relevance; zero-score entries are dropped."""
    query_tokens = _tokenize(query)
    if not query_tokens:
        return [(op, 0.0) for op in OPERATIONS]
    scores = bm25_scores(query_tokens, [_document(op) for op in OPERATIONS])
    ranked = sorted(
        ((op, s) for op, s in zip(OPERATIONS, scores) if s > 0),
        key=lambda pair: (-pair[1], pair[0]["name"]),
    )
    return ranked[:limit]


def _compact(op: dict[str, Any]) -> dict[str, Any]:
    return {k: op[k] for k in ("name", "status", "category", "summary")}


def search(query: str = "", limit: int = 10, detail: bool = False) -> list[dict[str, Any]]:
    """Search the catalog. Compact entries by default; detail=True adds parameters/examples.

    Empty query lists the whole catalog (roadmap included). With a query, results
    are BM25-ranked and carry a ``score`` field.
    """
    if not query.strip():
        return [dict(op) if detail else _compact(op) for op in OPERATIONS]
    results = []
    for op, score in rank(query, limit=limit):
        entry = dict(op) if detail else _compact(op)
        entry["score"] = round(score, 4)
        results.append(entry)
    return results


def describe_operation(name: str) -> dict[str, Any]:
    """Full structured doc of one operation by exact name (helpful error otherwise)."""
    for op in OPERATIONS:
        if op["name"] == name:
            return dict(op)
    suggestions = [op["name"] for op, _ in rank(name, limit=3)]
    hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
    raise ValueError(f"Unknown operation '{name}'.{hint} Use list_operations to search.")
