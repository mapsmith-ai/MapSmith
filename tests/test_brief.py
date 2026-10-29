"""D-063: only validated plans become briefs, and every statement is traced."""

from __future__ import annotations

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import Point

from mapsmith import server
from mapsmith.plans import Plan
from mapsmith.plans.brief import assert_traced, execute_plan_requests, render


def plan(tmp_path, operation="buffer_layer"):
    wells = tmp_path / "wells.gpkg"
    gpd.GeoDataFrame({"id": [1]}, geometry=[Point(9.19, 45.46)], crs="EPSG:4326").to_file(wells)
    return Plan.model_validate({"goal": "wells near risk", "steps": [{
        "id": "step", "operation": operation,
        "arguments": {"input_path": str(wells), "output_path": str(tmp_path / "out.parquet"),
                      **({"distance_meters": 10} if operation == "buffer_layer" else {})},
    }]})


def test_invalid_plan_returns_only_validation(tmp_path):
    response = server.brief_plan(Plan.model_validate({"steps": [{"id": "x", "operation": "invented", "arguments": {}}]}))
    assert response["valid"] is False
    assert "brief" not in response
    assert response["validation"]["errors"][0]["code"] == "UNKNOWN_OPERATION"


def test_valid_brief_has_operation_and_parameter_sources(tmp_path):
    current = plan(tmp_path)
    brief = render(current)
    assert_traced(brief)
    assert brief["plan_sha256"] == current.sha256()
    assert brief["steps"][0]["source"] == {"kind": "operation", "ref": "buffer_layer"}
    assert any(item["source"]["ref"] == "buffer_layer.distance_meters" for item in brief["decisions"])
    assert brief["install"] == []


def test_invalid_plan_cannot_be_rendered_directly(tmp_path):
    with pytest.raises(ValueError, match="no project brief"):
        render(Plan.model_validate({"steps": [{"id": "x", "operation": "invented", "arguments": {}}]}))


def test_validation_precedes_render(tmp_path, monkeypatch):
    calls = []
    from mapsmith.plans import validator

    current = plan(tmp_path)
    original = validator.validate
    monkeypatch.setattr("mapsmith.plans.validate", lambda plan: calls.append("validate") or original(plan))
    monkeypatch.setattr("mapsmith.plans.brief.render", lambda plan: calls.append("render") or {})
    server.brief_plan(current)
    assert calls == ["validate", "render"]


def dem_path(tmp_path, crs="EPSG:32633"):
    path = tmp_path / "dem.tif"
    with rasterio.open(
        path, "w", driver="GTiff", width=2, height=2, count=1, dtype="float32",
        crs=crs, transform=from_origin(0, 0, 10, 10),
    ) as dataset:
        dataset.write(np.zeros((2, 2), dtype="float32"), 1)
    return str(path)


def slope_plan(tmp_path, crs="EPSG:32633"):
    return Plan.model_validate({"goal": "slope", "steps": [{
        "id": "step", "operation": "slope", "arguments": {
            "dem_path": dem_path(tmp_path, crs), "output_path": str(tmp_path / "slope.tif"),
        },
    }]})


def test_missing_engine_returns_validation_and_an_install_command(tmp_path, monkeypatch):
    from mapsmith.engines import dispatch

    monkeypatch.setattr(dispatch, "available_engines", lambda: {"whitebox": False})
    response = server.brief_plan(slope_plan(tmp_path))
    assert response["valid"] is False
    assert "brief" not in response
    assert response["validation"]["errors"][0]["code"] == "MISSING_EXTRA"
    assert response["install"] == [{
        "text": "pip install mapsmith[whitebox]",
        "source": {"kind": "install", "ref": "whitebox"},
    }]


