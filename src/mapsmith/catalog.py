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
        "tool": "describe_dataset",
        "workload": "small_vector",
        "category": "inspection",
        "applicability": {"inputs": ["dataset"], "requires_projected_crs": False},
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
        "tool": "buffer_layer",
        "workload": "small_vector",
        "category": "vector",
        "applicability": {"inputs": ["vector"], "requires_projected_crs": False},
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
        "tool": "clip_layer",
        "workload": "heavy_join",
        "category": "vector",
        "applicability": {"inputs": ["vector", "vector"], "requires_projected_crs": False},
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
        "name": "overlay_layers",
        "status": "available",
        "tool": "overlay_layers",
        "workload": "heavy_join",
        "category": "vector",
        "applicability": {"inputs": ["vector", "vector"], "requires_projected_crs": False},
        "summary": "Set-theoretic overlay of two layers: intersection, union, identity, "
        "symmetric_difference, difference (CRS-aligned automatically)",
        "description": (
            "Combine two layers set-theoretically. how: intersection (default), union, "
            "identity, symmetric_difference or difference. The overlay layer is "
            "reprojected to the input CRS when they differ, with the decision recorded "
            "in the provenance manifest. Overlay pieces of lower dimension than the "
            "inputs (shared edges, corner contacts) are dropped and the manifest says "
            "so. Inputs without a CRS are refused; an empty result carries a 'warnings' "
            "entry instead of passing as a silent success."
        ),
        "parameters": [
            {
                "name": "input_path",
                "type": "str",
                "required": True,
                "description": "First layer (its CRS wins)",
            },
            {
                "name": "overlay_path",
                "type": "str",
                "required": True,
                "description": "Second layer, reprojected to the input CRS if needed",
            },
            {
                "name": "output_path",
                "type": "str",
                "required": True,
                "description": "Output path (.parquet or .gpkg)",
            },
            {
                "name": "how",
                "type": "str",
                "required": False,
                "description": "intersection (default), union, identity, "
                "symmetric_difference, difference",
            },
        ],
        "examples": [
            {
                "goal": "Cropland that falls inside a flood-risk zone",
                "call": {
                    "tool": "overlay_layers",
                    "arguments": {
                        "input_path": "cropland.parquet",
                        "overlay_path": "flood_zone.parquet",
                        "output_path": "cropland_at_risk.parquet",
                    },
                },
            },
            {
                "goal": "Municipal area NOT covered by any protected area",
                "call": {
                    "tool": "overlay_layers",
                    "arguments": {
                        "input_path": "municipality.gpkg",
                        "overlay_path": "protected_areas.parquet",
                        "output_path": "unprotected.parquet",
                        "how": "difference",
                    },
                },
            },
        ],
    },
    {
        "name": "dissolve_layer",
        "status": "available",
        "tool": "dissolve_layer",
        "workload": "sql",
        "category": "vector",
        "applicability": {"inputs": ["vector"], "requires_projected_crs": False},
        "summary": "Merge features into one geometry per key, with the aggregation "
        "recorded in the manifest and the group count verified",
        "description": (
            "Dissolve a layer: one output feature per distinct value of `by` (or one "
            "feature in all, with no key). aggfunc — first (default), last, sum, mean, "
            "median, min, max, count — is applied to the other columns and RECORDED in "
            "the provenance manifest, because a sum reported where a mean was meant is "
            "a plausible wrong number nobody can see. Features with a null key are "
            "dropped by the grouping and counted in the manifest. The output feature "
            "count is verified against the number of distinct keys."
        ),
        "parameters": [
            {
                "name": "input_path",
                "type": "str",
                "required": True,
                "description": "Layer to dissolve (must have a CRS)",
            },
            {
                "name": "output_path",
                "type": "str",
                "required": True,
                "description": "Output path (.parquet or .gpkg)",
            },
            {
                "name": "by",
                "type": "str",
                "required": False,
                "description": "Column to group by; omit to merge everything into one "
                "feature",
            },
            {
                "name": "aggfunc",
                "type": "str",
                "required": False,
                "description": "first (default), last, sum, mean, median, min, max, count",
            },
        ],
        "examples": [
            {
                "goal": "Merge census tracts into districts, summing population",
                "call": {
                    "tool": "dissolve_layer",
                    "arguments": {
                        "input_path": "tracts.parquet",
                        "output_path": "districts.parquet",
                        "by": "district",
                        "aggfunc": "sum",
                    },
                },
            },
            {
                "goal": "One national boundary from all municipal polygons",
                "call": {
                    "tool": "dissolve_layer",
                    "arguments": {
                        "input_path": "municipalities.gpkg",
                        "output_path": "country.parquet",
                    },
                },
            },
        ],
    },
    {
        "name": "nearest_join",
        "status": "available",
        "tool": "nearest_join",
        "workload": "heavy_join",
        "category": "vector",
        "applicability": {"inputs": ["vector", "vector"], "requires_projected_crs": False},
        "summary": "Nearest-neighbour join with the distance in meters in a named column "
        "(UTM-measured on geographic CRS, decision recorded)",
        "description": (
            "Attach each feature's nearest neighbour from another layer, with the "
            "distance IN METERS in a named column. Geographic-CRS inputs are measured "
            "in an estimated UTM zone and returned in the input CRS, with the decision "
            "recorded in the provenance manifest — a nearest distance in degrees is the "
            "classic silent error of this operation. max_distance_meters drops pairs "
            "farther than that; an emptied result carries a 'warnings' entry."
        ),
        "parameters": [
            {
                "name": "left_path",
                "type": "str",
                "required": True,
                "description": "Layer whose features receive their nearest neighbour",
            },
            {
                "name": "right_path",
                "type": "str",
                "required": True,
                "description": "Layer providing the neighbours",
            },
            {
                "name": "output_path",
                "type": "str",
                "required": True,
                "description": "Output path (.parquet or .gpkg)",
            },
            {
                "name": "max_distance_meters",
                "type": "float",
                "required": False,
                "description": "Drop pairs farther apart than this (meters, always)",
            },
            {
                "name": "distance_column",
                "type": "str",
                "required": False,
                "description": "Name of the distance column (default nearest_distance_m)",
            },
        ],
        "examples": [
            {
                "goal": "Nearest hospital for every school, with the distance",
                "call": {
                    "tool": "nearest_join",
                    "arguments": {
                        "left_path": "schools.parquet",
                        "right_path": "hospitals.parquet",
                        "output_path": "schools_hospital.parquet",
                    },
                },
            },
            {
                "goal": "Wells within 500 m of a river, river attributes attached",
                "call": {
                    "tool": "nearest_join",
                    "arguments": {
                        "left_path": "wells.gpkg",
                        "right_path": "rivers.parquet",
                        "output_path": "wells_near_rivers.parquet",
                        "max_distance_meters": 500,
                    },
                },
            },
        ],
    },
    {
        "name": "explode_layer",
        "status": "available",
        "tool": "explode_layer",
        "workload": "small_vector",
        "category": "vector",
        "applicability": {"inputs": ["vector"], "requires_projected_crs": False},
        "summary": "Split multi-part geometries into one feature per part, "
        "with the part count verified",
        "description": (
            "Split every multi-part geometry into one feature per part, copying the "
            "attributes. The output feature count is verified against the number of "
            "parts counted before the engine ran, so a lost part fails loudly instead "
            "of shipping. Inputs without a CRS are refused."
        ),
        "parameters": [
            {
                "name": "input_path",
                "type": "str",
                "required": True,
                "description": "Layer to explode (must have a CRS)",
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
                "goal": "One row per island from a multipolygon country layer",
                "call": {
                    "tool": "explode_layer",
                    "arguments": {
                        "input_path": "countries.parquet",
                        "output_path": "islands.parquet",
                    },
                },
            },
            {
                "goal": "Split multiline rivers into individual segments",
                "call": {
                    "tool": "explode_layer",
                    "arguments": {
                        "input_path": "rivers.gpkg",
                        "output_path": "river_segments.parquet",
                    },
                },
            },
        ],
    },
    {
        "name": "measure_area",
        "status": "available",
        "tool": "measure_area",
        "workload": "sql",
        "category": "vector",
        "applicability": {"inputs": ["vector"], "requires_projected_crs": False},
        "summary": "Area per feature in square metres — ground (ellipsoidal) or "
        "planar with the CRS's own unit, with the distortion checked",
        "description": (
            "Measure the area of every feature in square metres, written to a named "
            "column, with the total in the result. method='geodesic' (default) "
            "measures ground area on the ellipsoid the layer's CRS names, so no map "
            "plane and no projection distortion enter. method='planar' measures in "
            "the layer's own CRS and converts with its declared linear unit — a "
            "layer in US survey feet is never assumed to be in metres — and is "
            "refused on a geographic CRS, where an area would be in square degrees. "
            "Invalid geometry is repaired before measuring, because the planar area "
            "of a self-intersecting ring is the signed shoelace: a number that "
            "matches no region and comes back without complaint. A planar result is "
            "compared against the ground area, so a plane that is not equal-area at "
            "this location returns a warning carrying the ratio."
        ),
        "parameters": [
            {
                "name": "input_path",
                "type": "str",
                "required": True,
                "description": "Vector dataset to measure (must have a CRS)",
            },
            {
                "name": "output_path",
                "type": "str",
                "required": True,
                "description": "Output path (.parquet or .gpkg) with the area column added",
            },
            {
                "name": "method",
                "type": "str",
                "required": False,
                "description": "'geodesic' (default, ground area on the ellipsoid) or "
                "'planar' (the layer's own plane, converted from its linear unit)",
            },
            {
                "name": "area_column",
                "type": "str",
                "required": False,
                "description": "Name of the column to write (default 'area_m2')",
            },
        ],
        "examples": [
            {
                "goal": "How large are these parcels on the ground?",
                "call": {
                    "tool": "measure_area",
                    "arguments": {
                        "input_path": "parcels.gpkg",
                        "output_path": "parcels_measured.parquet",
                    },
                },
            },
            {
                "goal": "Area in the cadastral plane of a layer stored in US survey feet",
                "call": {
                    "tool": "measure_area",
                    "arguments": {
                        "input_path": "parcels_stateplane.gpkg",
                        "output_path": "parcels_planar.parquet",
                        "method": "planar",
                    },
                },
            },
        ],
    },
    {
        "name": "merge_layers",
        "status": "available",
        "tool": "merge_layers",
        "workload": "heavy_join",
        "category": "vector",
        "applicability": {"inputs": ["vector", "vector"], "requires_projected_crs": False},
        "summary": "Append two or more layers into one, schema union, "
        "count verified against the sum",
        "description": (
            "Append two or more vector layers into a single dataset. Attributes are "
            "aligned by column name (schema union); columns present in only some "
            "inputs are null-filled in the others and the manifest names them. "
            "Layers are reprojected to the first layer's CRS when they differ, with "
            "the decision recorded. The output feature count is verified against "
            "the sum of the input counts. This is an append, not a geometric union: "
            "use dissolve_layer afterwards to merge geometries."
        ),
        "parameters": [
            {
                "name": "input_paths",
                "type": "list[str]",
                "required": True,
                "description": "Two or more vector datasets to append (each must have a CRS)",
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
                "goal": "Combine three regional road layers into one national layer",
                "call": {
                    "tool": "merge_layers",
                    "arguments": {
                        "input_paths": ["roads_north.gpkg", "roads_centre.gpkg", "roads_south.gpkg"],
                        "output_path": "roads_all.parquet",
                    },
                },
            },
            {
                "goal": "Stack this year's and last year's survey points",
                "call": {
                    "tool": "merge_layers",
                    "arguments": {
                        "input_paths": ["survey_2025.parquet", "survey_2026.parquet"],
                        "output_path": "survey_both.parquet",
                    },
                },
            },
        ],
    },
    {
        "name": "simplify_layer",
        "status": "available",
        "tool": "simplify_layer",
        "workload": "small_vector",
        "category": "vector",
        "applicability": {"inputs": ["vector"], "requires_projected_crs": False},
        "summary": "Simplify geometries with the drift measured: area and length "
        "before/after recorded in the manifest",
        "description": (
            "Reduce vertex counts with Douglas-Peucker simplification, topology "
            "preserved. Simplification moves boundaries, so the manifest records "
            "total area and total length before and after with the drift percentage "
            "— measured, never assumed away. Geographic-CRS inputs are simplified "
            "in an estimated UTM zone (decision recorded) and returned in the input "
            "CRS; on projected CRS the tolerance is interpreted in the CRS units. "
            "The feature count is verified unchanged. Inputs without a CRS are "
            "refused."
        ),
        "parameters": [
            {
                "name": "input_path",
                "type": "str",
                "required": True,
                "description": "Layer to simplify (must have a CRS)",
            },
            {
                "name": "tolerance_meters",
                "type": "float",
                "required": True,
                "description": "Maximum deviation from the original geometry, in "
                "meters (CRS units on a projected CRS)",
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
                "goal": "Lighten a parcel layer for a web map, keeping shapes within 5 m",
                "call": {
                    "tool": "simplify_layer",
                    "arguments": {
                        "input_path": "parcels.parquet",
                        "tolerance_meters": 5,
                        "output_path": "parcels_light.parquet",
                    },
                },
            },
            {
                "goal": "Generalise coastline detail below 100 m before printing",
                "call": {
                    "tool": "simplify_layer",
                    "arguments": {
                        "input_path": "coastline.gpkg",
                        "tolerance_meters": 100,
                        "output_path": "coastline_100m.parquet",
                    },
                },
            },
        ],
    },
    {
        "name": "centroid_layer",
        "status": "available",
        "tool": "centroid_layer",
        "workload": "small_vector",
        "category": "vector",
        "applicability": {"inputs": ["vector"], "requires_projected_crs": False},
        "summary": "One point per feature: geometric centroids computed in a "
        "metric CRS, never on degrees",
        "description": (
            "Replace each geometry with its geometric centroid. Geographic-CRS "
            "inputs are measured in an estimated UTM zone (decision recorded in "
            "the manifest) and returned in the input CRS — a planar centroid of "
            "degree coordinates lands in the wrong place, quietly. Output verified: "
            "same feature count, Point geometry, input CRS. The manifest carries "
            "the caveat that a concave or multi-part feature's centroid can fall "
            "outside the feature. Inputs without a CRS are refused."
        ),
        "parameters": [
            {
                "name": "input_path",
                "type": "str",
                "required": True,
                "description": "Layer whose features to reduce to centroids (must have a CRS)",
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
                "goal": "Label points for a polygon layer of municipalities",
                "call": {
                    "tool": "centroid_layer",
                    "arguments": {
                        "input_path": "municipalities.gpkg",
                        "output_path": "municipality_points.parquet",
                    },
                },
            },
            {
                "goal": "Turn building footprints into points for a density analysis",
                "call": {
                    "tool": "centroid_layer",
                    "arguments": {
                        "input_path": "buildings.parquet",
                        "output_path": "building_points.parquet",
                    },
                },
            },
        ],
    },
    {
        "name": "convert_format",
        "status": "available",
        "tool": "convert_format",
        "workload": "small_vector",
        "category": "vector",
        "applicability": {"inputs": ["vector"], "requires_projected_crs": False},
        "summary": "Convert between vector formats, re-read and verified; "
        "lossy conversions are refused with the reason",
        "description": (
            "Convert a vector dataset to the format named by the output extension: "
            ".parquet (GeoParquet, canonical), .gpkg (GeoPackage) or .geojson. The "
            "output is re-read and verified: same feature count, same CRS. Two "
            "conversions are refused with the reason instead of performed lossily: "
            "shapefile output (field names silently truncated to 10 characters) and "
            "GeoJSON for non-WGS84 layers (RFC 7946 is WGS84 by definition — "
            "reproject to EPSG:4326 first). Inputs without a CRS are refused."
        ),
        "parameters": [
            {
                "name": "input_path",
                "type": "str",
                "required": True,
                "description": "Vector dataset to convert (must have a CRS)",
            },
            {
                "name": "output_path",
                "type": "str",
                "required": True,
                "description": "Output path; the extension picks the format "
                "(.parquet, .gpkg or .geojson)",
            },
        ],
        "examples": [
            {
                "goal": "Bring a GeoPackage into the canonical analytical format",
                "call": {
                    "tool": "convert_format",
                    "arguments": {
                        "input_path": "parcels.gpkg",
                        "output_path": "parcels.parquet",
                    },
                },
            },
            {
                "goal": "Export a WGS84 result as GeoJSON for a web client",
                "call": {
                    "tool": "convert_format",
                    "arguments": {
                        "input_path": "result_wgs84.parquet",
                        "output_path": "result.geojson",
                    },
                },
            },
        ],
    },
    {
        "name": "reproject_layer",
        "status": "available",
        "tool": "reproject_layer",
        "workload": "small_vector",
        "category": "vector",
        "applicability": {"inputs": ["vector"], "requires_projected_crs": False},
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
        "tool": "spatial_join",
        "workload": "heavy_join",
        "category": "vector",
        "applicability": {"inputs": ["vector", "vector"], "requires_projected_crs": False},
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
        "tool": "run_sql",
        "workload": "sql",
        "category": "sql",
        "applicability": {"inputs": ["dataset"], "requires_projected_crs": False},
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
        "tool": "zonal_statistics",
        "workload": "raster",
        "category": "raster",
        "applicability": {"inputs": ["raster", "vector"], "requires_projected_crs": False},
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
        "tool": "get_provenance",
        "workload": "small_vector",
        "category": "provenance",
        "applicability": {"inputs": ["dataset"], "requires_projected_crs": False},
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
        "tool": "hillshade",
        "workload": "raster",
        "category": "terrain",
        "applicability": {"inputs": ["raster"], "requires_projected_crs": False},
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
        "tool": "slope",
        "workload": "raster",
        "category": "terrain",
        "applicability": {"inputs": ["raster"], "requires_projected_crs": True},
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
        "tool": "aspect",
        "workload": "raster",
        "category": "terrain",
        "applicability": {"inputs": ["raster"], "requires_projected_crs": True},
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
        "tool": "flow_accumulation",
        "workload": "raster",
        "category": "hydrology",
        "applicability": {"inputs": ["raster"], "requires_projected_crs": False},
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
        "tool": "watershed",
        "workload": "raster",
        "category": "hydrology",
        "applicability": {"inputs": ["raster", "vector"], "requires_projected_crs": False},
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
        "tool": "validate_plan",
        "workload": "small_vector",
        "category": "planning",
        "applicability": {"inputs": ["plan"], "requires_projected_crs": False},
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
        "tool": "execute_plan",
        "workload": "small_vector",
        "category": "planning",
        "applicability": {"inputs": ["plan"], "requires_projected_crs": False},
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
        "tool": "preview_map",
        "workload": "small_vector",
        "category": "visualization",
        "applicability": {"inputs": ["dataset"], "requires_projected_crs": False},
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
        "name": "resample_raster",
        "status": "available",
        "tool": None,
        "workload": "raster",
        "category": "raster",
        "applicability": {"inputs": ["raster"], "requires_projected_crs": False},
        "summary": "Resample a raster to a target cell size; the method is required, "
        "and inventing class codes is reported",
        "description": (
            "Change a raster's cell size. The resampling method is a REQUIRED argument "
            "with no default, because both defaults are wrong half the time: nearest "
            "neighbour on a continuous surface gives blocky terrain, and bilinear or "
            "average on class codes invents classes that were never in the data. When "
            "the input looks categorical (integer, few distinct values) and the method "
            "averages neighbours, the result is compared against the input's own set of "
            "values and any code that appeared out of nothing is reported — in the "
            "manifest and in the result. The output shape is derived from the extent and "
            "the target resolution before the engine runs, then verified. Requires the "
            "[raster] extra. Called through run_operation."
        ),
        "parameters": [
            {
                "name": "input_path",
                "type": "str",
                "required": True,
                "description": "Raster to resample (.tif); must declare a CRS",
            },
            {
                "name": "output_path",
                "type": "str",
                "required": True,
                "description": "Output GeoTIFF path",
            },
            {
                "name": "resolution",
                "type": "float",
                "required": True,
                "description": "Target cell size, in the raster's own CRS units",
            },
            {
                "name": "resampling",
                "type": "str",
                "required": True,
                "description": "nearest, mode, min, max, med, q1, q3 (keep existing "
                "values — use for class codes) or bilinear, cubic, cubic_spline, "
                "lanczos, average, rms, sum (derive new values — use for continuous "
                "surfaces). No default: the choice belongs to the caller",
            },
        ],
        "examples": [
            {
                "goal": "Coarsen a 10 m land-cover map to 30 m without inventing classes",
                "call": {
                    "tool": "run_operation",
                    "arguments": {
                        "operation": "resample_raster",
                        "arguments": {
                            "input_path": "landcover.tif",
                            "output_path": "landcover_30m.tif",
                            "resolution": 30,
                            "resampling": "mode",
                        },
                    },
                },
            },
            {
                "goal": "Refine a coarse elevation grid to 10 m for a smooth surface",
                "call": {
                    "tool": "run_operation",
                    "arguments": {
                        "operation": "resample_raster",
                        "arguments": {
                            "input_path": "dem_30m.tif",
                            "output_path": "dem_10m.tif",
                            "resolution": 10,
                            "resampling": "bilinear",
                        },
                    },
                },
            },
        ],
    },
    {
        "name": "clip_raster",
        "status": "available",
        "tool": None,
        "workload": "raster",
        "category": "raster",
        "applicability": {"inputs": ["raster", "vector"], "requires_projected_crs": False},
        "summary": "Clip a raster to a vector mask, with the mask reprojected "
        "explicitly instead of assumed",
        "description": (
            "Cut a raster down to the area of a vector mask. The mask is reprojected "
            "to the raster's CRS when they differ and the decision is recorded — "
            "rasterio's own mask function never checks the CRS, and when two "
            "coordinate systems overlap numerically without meaning the same thing "
            "(metres against US survey feet, one UTM zone against its neighbour) it "
            "clips a plausible wrong piece of the raster in silence. The output is "
            "verified to be no larger than the source, and an all-nodata result "
            "comes back flagged. If the source declares no nodata value, the manifest "
            "says so: the area outside the mask is then filled with 0, which is a "
            "legal elevation. Requires the [raster] extra. Called through "
            "run_operation."
        ),
        "parameters": [
            {
                "name": "raster_path",
                "type": "str",
                "required": True,
                "description": "Raster to clip (.tif); must declare a CRS",
            },
            {
                "name": "mask_path",
                "type": "str",
                "required": True,
                "description": "Polygon layer defining the area to keep (must have a CRS)",
            },
            {
                "name": "output_path",
                "type": "str",
                "required": True,
                "description": "Output GeoTIFF path",
            },
            {
                "name": "all_touched",
                "type": "bool",
                "required": False,
                "description": "False (default) keeps cells whose centre is inside the "
                "mask; True keeps every cell the mask touches, which enlarges the "
                "result by roughly half a cell around the perimeter",
            },
        ],
        "examples": [
            {
                "goal": "Cut a national DEM down to one catchment",
                "call": {
                    "tool": "run_operation",
                    "arguments": {
                        "operation": "clip_raster",
                        "arguments": {
                            "raster_path": "dem.tif",
                            "mask_path": "catchment.gpkg",
                            "output_path": "dem_catchment.tif",
                        },
                    },
                },
            },
            {
                "goal": "Extract a land-cover tile for a municipality, keeping every "
                "touched cell",
                "call": {
                    "tool": "run_operation",
                    "arguments": {
                        "operation": "clip_raster",
                        "arguments": {
                            "raster_path": "landcover.tif",
                            "mask_path": "municipality.parquet",
                            "output_path": "landcover_city.tif",
                            "all_touched": True,
                        },
                    },
                },
            },
        ],
    },
    {
        "name": "reclassify_raster",
        "status": "available",
        "tool": None,
        "workload": "raster",
        "category": "raster",
        "applicability": {"inputs": ["raster"], "requires_projected_crs": False},
        "summary": "Map value ranges onto new codes, half-open by contract, "
        "with overlaps refused",
        "description": (
            "Reclassify raster values into new codes. Each interval is written "
            "'low:high:new' and is HALF-OPEN — low <= value < high — which is the "
            "only convention that tiles the number line without overlap, and the "
            "off-by-one at the boundary is the classic silent error here: a cell of "
            "exactly 100 belongs to the interval that starts at 100. Overlapping "
            "intervals are refused before anything runs, because a value in two of "
            "them would take whichever was listed first. Cells matching no interval "
            "become nodata and are counted in the manifest, rather than keeping "
            "their original value and mixing old codes with new ones in one band. "
            "The output is verified to contain only the codes that were asked for. "
            "Requires the [raster] extra. Called through run_operation."
        ),
        "parameters": [
            {
                "name": "input_path",
                "type": "str",
                "required": True,
                "description": "Raster to reclassify (.tif)",
            },
            {
                "name": "output_path",
                "type": "str",
                "required": True,
                "description": "Output GeoTIFF path (float32, nodata -9999)",
            },
            {
                "name": "intervals",
                "type": "list[str]",
                "required": True,
                "description": "Ranges as 'low:high:new', low inclusive and high "
                "exclusive, e.g. ['0:100:1', '100:200:2', '200:1000:3']",
            },
        ],
        "examples": [
            {
                "goal": "Turn a slope raster into three steepness classes",
                "call": {
                    "tool": "run_operation",
                    "arguments": {
                        "operation": "reclassify_raster",
                        "arguments": {
                            "input_path": "slope.tif",
                            "output_path": "slope_classes.tif",
                            "intervals": ["0:5:1", "5:15:2", "15:90:3"],
                        },
                    },
                },
            },
            {
                "goal": "Flag elevations above a flood threshold as safe, below as at risk",
                "call": {
                    "tool": "run_operation",
                    "arguments": {
                        "operation": "reclassify_raster",
                        "arguments": {
                            "input_path": "dem.tif",
                            "output_path": "flood_risk.tif",
                            "intervals": ["-100:12:1", "12:9000:0"],
                        },
                    },
                },
            },
        ],
    },
    {
        "name": "band_math",
        "status": "available",
        "tool": None,
        "workload": "raster",
        "category": "raster",
        "applicability": {"inputs": ["raster"], "requires_projected_crs": False},
        "summary": "Arithmetic across a raster's bands (NDVI and friends), with "
        "declared scale and offset applied",
        "description": (
            "Evaluate an arithmetic expression over a raster's bands, written with "
            "b1, b2, … and the operators + - * / and parentheses; nothing else is "
            "accepted, and the expression is evaluated over arrays rather than "
            "executed. Three things happen that a hand-rolled version usually skips, "
            "each of which is a plausible wrong number: the scale and offset the file "
            "DECLARES are applied and recorded (GDAL states this is the caller's job "
            "and does not do it, so an index on stored digital numbers is off by "
            "whatever the calibration was); arithmetic runs in float64, because "
            "subtracting two uint16 bands wraps around at zero and returns ~65535 "
            "silently; and the output is written as float32 with a declared nodata "
            "instead of inheriting an integer profile that would round an index in "
            "[-1, 1] to zeros and ones. Requires the [raster] extra. Called through "
            "run_operation."
        ),
        "parameters": [
            {
                "name": "input_path",
                "type": "str",
                "required": True,
                "description": "Multi-band raster (.tif)",
            },
            {
                "name": "output_path",
                "type": "str",
                "required": True,
                "description": "Output GeoTIFF path (float32, nodata -9999)",
            },
            {
                "name": "expression",
                "type": "str",
                "required": True,
                "description": "Arithmetic over band references, e.g. "
                "'(b2 - b1) / (b2 + b1)' for NDVI with red in band 1 and NIR in band 2; operators + - * / ** and parentheses",
            },
        ],
        "examples": [
            {
                "goal": "NDVI from a scene with red in band 1 and near-infrared in band 2",
                "call": {
                    "tool": "run_operation",
                    "arguments": {
                        "operation": "band_math",
                        "arguments": {
                            "input_path": "scene.tif",
                            "output_path": "ndvi.tif",
                            "expression": "(b2 - b1) / (b2 + b1)",
                        },
                    },
                },
            },
            {
                "goal": "Convert a thermal band from tenths of a kelvin to celsius",
                "call": {
                    "tool": "run_operation",
                    "arguments": {
                        "operation": "band_math",
                        "arguments": {
                            "input_path": "thermal.tif",
                            "output_path": "celsius.tif",
                            "expression": "b1 / 10 - 273.15",
                        },
                    },
                },
            },
        ],
    },
    {
        "name": "join_table",
        "status": "available",
        "tool": None,
        "workload": "sql",
        "category": "vector",
        "applicability": {"inputs": ["vector"], "requires_projected_crs": False},
        "summary": "Join a CSV table onto a layer by key, keys read as text and fan-out measured",
        "description": (
            "Join attributes from a CSV onto a layer. Keys are read as TEXT on both sides, always: a reader that infers types turns the identifier '001' into 1, which matches nothing, and rows drop out of an inner join with no error — leading zeros are the norm in ISTAT, FIPS, INSEE and postal codes. Cardinality is measured rather than assumed: if the table has more than one row per key the join multiplies features, and any sum over the result double-counts them, so the before/after counts and the duplicate keys are reported in the manifest and in the result. Called through run_operation."
        ),
        "parameters": [
            {
                "name": "input_path",
                "type": "str",
                "required": True,
                "description": "Layer to join onto (must have a CRS)",
            },
            {
                "name": "table_path",
                "type": "str",
                "required": True,
                "description": "CSV file with the attributes",
            },
            {
                "name": "output_path",
                "type": "str",
                "required": True,
                "description": "Output path (.parquet or .gpkg)",
            },
            {
                "name": "on",
                "type": "str",
                "required": True,
                "description": "Key column, present in both the layer and the table",
            },
            {
                "name": "how",
                "type": "str",
                "required": False,
                "description": "'left' (default, keeps every feature) or 'inner'",
            },
        ],
        "examples": [
            {
                "goal": "Attach population figures to municipalities by ISTAT code",
                "call": {
                    "tool": "run_operation",
                    "arguments": {
                        "operation": "join_table",
                        "arguments": {"input_path": "municipalities.gpkg", "table_path": "population.csv", "output_path": "municipalities_pop.parquet", "on": "istat_code"},
                    },
                },
            },
            {
                "goal": "Join owners to cadastral parcels, keeping only matched parcels",
                "call": {
                    "tool": "run_operation",
                    "arguments": {
                        "operation": "join_table",
                        "arguments": {"input_path": "parcels.parquet", "table_path": "owners.csv", "output_path": "owned.parquet", "on": "parcel_id", "how": "inner"},
                    },
                },
            },
        ],
    },
    {
        "name": "measure_length",
        "status": "available",
        "tool": None,
        "workload": "small_vector",
        "category": "vector",
        "applicability": {"inputs": ["vector"], "requires_projected_crs": False},
        "summary": "Length per feature in metres — geodesic, planar, or through space with the Z the geometry carries",
        "description": (
            "Measure length. method='3d' uses the elevations the geometry carries: a pipe climbing 300 m over 400 m of ground is 500 m of pipe, and every 2D length in the stack answers 400 without mentioning it — in PostGIS the difference is the function's name, in Shapely a property that drops the coordinate. 'geodesic' (default) measures on the ellipsoid the CRS names; 'planar' measures in the CRS plane and converts by its declared unit, and is refused on a geographic CRS. When the layer has Z and a flat method was chosen, the 3D length comes back beside it as a non-critical check. Called through run_operation."
        ),
        "parameters": [
            {
                "name": "input_path",
                "type": "str",
                "required": True,
                "description": "Line or polygon layer (must have a CRS)",
            },
            {
                "name": "output_path",
                "type": "str",
                "required": True,
                "description": "Output path (.parquet or .gpkg)",
            },
            {
                "name": "method",
                "type": "str",
                "required": False,
                "description": "geodesic (default), planar, or 3d",
            },
            {
                "name": "length_column",
                "type": "str",
                "required": False,
                "description": "Column for the per-feature length (default length_m)",
            },
        ],
        "examples": [
            {
                "goal": "Metres of pipe needed for a pipeline that climbs",
                "call": {
                    "tool": "run_operation",
                    "arguments": {
                        "operation": "measure_length",
                        "arguments": {"input_path": "pipeline.gpkg", "output_path": "pipe_lengths.parquet", "method": "3d"},
                    },
                },
            },
            {
                "goal": "Length of a coastline on the ellipsoid",
                "call": {
                    "tool": "run_operation",
                    "arguments": {
                        "operation": "measure_length",
                        "arguments": {"input_path": "coastline.parquet", "output_path": "coast_length.parquet"},
                    },
                },
            },
        ],
    },
    {
        "name": "aggregate_weighted",
        "status": "available",
        "tool": None,
        "workload": "sql",
        "category": "vector",
        "applicability": {"inputs": ["vector"], "requires_projected_crs": False},
        "summary": "A rate over an area: the ratio of totals, with the unweighted mean reported beside it",
        "description": (
            "Combine a per-feature rate into one figure for the whole area, weighted by a second column. Averaging three unemployment rates treats a town of a thousand as equal to a city of a hundred thousand: 13.67% where the area's actual rate is 1.38%. This computes sum(value * weight) / sum(weight), records both totals so the number can be checked without the data, and returns the unweighted mean beside it — when the two differ materially that difference is the finding, and hiding it would make this a black box that happens to be right. Called through run_operation."
        ),
        "parameters": [
            {
                "name": "input_path",
                "type": "str",
                "required": True,
                "description": "Layer whose features carry the value and the weight",
            },
            {
                "name": "output_path",
                "type": "str",
                "required": True,
                "description": "Output path (.parquet or .gpkg), one feature",
            },
            {
                "name": "value_column",
                "type": "str",
                "required": True,
                "description": "The rate or ratio to combine",
            },
            {
                "name": "weight_column",
                "type": "str",
                "required": True,
                "description": "The size each value should count for (population, area, labour force)",
            },
            {
                "name": "result_column",
                "type": "str",
                "required": False,
                "description": "Column for the weighted value (default weighted_value)",
            },
        ],
        "examples": [
            {
                "goal": "Unemployment rate of a wider area from its municipalities",
                "call": {
                    "tool": "run_operation",
                    "arguments": {
                        "operation": "aggregate_weighted",
                        "arguments": {"input_path": "municipalities.gpkg", "output_path": "area_rate.parquet", "value_column": "unemployment_rate_pct", "weight_column": "labour_force"},
                    },
                },
            },
            {
                "goal": "Mean tree cover across census tracts, weighted by tract area",
                "call": {
                    "tool": "run_operation",
                    "arguments": {
                        "operation": "aggregate_weighted",
                        "arguments": {"input_path": "tracts.parquet", "output_path": "cover.parquet", "value_column": "tree_cover_pct", "weight_column": "area_ha"},
                    },
                },
            },
        ],
    },
    {
        "name": "parse_coordinates",
        "status": "available",
        "tool": None,
        "workload": "small_vector",
        "category": "vector",
        "applicability": {"inputs": ["dataset"], "requires_projected_crs": False},
        "summary": "Point layer from a coordinate table, DMS or decimal, stated by the caller rather than guessed",
        "description": (
            "Build points from a CSV. The caller names the columns holding each coordinate: one for decimal degrees, three for degrees/minutes/seconds, optionally a fourth for the hemisphere. The caller says which because the file cannot: 41.5324 and 41 degrees 53 minutes 24 seconds latitudes for the same station and they are 40 km apart, so a reader that guesses is wrong quietly. The conversion and the columns used go in the manifest, and values outside the valid range are refused rather than wrapped: a latitude of 91 is a parsing failure, not a place. Called through run_operation."
        ),
        "parameters": [
            {
                "name": "table_path",
                "type": "str",
                "required": True,
                "description": "CSV with the coordinate columns",
            },
            {
                "name": "output_path",
                "type": "str",
                "required": True,
                "description": "Output path (.parquet or .gpkg)",
            },
            {
                "name": "latitude_columns",
                "type": "str",
                "required": True,
                "description": "Comma-separated: one column (decimal), three (deg,min,sec) or four (plus hemisphere letter)",
            },
            {
                "name": "longitude_columns",
                "type": "str",
                "required": True,
                "description": "Same, for longitude",
            },
            {
                "name": "crs",
                "type": "str",
                "required": False,
                "description": "CRS of the coordinates (default EPSG:4326)",
            },
        ],
        "examples": [
            {
                "goal": "Points from a survey register in degrees, minutes and seconds",
                "call": {
                    "tool": "run_operation",
                    "arguments": {
                        "operation": "parse_coordinates",
                        "arguments": {"table_path": "stations.csv", "output_path": "stations.parquet", "latitude_columns": "lat_deg,lat_min,lat_sec,lat_hem", "longitude_columns": "lon_deg,lon_min,lon_sec,lon_hem"},
                    },
                },
            },
            {
                "goal": "Points from a table already in decimal degrees",
                "call": {
                    "tool": "run_operation",
                    "arguments": {
                        "operation": "parse_coordinates",
                        "arguments": {"table_path": "sites.csv", "output_path": "sites.parquet", "latitude_columns": "latitude", "longitude_columns": "longitude"},
                    },
                },
            },
        ],
    },
    {
        "name": "point_on_surface",
        "status": "available",
        "tool": None,
        "workload": "small_vector",
        "category": "vector",
        "applicability": {"inputs": ["vector"], "requires_projected_crs": False},
        "summary": "One point per feature, verified to lie ON the feature — unlike a centroid",
        "description": (
            "Reduce each feature to a representative point that is guaranteed to be on it. The difference from centroid_layer is the reason this exists: the centroid of an L-shaped parcel, a crescent or a ring falls outside the shape, so locating a feature by its centroid can put it in the wrong district — and a district name carries no magnitude to sanity-check. Every output point is verified against its own input feature, which is a postcondition rather than an opinion. Called through run_operation."
        ),
        "parameters": [
            {
                "name": "input_path",
                "type": "str",
                "required": True,
                "description": "Polygon layer (must have a CRS)",
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
                "goal": "Label points for parcels that are not convex",
                "call": {
                    "tool": "run_operation",
                    "arguments": {
                        "operation": "point_on_surface",
                        "arguments": {"input_path": "parcels.gpkg", "output_path": "labels.parquet"},
                    },
                },
            },
            {
                "goal": "A point inside each administrative unit, for joining",
                "call": {
                    "tool": "run_operation",
                    "arguments": {
                        "operation": "point_on_surface",
                        "arguments": {"input_path": "districts.parquet", "output_path": "district_points.parquet"},
                    },
                },
            },
        ],
    },
    {
        "name": "hull_layer",
        "status": "available",
        "tool": None,
        "workload": "small_vector",
        "category": "vector",
        "applicability": {"inputs": ["vector"], "requires_projected_crs": False},
        "summary": "Convex hull, envelope or minimum rotated rectangle, with the inflation reported",
        "description": (
            "Wrap each feature in a hull. The three kinds differ by how much they claim: an envelope is axis-aligned and can be several times the feature's area, an oriented rectangle follows it, a convex hull follows it more closely. Which one was used goes in the manifest along with the ratio between the hull's area and the feature's, because 'the extent of the site' is a phrase that hides all three and any count over a hull includes ground the feature does not occupy. Called through run_operation."
        ),
        "parameters": [
            {
                "name": "input_path",
                "type": "str",
                "required": True,
                "description": "Layer to wrap (must have a CRS)",
            },
            {
                "name": "output_path",
                "type": "str",
                "required": True,
                "description": "Output path (.parquet or .gpkg)",
            },
            {
                "name": "kind",
                "type": "str",
                "required": False,
                "description": "convex (default), envelope, or oriented",
            },
        ],
        "examples": [
            {
                "goal": "Convex hull of each survey cluster",
                "call": {
                    "tool": "run_operation",
                    "arguments": {
                        "operation": "hull_layer",
                        "arguments": {"input_path": "samples.parquet", "output_path": "clusters.parquet"},
                    },
                },
            },
            {
                "goal": "Axis-aligned bounding box per feature, for a tile index",
                "call": {
                    "tool": "run_operation",
                    "arguments": {
                        "operation": "hull_layer",
                        "arguments": {"input_path": "scenes.gpkg", "output_path": "tiles.parquet", "kind": "envelope"},
                    },
                },
            },
        ],
    },
    {
        "name": "validate_geometry",
        "status": "available",
        "tool": None,
        "workload": "small_vector",
        "category": "vector",
        "applicability": {"inputs": ["vector"], "requires_projected_crs": False},
        "summary": "Report which geometries are invalid and why, repairing nothing",
        "description": (
            "Write the layer back with a validity flag and the GEOS reason per feature. Every other operation here repairs what it can and records the repair; this is the inspection that comes first, so a caller can decide what to do about a self-intersection instead of finding out afterwards that something was rewritten. An invalid ring does not crash — its area is the signed shoelace of a shape that means nothing — so knowing before measuring is the point. Called through run_operation."
        ),
        "parameters": [
            {
                "name": "input_path",
                "type": "str",
                "required": True,
                "description": "Layer to check",
            },
            {
                "name": "output_path",
                "type": "str",
                "required": True,
                "description": "Output path, with is_valid and validity_reason columns",
            },
        ],
        "examples": [
            {
                "goal": "Find the self-intersections in a digitised parcel layer",
                "call": {
                    "tool": "run_operation",
                    "arguments": {
                        "operation": "validate_geometry",
                        "arguments": {"input_path": "parcels.gpkg", "output_path": "parcels_checked.parquet"},
                    },
                },
            },
            {
                "goal": "Check a layer before measuring areas on it",
                "call": {
                    "tool": "run_operation",
                    "arguments": {
                        "operation": "validate_geometry",
                        "arguments": {"input_path": "concessions.parquet", "output_path": "checked.parquet"},
                    },
                },
            },
        ],
    },
    {
        "name": "count_in_polygons",
        "status": "available",
        "tool": None,
        "workload": "heavy_join",
        "category": "vector",
        "applicability": {"inputs": ["vector", "vector"], "requires_projected_crs": False},
        "summary": "Points per polygon with the boundary rule stated, and the points that fell nowhere counted",
        "description": (
            "Count points in each polygon. 'intersects' (default) includes points on the boundary; 'within' excludes them — and on a partition of districts that share edges, that is the difference between counting every point and dropping the ones on the seams, silently, because a join returning fewer rows looks exactly like a join that had fewer to find. The points that fall in no polygon are counted and reported, which is the number that makes the difference visible. Called through run_operation."
        ),
        "parameters": [
            {
                "name": "points_path",
                "type": "str",
                "required": True,
                "description": "Point layer",
            },
            {
                "name": "polygons_path",
                "type": "str",
                "required": True,
                "description": "Polygon layer to count into",
            },
            {
                "name": "output_path",
                "type": "str",
                "required": True,
                "description": "Output: the polygons with a count column",
            },
            {
                "name": "predicate",
                "type": "str",
                "required": False,
                "description": "intersects (default), within, or contains",
            },
            {
                "name": "count_column",
                "type": "str",
                "required": False,
                "description": "Column for the count (default point_count)",
            },
        ],
        "examples": [
            {
                "goal": "Wells per district, counting those on shared boundaries",
                "call": {
                    "tool": "run_operation",
                    "arguments": {
                        "operation": "count_in_polygons",
                        "arguments": {"points_path": "wells.gpkg", "polygons_path": "districts.gpkg", "output_path": "district_counts.parquet"},
                    },
                },
            },
            {
                "goal": "Incidents per census tract, strictly inside only",
                "call": {
                    "tool": "run_operation",
                    "arguments": {
                        "operation": "count_in_polygons",
                        "arguments": {"points_path": "incidents.parquet", "polygons_path": "tracts.parquet", "output_path": "tract_counts.parquet", "predicate": "within"},
                    },
                },
            },
        ],
    },
    {
        "name": "focal_statistics",
        "status": "available",
        "tool": None,
        "workload": "raster",
        "category": "raster",
        "applicability": {"inputs": ["raster"], "requires_projected_crs": False},
        "summary": "Moving-window statistic over a raster, window size required and checked odd",
        "description": (
            "Compute a statistic in a square window around every cell. The window size is required: Whitebox defaults to 11 x 11, which on a 1 m grid is a 5.5 m radius, so a 'local' statistic quietly stops being local and returns a perfectly ordinary-looking smoothed surface. It must be odd, because an even window has no centre cell and shifts the result half a cell against its input. For class codes use majority or diversity: mean on a land-cover map invents codes the same way an interpolating resample does, and the manifest says so. Requires the [whitebox] extra. Called through run_operation."
        ),
        "parameters": [
            {
                "name": "input_path",
                "type": "str",
                "required": True,
                "description": "Raster (.tif)",
            },
            {
                "name": "output_path",
                "type": "str",
                "required": True,
                "description": "Output GeoTIFF",
            },
            {
                "name": "statistic",
                "type": "str",
                "required": True,
                "description": "mean, median, maximum, minimum, range, standard_deviation, majority, diversity, total",
            },
            {
                "name": "window",
                "type": "int",
                "required": True,
                "description": "Window size in cells; odd, at least 3",
            },
        ],
        "examples": [
            {
                "goal": "Smooth a noisy DEM with a 3x3 mean",
                "call": {
                    "tool": "run_operation",
                    "arguments": {
                        "operation": "focal_statistics",
                        "arguments": {"input_path": "dem.tif", "output_path": "dem_smooth.tif", "statistic": "mean", "window": 3},
                    },
                },
            },
            {
                "goal": "Most common land-cover class in a 5x5 neighbourhood",
                "call": {
                    "tool": "run_operation",
                    "arguments": {
                        "operation": "focal_statistics",
                        "arguments": {"input_path": "landcover.tif", "output_path": "landcover_majority.tif", "statistic": "majority", "window": 5},
                    },
                },
            },
        ],
    },
    {
        "name": "extract_streams",
        "status": "available",
        "tool": None,
        "workload": "raster",
        "category": "hydrology",
        "applicability": {"inputs": ["raster"], "requires_projected_crs": False},
        "summary": "Stream network from a flow-accumulation grid, threshold required and its unit recorded",
        "description": (
            "Threshold a flow-accumulation grid into a stream network. The threshold is required and its unit is recorded, because that is where this goes wrong: flow accumulation is either a cell count or a specific contributing area depending on how it was produced, the two differ by orders of magnitude, and a threshold tuned for one applied to the other gives a network that is well formed, drawn on the map, and wrong. There is no defensible default — the literature says so plainly — so the caller states it and the record keeps it. Requires the [whitebox] extra. Called through run_operation."
        ),
        "parameters": [
            {
                "name": "flow_accumulation_path",
                "type": "str",
                "required": True,
                "description": "Flow-accumulation raster, e.g. the output of flow_accumulation",
            },
            {
                "name": "output_path",
                "type": "str",
                "required": True,
                "description": "Output GeoTIFF of the stream network",
            },
            {
                "name": "threshold",
                "type": "float",
                "required": True,
                "description": "Minimum accumulation for a cell to be a stream, in the input's own unit",
            },
            {
                "name": "zero_background",
                "type": "bool",
                "required": False,
                "description": "False (default) leaves non-stream cells as nodata; True writes zeros",
            },
        ],
        "examples": [
            {
                "goal": "Stream network from a cell-count accumulation grid",
                "call": {
                    "tool": "run_operation",
                    "arguments": {
                        "operation": "extract_streams",
                        "arguments": {"flow_accumulation_path": "flowacc.tif", "output_path": "streams.tif", "threshold": 1000},
                    },
                },
            },
            {
                "goal": "A denser network for a small catchment, zeros in the background",
                "call": {
                    "tool": "run_operation",
                    "arguments": {
                        "operation": "extract_streams",
                        "arguments": {"flow_accumulation_path": "flowacc.tif", "output_path": "streams_fine.tif", "threshold": 100, "zero_background": True},
                    },
                },
            },
        ],
    },
    {
        "name": "isochrone",
        "status": "planned",
        "workload": "heavy_join",
        "category": "network",
        "applicability": {"inputs": ["vector"], "requires_projected_crs": False},
        "summary": "Travel-time polygons (Valhalla engine)",
    },
    {
        "name": "qgis_processing",
        "status": "planned",
        "workload": "small_vector",
        "category": "bridge",
        "applicability": {"inputs": ["dataset"], "requires_projected_crs": False},
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


def document_text(op: dict[str, Any]) -> str:
    """The exact retrieval corpus of one entry, as plain text.

    One corpus for every ranking engine: the optional embedding layer
    (:mod:`mapsmith.retrieval`) embeds THIS text, so a comparison between BM25
    and embeddings measures the ranking, never a difference in what was read.
    """
    return " ".join(_document(op))


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


APPLICABILITY_KINDS = {"vector", "raster", "dataset", "plan"}


def applicable(input_kind: str | None = None, projected: bool | None = None) -> list[dict[str, Any]]:
    """The subset of the catalog applicable to the data in hand — deterministically.

    Narrow-then-rank: this filter runs BEFORE any ranking, uses only what each
    entry declares (no model, no scores), and its outcome is a statement simple
    enough to put in a manifest: "this operation was offered because the input
    is a projected raster". ``input_kind`` keeps entries that accept that kind
    (entries accepting any ``dataset`` match vector and raster); ``projected=False``
    drops entries that require a projected CRS — the ones that would refuse the
    data anyway.
    """
    if input_kind is not None and input_kind not in APPLICABILITY_KINDS:
        raise ValueError(
            f"input_kind must be one of {sorted(APPLICABILITY_KINDS)}, got {input_kind!r}"
        )
    kept = []
    for op in OPERATIONS:
        block = op["applicability"]
        if input_kind is not None:
            accepted = set(block["inputs"])
            widened = accepted | ({"vector", "raster"} if "dataset" in accepted else set())
            if input_kind not in widened and not (
                input_kind == "dataset" and accepted & {"vector", "raster", "dataset"}
            ):
                continue
        if projected is False and block["requires_projected_crs"]:
            continue
        kept.append(op)
    return kept


SEARCH_ENGINES = ("lexical", "vector", "auto")


def search(
    query: str = "",
    limit: int = 10,
    detail: bool = False,
    input_kind: str | None = None,
    projected: bool | None = None,
    engine: str = "lexical",
) -> list[dict[str, Any]]:
    """Search the catalog. Compact entries by default; detail=True adds parameters/examples.

    Empty query lists the whole catalog (roadmap included). With a query, results
    carry a ``score`` and an ``engine`` field. ``input_kind``/``projected``
    narrow the candidates deterministically BEFORE ranking, whichever engine
    ranks them (see :func:`applicable`).

    ``engine``: ``lexical`` (default) is BM25 — deterministic, dependency-free,
    no network ever. ``vector`` is the embedding engine (``[retrieval]`` extra),
    which fetches a revision-pinned model on first use. ``auto`` prefers the
    vector engine and falls back to lexical when the extra is absent, so a
    deployment can turn it on without the caller knowing. The default stays
    lexical because a default that needs a download is not a default.
    """
    if engine not in SEARCH_ENGINES:
        raise ValueError(f"engine must be one of {list(SEARCH_ENGINES)}, got {engine!r}")
    candidates = applicable(input_kind, projected)
    if not query.strip():
        return [dict(op) if detail else _compact(op) for op in candidates]

    used = engine
    ranked: list[tuple[dict[str, Any], float]]
    if engine in ("vector", "auto"):
        try:
            from . import retrieval

            ranked = retrieval.rank(query, limit=limit, candidates=candidates)
            used = "vector"
        except ImportError:
            if engine == "vector":
                raise
            used = "lexical"
    if used == "lexical":
        query_tokens = _tokenize(query)
        scores = bm25_scores(query_tokens, [_document(op) for op in candidates])
        ranked = sorted(
            ((op, s) for op, s in zip(candidates, scores, strict=True) if s > 0),
            key=lambda pair: (-pair[1], pair[0]["name"]),
        )
    results = []
    for op, score in ranked[:limit]:
        entry = dict(op) if detail else _compact(op)
        entry["score"] = round(score, 4)
        entry["engine"] = used
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
