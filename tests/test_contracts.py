"""Issue #27: declared argument rules are visible and enforced before execution."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "benchmarks"))

import method_choice as bench

from mapsmith import catalog
from mapsmith.plans import Plan, validate
from mapsmith.plans.validator import _check_contract


def codes(report):
    return [error.code for error in report.errors]


def parameter(operation, name):
    entry = next(op for op in catalog.OPERATIONS if op["name"] == operation)
    return next(param for param in entry["parameters"] if param["name"] == name)


def test_every_contract_lands_on_a_real_parameter_and_detail_exposes_it():
    for (operation, name), contract in catalog.PARAMETER_CONTRACTS.items():
        assert parameter(operation, name)
        assert contract
    result = catalog.search("reproject raster", detail=True)
    entry = next(op for op in result if op["name"] == "reproject_raster")
    resampling = next(param for param in entry["parameters"] if param["name"] == "resampling")
    assert "nearest" in resampling["choices"]
    assert "gauss" in resampling["choices"]


def test_every_declared_choice_rejects_an_outsider_and_accepts_every_choice():
    for (operation, name), contract in catalog.PARAMETER_CONTRACTS.items():
        if "choices" not in contract:
            continue
        param = parameter(operation, name)
        errors = []
        _check_contract("step", name, "__outside__", param, errors)
        assert [error.code for error in errors] == ["INVALID_ARGUMENT_VALUE"], (operation, name)
        assert "must be one of" in errors[0].message
        for choice in contract["choices"]:
            errors = []
            value = [choice] if param["type"] == "list[str]" else choice
            _check_contract("step", name, value, param, errors)
            assert errors == [], (operation, name, choice)


def test_engine_enums_match_the_declared_contracts():
    from mapsmith.engines import (
        conversion,
        dispatch,
        geodesy,
        linework,
        qgis,
        raster,
        sampling,
        spatial_stats,
        summaries,
        vector,
        whitebox_engine,
    )

    expected = {
        ("overlay_layers", "how"): vector.OVERLAY_HOWS,
        ("dissolve_layer", "aggfunc"): vector.DISSOLVE_AGGFUNCS,
        ("measure_area", "method"): vector.AREA_METHODS,
        ("select_features", "by"): vector.SELECT_BY,
        ("spatial_join", "predicate"): {"intersects", "within", "contains"},
        ("spatial_join", "engine"): dispatch.SPATIAL_JOIN_ENGINES,
        ("flow_accumulation", "out_type"): {"cells", "sca"},
        ("zonal_statistics", "stats"): raster.VALID_STATS | raster.WEIGHTED_STATS,
        ("resample_raster", "resampling"): raster.INTERPOLATING_RESAMPLING | raster.CATEGORICAL_RESAMPLING | raster.WARP_ONLY_RESAMPLING,
        ("reproject_raster", "resampling"): raster.INTERPOLATING_RESAMPLING | raster.CATEGORICAL_RESAMPLING | raster.WARP_ONLY_RESAMPLING,
        ("elevation_profile", "grade_height_unit"): set(sampling.HEIGHT_UNITS),
        ("locate_extreme_cell", "which"): {"min", "max"},
        ("measure_length", "method"): vector.LENGTH_METHODS,
        ("hull_layer", "kind"): vector.HULL_KINDS,
        ("join_table", "how"): vector.JOIN_KINDS,
        ("count_in_polygons", "predicate"): vector.COUNT_PREDICATES,
        ("summarize_points_in_polygons", "predicate"): vector.COUNT_PREDICATES,
        ("transform_by_control_points", "kind"): linework.TRANSFORMS,
        ("geodetic_distance", "ellipsoid"): geodesy.ELLIPSOIDS,
        ("sample_raster_at_points", "method"): sampling.SAMPLING_METHODS,
        ("elevation_profile", "method"): sampling.SAMPLING_METHODS,
        ("hot_spots", "weights"): spatial_stats.WEIGHT_SCHEMES,
        ("spatial_autocorrelation", "weights"): spatial_stats.WEIGHT_SCHEMES,
        ("summarize_field", "statistics"): summaries.STATISTICS,
        ("summarize_points_in_polygons", "statistics"): summaries.STATISTICS,
        ("slope", "units"): whitebox_engine.SLOPE_UNITS,
        ("focal_statistics", "statistic"): set(whitebox_engine.FOCAL_STATISTICS),
        ("curvature", "kind"): set(whitebox_engine.CURVATURE_KINDS),
        ("flow_direction", "method"): set(whitebox_engine.FLOW_DIRECTION_METHODS),
        ("flow_direction", "encoding"): set(whitebox_engine.POINTER_ENCODINGS),
        ("voronoi_polygons", "boundary"): vector.VORONOI_BOUNDARIES,
        ("polygonize_raster", "connectivity"): conversion.POLYGON_CONNECTIVITIES,
        ("qgis_processing", "algorithm"): qgis.ALGORITHMS,
    }
    for key, actual in expected.items():
        assert set(catalog.PARAMETER_CONTRACTS[key]["choices"]) == set(actual), key
    assert set(catalog.PARAMETER_CONTRACTS["resample_raster", "resampling"]["choices"]) == (
        raster.INTERPOLATING_RESAMPLING | raster.CATEGORICAL_RESAMPLING | raster.WARP_ONLY_RESAMPLING
    )
    assert set(catalog.PARAMETER_CONTRACTS["reproject_raster", "resampling"]["choices"]) == (
        raster.INTERPOLATING_RESAMPLING | raster.CATEGORICAL_RESAMPLING | raster.WARP_ONLY_RESAMPLING
    )
    assert set(expected) == {key for key, rule in catalog.PARAMETER_CONTRACTS.items() if "choices" in rule}


def make_step(operation, arguments):
    return Plan.model_validate({"steps": [{"id": "step", "operation": operation, "arguments": arguments}]})


def test_invalid_resampling_names_the_allowed_values():
    report = validate(make_step("resample_raster", {
        "input_path": "in.tif", "output_path": "out.tif", "resolution": 30, "resampling": "invented",
    }))
    assert codes(report)[0] == "INVALID_ARGUMENT_VALUE"
    assert "resampling" in report.errors[0].message
    assert "nearest" in report.errors[0].message
    assert "invented" in report.errors[0].message


def test_list_statistics_are_checked_per_member():
    report = validate(make_step("summarize_field", {
        "input_path": "in.parquet", "field": "value", "statistics": ["mean", "invented"],
    }))
    assert codes(report)[0] == "INVALID_ARGUMENT_VALUE"
    assert "statistics" in report.errors[0].message


def test_window_must_be_odd_and_at_least_three():
    for window, rule in ((2, "odd"), (1, ">= 3")):
        report = validate(make_step("focal_statistics", {
            "input_path": "in.tif", "output_path": "out.tif", "statistic": "mean", "window": window,
        }))
        assert codes(report)[0] == "INVALID_ARGUMENT_VALUE"
        assert any(rule in error.message for error in report.errors)


def test_positive_spacing_and_finite_numbers_are_enforced():
    report = validate(make_step("resample_raster", {
        "input_path": "in.tif", "output_path": "out.tif", "resolution": 0, "resampling": "nearest",
    }))
    assert codes(report)[0] == "INVALID_ARGUMENT_VALUE"
    assert "> 0" in report.errors[0].message
    report = validate(make_step("resample_raster", {
        "input_path": "in.tif", "output_path": "out.tif", "resolution": float("nan"), "resampling": "nearest",
    }))
    assert codes(report)[0] == "INVALID_ARGUMENT_VALUE"
    assert "finite" in report.errors[0].message


def test_geographic_bounds_and_alpha_range_are_enforced():
    report = validate(make_step("geodetic_distance", {
        "from_lon": 0, "from_lat": 91, "to_lon": 0, "to_lat": 0,
    }))
    assert codes(report)[0] == "INVALID_ARGUMENT_VALUE"
    assert "<= 90" in report.errors[0].message
    report = validate(make_step("hot_spots", {
        "input_path": "in.parquet", "output_path": "out.parquet", "value_field": "x",
        "weights": "contiguity", "alpha": 1,
    }))
    assert codes(report)[0] == "INVALID_ARGUMENT_VALUE"
    assert "< 1" in report.errors[0].message


def test_run_sql_output_must_be_parquet():
    report = validate(make_step("run_sql", {"query": "SELECT 1", "output_path": "answer.gpkg"}))
    assert codes(report)[0] == "INVALID_ARGUMENT_VALUE"
    assert ".parquet" in report.errors[0].message
    assert "answer.gpkg" in report.errors[0].message


def test_valid_values_do_not_raise_contract_errors():
    report = validate(make_step("resample_raster", {
        "input_path": "in.tif", "output_path": "out.tif", "resolution": 30, "resampling": "nearest",
    }))
    assert "INVALID_ARGUMENT_VALUE" not in codes(report)


def test_every_numeric_rule_rejects_its_outside_value():
    for (operation, name), contract in catalog.PARAMETER_CONTRACTS.items():
        param = parameter(operation, name)
        if "minimum" in contract:
            errors = []
            _check_contract("step", name, contract["minimum"] - 1, param, errors)
            assert errors, (operation, name, "minimum")
        if "maximum" in contract:
            errors = []
            _check_contract("step", name, contract["maximum"] + 1, param, errors)
            assert errors, (operation, name, "maximum")
        if "exclusive_minimum" in contract:
            errors = []
            _check_contract("step", name, contract["exclusive_minimum"], param, errors)
            assert errors, (operation, name, "exclusive_minimum")
        if "exclusive_maximum" in contract:
            errors = []
            _check_contract("step", name, contract["exclusive_maximum"], param, errors)
            assert errors, (operation, name, "exclusive_maximum")


def test_convert_format_refuses_shapefile_before_execution():
    report = validate(make_step("convert_format", {"input_path": "in.parquet", "output_path": "out.shp"}))
    assert codes(report)[0] == "INVALID_ARGUMENT_VALUE"
    assert ".geojson" in report.errors[0].message


def test_cardinality_rules_are_enforced():
    report = validate(make_step("merge_layers", {"input_paths": ["one.parquet"], "output_path": "out.parquet"}))
    assert codes(report)[0] == "INVALID_ARGUMENT_VALUE"
    assert "at least 2" in report.errors[0].message
    report = validate(make_step("summarize_points_in_polygons", {
        "points_path": "points.parquet", "polygons_path": "zones.parquet", "output_path": "out.parquet",
        "field": "value", "statistics": [],
    }))
    assert codes(report)[0] == "INVALID_ARGUMENT_VALUE"
    assert "at least 1" in report.errors[0].message


def test_conditionally_required_arguments_are_enforced():
    report = validate(make_step("select_features", {
        "input_path": "in.parquet", "output_path": "out.parquet", "by": "field_equals",
    }))
    assert "MISSING_ARGUMENT" in codes(report)
    report = validate(make_step("hot_spots", {
        "input_path": "in.parquet", "output_path": "out.parquet", "value_field": "x", "weights": "distance_band",
    }))
    assert "MISSING_ARGUMENT" in codes(report)
    report = validate(make_step("zonal_statistics", {
        "raster_path": "dem.tif", "zones_path": "zones.parquet", "output_path": "out.parquet",
        "stats": ["weighted_mean"],
    }))
    assert "MISSING_ARGUMENT" in codes(report)


def test_cross_parameter_refusals_carry_the_rule():
    report = validate(make_step("flow_direction", {
        "dem_path": "dem.tif", "output_path": "flow.tif", "method": "dinf", "encoding": "east_first",
    }))
    assert codes(report)[0] == "INVALID_ARGUMENT_VALUE"
    assert "east_first" in report.errors[0].message
    report = validate(make_step("zonal_statistics", {
        "raster_path": "dem.tif", "zones_path": "zones.parquet", "output_path": "out.parquet",
        "weights_path": "weights.tif", "stats": ["mean"],
    }))
    assert codes(report)[0] == "INVALID_ARGUMENT_VALUE"
    assert "weighted" in report.errors[0].message
    report = validate(make_step("elevation_profile", {
        "raster_path": "dem.tif", "line_path": "line.parquet", "output_path": "out.parquet",
        "spacing": 10, "grade_threshold_percent": 5,
    }))
    assert "MISSING_ARGUMENT" in codes(report)
    report = validate(make_step("elevation_profile", {
        "raster_path": "dem.tif", "line_path": "line.parquet", "output_path": "out.parquet",
        "spacing": 10, "grade_base_length": 15,
    }))
    assert codes(report)[0] == "INVALID_ARGUMENT_VALUE"
    assert "whole number" in report.errors[0].message


def test_select_feature_modes_use_their_actual_parameters():
    report = validate(make_step("select_features", {
        "input_path": "in.parquet", "output_path": "out.parquet", "by": "field_in", "values": ["a"],
    }))
    assert "MISSING_ARGUMENT" in codes(report)
    report = validate(make_step("select_features", {
        "input_path": "in.parquet", "output_path": "out.parquet", "by": "field_in", "field": "name", "values": [],
    }))
    assert codes(report)[0] == "INVALID_ARGUMENT_VALUE"
    report = validate(make_step("select_features", {
        "input_path": "in.parquet", "output_path": "out.parquet", "by": "field_between", "field": "area", "minimum": 2,
    }))
    assert "INVALID_ARGUMENT_VALUE" not in codes(report)
    report = validate(make_step("select_features", {
        "input_path": "in.parquet", "output_path": "out.parquet", "by": "field_between",
        "field": "area", "minimum": 3, "maximum": 2,
    }))
    assert codes(report)[0] == "INVALID_ARGUMENT_VALUE"


def test_weighted_zonal_defaults_and_bad_numeric_windows_do_not_crash():
    for stats in (None, []):
        arguments = {
            "raster_path": "dem.tif", "zones_path": "zones.parquet", "output_path": "out.parquet",
            "weights_path": "weights.tif",
        }
        if stats is not None:
            arguments["stats"] = stats
        report = validate(make_step("zonal_statistics", arguments))
        assert "INVALID_ARGUMENT_VALUE" not in codes(report)
    for spacing in (0, float("nan"), "ten"):
        report = validate(make_step("elevation_profile", {
            "raster_path": "dem.tif", "line_path": "line.parquet", "output_path": "out.parquet",
            "spacing": spacing, "grade_base_length": 100,
        }))
        assert codes(report)
    report = validate(make_step("elevation_profile", {
        "raster_path": "dem.tif", "line_path": "line.parquet", "output_path": "out.parquet",
        "grade_base_length": 100,
    }))
    assert "MISSING_ARGUMENT" in codes(report)
    report = validate(make_step("select_features", {
        "input_path": "in.parquet", "output_path": "out.parquet", "by": "field_between",
        "field": "area", "minimum": "3", "maximum": 2,
    }))
    assert "WRONG_TYPE" in codes(report)


def test_every_rule_is_visible_to_the_planner():
    text = bench.catalogue_text()
    for (operation, name), contract in catalog.PARAMETER_CONTRACTS.items():
        if "choices" in contract:
            assert f"{name} in {contract['choices']}" in text, (operation, name)
        for key, operator in (("minimum", ">="), ("maximum", "<="), ("exclusive_minimum", ">"), ("exclusive_maximum", "<")):
            if key in contract:
                assert f"{name} {operator} {contract[key]}" in text, (operation, name, key)
        if "min_items" in contract:
            assert f"{name} at least {contract['min_items']} items" in text
        if "extensions" in contract:
            assert f"{name} ends with {contract['extensions']}" in text
    for operation, rules in catalog.STEP_CONTRACTS.items():
        assert f"step rules: {bench.json.dumps(rules, sort_keys=True)}" in text, operation


def test_dispatcher_rejects_an_unknown_engine_before_execution():
    from mapsmith.engines import dispatch

    with pytest.raises(ValueError, match="engine must be one of"):
        dispatch.spatial_join_routed("left.parquet", "right.parquet", "out.parquet", engine="exactextract")


def test_resample_actually_supports_a_warp_only_method(tmp_path):
    np = pytest.importorskip("numpy")
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin

    from mapsmith.engines import raster

    source = tmp_path / "source.tif"
    output = tmp_path / "output.tif"
    with rasterio.open(
        source, "w", driver="GTiff", width=2, height=2, count=1, dtype="uint8",
        crs="EPSG:32633", transform=from_origin(0, 0, 10, 10),
    ) as dst:
        dst.write(np.array([[1, 2], [3, 4]], dtype="uint8"), 1)
    raster.resample(str(source), str(output), 20, "max")
    with rasterio.open(output) as result:
        assert result.read(1)[0, 0] == 4
