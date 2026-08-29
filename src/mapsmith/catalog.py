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
        "produces": "description",
        "applicability": {"inputs": ["dataset"], "requires_projected_crs": False, 'dataset_inputs': 1},
        "summary": "Inspect a vector or raster dataset: CRS, schema/bands, extent, "
        "nodata, statistics",
        "phrasings": "what is in this file; what am I looking at; how many features and what extent; which layers does it have; before I start",
        "distinguishes": "Reports what a file IS — CRS, schema, extent, bands, layers — before anything is "
        "computed over it. Not describe_crs, which answers about a coordinate system with "
        "no file involved.",
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
        "produces": "dataset:vector",
        "applicability": {"inputs": ["vector"], "requires_projected_crs": False, 'dataset_inputs': 1},
        "summary": "Metric buffer with automatic UTM estimation on geographic CRS",
        "phrasings": "everything within a distance of; a zone around; how far out from; catchment radius; protection zone",
        "distinguishes": "Grows each feature by a fixed distance. Not voronoi_polygons, which divides all "
        "the space between features; not hull_layer, which wraps them without a distance.",
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
        "produces": "dataset:vector",
        "applicability": {"inputs": ["vector", "vector"], "requires_projected_crs": False, 'dataset_inputs': 2},
        "summary": "Clip a layer with a mask layer (CRS-aligned automatically)",
        "phrasings": "cut to the study area; keep only what falls inside the boundary; trim to the region",
        "distinguishes": "Cuts one layer to the shape of another and keeps the first layer's attributes. Not "
        "overlay_layers, which combines the attributes of both; use clip when the second "
        "layer is a boundary rather than data.",
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
        "produces": "dataset:vector",
        "applicability": {"inputs": ["vector", "vector"], "requires_projected_crs": False, 'dataset_inputs': 2},
        "summary": "Intersect, union or subtract two polygon layers: set-theoretic "
        "overlay with intersection, union, identity, symmetric_difference and "
        "difference (CRS-aligned automatically)",
        "phrasings": "the part where two layers overlap; what falls in both; subtract one from the other; combine two sets of polygons",
        "distinguishes": "Two layers in, one out, keeping only where they coincide or differ. Not "
        "buffer_layer, which grows a single layer; not hull_layer, which wraps one. This is "
        "the one for what falls inside something else, or what is left after subtracting.",
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
        "produces": "dataset:vector",
        "applicability": {"inputs": ["vector"], "requires_projected_crs": False, 'dataset_inputs': 1},
        "summary": "Merge features into one geometry per key, with the aggregation "
        "recorded in the manifest and the group count verified",
        "phrasings": "collapse into; group by and combine; roll up smaller units into larger ones; merge by attribute",
        "distinguishes": "Merges features that share an attribute into one. Not merge_layers, which appends "
        "different files without combining anything; not simplify_layer, which thins "
        "vertices inside features that stay separate.",
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
        "produces": "dataset:vector",
        "applicability": {"inputs": ["vector", "vector"], "requires_projected_crs": False, 'dataset_inputs': 2},
        "summary": "Nearest-neighbour join with the distance in meters in a named column "
        "(UTM-measured on geographic CRS, decision recorded)",
        "phrasings": "which one is closest to each; find the nearest and how far; assign each to its closest",
        "distinguishes": "For each feature in one layer, the nearest feature in another and how far it is. "
        "Not geodetic_distance, which takes two coordinates; not voronoi_polygons, which "
        "draws the territories instead of naming the nearest.",
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
        "produces": "dataset:vector",
        "applicability": {"inputs": ["vector"], "requires_projected_crs": False, 'dataset_inputs': 1},
        "summary": "Split multi-part geometries into one feature per part, "
        "with the part count verified",
        "phrasings": "one row per part instead of one per feature; split multipart; separate the islands",
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
        "produces": "dataset:vector",
        "applicability": {"inputs": ["vector"], "requires_projected_crs": False, 'dataset_inputs': 1},
        "summary": "Area per feature in square metres — ground (ellipsoidal) or "
        "planar with the CRS's own unit, with the distortion checked",
        "phrasings": "how big is it really on the ground not on the map; true size; hectares; square metres of land",
        "distinguishes": "Answers how large something is on the ground, in the unit its CRS actually "
        "declares. Not count_in_polygons, which counts features rather than measuring "
        "surface; not zonal_statistics, which summarises a raster inside the shapes.",
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
        "produces": "dataset:vector",
        "applicability": {"inputs": ["vector", "vector"], "requires_projected_crs": False, 'dataset_inputs': None},
        "summary": "Append two or more layers into one, schema union, "
        "count verified against the sum",
        "phrasings": "put several files together into one; stack these layers; concatenate",
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
        "produces": "dataset:vector",
        "applicability": {"inputs": ["vector"], "requires_projected_crs": False, 'dataset_inputs': 1},
        "summary": "Simplify geometries with the drift measured: area and length "
        "before/after recorded in the manifest",
        "phrasings": "too many vertices; the outlines are too detailed; make the file lighter; generalise the shapes",
        "distinguishes": "Removes vertices and keeps the same features. Not dissolve_layer, which removes "
        "features and keeps the vertices; reach for this when a file is too heavy to draw, "
        "and for dissolve when the units are too fine to reason about.",
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
        "produces": "dataset:vector",
        "applicability": {"inputs": ["vector"], "requires_projected_crs": False, 'dataset_inputs': 1},
        "summary": "One point per feature: geometric centroids computed in a "
        "metric CRS, never on degrees",
        # Deliberately says what it is NOT for. Advertising a centroid as a label
        # point is Argleton trap 014 in our own catalog: the centroid of an
        # L-shaped parcel falls in the notch, on no part of the parcel. The
        # discovery contract found this by ranking `point_on_surface` above it
        # for the example we had written, which was the right answer.
        "phrasings": "the centre of mass of each shape; reduce polygons to points for a "
        "distance calculation; one point per feature. NOT for map labels: a centroid can "
        "fall outside its own polygon, and point_on_surface is the one that cannot",
        "distinguishes": "Collapses each feature to one point, for a distance calculation or a summary. Not "
        "for map labels: the centroid of a concave shape falls outside it, and "
        "point_on_surface is the one that cannot.",
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
                "goal": "Centre of mass of each catchment, as the origin for a "
                "distance matrix",
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
        "produces": "dataset:vector",
        "applicability": {"inputs": ["vector"], "requires_projected_crs": False, 'dataset_inputs': 1},
        "summary": "Convert between vector formats, re-read and verified; "
        "lossy conversions are refused with the reason",
        "phrasings": "save it as another format instead; export to; turn this into a geopackage",
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
        "produces": "dataset:vector",
        "applicability": {"inputs": ["vector"], "requires_projected_crs": False, 'dataset_inputs': 1},
        "summary": "Reproject a layer to a target CRS (EPSG code or WKT)",
        "phrasings": "my data is in degrees and I need metres; change the coordinate system; wrong units; put two layers on the same system",
        "distinguishes": "Changes the coordinate system of a layer that already has one. Not "
        "parse_coordinates, which builds geometry from text columns that have none yet; not "
        "convert_format, which changes the container and leaves the coordinates alone.",
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
        "produces": "dataset:vector",
        "applicability": {"inputs": ["vector", "vector"], "requires_projected_crs": False, 'dataset_inputs': 2},
        "summary": "Join by spatial predicate (intersects/within/contains); auto-routed to "
        "SedonaDB or DuckDB for speed, GeoPandas fallback",
        "phrasings": "give each feature the attribute of the area it sits in; tag points with their region; which district is each in",
        "distinguishes": "Copies attributes onto features from whatever area contains them, leaving the "
        "geometry untouched. Not overlay_layers, which cuts geometry; not "
        "count_in_polygons, which counts rather than labels; not join_table, which matches "
        "on a shared column instead of location.",
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
        "produces": "dataset:vector",
        "applicability": {"inputs": ["dataset"], "requires_projected_crs": False, 'dataset_inputs': None},
        "summary": "Spatial SQL (DuckDB dialect, ST_* functions) over GeoParquet and GDAL "
        "formats; materializes GeoParquet outputs with provenance",
        "phrasings": "query it like a database; a select with a spatial predicate; join and filter in one go",
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
        "produces": "dataset:vector",
        "applicability": {"inputs": ["raster", "vector"], "requires_projected_crs": False, 'dataset_inputs': 2},
        "summary": "Statistics of a raster within vector zones via exactextract "
        "(exact fractional pixel coverage); requires the [raster] extra",
        "phrasings": "average value of a grid inside each polygon; summarise a raster per area; mean elevation per zone",
        "distinguishes": "Summarises a raster inside each polygon — mean elevation per basin, rainfall per "
        "catchment. Not count_in_polygons, which counts vector features; not "
        "band_statistics, which summarises a whole grid with no zones at all.",
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
        "produces": "description",
        "applicability": {"inputs": ["dataset"], "requires_projected_crs": False, 'dataset_inputs': 1},
        "summary": "Full lineage manifest of any MapSmith output",
        "phrasings": "where did this number come from; what was run to make this; the audit trail",
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
        "produces": "dataset:raster",
        "applicability": {"inputs": ["raster"], "requires_projected_crs": False, 'dataset_inputs': 1},
        "summary": "Shaded relief from a DEM (Whitebox engine, in-memory); "
        "requires the [whitebox] extra",
        "phrasings": "make the terrain look three dimensional; relief for a map; shaded relief",
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
        "produces": "dataset:raster",
        "applicability": {"inputs": ["raster"], "requires_projected_crs": True, 'dataset_inputs': 1},
        "summary": "Slope gradient from a DEM in degrees, percent or radians "
        "(Whitebox engine); requires the [whitebox] extra",
        "phrasings": "how steep is the ground; steepness; gradient of the land; where is it too steep to build",
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
        "produces": "dataset:raster",
        "applicability": {"inputs": ["raster"], "requires_projected_crs": True, 'dataset_inputs': 1},
        "summary": "Aspect from a DEM: downslope azimuth in degrees, 0 = north, "
        "flat cells = -1 (Whitebox engine); requires the [whitebox] extra",
        "phrasings": "which way does the hillside face; north facing slopes; exposure",
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
        "produces": "dataset:raster",
        "applicability": {"inputs": ["raster"], "requires_projected_crs": False, 'dataset_inputs': 1},
        "summary": "D8 flow accumulation from a DEM with automatic depression filling; "
        "requires the [whitebox] extra",
        "phrasings": "how much water arrives at each cell; upstream area; where the channels form",
        "distinguishes": "How much upslope area drains through each cell — the grid you threshold to find "
        "channels. Not flow_direction, which is the pointer it is computed from; not "
        "watershed, which asks what drains to one chosen outlet.",
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
        "produces": "dataset:raster",
        "applicability": {"inputs": ["raster", "vector"], "requires_projected_crs": False, 'dataset_inputs': 2},
        "summary": "Watershed delineation from a DEM and pour points (Whitebox engine); "
        "requires the [whitebox] extra",
        "phrasings": "what drains to this point; the basin above; contributing area",
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
        "produces": "description",
        "applicability": {"inputs": ["plan"], "requires_projected_crs": False, 'dataset_inputs': 0},
        "summary": "Static validation of a multi-step plan before execution: operations, "
        "arguments, references, input files, simulated CRS flow",
        "phrasings": "will this sequence work before I run it; check the steps",
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
        "produces": "plan_result",
        "applicability": {"inputs": ["plan"], "requires_projected_crs": False, 'dataset_inputs': 0},
        "summary": "Validate then run a multi-step plan; per-step provenance plus a "
        "plan-level manifest tying the chain together",
        "phrasings": "run the whole sequence; do all these steps in order",
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
        "produces": "description",
        "applicability": {"inputs": ["dataset"], "requires_projected_crs": False, 'dataset_inputs': 1},
        "summary": "Interactive in-chat map of one or more datasets (MCP Apps panel) "
        "with per-layer provenance and verification status",
        "phrasings": "let me see it; show me the layer; a quick look at the result",
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
        "produces": "dataset:raster",
        "applicability": {"inputs": ["raster"], "requires_projected_crs": False, 'dataset_inputs': 1},
        "summary": "Resample a raster to a target cell size; the method is required, "
        "and inventing class codes is reported",
        "phrasings": "change the pixel size; coarser grid; finer grid; make the cells bigger",
        "distinguishes": "Changes the cell size and keeps the coordinate system. Not reproject_raster, which "
        "changes the coordinate system; both resample, so both refuse to guess the method.",
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
        "produces": "dataset:raster",
        "applicability": {"inputs": ["raster", "vector"], "requires_projected_crs": False, 'dataset_inputs': 2},
        "summary": "Clip a raster to a vector mask, with the mask reprojected "
        "explicitly instead of assumed",
        "phrasings": "cut the grid to the boundary; only the part over the study area",
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
        "produces": "dataset:raster",
        "applicability": {"inputs": ["raster"], "requires_projected_crs": False, 'dataset_inputs': 1},
        "summary": "Map value ranges onto new codes, half-open by contract, "
        "with overlaps refused",
        "phrasings": "bucket the values into classes; turn elevations into bands; group into categories",
        "distinguishes": "Buckets continuous values into classes. Not band_math, which computes a new "
        "continuous value; not focal_statistics, which smooths using neighbours.",
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
        "produces": "dataset:raster",
        "applicability": {"inputs": ["raster"], "requires_projected_crs": False, 'dataset_inputs': 1},
        "summary": "Arithmetic across a raster's bands (NDVI and friends), with "
        "declared scale and offset applied",
        "phrasings": "arithmetic between bands; a vegetation index; subtract one band from another",
        "distinguishes": "Arithmetic between bands, for an index or a difference. Not extract_band, which "
        "separates them; not reclassify_raster, which buckets one band's values into "
        "classes.",
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
        "produces": "dataset:vector",
        "applicability": {"inputs": ["vector"], "requires_projected_crs": False, 'dataset_inputs': 2},
        "summary": "Join a CSV table onto a layer by key, keys read as text and fan-out measured",
        "phrasings": "attach a spreadsheet by a shared key; bring in the csv columns; look up values by id",
        "distinguishes": "Matches rows by a shared key column, not by location. Use spatial_join when the "
        "relationship is where things are rather than an identifier they share.",
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
        "produces": "dataset:vector",
        "applicability": {"inputs": ["vector"], "requires_projected_crs": False, 'dataset_inputs': 1},
        "summary": "Length per feature in metres — geodesic, planar, or through space with the Z the geometry carries",
        "phrasings": "how long is this line really; kilometres of road; perimeter; distance along",
        "distinguishes": "How long a line is, on the ground, in three dimensions if the file has them. Not "
        "measure_area, which is for surfaces; not geodetic_distance, which is two points "
        "rather than a path.",
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
        "produces": "dataset:vector",
        "applicability": {"inputs": ["vector"], "requires_projected_crs": False, 'dataset_inputs': 1},
        "summary": "A rate over an area: the ratio of totals, with the unweighted mean reported beside it",
        "phrasings": "average that respects population; weighted mean; do not treat a village like a city",
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
        "produces": "dataset:vector",
        "applicability": {"inputs": ["dataset"], "requires_projected_crs": False, 'dataset_inputs': 1},
        "summary": "Point layer from a coordinate table, DMS or decimal, stated by the caller rather than guessed",
        "phrasings": "the coordinates are text in a csv; degrees minutes seconds; latitude and longitude columns",
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
        "produces": "dataset:vector",
        "applicability": {"inputs": ["vector"], "requires_projected_crs": False, 'dataset_inputs': 1},
        "summary": "One point per feature, verified to lie ON the feature — unlike a centroid",
        "phrasings": "a point guaranteed to be inside the shape; label point for an awkward polygon",
        "distinguishes": "One point guaranteed to lie on the feature, which is what a map label needs. Use "
        "centroid_layer when the point must be the centre of mass rather than merely "
        "inside.",
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
        "produces": "dataset:vector",
        "applicability": {"inputs": ["vector"], "requires_projected_crs": False, 'dataset_inputs': 1},
        "summary": "Convex hull, envelope or minimum rotated rectangle, with the inflation reported",
        "phrasings": "the outline around these points; bounding shape; extent of the site",
        "distinguishes": "Wraps features in a convex hull, bounding box or rotated rectangle — a shape that "
        "CLAIMS more ground than the features occupy, and the inflation is reported. Not "
        "buffer_layer, which grows by a fixed distance; not voronoi_polygons, which divides "
        "rather than wraps.",
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
        "produces": "dataset:vector",
        "applicability": {"inputs": ["vector"], "requires_projected_crs": False, 'dataset_inputs': 1},
        "summary": "Report which geometries are invalid and why, repairing nothing",
        "phrasings": "something is wrong with these shapes; broken polygons; self intersecting; why does the area look wrong",
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
        "produces": "dataset:vector",
        "applicability": {"inputs": ["vector", "vector"], "requires_projected_crs": False, 'dataset_inputs': 2},
        "summary": "Points per polygon with the boundary rule stated, and the points that fell nowhere counted",
        "phrasings": "tally by area; how many fall inside each; count per district; incidents per neighbourhood",
        "distinguishes": "Counts how many features fall inside each polygon, with the boundary rule stated. "
        "Not spatial_join, which labels each point with its polygon instead of counting "
        "them; not zonal_statistics, which reads a grid rather than counting features.",
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
        "produces": "dataset:raster",
        "applicability": {"inputs": ["raster"], "requires_projected_crs": False, 'dataset_inputs': 1},
        "summary": "Moving-window statistic over a raster, window size required and checked odd",
        "phrasings": "smooth the grid; a moving window average; local maximum",
        "distinguishes": "A moving window over a grid: smoothing, local maxima. Not zonal_statistics, which "
        "uses polygons as the zones; the window here is a fixed neighbourhood, not a shape "
        "you supply.",
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
        "produces": "dataset:raster",
        "applicability": {"inputs": ["raster"], "requires_projected_crs": False, 'dataset_inputs': 1},
        "summary": "Stream network from a flow-accumulation grid, threshold required and its unit recorded",
        "phrasings": "where the rivers are on this terrain; the channel network",
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
        "produces": "dataset:vector",
        "applicability": {"inputs": ["vector"], "requires_projected_crs": False, 'dataset_inputs': 1},
        "summary": "Travel-time polygons (Valhalla engine)",
        "phrasings": "how far can I get in fifteen minutes; travel time area; drive time",
    },
    {
        "name": "qgis_processing",
        "status": "planned",
        "workload": "small_vector",
        "category": "bridge",
        "produces": "dataset:vector",
        "applicability": {"inputs": ["dataset"], "requires_projected_crs": False, 'dataset_inputs': 1},
        "summary": "~900 QGIS/GRASS/SAGA algorithms via GPL-isolated subprocess sidecar",
        "phrasings": "one of the qgis algorithms",
    },
    {   'name': 'reproject_raster',
        'status': 'available',
        'tool': None,
        'workload': 'raster',
        'category': 'raster',
        "produces": "dataset:raster",
        'applicability': {'inputs': ['raster'], 'requires_projected_crs': False, 'dataset_inputs': 1},
        'summary': 'Warp a raster to another CRS; the resampling method is required and '
                   'recorded. Requires the [raster] extra',
        "phrasings": "warp the grid to another coordinate system; my geotiff is in the wrong projection",
        "distinguishes": "Changes the coordinate system of a grid, recomputing it. Not resample_raster, "
        "which keeps the CRS and only changes resolution; not reproject_layer, which is for "
        "vector data.",
        'description': 'Reproject a raster into a target CRS. The resampling method has NO '
                       'DEFAULT on purpose: warping resamples, and an interpolating method '
                       '(bilinear, cubic, average) derives values that lie between the '
                       'ones present. On a continuous surface that is correct; on class '
                       'codes it invents classes that were never in the file, and every '
                       'later count or area for those codes is fabricated with nothing '
                       'raising. Use nearest or mode for categorical rasters. The output '
                       'grid is recomputed for the target CRS, so its shape and extent '
                       'change; what is verified is that the output really is in the '
                       'requested CRS and that an interpolating method did not add codes '
                       'to a categorical raster (reported as a warning with the invented '
                       'values listed). Requires: pip install mapsmith[raster].',
        'parameters': [   {   'name': 'input_path',
                              'type': 'str',
                              'required': True,
                              'description': 'Source raster (GeoTIFF); a raster with no '
                                             'CRS is refused'},
                          {   'name': 'output_path',
                              'type': 'str',
                              'required': True,
                              'description': 'Output GeoTIFF path'},
                          {   'name': 'target_crs',
                              'type': 'str',
                              'required': True,
                              'description': "Target CRS, e.g. 'EPSG:32632'"},
                          {   'name': 'resampling',
                              'type': 'str',
                              'required': True,
                              'description': 'nearest | bilinear | cubic | cubic_spline | '
                                             'lanczos | average | mode | max | min | med | '
                                             'q1 | q3 — required: interpolating methods '
                                             'corrupt class codes silently'}],
        'examples': [   {   'goal': 'Bring an elevation model into UTM 32N before any '
                                    'metric analysis',
                            'call': {   'tool': 'run_operation',
                                        'arguments': {   'operation': 'reproject_raster',
                                                         'arguments': {   'input_path': 'dem_wgs84.tif',
                                                                          'output_path': 'dem_utm.tif',
                                                                          'target_crs': 'EPSG:32632',
                                                                          'resampling': 'bilinear'}}}},
                        {   'goal': 'Reproject a land-cover raster without inventing new '
                                    'classes',
                            'call': {   'tool': 'run_operation',
                                        'arguments': {   'operation': 'reproject_raster',
                                                         'arguments': {   'input_path': 'landcover_utm.tif',
                                                                          'output_path': 'landcover_wgs84.tif',
                                                                          'target_crs': 'EPSG:4326',
                                                                          'resampling': 'nearest'}}}}]},
    {   'name': 'extract_band',
        'status': 'available',
        'tool': None,
        'workload': 'raster',
        'category': 'raster',
        "produces": "dataset:raster",
        'applicability': {'inputs': ['raster'], 'requires_projected_crs': False, 'dataset_inputs': 1},
        'summary': 'Write one band of a multi-band raster to a single-band raster; '
                   '1-based, out-of-range refused. Requires the [raster] extra',
        "phrasings": "pull one band out; just the red channel; separate the layers of a composite",
        "distinguishes": "Pulls one band out into its own file. Not band_math, which combines bands into a "
        "new value; not band_statistics, which reports on them without writing anything.",
        'description': 'Extract one band of a multi-band raster into a single-band '
                       'GeoTIFF, keeping the grid, the CRS and the band description. Bands '
                       'are numbered FROM 1, as in GDAL and rasterio and unlike Python: '
                       'band 0 and any band past the end are refused rather than clamped, '
                       'because an off-by-one here produces a perfectly valid raster of '
                       'the wrong quantity — near-infrared where red was meant — that '
                       'nothing downstream can detect. The band that landed is verified '
                       "against the source band's own checksum, not against the index that "
                       'was passed. Use describe_dataset first if you do not know the band '
                       'order. Requires: pip install mapsmith[raster].',
        'parameters': [   {   'name': 'input_path',
                              'type': 'str',
                              'required': True,
                              'description': 'Multi-band raster (GeoTIFF)'},
                          {   'name': 'output_path',
                              'type': 'str',
                              'required': True,
                              'description': 'Output single-band GeoTIFF path'},
                          {   'name': 'band',
                              'type': 'int',
                              'required': True,
                              'description': 'Band number, 1-based; out of range is '
                                             'refused'}],
        'examples': [   {   'goal': 'Pull the red band out of a 4-band Sentinel composite',
                            'call': {   'tool': 'run_operation',
                                        'arguments': {   'operation': 'extract_band',
                                                         'arguments': {   'input_path': 'composite.tif',
                                                                          'output_path': 'red.tif',
                                                                          'band': 3}}}},
                        {   'goal': 'Isolate the first band of a multi-temporal stack for '
                                    'a single-date map',
                            'call': {   'tool': 'run_operation',
                                        'arguments': {   'operation': 'extract_band',
                                                         'arguments': {   'input_path': 'stack.tif',
                                                                          'output_path': 'date1.tif',
                                                                          'band': 1}}}}]},
    {   'name': 'band_statistics',
        'status': 'available',
        'tool': None,
        'workload': 'raster',
        'category': 'inspection',
        "produces": "answer",
        'applicability': {'inputs': ['raster'], 'requires_projected_crs': False, 'dataset_inputs': 1},
        'summary': 'Per-band min/max/mean/std/sum over the VALID cells only, with the '
                   'masked count. Reads, writes nothing. Requires the [raster] extra',
        "phrasings": "min and max of the grid; what range are these values; ignore the nodata",
        "distinguishes": "Min, max, mean of a whole grid, over the valid cells only. Not zonal_statistics, "
        "which needs polygons to summarise within; not describe_dataset, which reports the "
        "shape and the CRS rather than the values.",
        'description': 'Per-band statistics computed over the valid cells only: nodata is '
                       'excluded and how many cells that removed travels with every '
                       'statistic, so the caller can see what the mean is a mean OF. This '
                       'matters more than it sounds: a mean over a raster whose nodata is '
                       '-9999 and whose mask was ignored is the classic wrong number that '
                       'still looks like an elevation. A band where every cell is nodata '
                       'reports all_masked instead of a mean, because the mean of an empty '
                       'selection is not an answer. Read-only — no output, no manifest. '
                       'Requires: pip install mapsmith[raster].',
        'parameters': [   {   'name': 'input_path',
                              'type': 'str',
                              'required': True,
                              'description': 'Raster to inspect (GeoTIFF)'},
                          {   'name': 'band',
                              'type': 'int',
                              'required': False,
                              'description': 'One band (1-based); omit for every band'}],
        'examples': [   {   'goal': 'Check the elevation range of a DEM before choosing '
                                    'contour intervals',
                            'call': {   'tool': 'run_operation',
                                        'arguments': {   'operation': 'band_statistics',
                                                         'arguments': {   'input_path': 'dem_utm.tif'}}}},
                        {   'goal': 'Get the mean of band 2 alone, excluding nodata',
                            'call': {   'tool': 'run_operation',
                                        'arguments': {   'operation': 'band_statistics',
                                                         'arguments': {   'input_path': 'composite.tif',
                                                                          'band': 2}}}}]},
    {   'name': 'curvature',
        'status': 'available',
        'tool': None,
        'workload': 'raster',
        'category': 'terrain',
        "produces": "dataset:raster",
        'applicability': {'inputs': ['raster'], 'requires_projected_crs': True, 'dataset_inputs': 1},
        'summary': 'Surface curvature from a DEM; the kind is required (profile, plan, '
                   'tangential, mean, gaussian, total). Requires the [whitebox] extra',
        "phrasings": "where does the hillslope curve; convex and concave ground; hollows and ridges",
        "distinguishes": "Second derivative of the surface: where it bends. Not slope, which is the first "
        "derivative; profile curvature is along the slope and plan curvature across it, and "
        "they answer opposite questions about the same cell.",
        'description': 'Curvature of a terrain surface. The kind has NO DEFAULT because '
                       "the kinds answer opposite questions about the same cell: 'profile' "
                       'is curvature ALONG the slope, where flow accelerates and '
                       "decelerates, and 'plan' is curvature ACROSS it, where flow "
                       'converges and diverges — a hillslope can be convex in profile and '
                       'concave in plan at the same time. A caller who wanted convergence '
                       'and got acceleration receives a plausible raster of the wrong '
                       "quantity with no way to tell, so the kind is asked for. 'mean', "
                       "'gaussian', 'total' and 'tangential' are the standard surface "
                       'invariants. Curvature is a second derivative in units of 1/length, '
                       'so no range check applies; DEMs in a geographic CRS are refused, '
                       'as for slope. Requires: pip install mapsmith[whitebox].',
        'parameters': [   {   'name': 'dem_path',
                              'type': 'str',
                              'required': True,
                              'description': 'Digital elevation model (GeoTIFF, projected '
                                             'CRS required)'},
                          {   'name': 'output_path',
                              'type': 'str',
                              'required': True,
                              'description': 'Output GeoTIFF path'},
                          {   'name': 'kind',
                              'type': 'str',
                              'required': True,
                              'description': 'profile | plan | tangential | mean | '
                                             'gaussian | total — required: profile and '
                                             'plan answer opposite questions'},
                          {   'name': 'z_factor',
                              'type': 'float',
                              'required': False,
                              'description': 'Vertical unit conversion factor (default '
                                             '1.0)'}],
        'examples': [   {   'goal': 'Find convergent hollows where runoff concentrates',
                            'call': {   'tool': 'run_operation',
                                        'arguments': {   'operation': 'curvature',
                                                         'arguments': {   'dem_path': 'dem_utm.tif',
                                                                          'output_path': 'plan_curv.tif',
                                                                          'kind': 'plan'}}}},
                        {   'goal': 'Map slope breaks where flow accelerates, for erosion '
                                    'screening',
                            'call': {   'tool': 'run_operation',
                                        'arguments': {   'operation': 'curvature',
                                                         'arguments': {   'dem_path': 'dem_utm.tif',
                                                                          'output_path': 'profile_curv.tif',
                                                                          'kind': 'profile'}}}}]},
    {   'name': 'flow_direction',
        'status': 'available',
        'tool': None,
        'workload': 'raster',
        'category': 'hydrology',
        "produces": "dataset:raster",
        'applicability': {'inputs': ['raster'], 'requires_projected_crs': True, 'dataset_inputs': 1},
        'summary': 'Flow-direction pointer raster (d8, rho8, dinf, fd8) whose direction '
                   'TABLE is written into the manifest. Requires the [whitebox] extra',
        "phrasings": "where the rain runs off; which way water leaves; drainage direction",
        "distinguishes": "A pointer grid saying which way water leaves each cell, and the direction TABLE "
        "travels with it. Not flow_accumulation, which says how much arrives; not aspect, "
        "which is the compass direction a slope faces and has nothing to do with routing.",
        'description': "Flow direction from a DEM. 'd8' sends all of a cell's water to its "
                       "steepest neighbour and 'dinf' splits it between two, which is the "
                       'difference between a drainage network that looks like a line and '
                       "one that looks like a fan; 'rho8' is d8 with a stochastic "
                       "tie-break and 'fd8' spreads over all downslope neighbours. A "
                       'pointer raster is a grid of small integers whose MEANING lives '
                       'outside the file, and the two conventions in use disagree on every '
                       'direction: in the northeast_first table 1 is northeast, in the '
                       'east_first table '
                       '1 is east. Read a raster with the wrong table and every cell '
                       'points somewhere else — the network stays connected, stays '
                       'plausible, and drains the wrong way. So the manifest carries the '
                       'WHOLE TABLE by direction name for the encoding used, and a '
                       'consumer never has to guess which engine wrote the file. '
                       "'encoding' selects the table (northeast_first, this engine's default, "
                       'or east_first, what most desktop GIS software writes) and is '
                       'refused for dinf and fd8, which write '
                       'continuous values and have no table. Requires: pip install '
                       'mapsmith[whitebox].',
        'parameters': [   {   'name': 'dem_path',
                              'type': 'str',
                              'required': True,
                              'description': 'Digital elevation model (GeoTIFF, projected '
                                             'CRS required)'},
                          {   'name': 'output_path',
                              'type': 'str',
                              'required': True,
                              'description': 'Output GeoTIFF path'},
                          {   'name': 'method',
                              'type': 'str',
                              'required': False,
                              'description': 'd8 (default) | rho8 | dinf | fd8'},
                          {   'name': 'encoding',
                              'type': 'str',
                              'required': False,
                              'description': 'northeast_first (default) | east_first — the '
                                             'direction-code table, written into the '
                                             'manifest; refused for dinf and fd8'}],
        'examples': [   {   'goal': 'D8 pointer raster as the input to a watershed '
                                    'delineation',
                            'call': {   'tool': 'run_operation',
                                        'arguments': {   'operation': 'flow_direction',
                                                         'arguments': {   'dem_path': 'dem_filled.tif',
                                                                          'output_path': 'd8.tif',
                                                                          'method': 'd8'}}}},
                        {   'goal': 'A pointer raster another team will read with the '
                                    'east-first convention most desktop GIS uses',
                            'call': {   'tool': 'run_operation',
                                        'arguments': {   'operation': 'flow_direction',
                                                         'arguments': {   'dem_path': 'dem_filled.tif',
                                                                          'output_path': 'd8_east_first.tif',
                                                                          'method': 'd8',
                                                                          'encoding': 'east_first'}}}}]},
    {   'name': 'euclidean_distance',
        'status': 'available',
        'tool': None,
        'workload': 'raster',
        'category': 'raster',
        "produces": "dataset:raster",
        'applicability': {'inputs': ['raster'], 'requires_projected_crs': True, 'dataset_inputs': 1},
        'summary': "Distance from every cell to the nearest non-zero cell, in the CRS's "
                   'own units. Requires the [whitebox] extra',
        "phrasings": "how far is every place from the nearest one; proximity surface; distance to roads",
        "distinguishes": "A grid where every cell holds its distance to the nearest feature. Not "
        "geodetic_distance, which answers about two points and writes nothing; not "
        "buffer_layer, which draws one fixed ring instead of a continuous surface.",
        'description': 'A proximity surface: every cell gets its distance to the nearest '
                       "non-zero cell, measured in the raster's own horizontal unit. A "
                       'geographic CRS is REFUSED, because a distance in degrees is not a '
                       'distance — a degree of longitude is 111 km at the equator and 83 '
                       'km in Rome — and it would come back as a number that looks like '
                       'metres. Note what counts as a source: whitebox treats the NON-ZERO '
                       'cells as the features, so a mask of 1s on a background of 0s '
                       'behaves as expected, while a mask whose background is nodata '
                       'rather than 0 does not. Verified against the grid itself: no '
                       "distance is negative, and none exceeds the grid's own diagonal. "
                       'Requires: pip install mapsmith[whitebox].',
        'parameters': [   {   'name': 'input_path',
                              'type': 'str',
                              'required': True,
                              'description': 'Source raster (GeoTIFF, projected CRS '
                                             'required); non-zero cells are the features '
                                             'to measure from'},
                          {   'name': 'output_path',
                              'type': 'str',
                              'required': True,
                              'description': 'Output GeoTIFF path'}],
        'examples': [   {   'goal': 'Distance to the nearest road cell, for an '
                                    'accessibility surface',
                            'call': {   'tool': 'run_operation',
                                        'arguments': {   'operation': 'euclidean_distance',
                                                         'arguments': {   'input_path': 'roads_mask.tif',
                                                                          'output_path': 'dist_roads.tif'}}}},
                        {   'goal': 'Distance from surface water, as a habitat-suitability '
                                    'input',
                            'call': {   'tool': 'run_operation',
                                        'arguments': {   'operation': 'euclidean_distance',
                                                         'arguments': {   'input_path': 'water_mask.tif',
                                                                          'output_path': 'dist_water.tif'}}}}]},
    {   'name': 'idw_interpolation',
        'status': 'available',
        'tool': None,
        'workload': 'raster',
        'category': 'raster',
        "produces": "dataset:raster",
        # True since 0.3.0, and it was a claim rather than an oversight: a caller
        # who honestly passed projected=False was OFFERED an operation that
        # weights samples by distance, on a CRS where distance is degrees.
        'applicability': {'inputs': ['vector'], 'requires_projected_crs': True, 'dataset_inputs': 1},
        'summary': 'Inverse-distance-weighted surface from a point layer; the field is '
                   'REQUIRED. Requires the [whitebox] extra',
        "phrasings": "a surface from scattered measurements; fill in between the gauges; interpolate the readings",
        "distinguishes": "Builds a continuous surface from scattered point measurements. Not "
        "voronoi_polygons, which gives each point a hard territory instead of a gradient; "
        "not focal_statistics, which needs a grid to start from.",
        'description': 'Build a continuous surface from scattered point measurements by '
                       "inverse-distance weighting. 'field_name' is REQUIRED here because "
                       "of the library's default: whitebox interpolates FID when you do "
                       'not say otherwise, which produces a smooth, plausible and '
                       'perfectly meaningless surface of ROW NUMBERS — nothing raises and '
                       "the raster renders. 'weight' is the distance exponent (2 by "
                       'default; higher makes the surface flatter between points and '
                       'peakier at them) and it is recorded, because an IDW surface '
                       "without its exponent cannot be reproduced. 'cell_size' is read in "
                       "the point layer's own CRS units, so check them with describe_crs "
                       'first if the layer is geographic. Requires: pip install '
                       'mapsmith[whitebox].',
        'parameters': [   {   'name': 'points_path',
                              'type': 'str',
                              'required': True,
                              'description': 'Point layer with the values to interpolate'},
                          {   'name': 'output_path',
                              'type': 'str',
                              'required': True,
                              'description': 'Output GeoTIFF path'},
                          {   'name': 'field_name',
                              'type': 'str',
                              'required': True,
                              'description': 'Attribute to interpolate — required: the '
                                             'library default interpolates row numbers '
                                             'without warning'},
                          {   'name': 'cell_size',
                              'type': 'float',
                              'required': True,
                              'description': "Output cell size, in the layer's CRS units"},
                          {   'name': 'weight',
                              'type': 'float',
                              'required': False,
                              'description': 'Distance exponent (default 2.0)'},
                          {   'name': 'radius',
                              'type': 'float',
                              'required': False,
                              'description': 'Search radius in CRS units; 0 = unlimited '
                                             '(default)'},
                          {   'name': 'min_points',
                              'type': 'int',
                              'required': False,
                              'description': 'Minimum points per cell (default 0)'}],
        'examples': [   {   'goal': 'Rainfall surface from gauge measurements at 100 m',
                            'call': {   'tool': 'run_operation',
                                        'arguments': {   'operation': 'idw_interpolation',
                                                         'arguments': {   'points_path': 'gauges.gpkg',
                                                                          'output_path': 'rainfall.tif',
                                                                          'field_name': 'mm_year',
                                                                          'cell_size': 100.0}}}},
                        {   'goal': 'Groundwater level surface, weighting near wells '
                                    'harder',
                            'call': {   'tool': 'run_operation',
                                        'arguments': {   'operation': 'idw_interpolation',
                                                         'arguments': {   'points_path': 'wells.parquet',
                                                                          'output_path': 'water_table.tif',
                                                                          'field_name': 'level_m',
                                                                          'cell_size': 50.0,
                                                                          'weight': 3.0}}}}]},
    {   'name': 'voronoi_polygons',
        'status': 'available',
        'tool': None,
        'workload': 'small_vector',
        'category': 'vector',
        "produces": "dataset:vector",
        'applicability': {'inputs': ['vector'], 'requires_projected_crs': False, 'dataset_inputs': 1},
        'summary': 'Thiessen polygons from points, each verified to hold its own point; '
                   'the clipping boundary is declared',
        "phrasings": "every place goes to its closest one; catchment areas; service area per shop; Thiessen",
        "distinguishes": "Partitions a region so every place belongs to its nearest point. Not buffer_layer, "
        "whose radius is fixed and whose circles overlap; here the boundaries fall where "
        "two points are equidistant and the cells tile the area exactly once.",
        'description': 'Thiessen polygons: the area closer to each point than to any '
                       "other, with each point's attributes on its own cell. Two things "
                       'about a Voronoi diagram are easy to get wrong and impossible to '
                       'see afterwards, so both are handled here. The JOIN: the cells come '
                       'back in an order that is an implementation detail, so pairing them '
                       'with the input rows positionally puts every attribute on a '
                       "neighbour's cell — a map correct in shape and wrong in every "
                       'value. This asks for the ordered form and then VERIFIES the join '
                       'geometrically, cell by cell, which a declaration cannot do. The '
                       'BOUNDARY: the outermost cells are mathematically infinite, so '
                       'every real Voronoi layer is clipped and the clip decides their '
                       "areas — 'boundary' says which one was used (envelope or "
                       "convex_hull), 'margin_fraction' expands it, and both are recorded "
                       'with a note that the outer areas are partly a property of that '
                       'choice. Point layers only: polygons are refused rather than '
                       'silently reduced to their vertices.',
        'parameters': [   {   'name': 'input_path',
                              'type': 'str',
                              'required': True,
                              'description': 'Point layer (at least 2 usable points)'},
                          {   'name': 'output_path',
                              'type': 'str',
                              'required': True,
                              'description': 'Output path (.parquet, .gpkg or .geojson)'},
                          {   'name': 'boundary',
                              'type': 'str',
                              'required': False,
                              'description': 'envelope (default) | convex_hull — what the '
                                             'infinite outer cells are clipped to'},
                          {   'name': 'margin_fraction',
                              'type': 'float',
                              'required': False,
                              'description': 'Expand the boundary by this fraction of its '
                                             'larger side (default 0.0)'}],
        'examples': [   {   'goal': 'Catchment areas for a set of retail stores',
                            'call': {   'tool': 'run_operation',
                                        'arguments': {   'operation': 'voronoi_polygons',
                                                         'arguments': {   'input_path': 'stores.gpkg',
                                                                          'output_path': 'catchments.parquet'}}}},
                        {   'goal': 'Service areas around weather stations, clipped to '
                                    'their convex hull',
                            'call': {   'tool': 'run_operation',
                                        'arguments': {   'operation': 'voronoi_polygons',
                                                         'arguments': {   'input_path': 'stations.parquet',
                                                                          'output_path': 'service_areas.parquet',
                                                                          'boundary': 'convex_hull'}}}}]},
    {   'name': 'describe_crs',
        'status': 'available',
        'tool': None,
        'workload': 'small_vector',
        'category': 'inspection',
        "produces": "description",
        'applicability': {'inputs': ['none'], 'requires_projected_crs': False, 'dataset_inputs': 0},
        'summary': 'What a CRS actually declares: axis order, unit and its factor to the '
                   'metre, datum, ellipsoid, area of use. Reads no data',
        "phrasings": "is this in feet or metres; what unit; which axis comes first; is it projected or degrees",
        "distinguishes": "Answers about a coordinate system itself: its unit, its axis order, its datum. Not "
        "describe_dataset, which needs a file; ask this when the question is what the "
        "numbers in a file MEAN.",
        'description': 'Everything about a coordinate reference system that changes the '
                       "meaning of a number computed in it. Accepts 'EPSG:4326', the bare "
                       'code 4326, a PROJ string or WKT. Four fields are worth reading '
                       'before anything else, because each one turns a plausible number '
                       "into a wrong one on its own. 'axis_order' is 'lat,lon' or "
                       "'lon,lat': EPSG:4326 declares LATITUDE first while most software "
                       'and every GeoJSON put longitude first, and this reports what the '
                       "CRS declares rather than the habit. 'unit' and 'unit_to_metre': a "
                       'length in EPSG:2263 comes out in US survey feet, and '
                       '0.30480060960121924 is not 0.3048 — over a state plane that '
                       "difference is centimetres per kilometre. 'kind' says geographic or "
                       'projected, and any distance or area computed in a geographic CRS '
                       'is in degrees, which is not a length at any latitude. '
                       "'area_of_use': outside it a projected CRS still returns numbers "
                       'and a datum transformation may fall back to a ballpark one. '
                       "'is_deprecated' is reported too, because a superseded EPSG code "
                       'keeps working and keeps giving the answer its superseded '
                       'definition implies. Read-only: no dataset is touched, no manifest '
                       'is written.',
        'parameters': [   {   'name': 'crs',
                              'type': 'str',
                              'required': True,
                              'description': "CRS in any form pyproj accepts: 'EPSG:4326', "
                                             '4326, a PROJ string, or WKT'}],
        'examples': [   {   'goal': "Check whether a layer's CRS is metric before "
                                    'measuring anything in it',
                            'call': {   'tool': 'run_operation',
                                        'arguments': {   'operation': 'describe_crs',
                                                         'arguments': {   'crs': 'EPSG:2263'}}}},
                        {   'goal': 'Find out which axis comes first in the CRS a file '
                                    'declares',
                            'call': {   'tool': 'run_operation',
                                        'arguments': {   'operation': 'describe_crs',
                                                         'arguments': {   'crs': 'EPSG:4326'}}}}]},
    {   'name': 'geodetic_distance',
        'status': 'available',
        'tool': None,
        'workload': 'small_vector',
        'category': 'inspection',
        "produces": "answer",
        'applicability': {'inputs': ['none'], 'requires_projected_crs': False, 'dataset_inputs': 0},
        'summary': 'Distance and azimuths between two lon/lat points measured on the '
                   'ellipsoid — no projection involved. Reads no data',
        "phrasings": "how far apart are two places as the crow flies; true distance between coordinates; without picking a projection",
        "distinguishes": "How far apart two coordinates are on the ellipsoid, with no projection chosen and "
        "no file read. Not euclidean_distance, which builds a raster surface; not "
        "nearest_join, which needs two layers.",
        'description': 'The distance between two places, measured along the ellipsoid, '
                       'which is the answer no projection can spoil. A distance computed '
                       "in a projected CRS is a distance on that projection's plane, and "
                       'the two differ by a factor that grows with latitude — at 42 '
                       'degrees Web Mercator is off by about 1.8x and returns a number in '
                       "metres that looks correct. Here there is no plane: Karney's "
                       'algorithm measures along the ellipsoid, accurate to nanometres '
                       'anywhere on Earth. Coordinates are LONGITUDE FIRST, which is in '
                       'the parameter names because the swap is the commonest error in '
                       'this signature and it returns a valid distance between the wrong '
                       'two places; latitudes outside +-90 are refused for the same '
                       'reason. Returns metres plus the forward and back azimuths in '
                       'degrees clockwise from north — the forward azimuth is the bearing '
                       'at the start, not for the whole way, since on a geodesic it '
                       'changes continuously except along the equator and the meridians. '
                       "'ellipsoid' is a fixed list, not free text: a name silently "
                       'falling back to WGS84 would move a legacy answer by hundreds of '
                       'metres. Read-only: no dataset, no manifest.',
        'parameters': [   {   'name': 'from_lon',
                              'type': 'float',
                              'required': True,
                              'description': 'Start longitude in degrees, [-180, 180]'},
                          {   'name': 'from_lat',
                              'type': 'float',
                              'required': True,
                              'description': 'Start latitude in degrees, [-90, 90]'},
                          {   'name': 'to_lon',
                              'type': 'float',
                              'required': True,
                              'description': 'End longitude in degrees, [-180, 180]'},
                          {   'name': 'to_lat',
                              'type': 'float',
                              'required': True,
                              'description': 'End latitude in degrees, [-90, 90]'},
                          {   'name': 'ellipsoid',
                              'type': 'str',
                              'required': False,
                              'description': 'WGS84 (default) | GRS80 | WGS72 | intl | '
                                             'clrk66 | airy | bessel — for reproducing a '
                                             'legacy number on its own ellipsoid'}],
        'examples': [   {   'goal': 'True ground distance between two cities, without '
                                    'choosing a projection',
                            'call': {   'tool': 'run_operation',
                                        'arguments': {   'operation': 'geodetic_distance',
                                                         'arguments': {   'from_lon': 12.4964,
                                                                          'from_lat': 41.9028,
                                                                          'to_lon': 9.19,
                                                                          'to_lat': 45.4642}}}},
                        {   'goal': 'Reproduce a historical distance computed on the '
                                    'International 1924 ellipsoid',
                            'call': {   'tool': 'run_operation',
                                        'arguments': {   'operation': 'geodetic_distance',
                                                         'arguments': {   'from_lon': 12.4964,
                                                                          'from_lat': 41.9028,
                                                                          'to_lon': 9.19,
                                                                          'to_lat': 45.4642,
                                                                          'ellipsoid': 'intl'}}}}]},
    {   'name': 'sample_raster_at_points',
        'status': 'available',
        'tool': None,
        'workload': 'raster',
        'category': 'raster',
        "produces": "dataset:vector",
        'applicability': {'inputs': ['raster', 'vector'], 'requires_projected_crs': False, 'dataset_inputs': 2},
        'summary': "The raster's value at each point, with the ones it could not read "
                   'counted rather than filled in. Requires the [raster] extra',
        "phrasings": "what is the elevation at each of my survey shots; read the grid "
                     "at these locations; are my levels off compared to the surface",
        "distinguishes": "Reads ONE cell per point. Not zonal_statistics, which "
                         "summarises a raster inside polygons and weights partial "
                         "pixels; not band_statistics, which describes a whole grid "
                         "with no locations at all.",
        'description': "Adds a column holding the raster's value at every point. "
                       "method is required and has no default because the two are "
                       "right for different data: 'nearest' returns the cell the "
                       "point falls in, which is what class codes need, and "
                       "'bilinear' interpolates the four surrounding cell CENTRES, "
                       "which is what a survey comparison needs \u2014 a total station "
                       "shot does not land on a cell centre, and snapping it there "
                       "adds up to half a cell of horizontal error to a vertical "
                       "difference somebody is about to call a datum offset. A point "
                       "outside the raster or on a nodata cell comes back NULL rather "
                       "than as the nodata value, and the count of those is a "
                       "non-critical check in the manifest: a table with silent nulls "
                       "averages to a number nobody can defend. Points are reprojected "
                       "to the raster's CRS with the decision recorded.",
        'parameters': [{'name': 'raster_path', 'type': 'str', 'required': True,
                        'description': 'Raster to read (must have a CRS)'},
                       {'name': 'points_path', 'type': 'str', 'required': True,
                        'description': 'Point layer giving the positions'},
                       {'name': 'output_path', 'type': 'str', 'required': True,
                        'description': 'Output path (.parquet or .gpkg) with the value column added'},
                       {'name': 'method', 'type': 'str', 'required': True,
                        'description': "'nearest' for class codes, 'bilinear' for a continuous surface"},
                       {'name': 'band', 'type': 'int', 'required': False,
                        'description': '1-based band number (default 1)'},
                       {'name': 'column_name', 'type': 'str', 'required': False,
                        'description': "Column to write (default 'value'); refuses to overwrite an existing one"}],
        'examples': [{'goal': 'Are my levelling shots consistently above the city surface model?',
                      'call': {'tool': 'run_operation',
                               'arguments': {'operation': 'sample_raster_at_points',
                                             'arguments': {'raster_path': 'surface.tif',
                                                           'points_path': 'shots.gpkg',
                                                           'output_path': 'shots_vs_surface.parquet',
                                                           'method': 'bilinear'}}}},
                     {'goal': 'Which land-cover class is each sensor standing on?',
                      'call': {'tool': 'run_operation',
                               'arguments': {'operation': 'sample_raster_at_points',
                                             'arguments': {'raster_path': 'landcover.tif',
                                                           'points_path': 'sensors.gpkg',
                                                           'output_path': 'sensors_class.parquet',
                                                           'method': 'nearest'}}}}]},
    {   'name': 'elevation_profile',
        'status': 'available',
        'tool': None,
        'workload': 'raster',
        'category': 'terrain',
        "produces": "dataset:vector",
        'applicability': {'inputs': ['raster', 'vector'], 'requires_projected_crs': True, 'dataset_inputs': 2},
        'summary': 'One point every N metres along each line, carrying the surface '
                   'value and the distance travelled. Requires the [raster] extra',
        "phrasings": "elevation every 20 metres along the centreline so I can plot it; "
                     "how does the ground change along this slice; cross section of the "
                     "terrain",
        "distinguishes": "Walks a line and reads the surface at a fixed step. Not "
                         "sample_raster_at_points, which reads at positions you already "
                         "have; not slope, which describes steepness everywhere rather "
                         "than along one route.",
        'description': 'Produces a point layer with distance, value, point_index and '
                       'line_index, ordered along each line. The spacing is a length '
                       "in the raster's own linear unit, so a geographic CRS is "
                       'refused: 20 of a degree is not 20 metres and the distance axis '
                       'would mean nothing at a plausible-looking scale. Both ends of '
                       'each line are always included, the far one clamped to the '
                       "line's length when the step does not divide evenly \u2014 a "
                       'profile that silently stops short of the summit is the worst '
                       'kind of nearly-right. The point count is checked in closed '
                       'form against floor(length/spacing) + 1.',
        'parameters': [{'name': 'raster_path', 'type': 'str', 'required': True,
                        'description': 'Surface to read, usually a DEM'},
                       {'name': 'line_path', 'type': 'str', 'required': True,
                        'description': 'Line layer to walk (projected CRS)'},
                       {'name': 'output_path', 'type': 'str', 'required': True,
                        'description': 'Output point layer (.parquet or .gpkg)'},
                       {'name': 'spacing', 'type': 'float', 'required': True,
                        'description': "Step between samples, in the raster CRS's linear unit"},
                       {'name': 'method', 'type': 'str', 'required': False,
                        'description': "'bilinear' (default) or 'nearest'"},
                       {'name': 'band', 'type': 'int', 'required': False,
                        'description': '1-based band number (default 1)'}],
        'examples': [{'goal': 'Elevation every 20 metres along the road centreline, to plot a profile',
                      'call': {'tool': 'run_operation',
                               'arguments': {'operation': 'elevation_profile',
                                             'arguments': {'raster_path': 'dem.tif',
                                                           'line_path': 'centreline.gpkg',
                                                           'output_path': 'profile.parquet',
                                                           'spacing': 20.0}}}},
                     {'goal': 'How the ground changes along a straight slice from the ridge to the valley',
                      'call': {'tool': 'run_operation',
                               'arguments': {'operation': 'elevation_profile',
                                             'arguments': {'raster_path': 'dem.tif',
                                                           'line_path': 'slice.gpkg',
                                                           'output_path': 'slice_profile.parquet',
                                                           'spacing': 5.0}}}}]},
    {   'name': 'line_of_sight',
        'status': 'available',
        'tool': None,
        'workload': 'raster',
        'category': 'terrain',
        "produces": "answer",
        'applicability': {'inputs': ['raster'], 'requires_projected_crs': True, 'dataset_inputs': 1},
        'summary': 'Whether the terrain blocks the view between two positions, and '
                   'where it first does. Requires the [raster] extra',
        "phrasings": "line of sight check, is the ridge blocking it; can these two "
                     "masts see each other; why is there a dead zone behind the hill",
        "distinguishes": "Two positions, one answer. Not viewshed, which maps "
                         "everything visible FROM a set of stations and cannot raise "
                         "the target end; not elevation_profile, which gives the shape "
                         "of the ground without deciding whether it blocks anything.",
        'description': 'Samples the terrain along the sight line at one sample per '
                       'cell and reports whether anything rises above it, the distance '
                       'at which it first does, and the minimum clearance. '
                       'earth_curvature has no default: over 5 km the planet drops '
                       'about 1.7 m below the tangent plane and over 30 km about 62 m '
                       'net of refraction, so a flat-Earth answer is right for a '
                       'rooftop survey and badly wrong for a radio link, and there is '
                       'no way to guess which the caller has. When true the standard '
                       'refraction coefficient of 0.13 is applied with it, because '
                       'curvature without refraction over-corrects. Observer and '
                       'target heights are added to the ground elevation at each end.',
        'parameters': [{'name': 'raster_path', 'type': 'str', 'required': True,
                        'description': 'DEM in a projected CRS'},
                       {'name': 'observer_x', 'type': 'float', 'required': True,
                        'description': "Observer easting in the raster's CRS"},
                       {'name': 'observer_y', 'type': 'float', 'required': True,
                        'description': "Observer northing in the raster's CRS"},
                       {'name': 'target_x', 'type': 'float', 'required': True,
                        'description': "Target easting in the raster's CRS"},
                       {'name': 'target_y', 'type': 'float', 'required': True,
                        'description': "Target northing in the raster's CRS"},
                       {'name': 'earth_curvature', 'type': 'bool', 'required': True,
                        'description': 'Whether to lower the sight line by the curvature sagitta, with refraction'},
                       {'name': 'observer_height', 'type': 'float', 'required': False,
                        'description': 'Height above ground at the observer (default 0)'},
                       {'name': 'target_height', 'type': 'float', 'required': False,
                        'description': 'Height above ground at the target (default 0)'},
                       {'name': 'samples', 'type': 'int', 'required': False,
                        'description': 'Samples along the line (default: one per cell)'},
                       {'name': 'band', 'type': 'int', 'required': False,
                        'description': '1-based band number (default 1)'}],
        'examples': [{'goal': 'Is the ridge blocking the view between these two sites?',
                      'call': {'tool': 'run_operation',
                               'arguments': {'operation': 'line_of_sight',
                                             'arguments': {'raster_path': 'dem.tif',
                                                           'observer_x': 512340.0, 'observer_y': 4928110.0,
                                                           'target_x': 519880.0, 'target_y': 4931450.0,
                                                           'earth_curvature': False,
                                                           'observer_height': 10.0,
                                                           'target_height': 15.0}}}},
                     {'goal': 'A 30 km microwave link: does the Earth itself get in the way?',
                      'call': {'tool': 'run_operation',
                               'arguments': {'operation': 'line_of_sight',
                                             'arguments': {'raster_path': 'dem.tif',
                                                           'observer_x': 500000.0, 'observer_y': 4900000.0,
                                                           'target_x': 530000.0, 'target_y': 4900000.0,
                                                           'earth_curvature': True,
                                                           'observer_height': 40.0,
                                                           'target_height': 40.0}}}}]},
    {   'name': 'viewshed',
        'status': 'available',
        'tool': None,
        'workload': 'raster',
        'category': 'terrain',
        "produces": "dataset:raster",
        'applicability': {'inputs': ['raster', 'vector'], 'requires_projected_crs': True, 'dataset_inputs': 2},
        'summary': 'How many observing stations can see each cell \u2014 a COUNT, not a '
                   'yes/no. Requires the [whitebox] extra',
        "phrasings": "which areas can be seen from these towers; the coverage circles "
                     "ignore the hills and the client will ask about the dead zone; "
                     "what is visible from the summit",
        "distinguishes": "Maps everything visible from a set of stations. Not "
                         "line_of_sight, which answers about ONE pair of positions and "
                         "can raise the target end too; not euclidean_distance, which "
                         "measures how far away things are regardless of whether you "
                         "can see them.",
        'description': 'Each cell holds the NUMBER of stations that can see it, which '
                       "contradicts the tool's own documentation \u2014 Whitebox's help "
                       'says "a Boolean raster, containing 1\'s and 0\'s", and measured '
                       'on 2.0.6 with two stations on flat ground every cell comes back '
                       '2.0. Threshold at > 0 for a boolean; do not sum the raster '
                       'expecting an area. station_height has no default because its '
                       "unit is the DEM's Z unit rather than metres: on a DEM in US "
                       'survey feet, 2.0 is two feet. There is no target height in this '
                       'tool \u2014 only the observer is raised \u2014 so use '
                       'line_of_sight when the far end has a mast on it. A geographic '
                       'CRS is refused: the height would be in Z units against cell '
                       'sizes in degrees.',
        'parameters': [{'name': 'dem_path', 'type': 'str', 'required': True,
                        'description': 'DEM in a projected CRS'},
                       {'name': 'stations_path', 'type': 'str', 'required': True,
                        'description': 'Point layer of observing stations (at least one)'},
                       {'name': 'output_path', 'type': 'str', 'required': True,
                        'description': 'Output raster: count of stations that see each cell'},
                       {'name': 'station_height', 'type': 'float', 'required': True,
                        'description': "Observer height above ground, in the DEM's Z unit"}],
        'examples': [{'goal': 'Which streets can the two new masts actually cover, given the hills?',
                      'call': {'tool': 'run_operation',
                               'arguments': {'operation': 'viewshed',
                                             'arguments': {'dem_path': 'dem.tif',
                                                           'stations_path': 'masts.gpkg',
                                                           'output_path': 'coverage.tif',
                                                           'station_height': 25.0}}}},
                     {'goal': 'What can be seen from the lookout at eye height?',
                      'call': {'tool': 'run_operation',
                               'arguments': {'operation': 'viewshed',
                                             'arguments': {'dem_path': 'dem.tif',
                                                           'stations_path': 'lookout.gpkg',
                                                           'output_path': 'visible.tif',
                                                           'station_height': 1.7}}}}]},
    {   'name': 'network_shortest_path',
        'status': 'available',
        'tool': None,
        'workload': 'small_vector',
        'category': 'network',
        "produces": "dataset:vector",
        'applicability': {'inputs': ['vector'], 'requires_projected_crs': False, 'dataset_inputs': 1},
        'summary': 'Cheapest route between two positions over a line network, with the '
                   'connectivity of that network reported',
        "phrasings": "cheapest path to lay the cable avoiding steep slopes; how do I "
                     "actually drive from here to there; the route along the streets "
                     "not as the crow flies",
        "distinguishes": "Follows the network. Not nearest_join, which measures "
                         "straight-line distance to the closest feature; not "
                         "service_area, which is everything reachable rather than the "
                         "way to one place.",
        'description': 'Dijkstra over a graph built from the line layer, with junctions '
                       'formed by snapping endpoints. tolerance has no default and it '
                       'is the parameter that decides whether the answer means '
                       'anything: two segments a millimetre apart are one junction on '
                       'the ground and two nodes in a naive build, so the route detours '
                       'or comes back impossible \u2014 and nothing raises, because a '
                       'graph with a gap is a valid graph. The manifest carries the '
                       'number of connected components and the number of merged '
                       'endpoints, and how far each end snapped to the network. '
                       'cost_field is any non-negative numeric column (minutes, euros, '
                       'a slope-weighted length); without it the cost is geometric '
                       'length, which needs a projected CRS. Negative or missing costs '
                       'are refused rather than answered, because Dijkstra is only '
                       'correct on non-negative weights and would return a confident '
                       'wrong route.',
        'parameters': [{'name': 'network_path', 'type': 'str', 'required': True,
                        'description': 'Line layer forming the network'},
                       {'name': 'output_path', 'type': 'str', 'required': True,
                        'description': 'Output line layer: the route, with cumulative cost'},
                       {'name': 'from_x', 'type': 'float', 'required': True,
                        'description': 'Origin easting'},
                       {'name': 'from_y', 'type': 'float', 'required': True,
                        'description': 'Origin northing'},
                       {'name': 'to_x', 'type': 'float', 'required': True,
                        'description': 'Destination easting'},
                       {'name': 'to_y', 'type': 'float', 'required': True,
                        'description': 'Destination northing'},
                       {'name': 'tolerance', 'type': 'float', 'required': True,
                        'description': 'How far apart two endpoints can be and still be the same junction'},
                       {'name': 'cost_field', 'type': 'str', 'required': False,
                        'description': 'Non-negative numeric column to use as cost (default: geometric length)'},
                       {'name': 'oneway_field', 'type': 'str', 'required': False,
                        'description': 'Boolean column marking one-way edges (default: undirected)'}],
        'examples': [{'goal': 'The cheapest trench route from the substation, avoiding steep ground',
                      'call': {'tool': 'run_operation',
                               'arguments': {'operation': 'network_shortest_path',
                                             'arguments': {'network_path': 'corridors.gpkg',
                                                           'output_path': 'route.parquet',
                                                           'from_x': 512000.0, 'from_y': 4928000.0,
                                                           'to_x': 515500.0, 'to_y': 4930200.0,
                                                           'tolerance': 0.5,
                                                           'cost_field': 'slope_weighted_length'}}}},
                     {'goal': 'Quickest way between two depots along the road network',
                      'call': {'tool': 'run_operation',
                               'arguments': {'operation': 'network_shortest_path',
                                             'arguments': {'network_path': 'roads.gpkg',
                                                           'output_path': 'route.parquet',
                                                           'from_x': 512000.0, 'from_y': 4928000.0,
                                                           'to_x': 515500.0, 'to_y': 4930200.0,
                                                           'tolerance': 0.5,
                                                           'cost_field': 'minutes'}}}}]},
    {   'name': 'service_area',
        'status': 'available',
        'tool': None,
        'workload': 'small_vector',
        'category': 'network',
        "produces": "dataset:vector",
        'applicability': {'inputs': ['vector'], 'requires_projected_crs': False, 'dataset_inputs': 1},
        'summary': 'Every stretch of network reachable within a cost budget, with the '
                   'last segment cut where the budget runs out',
        "phrasings": "which blocks are more than a ten minute walk from a clinic; what "
                     "can be reached from here in twenty minutes; who is cut off if "
                     "these roads flood",
        "distinguishes": "Reach along the network. Not buffer_layer, which draws a "
                         "circle that includes the far side of a river with no bridge "
                         "and excludes the house 900 m away along a straight road; not "
                         "network_shortest_path, which is the way to one place rather "
                         "than everywhere.",
        'description': 'Dijkstra from one position, keeping every edge whose start is '
                       'within the budget and cutting the last one at the point where '
                       'the budget runs out, so the edge of the area is where the walk '
                       'actually ends rather than at the nearest junction. Each output '
                       'segment carries cost_at_start, cost_at_end and a partial flag. '
                       'The connectivity caveats of network_shortest_path apply '
                       'identically and are reported the same way: tolerance decides '
                       'whether the network is one piece, and a disconnected graph '
                       'makes destinations unreachable by construction rather than by '
                       'distance.',
        'parameters': [{'name': 'network_path', 'type': 'str', 'required': True,
                        'description': 'Line layer forming the network'},
                       {'name': 'output_path', 'type': 'str', 'required': True,
                        'description': 'Output line layer: the reachable stretches'},
                       {'name': 'from_x', 'type': 'float', 'required': True,
                        'description': 'Origin easting'},
                       {'name': 'from_y', 'type': 'float', 'required': True,
                        'description': 'Origin northing'},
                       {'name': 'budget', 'type': 'float', 'required': True,
                        'description': "Cost budget, in the cost field's unit or in the CRS's length unit"},
                       {'name': 'tolerance', 'type': 'float', 'required': True,
                        'description': 'How far apart two endpoints can be and still be the same junction'},
                       {'name': 'cost_field', 'type': 'str', 'required': False,
                        'description': 'Non-negative numeric column to use as cost (default: geometric length)'},
                       {'name': 'oneway_field', 'type': 'str', 'required': False,
                        'description': 'Boolean column marking one-way edges (default: undirected)'}],
        'examples': [{'goal': 'Everything within a ten-minute walk of this clinic, along the actual streets',
                      'call': {'tool': 'run_operation',
                               'arguments': {'operation': 'service_area',
                                             'arguments': {'network_path': 'footways.gpkg',
                                                           'output_path': 'ten_minutes.parquet',
                                                           'from_x': 512000.0, 'from_y': 4928000.0,
                                                           'budget': 10.0, 'tolerance': 0.5,
                                                           'cost_field': 'walk_minutes'}}}},
                     {'goal': 'How far can an ambulance get from this station in 800 metres of road?',
                      'call': {'tool': 'run_operation',
                               'arguments': {'operation': 'service_area',
                                             'arguments': {'network_path': 'roads.gpkg',
                                                           'output_path': 'reach.parquet',
                                                           'from_x': 512000.0, 'from_y': 4928000.0,
                                                           'budget': 800.0, 'tolerance': 0.5}}}}]},
    {   'name': 'hot_spots',
        'status': 'available',
        'tool': None,
        'workload': 'small_vector',
        'category': 'inspection',
        "produces": "dataset:vector",
        'applicability': {'inputs': ['vector'], 'requires_projected_crs': False, 'dataset_inputs': 1},
        'summary': 'Getis-Ord Gi* with a false-discovery-rate correction: where high '
                   'values cluster with other high values',
        "phrasings": "the high-count villages seem grouped but I am not sure what I am "
                     "seeing; is this a real cluster or noise; where are the hot spots",
        "distinguishes": "Tests whether a grouping is more than chance. Not "
                         "count_in_polygons, which counts without asking whether the "
                         "pattern means anything; not smooth_rates, which stabilises "
                         "small-denominator rates rather than locating clusters.",
        'description': 'Gi* including the feature itself, with binary weights from '
                       'either shared-boundary contiguity or a distance band. Every '
                       'feature is a hypothesis test, so over 300 districts at the '
                       'conventional 0.05 about fifteen come back significant from '
                       'noise alone \u2014 and they will be somewhere, and that '
                       'somewhere will look like a pattern. The significant column is '
                       'therefore a Benjamini-Hochberg false-discovery-rate decision '
                       'across all features, and the uncorrected count is reported '
                       'alongside so the difference is visible rather than implied. '
                       'gi_z and gi_p carry the raw statistic. Features with no '
                       'neighbours are counted in a non-critical check, because an '
                       'isolated Gi* is computed over the feature alone and says more '
                       'about the weights than about the data.',
        'parameters': [{'name': 'input_path', 'type': 'str', 'required': True,
                        'description': 'Layer to test (polygons or points)'},
                       {'name': 'output_path', 'type': 'str', 'required': True,
                        'description': 'Output layer with gi_z, gi_p, significant, hot_or_cold'},
                       {'name': 'value_field', 'type': 'str', 'required': True,
                        'description': 'Numeric column to test for clustering'},
                       {'name': 'weights', 'type': 'str', 'required': True,
                        'description': "'contiguity' (shared boundaries) or 'distance_band'"},
                       {'name': 'distance_band', 'type': 'float', 'required': False,
                        'description': "Radius within which features are neighbours (required for 'distance_band')"},
                       {'name': 'alpha', 'type': 'float', 'required': False,
                        'description': 'False-discovery rate (default 0.05)'}],
        'examples': [{'goal': 'Are the high-count villages really clustered, or does it just look that way?',
                      'call': {'tool': 'run_operation',
                               'arguments': {'operation': 'hot_spots',
                                             'arguments': {'input_path': 'villages.gpkg',
                                                           'output_path': 'clusters.parquet',
                                                           'value_field': 'cases',
                                                           'weights': 'distance_band',
                                                           'distance_band': 5000.0}}}},
                     {'goal': 'Which districts form a genuine cluster of high values?',
                      'call': {'tool': 'run_operation',
                               'arguments': {'operation': 'hot_spots',
                                             'arguments': {'input_path': 'districts.gpkg',
                                                           'output_path': 'hot.parquet',
                                                           'value_field': 'incidents',
                                                           'weights': 'contiguity'}}}}]},
    {   'name': 'smooth_rates',
        'status': 'available',
        'tool': None,
        'workload': 'small_vector',
        'category': 'inspection',
        "produces": "dataset:vector",
        'applicability': {'inputs': ['vector'], 'requires_projected_crs': False, 'dataset_inputs': 1},
        'summary': 'Empirical-Bayes rates: the small-denominator problem handled '
                   'explicitly, with the shrinkage reported per area',
        "phrasings": "the rates jump from 0 to 800 per 100000 because the districts are "
                     "tiny; my choropleth is really a map of population size; stabilise "
                     "these rates",
        "distinguishes": "Estimates underlying risk from counts and populations. Not "
                         "aggregate_weighted, which computes a rate over an area "
                         "without stabilising it; not hot_spots, which asks where "
                         "values cluster rather than what each one really is.",
        'description': 'Marshall (1991) empirical Bayes with a global prior: each raw '
                       'rate is shrunk toward the global rate by an amount that depends '
                       'on how much information its denominator carries, so a district '
                       'of two million barely moves and one of 120 moves most of the '
                       'way. Output carries raw_rate, smoothed_rate and shrinkage \u2014 '
                       'the weight given to the local rate, between 0 and 1 \u2014 so a '
                       'reader can see which numbers are evidence and which are mostly '
                       'the prior. When the between-area variance comes out at or below '
                       'zero the variation is no larger than sampling noise alone would '
                       'produce, every area is shrunk to the global rate, and the '
                       'manifest says so. A smoothed rate is an estimate of risk and is '
                       'the wrong number for how many people were actually ill.',
        'parameters': [{'name': 'input_path', 'type': 'str', 'required': True,
                        'description': 'Area layer with counts and populations'},
                       {'name': 'output_path', 'type': 'str', 'required': True,
                        'description': 'Output layer with raw_rate, smoothed_rate, shrinkage'},
                       {'name': 'count_field', 'type': 'str', 'required': True,
                        'description': 'Numerator: events observed in each area'},
                       {'name': 'population_field', 'type': 'str', 'required': True,
                        'description': 'Denominator: population at risk (must be positive)'},
                       {'name': 'per', 'type': 'float', 'required': False,
                        'description': 'Rate denominator for reporting (default 100000)'}],
        'examples': [{'goal': 'District TB rates swing from 0 to 800 per 100,000 because the denominators are tiny',
                      'call': {'tool': 'run_operation',
                               'arguments': {'operation': 'smooth_rates',
                                             'arguments': {'input_path': 'districts.gpkg',
                                                           'output_path': 'smoothed.parquet',
                                                           'count_field': 'cases',
                                                           'population_field': 'population'}}}},
                     {'goal': 'Stabilise incident rates per 1,000 households before mapping them',
                      'call': {'tool': 'run_operation',
                               'arguments': {'operation': 'smooth_rates',
                                             'arguments': {'input_path': 'wards.gpkg',
                                                           'output_path': 'wards_eb.parquet',
                                                           'count_field': 'incidents',
                                                           'population_field': 'households',
                                                           'per': 1000.0}}}}]},
    {   'name': 'aggregate_to_threshold',
        'status': 'available',
        'tool': None,
        'workload': 'small_vector',
        'category': 'vector',
        "produces": "dataset:vector",
        'applicability': {'inputs': ['vector'], 'requires_projected_crs': False, 'dataset_inputs': 1},
        'summary': 'Merge neighbouring areas until every one meets a minimum count, '
                   'deterministically \u2014 disclosure control that can be defended',
        "phrasings": "the ethics committee will not let me publish counts below five; "
                     "patient confidentiality on this map; combine the small areas until "
                     "they are safe to release",
        "distinguishes": "Merges by COUNT until a threshold is met. Not dissolve_layer, "
                         "which merges by a key you already have; not "
                         "aggregate_weighted, which computes a rate without changing "
                         "the geometry.",
        'description': 'Greedy and deterministic: repeatedly take the area with the '
                       'smallest count still under the minimum and merge it into '
                       'whichever neighbour has the smallest count, ties broken by '
                       'feature order. The same input produces the same grouping every '
                       'run, which matters more here than optimality \u2014 a disclosure '
                       'decision that changes between runs cannot be defended to an '
                       'ethics committee. Output carries the merged count, the number '
                       'of members and the source feature indices. An area with no '
                       'neighbours cannot be merged and the operation REFUSES rather '
                       'than emitting it below the threshold, because quietly '
                       'publishing the one island that could not be fixed is exactly '
                       'the disclosure this prevents. Three closed-form checks: nothing '
                       'below the minimum, the total unchanged, every input area in '
                       'exactly one group.',
        'parameters': [{'name': 'input_path', 'type': 'str', 'required': True,
                        'description': 'Area layer with a count column'},
                       {'name': 'output_path', 'type': 'str', 'required': True,
                        'description': 'Output layer of merged areas'},
                       {'name': 'count_field', 'type': 'str', 'required': True,
                        'description': 'Numeric column that must reach the minimum'},
                       {'name': 'minimum', 'type': 'float', 'required': True,
                        'description': 'Smallest count an area may have in the output'}],
        'examples': [{'goal': 'No published area may have fewer than five cases',
                      'call': {'tool': 'run_operation',
                               'arguments': {'operation': 'aggregate_to_threshold',
                                             'arguments': {'input_path': 'wards.gpkg',
                                                           'output_path': 'safe_wards.parquet',
                                                           'count_field': 'cases',
                                                           'minimum': 5}}}},
                     {'goal': 'Combine tiny districts so every one has at least 1,000 residents',
                      'call': {'tool': 'run_operation',
                               'arguments': {'operation': 'aggregate_to_threshold',
                                             'arguments': {'input_path': 'districts.gpkg',
                                                           'output_path': 'merged.parquet',
                                                           'count_field': 'population',
                                                           'minimum': 1000}}}}]},
    {   'name': 'thin_points',
        'status': 'available',
        'tool': None,
        'workload': 'small_vector',
        'category': 'vector',
        "produces": "dataset:vector",
        'applicability': {'inputs': ['vector'], 'requires_projected_crs': True, 'dataset_inputs': 1},
        'summary': 'Keep points no closer together than a distance, deterministically '
                   'and in priority order. Removes data, and says so',
        "phrasings": "a million dots and the map freezes; the town names overlap into a "
                     "dark bar, drop the smaller ones; my readings are dense near the "
                     "road and sparse elsewhere",
        "distinguishes": "Drops points to open up space. Not simplify_layer, which "
                         "reduces vertices within a geometry rather than removing "
                         "features; not centroid_layer, which replaces geometries "
                         "instead of thinning them.",
        'description': 'Greedy in priority order, ties by feature index: a point is '
                       'kept when no already-kept point lies within min_distance. That '
                       'is deterministic, which random or grid-jittered thinning is '
                       'not, and a map that changes between runs cannot carry a '
                       'manifest. priority_field is what makes it usable on labels: '
                       'without it, thinning keeps whichever point came first in the '
                       'file, which on a place-name layer means keeping hamlets and '
                       'dropping capitals. A geographic CRS is refused because the '
                       'distance would be in degrees. This operation REMOVES DATA: it '
                       'is for drawing, the manifest records how many went, and any '
                       'total computed from the output is a total of what survived '
                       'thinning.',
        'parameters': [{'name': 'input_path', 'type': 'str', 'required': True,
                        'description': 'Point layer in a projected CRS'},
                       {'name': 'output_path', 'type': 'str', 'required': True,
                        'description': 'Output layer holding the surviving points'},
                       {'name': 'min_distance', 'type': 'float', 'required': True,
                        'description': "Smallest allowed spacing, in the CRS's linear unit"},
                       {'name': 'priority_field', 'type': 'str', 'required': False,
                        'description': 'Numeric column deciding which point survives a crowd'},
                       {'name': 'keep_highest', 'type': 'bool', 'required': False,
                        'description': 'Whether the largest priority wins (default true)'}],
        'examples': [{'goal': 'A million logged points freeze the map; thin them to something drawable',
                      'call': {'tool': 'run_operation',
                               'arguments': {'operation': 'thin_points',
                                             'arguments': {'input_path': 'harvest.parquet',
                                                           'output_path': 'harvest_thin.parquet',
                                                           'min_distance': 5.0}}}},
                     {'goal': 'The place names overlap; keep the larger towns and drop the rest',
                      'call': {'tool': 'run_operation',
                               'arguments': {'operation': 'thin_points',
                                             'arguments': {'input_path': 'places.gpkg',
                                                           'output_path': 'labels.parquet',
                                                           'min_distance': 30000.0,
                                                           'priority_field': 'population'}}}}]},
]