def test_available_engine_and_exactextract_extra_are_explicit(tmp_path, monkeypatch):
    from mapsmith.engines import dispatch

    monkeypatch.setattr(dispatch, "available_engines", lambda: {"whitebox": True})
    response = server.brief_plan(slope_plan(tmp_path))
    assert response["valid"] is True
    assert response["brief"]["install"] == []
    assert response["brief"]["engines"][0]["available"] is True

    zones = tmp_path / "zones.gpkg"
    gpd.GeoDataFrame({"id": [1]}, geometry=[Point(5, -5)], crs="EPSG:32633").to_file(zones)
    current = Plan.model_validate({"steps": [{
        "id": "step", "operation": "zonal_statistics", "arguments": {
            "raster_path": dem_path(tmp_path), "zones_path": str(zones),
            "output_path": str(tmp_path / "zonal.parquet"),
        },
    }]})
    monkeypatch.setattr(dispatch, "available_engines", lambda: {"exactextract": False})
    response = server.brief_plan(current)
    assert response["install"][0]["text"] == "pip install mapsmith[raster]"


def test_geographic_dem_is_invalid_before_rendering(tmp_path):
    response = server.brief_plan(slope_plan(tmp_path, "EPSG:4326"))
    assert response["valid"] is False
    assert "brief" not in response
    assert response["validation"]["errors"][0]["code"] == "CRS_UNSUITABLE"
    with pytest.raises(ValueError, match="no project brief"):
        render(slope_plan(tmp_path, "EPSG:4326"))


def test_explicit_spatial_join_engine_gets_its_own_install_guidance(tmp_path, monkeypatch):
    from mapsmith.engines import dispatch
    from mapsmith.plans.brief import engine_requirements

    wells = tmp_path / "wells.gpkg"
    gpd.GeoDataFrame({"id": [1]}, geometry=[Point(9.19, 45.46)], crs="EPSG:4326").to_file(wells)
    current = Plan.model_validate({"steps": [{
        "id": "step", "operation": "spatial_join", "arguments": {
            "left_path": str(wells), "right_path": str(wells),
            "output_path": str(tmp_path / "joined.parquet"), "engine": "sedonadb",
        },
    }]})
    monkeypatch.setattr(dispatch, "available_engines", lambda: {"sedonadb": False})
    response = server.brief_plan(current)
    assert response["validation"]["errors"][0]["code"] == "ENGINE_NOT_AVAILABLE"
    assert response["install"][0]["text"] == "pip install mapsmith[sedona]"
    assert engine_requirements(current)[0]["engine"] == "sedonadb"


def test_untracked_prose_and_bad_parameter_references_fail(tmp_path):
    brief = render(plan(tmp_path))
    brief["steps"].append({"text": "trust me"})
    with pytest.raises(ValueError, match="untracked"):
        assert_traced(brief)
    brief["steps"][-1] = {"text": "bad", "source": {"kind": "parameter", "ref": "buffer_layer.nope"}}
    with pytest.raises(ValueError, match="unknown parameter"):
        assert_traced(brief)

    current = plan(tmp_path)
    brief = render(current)
    brief["steps"][0]["text"] = "a false statement with a valid source"
    with pytest.raises(ValueError, match="does not match"):
        assert_traced(brief, current)


def test_d063_population_is_exactly_sixteen():
    found = execute_plan_requests()
    assert len(found) == 16
    assert [index for index, _ in found] == [19, 21, 24, 43, 47, 93, 99, 107, 109, 111, 112, 122, 140, 141, 142, 147]


def test_unknown_spatial_join_engine_has_no_invented_install_extra(tmp_path):
    from mapsmith.plans.brief import engine_requirements

    wells = tmp_path / "wells.gpkg"
    gpd.GeoDataFrame({"id": [1]}, geometry=[Point(9.19, 45.46)], crs="EPSG:4326").to_file(wells)
    current = Plan.model_validate({"steps": [{
        "id": "step", "operation": "spatial_join", "arguments": {
            "left_path": str(wells), "right_path": str(wells),
            "output_path": str(tmp_path / "joined.parquet"), "engine": "invented",
        },
    }]})

    response = server.brief_plan(current)

    assert [item["code"] for item in response["validation"]["errors"]] == [
        "INVALID_ARGUMENT_VALUE"
    ]
    assert "install" not in response
    assert engine_requirements(current)[0]["extra"] is None
