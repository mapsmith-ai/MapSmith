"""apportion_by_area, on zones whose answer can be worked out on paper.

Two source zones side by side in UTM, 100 m squares: West holds 100 people at a
rate of 10, East holds 40 at a rate of 20. Three targets: A takes the western
half of West; B takes the eastern half of West and all of East; C lies beyond
both and touches neither.

    population  A = 100 * 1/2         = 50,  bounds [0, 100]
                B = 100 * 1/2 + 40    = 90,  bounds [40, 140]   (East lies wholly in B)
                C = 0,                       bounds [0, 0]
    rate        A = 10,                      bounds [10, 10]
                B = (10*5000 + 20*10000) / 15000 = 16.667, bounds [10, 20]
    total       50 + 90 = 140, the source total: nothing lost, nothing made up

Geodesic areas make the halves equal to about one part in a million rather
than exactly, which is the tolerance used.
"""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import Point, box

from mapsmith.engines import vector

CRS = "EPSG:32632"
X0, Y0 = 500000.0, 5000000.0


def _square(x, y, w, h=100.0):
    return box(X0 + x, Y0 + y, X0 + x + w, Y0 + y + h)


@pytest.fixture
def zones(tmp_path):
    source = gpd.GeoDataFrame(
        {"name": ["West", "East"], "population": [100, 40], "rate": [10.0, 20.0]},
        geometry=[_square(0, 0, 100), _square(100, 0, 100)],
        crs=CRS,
    )
    target = gpd.GeoDataFrame(
        {"zone": ["A", "B", "C"]},
        geometry=[_square(0, 0, 50), _square(50, 0, 150), _square(200, 0, 100)],
        crs=CRS,
    )
    paths = {"source": tmp_path / "source.gpkg", "target": tmp_path / "target.gpkg"}
    source.to_file(paths["source"])
    target.to_file(paths["target"])
    return {k: str(v) for k, v in paths.items()}


def _run(zones, tmp_path, **kwargs):
    out = tmp_path / "apportioned.parquet"
    result = vector.apportion_by_area(zones["source"], zones["target"], str(out), **kwargs)
    return result, gpd.read_parquet(out).set_index("zone")


def test_a_count_splits_by_area_and_its_bounds_need_no_assumption(zones, tmp_path):
    result, out = _run(zones, tmp_path, extensive=["population"])
    assert out.loc["A", "population"] == pytest.approx(50, rel=1e-6)
    assert out.loc["B", "population"] == pytest.approx(90, rel=1e-6)
    assert out.loc["C", "population"] == 0
    assert (out.loc["A", "population_min"], out.loc["A", "population_max"]) == (0, 100)
    assert (out.loc["B", "population_min"], out.loc["B", "population_max"]) == (40, 140)
    assert out["population"].sum() == pytest.approx(140, rel=1e-9)
    assert result["verified"] is True


def test_a_rate_is_averaged_by_area_not_split(zones, tmp_path):
    _, out = _run(zones, tmp_path, intensive=["rate"])
    assert out.loc["A", "rate"] == pytest.approx(10, rel=1e-6)
    assert out.loc["B", "rate"] == pytest.approx(50 / 3, rel=1e-6)
    assert (out.loc["B", "rate_min"], out.loc["B", "rate_max"]) == (10, 20)
    # C touches no source zone: a rate there is unknown, not zero.
    assert out["rate"].isna()["C"]


def test_coverage_says_how_much_of_each_target_the_sources_reach(zones, tmp_path):
    _, out = _run(zones, tmp_path, extensive=["population"])
    assert out.loc["A", "source_coverage"] == pytest.approx(1.0, rel=1e-9)
    assert out.loc["B", "source_coverage"] == pytest.approx(1.0, rel=1e-9)
    assert out.loc["C", "source_coverage"] == 0.0


def test_the_record_states_the_assumption_and_the_total_is_checked(zones, tmp_path):
    result, _ = _run(zones, tmp_path, extensive=["population"], intensive=["rate"])
    manifest = json.loads(Path(result["provenance"]).read_text(encoding="utf-8"))
    assert any("uniformly" in note for note in manifest["notes"])
    checks = {c["name"]: c["passed"] for c in manifest["verification"]}
    assert checks["x-mapsmith:extensive_total_preserved"] is True
    assert checks["x-mapsmith:estimate_within_bounds"] is True
    assert checks["x-mapsmith:every_source_value_placed"] is True
    assert [i["argument"] for i in manifest["inputs"]] == ["source_path", "target_path"]


def test_value_outside_every_target_is_reported_not_lost_silently(zones, tmp_path):
    only_a = gpd.read_file(zones["target"]).iloc[[0]]
    path = tmp_path / "only_a.gpkg"
    only_a.to_file(path)
    out = tmp_path / "o.parquet"
    result = vector.apportion_by_area(zones["source"], str(path), str(out), extensive=["population"])
    manifest = json.loads(Path(result["provenance"]).read_text(encoding="utf-8"))
    checks = {c["name"]: c for c in manifest["verification"]}
    assert checks["x-mapsmith:extensive_total_preserved"]["passed"] is True
    placed = checks["x-mapsmith:every_source_value_placed"]
    assert placed["passed"] is False and "90" in placed["detail"]


def test_targets_in_another_crs_come_back_in_their_own(zones, tmp_path):
    target = gpd.read_file(zones["target"]).to_crs("EPSG:4326")
    path = tmp_path / "target_wgs.gpkg"
    target.to_file(path)
    out = tmp_path / "o.parquet"
    vector.apportion_by_area(zones["source"], str(path), str(out), extensive=["population"])
    written = gpd.read_parquet(out).set_index("zone")
    assert written.crs.to_epsg() == 4326
    assert written.loc["B", "population"] == pytest.approx(90, rel=1e-4)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({}, "declare each column"),
        ({"extensive": ["population"], "intensive": ["population"]}, "both extensive and intensive"),
        ({"extensive": ["nope"]}, "not in"),
    ],
)
def test_what_is_refused_before_anything_is_written(zones, tmp_path, kwargs, message):
    out = tmp_path / "o.parquet"
    with pytest.raises(ValueError, match=message):
        vector.apportion_by_area(zones["source"], zones["target"], str(out), **kwargs)
    assert not out.exists()


def test_a_negative_count_is_refused(tmp_path, zones):
    source = gpd.read_file(zones["source"])
    source.loc[0, "population"] = -5
    path = tmp_path / "neg.gpkg"
    source.to_file(path)
    with pytest.raises(ValueError, match="negative"):
        vector.apportion_by_area(str(path), zones["target"], str(tmp_path / "o.parquet"),
                                 extensive=["population"])


def test_a_target_column_that_would_be_overwritten_is_refused(tmp_path, zones):
    target = gpd.read_file(zones["target"])
    target["population"] = 1
    path = tmp_path / "clash.gpkg"
    target.to_file(path)
    with pytest.raises(ValueError, match="already has"):
        vector.apportion_by_area(zones["source"], str(path), str(tmp_path / "o.parquet"),
                                 extensive=["population"])


def test_points_are_not_zones(tmp_path, zones):
    points = gpd.GeoDataFrame({"population": [1]}, geometry=[Point(X0, Y0)], crs=CRS)
    path = tmp_path / "pts.gpkg"
    points.to_file(path)
    with pytest.raises(ValueError, match="polygons"):
        vector.apportion_by_area(str(path), zones["target"], str(tmp_path / "o.parquet"),
                                 extensive=["population"])