# --- Okapi BM25 over the catalog (deterministic, no dependencies) ---------

_K1 = 1.5  # term-frequency saturation
_B = 0.75  # document-length normalization
_TOKEN = re.compile(r"[a-z0-9]+")

# English function words, dropped from both documents and queries before BM25.
#
# Measured on 27/08/2026, and the reason is not tidiness: BM25 weights a term by
# how RARE it is, so a word that carries no information about which operation to
# call still scores when few entries happen to use it. On the query "how many
# features and what extent does this layer have", `band_statistics` came SECOND
# on the strength of 'how', 'many', 'what', 'this' and 'and' alone -- it matched
# not one content word -- while `describe_dataset`, which matched 'extent' twice
# and 'layer' six times, came fourth and out of the top three the test checks.
#
# Only closed-class words are here: no domain word, no verb, nothing a caller
# could be using to mean something. 'to' and 'from' are deliberately KEPT: in
# this catalog they carry direction ("reproject to", "distance from"), which is
# exactly the information a query about a conversion is built on.
_STOPWORDS = frozenset({
    "a", "an", "the", "this", "that", "these", "those",
    "and", "or", "but", "if", "then", "than", "so",
    "of", "in", "on", "at", "by", "for", "with", "without", "into", "onto", "over", "under",
    "is", "are", "was", "were", "be", "been", "being", "am",
    "do", "does", "did", "done", "doing",
    "have", "has", "had", "having",
    "it", "its", "they", "them", "their", "there", "here",
    "i", "you", "he", "she", "we", "us", "our", "your",
    "what", "which", "who", "whom", "whose", "how", "why", "when", "where",
    "all", "any", "both", "each", "more", "most", "other", "some", "such",
    "no", "nor", "not", "only", "own", "same", "too", "very",
    "can", "will", "just", "should", "now",
})


