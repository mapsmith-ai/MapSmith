"""Sixteen executable D-063 plans with small, hand-checkable fixtures."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import LineString, Point, Polygon, box

from mapsmith.plans import Plan

CRS = "EPSG:32633"


def vector(path: Path, geometries, fields: dict[str, list]) -> str:
    gpd.GeoDataFrame(fields, geometry=geometries, crs=CRS).to_file(path)
    return str(path)


def raster(path: Path, values) -> str:
    array = np.asarray(values, dtype="float32")
    with rasterio.open(
        path, "w", driver="GTiff", width=array.shape[1], height=array.shape[0], count=1,
        dtype="float32", crs=CRS, transform=from_origin(0, 0, 10, 10),
    ) as dst:
        dst.write(array, 1)
    return str(path)


def plan(goal: str, steps: list[dict]) -> Plan:
    return Plan.model_validate({"goal": goal, "steps": steps})


def case(index: int, workdir: Path) -> Plan:
    workdir.mkdir(parents=True, exist_ok=True)
    p = lambda name: str(workdir / name)
    if index == 19:
        road = vector(workdir / "road.gpkg", [LineString([(0, 0), (100, 0)])], {"id": [1]})
        candidates = vector(workdir / "candidates.gpkg", [box(10, -10, 60, 10)], {"id": [1]})
        flood = vector(workdir / "flood.gpkg", [box(-5, -5, 5, 5)], {"id": [1]})
        overhead = vector(workdir / "overhead.gpkg", [box(50, -5, 60, 5)], {"id": [1]})
        return plan("site outside flood and overhead, within 20 m of road", [
            {"id": "road_buffer", "operation": "buffer_layer", "arguments": {"input_path": road, "distance_meters": 20, "output_path": p("road_buffer.parquet")}},
            {"id": "intersection", "operation": "overlay_layers", "arguments": {"input_path": candidates, "overlay_path": p("road_buffer.parquet"), "output_path": p("intersection.parquet")}},
            {"id": "flood_out", "operation": "overlay_layers", "arguments": {"input_path": p("intersection.parquet"), "overlay_path": flood, "output_path": p("flood_out.parquet"), "how": "difference"}},
            {"id": "overhead_out", "operation": "overlay_layers", "arguments": {"input_path": p("flood_out.parquet"), "overlay_path": overhead, "output_path": p("overhead_out.parquet"), "how": "difference"}},
        ])
    if index == 21:
        ring = [(0, 0), (100, 0), (100, 1), (99, 2), (100, 100), (0, 100)]
        district = vector(workdir / "district.gpkg", [Polygon(ring)], {"cases": [2], "population": [20]})
        return plan("simplify then smooth rates", [
            {"id": "simplify", "operation": "simplify_layer", "arguments": {"input_path": district, "tolerance_meters": 1, "output_path": p("simplified.parquet")}},
            {"id": "smooth", "operation": "smooth_rates", "arguments": {"input_path": p("simplified.parquet"), "count_field": "cases", "population_field": "population", "per": 100, "output_path": p("rates.parquet")}},
        ])
    if index == 24:
        hospitals = vector(workdir / "hospitals.gpkg", [box(0, 0, 60, 100)], {"cases": [10]})
        municipalities = vector(workdir / "municipalities.gpkg", [box(0, 0, 30, 100)], {"id": [1], "population": [200]})
        return plan("areal incidence interpolation", [
            {"id": "hospital_area", "operation": "measure_area", "arguments": {"input_path": hospitals, "output_path": p("hospital_area.parquet"), "method": "planar"}},
            {"id": "intersection", "operation": "overlay_layers", "arguments": {"input_path": hospitals, "overlay_path": municipalities, "output_path": p("intersection.parquet")}},
            {"id": "area", "operation": "measure_area", "arguments": {"input_path": p("intersection.parquet"), "output_path": p("area.parquet"), "method": "planar"}},
            {"id": "rate", "operation": "run_sql", "arguments": {"query": f"SELECT sum(i.cases * i.area_m2 / h.area_m2) / sum(i.population) AS incidence FROM read_parquet('{p('area.parquet')}') i CROSS JOIN read_parquet('{p('hospital_area.parquet')}') h", "output_path": p("incidence.parquet")}},
        ])
    if index == 43:
        parcels = vector(workdir / "parcels.gpkg", [box(0, 0, 10, 10)], {"id": [1]})
        primary = vector(workdir / "primary.gpkg", [box(1, 1, 2, 2)], {"id": [1]})
        footprint = vector(workdir / "footprint.gpkg", [box(0, 0, 8, 8)], {"id": [1]})
        accessory = vector(workdir / "accessory.gpkg", [box(7, 7, 12, 12)], {"kind": ["shed"]})
        query = (
            f"SELECT parcels.id FROM ST_Read('{parcels}') AS parcels "
            f"JOIN ST_Read('{primary}') AS p ON ST_Within(p.geom, parcels.geom) "
            f"JOIN ST_Read('{footprint}') AS f ON ST_Within(p.geom, f.geom) "
            f"JOIN ST_Read('{accessory}') AS a ON ST_Intersects(a.geom, ST_Boundary(f.geom))"
        )
        return plan("primary inside and accessory crossing", [
            {"id": "flag", "operation": "run_sql", "arguments": {"query": query, "output_path": p("flagged.parquet")}},
        ])
    if index == 47:
        plane = 600 + 100 * np.tile(np.arange(5, dtype="float32"), (5, 1))
        dem = raster(workdir / "dem.tif", plane)
        candidates = vector(workdir / "candidates.gpkg", [box(10, -20, 20, -10)], {"id": [1]})
        pine = vector(workdir / "pine.gpkg", [box(10, -20, 20, -10)], {"id": [1]})
        turbines = vector(workdir / "turbines.gpkg", [Point(1000, 1000)], {"id": [1]})
        return plan("steep high habitat near pine and away from turbines", [
            {"id": "slope", "operation": "slope", "arguments": {"dem_path": dem, "output_path": p("slope.tif"), "units": "degrees"}},
            {"id": "steep", "operation": "reclassify_raster", "arguments": {"input_path": p("slope.tif"), "output_path": p("steep.tif"), "intervals": ["-1000000:30:0", "30:1000000:1"]}},
            {"id": "high", "operation": "reclassify_raster", "arguments": {"input_path": dem, "output_path": p("high.tif"), "intervals": ["-1000000:500:0", "500:1000000:1"]}},
            {"id": "pine_near", "operation": "buffer_layer", "arguments": {"input_path": pine, "distance_meters": 1, "output_path": p("pine_near.parquet")}},
            {"id": "turbine_near", "operation": "buffer_layer", "arguments": {"input_path": turbines, "distance_meters": 1, "output_path": p("turbine_near.parquet")}},
            {"id": "near_pine", "operation": "overlay_layers", "arguments": {"input_path": candidates, "overlay_path": p("pine_near.parquet"), "output_path": p("near_pine.parquet")}},
            {"id": "eligible", "operation": "overlay_layers", "arguments": {"input_path": p("near_pine.parquet"), "overlay_path": p("turbine_near.parquet"), "output_path": p("eligible.parquet"), "how": "difference"}},
            {"id": "steep_fraction", "operation": "zonal_statistics", "arguments": {"raster_path": p("steep.tif"), "zones_path": p("eligible.parquet"), "output_path": p("steep_fraction.parquet"), "stats": ["mean"]}},
            {"id": "high_fraction", "operation": "zonal_statistics", "arguments": {"raster_path": p("high.tif"), "zones_path": p("eligible.parquet"), "output_path": p("high_fraction.parquet"), "stats": ["mean"]}},
            {"id": "score", "operation": "run_sql", "arguments": {"query": f"SELECT s.id_1, s.mean * h.mean AS suitability FROM read_parquet('{p('steep_fraction.parquet')}') s JOIN read_parquet('{p('high_fraction.parquet')}') h USING (id_1, id_2)", "output_path": p("score.parquet")}},
        ])
    if index == 93:
        creek = vector(workdir / "creek.gpkg", [LineString([(5, 0), (5, 10)])], {"id": [1]})
        land = vector(workdir / "land.gpkg", [box(0, 0, 10, 10)], {"id": [1]})
        return plan("riparian exclusion area", [
            {"id": "creek_buffer", "operation": "buffer_layer", "arguments": {"input_path": creek, "distance_meters": 1, "output_path": p("creek_buffer.parquet")}},
            {"id": "difference", "operation": "overlay_layers", "arguments": {"input_path": land, "overlay_path": p("creek_buffer.parquet"), "output_path": p("difference.parquet"), "how": "difference"}},
            {"id": "area", "operation": "measure_area", "arguments": {"input_path": p("difference.parquet"), "output_path": p("area.parquet"), "method": "planar"}},
        ])
    if index == 99:
        heat = raster(workdir / "heat.tif", [[1, 2], [3, 4]])
        population = raster(workdir / "population.tif", [[1, 1], [2, 4]])
        districts = vector(workdir / "districts.gpkg", [box(0, -20, 10, 0), box(10, -20, 20, 0)], {"id": [1, 2]})
        return plan("population weighted heat by district", [
            {"id": "weighted", "operation": "zonal_statistics", "arguments": {"raster_path": heat, "zones_path": districts, "output_path": p("weighted.parquet"), "stats": ["weighted_mean"], "weights_path": population}},
            {"id": "deliver", "operation": "run_sql", "arguments": {"query": f"SELECT id, weighted_mean AS weighted_heat FROM read_parquet('{p('weighted.parquet')}') ORDER BY id", "output_path": p("heat.parquet")}},
        ])
    if index in (107, 109, 111):
        ramp = np.tile(10 * np.arange(20, dtype="float32"), (20, 1))
        dem = raster(workdir / "dem.tif", ramp)
        line = vector(workdir / "line.gpkg", [LineString([(5, -5), (195, -5)])], {"id": [1]})
        base = 100 if index == 109 else 10
        query = (
            f"SELECT distance AS chainage, grade_percent FROM read_parquet('{p('profile.parquet')}') "
            "WHERE grade_percent IS NOT NULL ORDER BY grade_percent DESC, distance LIMIT 1"
            if index in (107, 109) else
            f"SELECT max(grade_percent > 2.5) AS exceeds FROM read_parquet('{p('profile.parquet')}')"
        )
        return plan("gradient at a declared sliding base length", [
            {"id": "profile", "operation": "elevation_profile", "arguments": {
                "raster_path": dem, "line_path": line, "output_path": p("profile.parquet"), "spacing": 10,
                "grade_base_length": base,
                **({"grade_threshold_percent": 2.5} if index == 111 else {}),
            }},
            {"id": "summarize", "operation": "run_sql", "arguments": {"query": query, "output_path": p("grade.parquet")}},
        ])
    if index == 112:
        ramps = np.tile(10 * np.arange(20, dtype="float32"), (20, 1))
        ramps[1] *= 0.5
        dem = raster(workdir / "dem.tif", ramps)
        first = vector(workdir / "first.gpkg", [LineString([(5, -5), (195, -5)])], {"id": [1]})
        second = vector(workdir / "second.gpkg", [LineString([(5, -15), (195, -15)])], {"id": [1]})
        query = (
            "SELECT (SELECT sum(rise) FROM (SELECT value - lag(value) OVER (ORDER BY distance) AS rise "
            f"FROM read_parquet('{p('first.parquet')}')) WHERE rise > 0) - "
            "(SELECT sum(rise) FROM (SELECT value - lag(value) OVER (ORDER BY distance) AS rise "
            f"FROM read_parquet('{p('second.parquet')}')) WHERE rise > 0) AS ascent_difference"
        )
        return plan("compare total positive gain", [
            {"id": "first", "operation": "elevation_profile", "arguments": {"raster_path": dem, "line_path": first, "output_path": p("first.parquet"), "spacing": 10, "grade_base_length": 10}},
            {"id": "second", "operation": "elevation_profile", "arguments": {"raster_path": dem, "line_path": second, "output_path": p("second.parquet"), "spacing": 10, "grade_base_length": 10}},
            {"id": "compare", "operation": "run_sql", "arguments": {"query": query, "output_path": p("compare.parquet")}},
        ])
    if index == 122:
        old = vector(workdir / "old.gpkg", [box(0, 0, 10, 10)], {"id": [1]})
        new = vector(workdir / "new.gpkg", [box(0, 0, 5, 10), box(5, 0, 10, 10)], {"id": [1, 2]})
        points = vector(workdir / "points.gpkg", [Point(2, 2), Point(7, 7)], {"id": [1, 2]})
        return plan("count under both boundary vintages", [
            {"id": "old", "operation": "count_in_polygons", "arguments": {"points_path": points, "polygons_path": old, "output_path": p("old.parquet")}},
            {"id": "new", "operation": "count_in_polygons", "arguments": {"points_path": points, "polygons_path": new, "output_path": p("new.parquet")}},
        ])
    if index == 140:
        dem = raster(workdir / "dem.tif", np.zeros((5, 5)))
        fuel = raster(workdir / "fuel.tif", np.ones((5, 5)))
        parcels = vector(workdir / "parcels.gpkg", [box(10, -20, 20, -10)], {"id": [1]})
        return plan("ridge resistance from curvature and slope, weighted by fuel", [
            {"id": "curvature", "operation": "curvature", "arguments": {"dem_path": dem, "output_path": p("curvature.tif"), "kind": "plan"}},
            {"id": "resistance", "operation": "reclassify_raster", "arguments": {"input_path": p("curvature.tif"), "output_path": p("resistance.tif"), "intervals": ["-1000000:0:2", "0:1000000:1"]}},
            {"id": "combine", "operation": "band_math", "arguments": {"input_path": p("resistance.tif"), "output_path": p("combine.tif"), "expression": "b1 * 2"}},
            {"id": "slope", "operation": "slope", "arguments": {"dem_path": dem, "output_path": p("slope.tif"), "units": "degrees"}},
            {"id": "fuel_score", "operation": "zonal_statistics", "arguments": {"raster_path": p("combine.tif"), "zones_path": parcels, "output_path": p("fuel_score.parquet"), "stats": ["weighted_mean"], "weights_path": fuel}},
            {"id": "slope_score", "operation": "zonal_statistics", "arguments": {"raster_path": p("slope.tif"), "zones_path": parcels, "output_path": p("slope_score.parquet"), "stats": ["mean"]}},
            {"id": "score", "operation": "run_sql", "arguments": {"query": f"SELECT f.id, f.weighted_mean / (1 + s.mean) AS resistance FROM read_parquet('{p('fuel_score.parquet')}') f JOIN read_parquet('{p('slope_score.parquet')}') s USING (id)", "output_path": p("score.parquet")}},
        ])
    if index == 141:
        dem = raster(workdir / "dem.tif", np.zeros((5, 5)))
        vegetation = raster(workdir / "vegetation.tif", np.full((5, 5), 2))
        parcels = vector(workdir / "parcels.gpkg", [box(10, -20, 20, -10)], {"id": [1]})
        return plan("defensibility from equally weighted slope and vegetation", [
            {"id": "slope", "operation": "slope", "arguments": {"dem_path": dem, "output_path": p("slope.tif"), "units": "percent"}},
            {"id": "slope_mean", "operation": "zonal_statistics", "arguments": {"raster_path": p("slope.tif"), "zones_path": parcels, "output_path": p("slope_mean.parquet"), "stats": ["mean"]}},
            {"id": "vegetation_mean", "operation": "zonal_statistics", "arguments": {"raster_path": vegetation, "zones_path": parcels, "output_path": p("vegetation_mean.parquet"), "stats": ["mean"]}},
            {"id": "score", "operation": "run_sql", "arguments": {"query": f"SELECT s.id, s.mean + v.mean AS score FROM read_parquet('{p('slope_mean.parquet')}') s JOIN read_parquet('{p('vegetation_mean.parquet')}') v USING (id)", "output_path": p("score.parquet")}},
        ])
    if index == 142:
        plane = 10 * np.tile(np.arange(5, dtype="float32"), (5, 1))
        dem = raster(workdir / "dem.tif", plane)
        fuelbreak = vector(workdir / "fuelbreak.gpkg", [box(10, -20, 20, -10)], {"id": [1]})
        return plan("fuel break fraction over 30 percent", [
            {"id": "slope", "operation": "slope", "arguments": {"dem_path": dem, "output_path": p("slope.tif"), "units": "percent"}},
            {"id": "mask", "operation": "reclassify_raster", "arguments": {"input_path": p("slope.tif"), "output_path": p("mask.tif"), "intervals": ["-1000000:30:0", "30:1000000:1"]}},
            {"id": "fraction", "operation": "zonal_statistics", "arguments": {"raster_path": p("mask.tif"), "zones_path": fuelbreak, "output_path": p("fraction.parquet"), "stats": ["mean"]}},
        ])
    if index == 147:
        dem = raster(workdir / "dem.tif", [[0, 0], [0, 0]])
        stations = vector(workdir / "stations.gpkg", [Point(5, -5)], {"id": [1]})
        population = raster(workdir / "population.tif", [[1, 2], [3, 4]])
        candidates = vector(workdir / "candidates.gpkg", [box(0, -20, 10, 0), box(10, -20, 20, 0)], {"id": [1, 2]})
        return plan("rank mast candidates by visible population", [
            {"id": "viewshed", "operation": "viewshed", "arguments": {"dem_path": dem, "stations_path": stations, "output_path": p("viewshed.tif"), "station_height": 2}},
            {"id": "score", "operation": "zonal_statistics", "arguments": {"raster_path": p("viewshed.tif"), "zones_path": candidates, "output_path": p("score.parquet"), "stats": ["weighted_sum"], "weights_path": population}},
            {"id": "best", "operation": "run_sql", "arguments": {"query": f"SELECT id, weighted_sum AS visible_population FROM read_parquet('{p('score.parquet')}') ORDER BY weighted_sum DESC LIMIT 1", "output_path": p("best.parquet")}},
        ])
    raise ValueError(f"no fixture for {index}")
