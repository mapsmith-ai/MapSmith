"""QGIS sidecar: real native:buffer plus bounded refusal paths."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import geopandas as gpd
import pytest
import shapely

from mapsmith import stacks
from mapsmith.engines import dispatch, qgis
from mapsmith.plans import Plan, execute, validate

CRS = "EPSG:32632"
HAS_QGIS = bool(stacks.installed()["opensource"]["qgis"])


def squares(tmp_path):
    path = tmp_path / "squares.gpkg"
    gpd.GeoDataFrame(
        {"long_field_name": [1, 2]},
        geometry=[shapely.box(0, 0, 100, 100), shapely.box(300, 0, 400, 100)],
        crs=CRS,
    ).to_file(path)
    return str(path)


def test_only_the_whitelisted_algorithm_is_accepted(tmp_path):
    with pytest.raises(ValueError, match="native:buffer"):
        qgis.process("native:centroids", "in.gpkg", "out.parquet", 10)


def test_geographic_crs_is_refused_before_buffering(tmp_path):
    source = tmp_path / "geographic.gpkg"
    gpd.GeoDataFrame({"id": [1]}, geometry=[shapely.Point(9, 45)], crs="EPSG:4326").to_file(source)
    with pytest.raises(ValueError, match="projected CRS"):
        qgis.process("native:buffer", str(source), str(tmp_path / "out.parquet"), 10)


def test_untrusted_result_path_is_refused_without_being_opened(tmp_path, monkeypatch):
    monkeypatch.setattr(stacks, "_qgis_process", lambda: "qgis")

    def compromised(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=["qgis"], returncode=0,
            stdout=json.dumps({
                "algorithm_details": {"id": "native:buffer"},
                "results": {"OUTPUT": "//attacker/share/output.gpkg"},
                "log": {},
            }),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", compromised)
    with pytest.raises(RuntimeError, match="unsafe output path"):
        qgis.process("native:buffer", squares(tmp_path), str(tmp_path / "out.parquet"), 10)


def test_missing_qgis_is_an_actionable_engine_error(tmp_path, monkeypatch):
    monkeypatch.setattr(stacks, "_qgis_process", lambda: None)
    with pytest.raises(RuntimeError, match="qgis_process"):
        qgis.process("native:buffer", "in.gpkg", "out.parquet", 10)

    current = Plan.model_validate({"steps": [{
        "id": "buffer", "operation": "qgis_processing", "arguments": {
            "algorithm": "native:buffer", "input_path": squares(tmp_path),
            "distance": 10, "output_path": "out.parquet",
        },
    }]})
    monkeypatch.setattr(dispatch, "available_engines", lambda: {"qgis": False})
    report = validate(current)
    assert [issue.code for issue in report.errors] == ["ENGINE_NOT_AVAILABLE"]
    assert "pip install" not in report.errors[0].message


@pytest.mark.skipif(not HAS_QGIS, reason="QGIS is not installed on this machine")
@pytest.mark.slow
def test_real_qgis_buffer_has_closed_form_geometry_and_provenance(tmp_path, monkeypatch):
    monkeypatch.setenv("MAPSMITH_WORKSPACE", str(tmp_path))
    output = tmp_path / "buffered.parquet"
    result = qgis.process(
        "native:buffer", squares(tmp_path), str(output), 10, segments=24
    )
    frame = gpd.read_parquet(output)
    assert result["feature_count"] == 2
    assert result["engine_used"] == "qgis"
    assert frame.area.sum() == pytest.approx(28628.3, rel=0.002)
    assert frame.crs.to_epsg() == 32632
    assert "long_field_name" in frame.columns
    record = json.loads(Path(result["provenance"]).read_text(encoding="utf-8"))
    assert record["engine"]["name"] == "QGIS Processing"
    assert record["engine"]["version"].startswith("4.2.3")
    assert record["parameters"]["distance_unit"].startswith("the input CRS")
    assert record["parameters"]["segments"] == 24
    assert record["parameters"]["end_cap_style"] == 0


@pytest.mark.skipif(not HAS_QGIS, reason="QGIS is not installed on this machine")
@pytest.mark.slow
def test_real_qgis_dissolve_and_plan_execution(tmp_path):
    current = Plan.model_validate({"goal": "one QGIS buffer", "steps": [{
        "id": "buffer", "operation": "qgis_processing", "arguments": {
            "algorithm": "native:buffer", "input_path": squares(tmp_path),
            "distance": 10, "dissolve": True,
            "output_path": str(tmp_path / "dissolved.parquet"),
        },
    }]})
    result = execute(current)
    assert result["executed"] is True, result
    assert result["steps"][0]["engine_used"] == "qgis"
    assert result["steps"][0]["feature_count"] == 1
    assert result["plan_manifest"]


@pytest.mark.skipif(not HAS_QGIS, reason="QGIS is not installed on this machine")
@pytest.mark.slow
def test_empty_input_returns_an_empty_buffer_with_a_warning(tmp_path):
    source = tmp_path / "empty.gpkg"
    gpd.GeoDataFrame({"id": []}, geometry=[], crs=CRS).to_file(source)
    output = tmp_path / "empty.parquet"
    result = qgis.process("native:buffer", str(source), str(output), 10)
    assert result["feature_count"] == 0
    record = json.loads(Path(result["provenance"]).read_text(encoding="utf-8"))
    empty = next(check for check in record["verification"] if check["name"] == "result_not_empty")
    assert empty["passed"] is False
    assert empty["critical"] is False