def _tokenize(text: str) -> list[str]:
    """Tokens that can distinguish one operation from another.

    A text of nothing but function words tokenizes to the EMPTY list, and that is
    the honest outcome rather than a fallback to the raw words: the documents no
    longer contain those words either, so a fallback would match nothing while
    looking like it had a plan. :func:`search` reads the empty result for what it
    is -- a query carrying no signal about which operation to call -- and lists
    the catalog, which is what it already does for an empty string.
    """
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS]


def _document(op: dict[str, Any]) -> list[str]:
    """Flatten one catalog entry into a token list (name and category weighted)."""
    parts = [op["name"]] * 3 + [op["category"]] * 2 + [op["summary"]]
    parts.append(op.get("description", ""))
    # The words a CALLER uses, which are not the words the catalog is written
    # in, and this is the single largest lever measured on retrieval quality:
    # on queries phrased the way people phrase them, adding `phrasings` moved
    # BM25 from 40% found@3 to 100% at fifty-one operations, and the embedding
    # engine from 55% to 70%. The catalog said "simplify the geometry"; nobody
    # asks that, they say "these outlines have far too many vertices". Both
    # ranking engines read the same corpus, so both benefit.
    parts.append(op.get("phrasings", ""))
    # What this operation is NOT, and which neighbour does that instead. After the
    # facets have narrowed 800 candidates to about twenty of the same family,
    # nothing in the shape of an entry separates them — they all take a layer and
    # return a layer — and ranking inside that residue is close to random. This is
    # the text that separates them, and it is the only field written by contrast.
    parts.append(op.get("distinguishes", ""))
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
    entry = {k: op[k] for k in ("name", "status", "category", "summary")}
    # What separates this from its neighbours travels with it, because the
    # caller is the one choosing. Measured: a model reading name + summary +
    # this picks the right operation 69% of the time, against 48% for our own
    # ranking putting it in the top three — and it is the only field written to
    # be read against the other candidates rather than on its own.
    if op.get("distinguishes"):
        entry["distinguishes"] = op["distinguishes"]
    return entry


# Above this many survivors the result is a ranked shortlist; at or below it,
# the whole set comes back and the caller chooses.
#
# 30, and the number is measured rather than chosen. With the facets a caller can
# actually know — input kind and desired output — the surviving set over 118
# independent requests has a median of 26 and never exceeded 26. A threshold of 25
# sat one candidate below that and turned the normal case into the exception: 33%
# of requests got a shortlist instead of the set, for no reason but the rounding.
# At 26 candidates the payload is about 2,100 tokens, which is less than one wrong
# operation costs to run and undo.
#
# It is a presentation choice, not a quality threshold: nothing is hidden either
# way, and `limit` still governs the ranked case.
CHOOSABLE = 30


APPLICABILITY_KINDS = {"vector", "raster", "dataset", "plan"}


def applicable(
    input_kind: str | None = None,
    projected: bool | None = None,
    produces: str | None = None,
    category: str | None = None,
    dataset_inputs: int | None = None,
) -> list[dict[str, Any]]:
    """The subset of the catalog applicable to the data in hand — deterministically.

    Narrow-then-rank: this filter runs BEFORE any ranking, uses only what each
    entry declares (no model, no scores), and its outcome is a statement simple
    enough to put in a manifest: "this operation was offered because the input
    is a projected raster". ``input_kind`` keeps entries that accept that kind
    (entries accepting any ``dataset`` match vector and raster); ``projected=False``
    drops entries that require a projected CRS — the ones that would refuse the
    data anyway.

    ``produces`` narrows the same way. Measured on 2026-08-28 over 118 requests
    written by other model families, none of which had seen this catalog:

    ==========================================  ==========  ==========
    facets declared                             candidates  found@3
    ==========================================  ==========  ==========
    none                                                51         25%
    input kind                                          33         27%
    input kind + produces                               21         44%
    input kind + produces + category                    15         56%
    ==========================================  ==========  ==========

    ``dataset_inputs`` is how many datasets the caller is holding, and it is the
    facet that made the catalog scale past sixty operations. Added 2026-08-29,
    when ten new entries pushed the vector-in/vector-out set from 26 candidates
    to 34 — over the threshold at which the whole set can be handed over — and
    the measured consequence was that `delivered` fell from 100% to 45% and
    found@3 from 48% to 36%. **Adding capability had made discovery worse**,
    which is the scaling wall this project had predicted at eight hundred
    operations and met at sixty-one.

    Raising the threshold would have postponed it. Arity does not: it takes the
    median surviving set from 34 to 9 and the worst case from 34 to 22. And it
    is the right KIND of facet, which is the part worth keeping — a caller knows
    whether they have one layer or two without knowing anything about our
    taxonomy, and the number is derivable from each operation's binding, so
    `test_the_declared_arity_matches_the_binding` checks the declaration against
    the code rather than trusting it.

    **``category`` is a hard filter here and** :func:`search` **deliberately does
    not use it as one.** The difference is who knows the answer. ``input_kind``
    and ``projected`` are facts about the data in hand; ``produces`` is what the
    caller wants back. ``category`` is a guess about OUR taxonomy, and the table
    shows what it buys: six candidates out of twenty-one. What it costs, when the
    guess is wrong, is the right operation, removed with no error and replaced by
    a confident answer made of neighbours. On this sample every request has 4.4
    plausible families, so that is not a corner case. A discovery layer that
    silently deletes the answer is the failure this product exists to measure in
    other people's systems.

    So the hard cut stays available on this function, where a caller asking for
    it means it, and :func:`search` treats the declared family as an ordering
    instead. Six candidates are not worth a silent drop.

    An entry declaring ``none`` takes no dataset (``describe_crs`` answers about a
    CRS, ``geodetic_distance`` about two coordinates) and is kept for every kind.
    That is not a special case for convenience: ``describe_crs`` is exactly what
    you call to find out whether the raster in hand is geographic, so filtering
    it out when the input is a geographic raster would hide the answer at the one
    moment it is needed.
    """
    if input_kind is not None and input_kind not in APPLICABILITY_KINDS:
        raise ValueError(
            f"input_kind must be one of {sorted(APPLICABILITY_KINDS)}, got {input_kind!r}"
        )
    if produces is not None and produces not in PRODUCES_KINDS:
        raise ValueError(
            f"produces must be one of {sorted(PRODUCES_KINDS)}, got {produces!r}"
        )
    known_categories = {op["category"] for op in OPERATIONS}
    if category is not None and category not in known_categories:
        raise ValueError(
            f"category must be one of {sorted(known_categories)}, got {category!r}"
        )
    kept = []
    for op in OPERATIONS:
        block = op["applicability"]
        if input_kind is not None:
            accepted = set(block["inputs"])
            widened = accepted | ({"vector", "raster"} if "dataset" in accepted else set())
            if "none" not in accepted and input_kind not in widened and not (
                input_kind == "dataset" and accepted & {"vector", "raster", "dataset"}
            ):
                continue
        if projected is False and block["requires_projected_crs"]:
            continue
        if produces is not None and op.get("produces") != produces:
            continue
        if category is not None and op["category"] != category:
            continue
        # `None` on an ENTRY means the arity is variable or not expressible as a
        # count — a list of inputs, or inputs named inside a query string — and
        # such an entry is kept for every declared arity rather than dropped from
        # all of them. Exactly the rule `inputs: ["none"]` already follows above,
        # and it exists for the same reason: the first version dropped
        # `merge_layers` for anyone holding two layers and `run_sql` for anyone
        # holding any, in the release whose notes said discovery was fixed.
        declared_arity = op["applicability"]["dataset_inputs"]
        if (
            dataset_inputs is not None
            and declared_arity is not None
            and declared_arity != dataset_inputs
        ):
            continue
        kept.append(op)
    return kept



# Below this many shared entries between the two engines' top results, the
# search reports that it is unsure instead of answering.
#
# Measured on 2026-08-28 over 20 in-domain queries phrased without the catalog's
# vocabulary and 12 out-of-domain ones. Mean overlap of the two top-3 lists:
# 0.90 of 3 when an answer exists, 0.25 of 3 when it does not. Requiring at
# least ONE shared entry flags 9 of the 12 out-of-domain queries and costs a
# single false alarm in 20.
#
# It is a disagreement signal and not a score threshold because a threshold was
# tried first and does not exist: the similarity of "convert this mp4 to a gif"
# is higher than that of sixteen of the twenty real queries. There is no line to
# draw. Two independent rankers landing on nothing in common is a different kind
# of evidence, and it is the only one that separated the two populations.
AGREEMENT_FLOOR = 1


def _nothing_applies(
    query: str,
    input_kind: str | None,
    projected: bool | None,
    produces: str | None,
    dataset_inputs: int | None,
) -> dict[str, Any]:
    """What to say when the facets leave no operation at all.

    Found by the discovery log on its first real session, which is the argument
    for having built it: *"how much land is in each of these parcels"* with
    `produces="answer"` left zero candidates, and zero candidates fell into the
    `choose` branch and came back as *"0 operations survive, which is few enough
    to read"*. Nonsense as prose, and worse as an answer — an agent reads an
    empty candidate list as "MapSmith cannot do this", when the truth was
    `measure_area`, which computes exactly that and declares
    `produces="dataset:vector"` because it writes the areas to a layer.

    So this returns the diagnosis instead, and it is arithmetic rather than
    ranking: drop each declared facet in turn, and report which single one is
    doing the excluding. A caller who declared something that cannot be true of
    any operation learns which of its assumptions was wrong, which is the one
    thing a ranking could never have told it.
    """
    declared = {
        "input_kind": input_kind,
        "produces": produces,
        "projected": projected,
        "dataset_inputs": dataset_inputs,
    }
    declared = {k: v for k, v in declared.items() if v is not None}
    relax = []
    for facet in declared:
        without = dict(declared)
        without.pop(facet)
        survivors = applicable(
            without.get("input_kind"),
            without.get("projected"),
            without.get("produces"),
            dataset_inputs=without.get("dataset_inputs"),
        )
        if survivors:
            relax.append(
                {
                    "drop": facet,
                    "you_declared": declared[facet],
                    "would_leave": len(survivors),
                    "for_example": [op["name"] for op in survivors[:3]],
                }
            )
    relax.sort(key=lambda item: item["would_leave"])
    return {
        "status": "none_apply",
        "query": query,
        "reason": (
            "no operation in the catalog matches everything you declared. This is "
            "a statement about the facets, not about the words: nothing was ranked."
        ),
        "declared": declared,
        "relax": relax,
        "hint": (
            "Each entry above is one declaration removed. If dropping `produces` "
            "brings back what you wanted, the operation exists and hands its answer "
            "back in a different shape — several compute a number and write it as a "
            "column rather than returning it. If nothing here helps, MapSmith "
            "probably does not do this: say so rather than substituting a "
            "neighbouring operation."
        ),
    }


def _clarification(query: str, lexical: list[str], vector: list[str],
                   candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """What to say when the two engines disagree completely.

    Not an error and not an empty list. An empty list tells an agent "MapSmith
    cannot do this", which is often false — the usual case is a real request the
    ranking could not place. A wrong answer is worse still: this is a discovery
    layer feeding an agent, so a confident suggestion of `idw_interpolation` for
    "send an email" is a silent error of exactly the kind the rest of this
    product exists to prevent.

    So it returns the disagreement itself, and the one question that narrows the
    catalog deterministically rather than by ranking: what kind of data is in
    hand. `applicable()` cuts the candidates with no model in the loop, and on a
    catalog of this size that is worth more than another guess.
    """
    return {
        "status": "unsure",
        "query": query,
        "reason": (
            "the two ranking engines returned no operation in common, which on "
            "this catalog usually means the request was not understood rather "
            "than that MapSmith cannot do it"
        ),
        "lexical_suggests": lexical,
        "vector_suggests": vector,
        "clarify": [
            (
                "What kind of data do you have — vector (points, lines, polygons), "
                "raster (a grid, a GeoTIFF), or a table? Passing `input_kind` "
                "narrows the catalog deterministically, before any ranking."
            ),
            (
                "What should come out — a number, a new dataset, or a description "
                "of something you already have?"
            ),
            (
                "If one of the operations listed above is close, ask for it by name "
                "with `describe_operation` and it will say what it needs."
            ),
        ],
        "operations_available": len(candidates),
    }

# What an operation hands back. The third facet, and the one that was missing:
# a caller almost always knows whether they want a new dataset, a number, or a
# description of something they already have, and declaring it cuts the catalog
# roughly in half before any ranking runs.
PRODUCES_KINDS = (
    "dataset:vector",
    "dataset:raster",
    "answer",        # a number or a small structure; writes nothing
    "description",   # what something IS, rather than a computation over it
    "plan_result",
)

SEARCH_ENGINES = ("lexical", "vector", "auto")


def search(
    query: str = "",
    limit: int = 10,
    detail: bool = False,
    input_kind: str | None = None,
    projected: bool | None = None,
    produces: str | None = None,
    category: str | None = None,
    dataset_inputs: int | None = None,
    engine: str = "auto",
) -> list[dict[str, Any]]:
    """Search the catalog. Compact entries by default; detail=True adds parameters/examples.

    Empty query lists the whole catalog (roadmap included), and so does a query
    of nothing but function words ("how is it"), which carries no more signal
    than an empty one. With a query, results carry a ``score`` and an ``engine``
    field.

    ``input_kind``, ``produces``, ``category`` and ``projected`` narrow the
    candidates deterministically BEFORE ranking, whichever engine ranks them, and
    **they are the part that scales**. See :func:`applicable` for the table.

    **When few enough survive, this returns a choice rather than a verdict.** At
    or below :data:`CHOOSABLE` candidates the result is a single entry with
    ``status: "choose"`` carrying all of them, ordered by relevance and each with
    the text that separates it from its neighbours. Measured over the 118 requests in
    ``tests/data/discovery_queries.json``, written by two other model families:
    our ranking puts the right operation in the top three 48% of the time; a model handed the same candidates and asked to choose
    gets its FIRST pick right 69%. Over those same 118 requests the two labellers
    who produced the ground truth agree WITH EACH OTHER 70% of the time — both are
    language models, and no GIS analyst has tried it — so 69% is the ceiling of the
    task and not a score to improve — past that point the honest move is to show the
    alternatives, to the agent and through it to the person who asked.

    ``engine``: ``auto`` (default) prefers the embedding engine and falls back to
    BM25 when the model cannot be loaded — no network, no cache, air-gapped
    machine — so a caller always gets an answer and is always told by which
    engine. ``lexical`` forces BM25: deterministic, no model, no network ever.
    ``vector`` forces the embedding engine and raises rather than falling back.

    The default was lexical until 2026-08-28, on the argument that a default
    needing a download is not a default. What changed is a measurement, not the
    argument. On queries phrased the way a caller phrases them rather than the
    way the catalog is written — 35% word overlap — BM25 falls from 78% found@3
    at ten operations to 40% at fifty-one, while the embedding engine falls from
    83% to 55%. Both degrade with the catalog; BM25 degrades faster and the gap
    widens with every entry. The old default was measured on golden queries
    written by whoever wrote the catalog text, which is a lexical-overlap test
    dressed as a retrieval test: on those, BM25 scores 100% and the finding
    reverses. The download objection is answered by handling it rather than
    avoiding it — the fallback below is the answer, and `engine` in every result
    says which one ran.
    """
    if engine not in SEARCH_ENGINES:
        raise ValueError(f"engine must be one of {list(SEARCH_ENGINES)}, got {engine!r}")
    # `category` is NOT passed. It orders the survivors further down instead of
    # removing them: it is the one facet the caller has to guess about our own
    # taxonomy, and a wrong guess would delete the right answer in silence. See
    # `applicable` for the measurement behind that.
    candidates = applicable(input_kind, projected, produces, dataset_inputs=dataset_inputs)
    # Before anything else, because every branch below assumes there is something
    # to rank or to hand over, and with nothing they all lie in their own way.
    if not candidates:
        return [_nothing_applies(query, input_kind, projected, produces, dataset_inputs)]
    # `_tokenize` is what decides whether there is a query at all: a string of
    # function words scores nothing against every entry, and returning the
    # catalog says "ask me better" more usefully than returning nothing.
    if not query.strip() or not _tokenize(query):
        return [dict(op) if detail else _compact(op) for op in candidates]

    used = engine
    ranked: list[tuple[dict[str, Any], float]]
    if engine in ("vector", "auto"):
        try:
            from . import retrieval

            # Rank enough of them to hand the whole set over in order. The
            # embedding ranker truncates to `limit`, and a set delivered with
            # ten ordered entries followed by sixteen in catalog order is not
            # an ordered set.
            ranked = retrieval.rank(
                query, limit=max(limit, CHOOSABLE), candidates=candidates
            )
            used = "vector"
        except Exception:
            # Broad on purpose, and it was NARROW and wrong until 2026-08-28.
            # While the engine sat behind an extra, `ImportError` was the whole
            # failure surface. Now that model2vec is a dependency, the way this
            # fails is the model DOWNLOAD: no network, a proxy, a cold cache on
            # an air-gapped machine, a corrupted blob. Those raise OSError, or
            # whatever huggingface_hub decides to raise next release — none of
            # them ImportError. A fallback that catches only the failure that
            # can no longer happen is a fallback that never runs, which is worse
            # than none because it reads like a guarantee.
            #
            # `engine="vector"` still raises: a caller who asked for that engine
            # by name wants to know it did not run.
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
    # The two engines are asked to agree before either is believed. The second
    # ranking is cheap next to the first: BM25 over a few dozen documents is
    # microseconds, and the embedding index is already in memory.
    top_vector: list[str] = []
    top_lexical: list[str] = []
    agreed = True
    if used == "vector" and ranked:
        query_tokens = _tokenize(query)
        scores = bm25_scores(query_tokens, [_document(op) for op in candidates])
        other = sorted(
            ((op, s) for op, s in zip(candidates, scores, strict=True) if s > 0),
            key=lambda pair: (-pair[1], pair[0]["name"]),
        )
        top_vector = [op["name"] for op, _ in ranked[:3]]
        top_lexical = [op["name"] for op, _ in other[:3]]
        agreed = len(set(top_vector) & set(top_lexical)) >= AGREEMENT_FLOOR

    # THE SHORTLIST IS NOT THE ANSWER when there are few enough to read.
    #
    # Measured over 118 requests written by two other model families: our ranking
    # puts the right operation in the top three 48% of the time, while a model
    # handed the same candidates and asked to CHOOSE gets its first pick right 69% —
    # and 70% is where the two labellers who wrote the ground truth agree with each
    # OTHER on those same requests, so 69% is the ceiling of the task rather than a
    # score to beat. Both labellers are language models; no analyst has tried it.
    #
    # The caller knows things no ranking can: which file is actually open, what
    # was run a minute ago, what the person in front of them meant. So when the
    # facets have narrowed the catalog to something readable, hand over the set
    # and say that it is a choice. Below, `ranked` still orders it — an ordering
    # is a useful hint and a truncation is not.
    # The declared family lifts its own members to the front of whatever the
    # ranker produced and leaves the rest reachable below. Stable, so the ranking
    # survives inside each group. A wrong guess now costs positions; the hard
    # filter it replaced cost the answer.
    #
    # After the agreement check on purpose: the two engines are compared on what
    # they actually think, not on an order one of them did not produce.
    if category is not None:
        ranked = sorted(ranked, key=lambda pair: pair[0]["category"] != category)

    if len(candidates) <= CHOOSABLE and query.strip():
        # The promotion above only reaches operations the ranker SCORED. BM25
        # returns nothing for a candidate that shares no term with the query, so
        # an operation of the declared family could score zero, fall into the
        # tail below, and never be lifted — declaring the family bought nothing
        # for exactly the entry that needed it most, the one the words missed.
        # Found by pinning the engine in the contract test, which had been
        # measuring whichever ranker the machine could download.
        # Disagreement stops being a refusal here. It was one because we were
        # deciding; handing over the set is not deciding, so the signal becomes
        # a warning about the ORDER — which is the only thing it was ever
        # evidence about. Above the threshold it still refuses, below.
        # Ordered ones first, then whatever the ranker scored at zero — a
        # candidate BM25 never saw is still a candidate. Compared by name
        # because a catalog entry is a dict and dicts do not hash.
        scored = {op["name"] for op, _ in ranked}
        chosen = [op for op, _ in ranked] + [
            op for op in candidates if op["name"] not in scored
        ]
        if category is not None:
            chosen = sorted(chosen, key=lambda op: op["category"] != category)
        return [
            {
                "status": "choose",
                "query": query,
                "reason": (
                    f"{len(chosen)} operations survive what you declared, which is few "
                    "enough to read. They are ordered by relevance, but the order is a "
                    "hint: pick by what they say, and ask the person who made the "
                    "request if two of them would both be defensible."
                ),
                "engine": used,
                "candidates": [dict(op) if detail else _compact(op) for op in chosen],
                **(
                    {}
                    if agreed
                    else {
                        "order_is_weak": (
                            "The two rankers share nothing in their top three "
                            f"({', '.join(top_lexical[:3])} against "
                            f"{', '.join(top_vector[:3])}), which usually means the "
                            "request does not match this catalog well. Read the "
                            "candidates rather than trusting the order, and say so "
                            "to the person who asked if none of them fits."
                        )
                    }
                ),
            }
        ]

    if not agreed:
        return [_clarification(query, top_lexical, top_vector, candidates)]

    results = []
    for op, score in ranked[:limit]:
        entry = dict(op) if detail else _compact(op)
        entry["score"] = round(score, 4)
        entry["engine"] = used
        results.append(entry)
    return results


def entries(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The operations in a `search` result, whichever shape it came back in.

    `search` answers in three shapes — a ranked list, one `choose` entry holding
    every survivor, one `unsure` entry holding none — and a caller that reads
    `results[0]["name"]` is right two times in three, which is the worst kind of
    right. This flattens them: the operations to read, in order, or empty when
    the search declined to place the query.
    """
    if len(results) == 1 and results[0].get("status") in ("choose", "unsure", "none_apply"):
        return list(results[0].get("candidates", []))
    return list(results)


def describe_operation(name: str) -> dict[str, Any]:
    """Full structured doc of one operation by exact name (helpful error otherwise)."""
    for op in OPERATIONS:
        if op["name"] == name:
            return dict(op)
    suggestions = [op["name"] for op, _ in rank(name, limit=3)]
    hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
    raise ValueError(f"Unknown operation '{name}'.{hint} Use list_operations to search.")
