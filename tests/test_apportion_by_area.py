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

The zones are cut in their own CRS and the areas measured on an equal-area
plane, so the halves are equal to rounding, and the tolerance is tight.
"""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
from shapely import segmentize as shapely_segmentize
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
    notes = " ".join(manifest["notes"])
    # The two kinds of bound are not the same claim, and the record says which is which.
    assert "hold without that assumption" in notes and "rest on the same assumption" in notes
    checks = {c["name"]: c["passed"] for c in manifest["verification"]}
    assert checks["x-mapsmith:extensive_total_preserved"] is True
    assert checks["x-mapsmith:every_source_value_placed"] is True
    # An arithmetic identity is not a check (D-079): removed in review.
    assert "x-mapsmith:estimate_within_bounds" not in checks
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


def test_overlapping_targets_are_said_and_do_not_fail_the_total(zones, tmp_path):
    """Two targets over the same ground each receive the piece: the totals differ by design."""
    target = gpd.GeoDataFrame(
        {"zone": ["A", "A2"]}, geometry=[_square(0, 0, 50), _square(0, 0, 50)], crs=CRS
    )
    path = tmp_path / "twice.gpkg"
    target.to_file(path)
    out = tmp_path / "o.parquet"
    result = vector.apportion_by_area(zones["source"], str(path), str(out), extensive=["population"])
    manifest = json.loads(Path(result["provenance"]).read_text(encoding="utf-8"))
    total = next(c for c in manifest["verification"]
                 if c["name"] == "x-mapsmith:extensive_total_preserved")
    # Checked against the pieces, which count a piece once per target: a real
    # comparison even when the targets overlap (it used to give up there).
    assert total["passed"] is True and total["critical"] is True and "overlap" in total["detail"]
    assert any("overlap" in note for note in manifest["notes"])
    assert gpd.read_parquet(out)["population"].sum() == pytest.approx(100, rel=1e-6)
    # And the 50 in the eastern half of West, and East's 40, are in no target.
    placed = next(c for c in manifest["verification"]
                  if c["name"] == "x-mapsmith:every_source_value_placed")
    assert placed["passed"] is False and placed["detail"].startswith("population: 90 of 140")


def test_the_total_is_checked_on_the_file_written(zones, tmp_path, monkeypatch):
    """A count doubled between the computation and the disk stops the operation."""
    from mapsmith.verify import VerificationError

    real = vector._write

    def doubling(frame, path):
        frame = frame.copy()
        frame["population"] = frame["population"] * 2
        return real(frame, path)

    monkeypatch.setattr(vector, "_write", doubling)
    out = tmp_path / "o.parquet"
    with pytest.raises(VerificationError, match="extensive_total_preserved"):
        vector.apportion_by_area(zones["source"], zones["target"], str(out), extensive=["population"])


def test_every_manifest_shape_conforms(zones, tmp_path):
    """Counts and rates, same CRS and reprojected targets: all valid records."""
    from conftest import _spec_problems

    target = gpd.read_file(zones["target"]).to_crs("EPSG:4326")
    reprojected = tmp_path / "t4326.gpkg"
    target.to_file(reprojected)
    for n, (tgt, kwargs) in enumerate([
        (zones["target"], {"intensive": ["rate"]}),
        (str(reprojected), {"extensive": ["population"], "intensive": ["rate"]}),
    ]):
        result = vector.apportion_by_area(zones["source"], tgt, str(tmp_path / f"o{n}.parquet"), **kwargs)
        manifest = json.loads(Path(result["provenance"]).read_text(encoding="utf-8"))
        assert _spec_problems(manifest) == [], manifest


# --- the first review's cases: geodesic pieces did not recompose a zone ------------------------


def _layer(tmp_path, name, geometries, crs, **columns):
    path = tmp_path / f"{name}.gpkg"
    gpd.GeoDataFrame(columns or {"zone": [f"z{i}" for i in range(len(geometries))]},
                     geometry=list(geometries), crs=crs).to_file(path)
    return str(path)


@pytest.mark.parametrize(
    ("crs", "x0", "y0", "size"),
    [
        ("EPSG:3035", 4_300_000.0, 2_400_000.0, 50_000.0),  # 50 km, Europe equal-area
        ("EPSG:4326", 9.0, 45.0, 0.1),  # a 0.1 degree cell
        ("EPSG:4326", 0.0, 40.0, 10.0),  # a 10 degree cell: 941.8 people were made up
    ],
)
def test_a_zone_split_in_two_by_its_targets_keeps_its_total(tmp_path, crs, x0, y0, size):
    source = _layer(tmp_path, "s", [box(x0, y0, x0 + size, y0 + size)], crs, population=[1_000_000])
    target = _layer(tmp_path, "t", [box(x0, y0, x0 + size / 2, y0 + size),
                                    box(x0 + size / 2, y0, x0 + size, y0 + size)], crs)
    out = tmp_path / "o.parquet"
    vector.apportion_by_area(source, target, str(out), extensive=["population"])
    written = gpd.read_parquet(out)
    assert written["population"].sum() == pytest.approx(1_000_000, rel=1e-9)
    # Two targets that tile the zone are not "overlapping".
    manifest = json.loads(Path(str(out) + ".provenance.json").read_text(encoding="utf-8"))
    assert not any("overlap" in note for note in manifest["notes"])


def test_a_zone_wholly_inside_its_target_is_seen_as_wholly_inside(tmp_path):
    """The target has an extra vertex half way along the zone's west edge.

    Measured geodesically the zone came out 0.002% outside its own target, so
    `_min` was 0 instead of 1000 and the value was reported as partly unplaced.
    """
    x0, y0, s = 4_300_000.0, 2_400_000.0, 50_000.0
    from shapely.geometry import Polygon

    zone = box(x0, y0, x0 + s, y0 + s)
    target = Polygon([(x0, y0), (x0 + s, y0), (x0 + s, y0 + s), (x0, y0 + s), (x0, y0 + s / 2)])
    src = _layer(tmp_path, "s", [zone], "EPSG:3035", population=[1000])
    tgt = _layer(tmp_path, "t", [target], "EPSG:3035")
    out = tmp_path / "o.parquet"
    result = vector.apportion_by_area(src, tgt, str(out), extensive=["population"])
    row = gpd.read_parquet(out).iloc[0]
    assert (row["population"], row["population_min"], row["population_max"]) == pytest.approx((1000, 1000, 1000))
    manifest = json.loads(Path(result["provenance"]).read_text(encoding="utf-8"))
    checks = {c["name"]: c["passed"] for c in manifest["verification"]}
    assert checks["x-mapsmith:every_source_value_placed"] is True
    # The zones are cut in the source CRS; the plane only measures areas.
    assert manifest["crs_decisions"]["analysis_crs"] == "EPSG:3035"
    assert "equal-area" in manifest["crs_decisions"]["x-mapsmith:areas_measured_on"]


def test_a_target_equal_to_its_source_in_another_crs_gets_the_whole_value(tmp_path):
    """The same region, held in another CRS: the estimate never falls below its own bounds.

    The target is densified before it is reprojected, so it is the same region
    and not a polygon of four chords. The review measured an estimate BELOW its
    own `_min` here (999999.9997 against 1000000).
    """
    import shapely

    zone = box(9.0, 45.0, 9.5, 45.5)
    src = _layer(tmp_path, "s", [zone], "EPSG:4326", population=[1_000_000])
    dense = shapely.segmentize(zone, 0.0005)
    tgt_frame = gpd.GeoDataFrame({"zone": ["same"]}, geometry=[dense], crs="EPSG:4326").to_crs("EPSG:3035")
    tgt = tmp_path / "t.gpkg"
    tgt_frame.to_file(tgt)
    out = tmp_path / "o.parquet"
    vector.apportion_by_area(src, str(tgt), str(out), extensive=["population"])
    row = gpd.read_parquet(out).iloc[0]
    assert row["population"] == pytest.approx(1_000_000, rel=1e-5)
    assert row["population_min"] <= row["population"] <= row["population_max"]


def test_an_invalid_zone_is_repaired_and_the_repair_recorded(tmp_path):
    """A bowtie: written with 1e11 people before, then a raw GEOS error."""
    from shapely.geometry import Polygon

    bowtie = Polygon([(500_000, 5_000_000), (500_100, 5_000_100), (500_100, 5_000_000), (500_000, 5_000_100)])
    src = _layer(tmp_path, "s", [bowtie], CRS, population=[100])
    tgt = _layer(tmp_path, "t", [box(499_000, 4_999_000, 501_000, 5_001_000)], CRS)
    out = tmp_path / "o.parquet"
    result = vector.apportion_by_area(src, tgt, str(out), extensive=["population"])
    assert gpd.read_parquet(out)["population"].iloc[0] == pytest.approx(100, rel=1e-9)
    manifest = json.loads(Path(result["provenance"]).read_text(encoding="utf-8"))
    assert manifest["repairs"] and "make_valid" in manifest["repairs"][0]["action"]


def test_a_zone_drawn_across_the_antimeridian_as_one_ring_is_refused(tmp_path):
    from shapely.geometry import Polygon

    naive = Polygon([(179, -10), (-179, -10), (-179, -9), (179, -9)])
    src = _layer(tmp_path, "s", [naive], "EPSG:4326", population=[100])
    tgt = _layer(tmp_path, "t", [box(0, -10, 1, -9)], "EPSG:4326")
    with pytest.raises(ValueError, match="(?i)antimeridian|180"):
        vector.apportion_by_area(src, tgt, str(tmp_path / "o.parquet"), extensive=["population"])


def test_a_crs_with_no_datum_is_refused_with_the_reason(tmp_path):
    engineering = 'LOCAL_CS["site grid",UNIT["metre",1],AXIS["X",EAST],AXIS["Y",NORTH]]'
    src = _layer(tmp_path, "s", [box(0, 0, 10, 10)], engineering, population=[1])
    tgt = _layer(tmp_path, "t", [box(0, 0, 5, 10)], engineering)
    with pytest.raises(ValueError, match="no geodetic datum"):
        vector.apportion_by_area(src, tgt, str(tmp_path / "o.parquet"), extensive=["population"])


def test_widest_bounds_is_none_rather_than_nan_where_nothing_was_received(zones, tmp_path):
    only_c = gpd.read_file(zones["target"]).iloc[[2]]
    path = tmp_path / "only_c.gpkg"
    only_c.to_file(path)
    result = vector.apportion_by_area(zones["source"], str(path), str(tmp_path / "o.parquet"),
                                      intensive=["rate"])
    assert result["widest_bounds"] == {"rate": None}
    json.dumps(result["widest_bounds"], allow_nan=False)


# --- the second review's cases: the equal-area plane cut the zones ------------------------------


def _manifest(out):
    return json.loads(Path(str(out) + ".provenance.json").read_text(encoding="utf-8"))


def test_a_zone_sharing_two_edges_with_a_larger_target_is_wholly_inside(tmp_path):
    """Layers of different extents were densified with different steps.

    On the plane the zone's shared edges then ran a chord away from the
    target's, the zone came out a sliver outside, and `_min` was 0 instead of
    1000. Cut in the source CRS, a shared edge is the same line in both.
    """
    src = _layer(tmp_path, "s", [box(9.0, 45.0, 9.1, 45.1)], "EPSG:4326", population=[1000])
    tgt = _layer(tmp_path, "t", [box(9.0, 45.0, 11.0, 47.0)], "EPSG:4326")
    out = tmp_path / "o.parquet"
    vector.apportion_by_area(src, tgt, str(out), extensive=["population"])
    row = gpd.read_parquet(out).iloc[0]
    assert (row["population"], row["population_min"], row["population_max"]) == (1000, 1000, 1000)
    checks = {c["name"]: c["passed"] for c in _manifest(out)["verification"]}
    assert checks["x-mapsmith:every_source_value_placed"] is True


def test_a_zone_the_repair_leaves_with_no_area_is_refused_by_index(tmp_path):
    """A ring folded onto itself is a line once repaired.

    Before, its value vanished: the written total was 10 of 110 and the repair
    was recorded as resolved.
    """
    from shapely.geometry import Polygon

    folded = Polygon([(500_000, 5_000_000), (500_100, 5_000_000), (500_050, 5_000_000)])
    src = _layer(tmp_path, "s", [box(500_000, 5_000_100, 500_100, 5_000_200), folded], CRS,
                 population=[10, 100])
    tgt = _layer(tmp_path, "t", [box(499_000, 4_999_000, 501_000, 5_001_000)], CRS)
    out = tmp_path / "o.parquet"
    with pytest.raises(ValueError, match=r"zone\(s\) \[1\] of source_path enclose no area"):
        vector.apportion_by_area(src, tgt, str(out), extensive=["population"])
    assert not out.exists()


def test_a_zone_split_at_the_antimeridian_splits_by_its_true_areas(tmp_path):
    """One zone in two parts either side of 180 degrees, 1 and 3 degrees wide.

    On one latitude band the areas are exactly 1:3, so 25 and 75. A plane
    centred on the bounding boxes sat at longitude 0, on the far side of the
    Earth: 24.9977, and in the review's larger case 98.90 of 100, with no error.
    """
    from shapely.geometry import MultiPolygon

    east, west = box(179.0, -10.0, 180.0, -9.0), box(-180.0, -10.0, -177.0, -9.0)
    src = _layer(tmp_path, "s", [MultiPolygon([east, west])], "EPSG:4326", population=[100])
    tgt = _layer(tmp_path, "t", [east, west], "EPSG:4326")
    out = tmp_path / "o.parquet"
    vector.apportion_by_area(src, tgt, str(out), extensive=["population"])
    assert list(gpd.read_parquet(out)["population"]) == pytest.approx([25, 75], rel=1e-7)
    plane = _manifest(out)["crs_decisions"]["x-mapsmith:areas_measured_on"]
    # Centred on the zone, at the antimeridian, not on the far side of the Earth.
    assert abs(float(plane.split("lon_0=")[1].split(",")[0])) > 170


def test_zones_around_the_whole_globe_are_refused(tmp_path):
    """A global grid has no plane with every zone on one side: a GEOS error before."""
    cells = [box(lon, lat, lon + 30, lat + 30) for lon in range(-180, 180, 30) for lat in range(-90, 90, 30)]
    src = _layer(tmp_path, "s", cells, "EPSG:4326", population=[1] * len(cells))
    tgt = _layer(tmp_path, "t", [box(-180, -90, 180, 90)], "EPSG:4326")
    with pytest.raises(ValueError, match="(?i)hemisphere|around the globe"):
        vector.apportion_by_area(src, tgt, str(tmp_path / "o.parquet"), extensive=["population"])


def test_a_small_target_inside_a_large_zone_receives_its_share(tmp_path):
    """400 m2 inside 10^12 m2: a sliver threshold on the source alone dropped it to 0."""
    x0, y0 = 4_000_000.0, 2_000_000.0
    src = _layer(tmp_path, "s", [box(x0, y0, x0 + 1_000_000, y0 + 1_000_000)], "EPSG:3035",
                 population=[1e12])
    tgt = _layer(tmp_path, "t", [box(x0 + 500_000, y0 + 500_000, x0 + 500_020, y0 + 500_020)], "EPSG:3035")
    out = tmp_path / "o.parquet"
    vector.apportion_by_area(src, tgt, str(out), extensive=["population"])
    row = gpd.read_parquet(out).iloc[0]
    assert row["population"] == pytest.approx(400, rel=1e-9)
    assert row["population_max"] == 1e12


def test_an_equal_area_source_is_measured_in_its_own_crs(zones, tmp_path):
    """EPSG:3035 is equal-area already: no second plane, and the record says so."""
    src = _layer(tmp_path, "s", [box(4_300_000, 2_400_000, 4_300_100, 2_400_100)], "EPSG:3035",
                 population=[100])
    tgt = _layer(tmp_path, "t", [box(4_300_000, 2_400_000, 4_300_050, 2_400_100)], "EPSG:3035")
    out = tmp_path / "o.parquet"
    vector.apportion_by_area(src, tgt, str(out), extensive=["population"])
    assert gpd.read_parquet(out)["population"].iloc[0] == 50
    assert "the source CRS, which is equal-area" in _manifest(out)["crs_decisions"]["x-mapsmith:areas_measured_on"]


def test_the_plane_is_named_not_hashed_and_a_moved_target_is_listed(zones, tmp_path):
    target = gpd.read_file(zones["target"]).to_crs("EPSG:4326")
    path = tmp_path / "t4326.gpkg"
    target.to_file(path)
    out = tmp_path / "o.parquet"
    vector.apportion_by_area(zones["source"], str(path), str(out), extensive=["population"])
    decisions = _manifest(out)["crs_decisions"]
    assert decisions["analysis_crs"] == CRS
    assert "sha256" not in json.dumps(decisions)
    assert "lat_0=" in decisions["x-mapsmith:areas_measured_on"]
    assert [m["argument"] for m in decisions["x-mapsmith:inputs_reprojected"]] == ["target_path"]


# --- the third review's cases: what cutting in the source CRS broke -----------------------------


def test_a_projected_target_across_the_antimeridian_is_refused_in_degrees(tmp_path):
    """Valid in EPSG:3832; in the source's degrees, a ring around the rest of the planet.

    Before: the target received 5000 from a zone 59 degrees away and nothing
    from the zone inside it, with the total check green.
    """
    ring = gpd.GeoSeries([box(179, -10, 180, -9), box(-180, -10, -179, -9)],
                         crs="EPSG:4326").to_crs("EPSG:3832").union_all()
    tgt = _layer(tmp_path, "t", [ring], "EPSG:3832")
    src = _layer(tmp_path, "s", [box(179.2, -9.8, 179.8, -9.2), box(120, -10, 121, -9)],
                 "EPSG:4326", population=[100, 5000])
    out = tmp_path / "o.parquet"
    with pytest.raises(ValueError, match=r"target zone\(s\) \[0\].*180th meridian"):
        vector.apportion_by_area(src, tgt, str(out), extensive=["population"])
    assert not out.exists()


def test_a_projected_source_across_the_antimeridian_is_measured_not_refused(tmp_path):
    """A 2-degree zone at Fiji in EPSG:3832 was refused as spanning 162 degrees."""
    halves = gpd.GeoSeries([box(179, -18, 180, -17), box(-180, -18, -179, -17)],
                           crs="EPSG:4326").to_crs("EPSG:3832")
    src = _layer(tmp_path, "s", [halves.union_all()], "EPSG:3832", population=[100])
    tgt = _layer(tmp_path, "t", list(halves), "EPSG:3832")
    out = tmp_path / "o.parquet"
    vector.apportion_by_area(src, tgt, str(out), extensive=["population"])
    assert list(gpd.read_parquet(out)["population"]) == pytest.approx([50, 50], rel=1e-6)


def test_targets_overlapping_over_one_building_say_so(tmp_path):
    """100 m2 of overlap among 1.8e11: decided for the whole layer it passed for none.

    A 36 m2 building inside the overlap was then counted in both targets, 110
    written for 60, every check green and no note.
    """
    a = box(200_000, 4_500_000, 500_000, 4_800_000)
    b = box(500_000, 4_500_000, 800_000, 4_800_000).union(box(499_990, 4_650_000, 500_000, 4_650_010))
    src = _layer(tmp_path, "s", [box(499_992, 4_650_002, 499_998, 4_650_008),
                                 box(300_000, 4_600_000, 301_000, 4_601_000)],
                 "EPSG:32633", population=[50, 10])
    tgt = _layer(tmp_path, "t", [a, b], "EPSG:32633")
    out = tmp_path / "o.parquet"
    vector.apportion_by_area(src, tgt, str(out), extensive=["population"])
    notes = " ".join(_manifest(out)["notes"])
    assert "overlap one another over 1 source zone" in notes


def test_a_zone_cannot_give_out_more_than_it_holds_where_targets_do_not_overlap(zones, tmp_path, monkeypatch):
    """The check that replaced the identity: sabotage "wholly inside" and it goes red."""
    import shapely

    monkeypatch.setattr(shapely, "covers", lambda a, b: np.ones(len(a), dtype=bool))
    with pytest.raises(Exception, match="no_zone_gives_more_than_it_holds"):
        _run(zones, tmp_path, extensive=["population"])


MOLLWEIDE_WGS84 = "+proj=moll +lon_0=0 +x_0=0 +y_0=0 +datum=WGS84 +units=m +no_defs"


def test_mollweide_on_an_ellipsoid_is_not_taken_as_equal_area(tmp_path):
    """GHSL's Mollweide is on WGS 84, and PROJ's Mollweide is spherical: 0.3% out.

    A zone from the equator to 75N split at 30N: 517.64 / 482.36 before, where
    the areas on the ellipsoid give 516.06 / 483.94.
    """
    cells = gpd.GeoSeries([box(0, 0, 10, 75), box(0, 0, 10, 30), box(0, 30, 10, 75)],
                          crs="EPSG:4326")
    dense = gpd.GeoSeries(shapely_segmentize(cells, 0.01), crs="EPSG:4326").to_crs(MOLLWEIDE_WGS84)
    src = _layer(tmp_path, "s", [dense.iloc[0]], MOLLWEIDE_WGS84, population=[1000])
    tgt = _layer(tmp_path, "t", list(dense.iloc[1:]), MOLLWEIDE_WGS84)
    out = tmp_path / "o.parquet"
    vector.apportion_by_area(src, tgt, str(out), extensive=["population"])
    assert list(gpd.read_parquet(out)["population"]) == pytest.approx([516.06, 483.94], abs=0.02)
    assert "Lambert azimuthal" in _manifest(out)["crs_decisions"]["x-mapsmith:areas_measured_on"]


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_a_coordinate_that_is_not_a_number_is_refused_by_index(tmp_path, bad):
    from shapely.geometry import Polygon

    broken = Polygon([(500_000, 5_000_000), (500_100, 5_000_000), (500_100, bad), (500_000, 5_000_100)])
    src = _layer(tmp_path, "s", [box(500_000, 5_000_000, 500_100, 5_000_100), broken], CRS,
                 population=[1, 2])
    tgt = _layer(tmp_path, "t", [box(499_000, 4_999_000, 501_000, 5_001_000)], CRS)
    with pytest.raises(ValueError, match=r"zone\(s\) \[1\].*not a finite number"):
        vector.apportion_by_area(src, tgt, str(tmp_path / "o.parquet"), extensive=["population"])


def test_the_surfaces_are_named_by_their_ellipsoid_not_by_a_hash(tmp_path):
    """An authority-less CRS: the record named it by a digest and a datum called 'unknown'."""
    custom = ("+proj=lcc +lat_1=44 +lat_2=46 +lat_0=45 +lon_0=9 +x_0=0 +y_0=0 "
              "+a=6378137 +rf=298.257223563 +units=m +no_defs")
    src = _layer(tmp_path, "s", [box(0, 0, 1000, 1000)], custom, population=[100])
    tgt = _layer(tmp_path, "t", [box(0, 0, 500, 1000)], custom)
    out = tmp_path / "o.parquet"
    vector.apportion_by_area(src, tgt, str(out), extensive=["population"])
    plane = _manifest(out)["crs_decisions"]["x-mapsmith:areas_measured_on"]
    assert "a=6378137.0000 m" in plane and "1/f=298.257224" in plane
    assert "unknown" not in plane and "sha256" not in plane


def test_the_record_says_how_edges_were_densified(zones, tmp_path):
    target = gpd.read_file(zones["target"]).to_crs("EPSG:4326")
    path = tmp_path / "t4326.gpkg"
    target.to_file(path)
    out = tmp_path / "o.parquet"
    vector.apportion_by_area(zones["source"], str(path), str(out), extensive=["population"])
    notes = " ".join(_manifest(out)["notes"])
    assert "densified at" in notes and "before being brought onto the source CRS" in notes


# --- the fourth review's cases: seams that are not in degrees -----------------------------------


def _fiji_target(tmp_path):
    ring = gpd.GeoSeries([box(179, -10, 180, -9), box(-180, -10, -179, -9)],
                         crs="EPSG:4326").to_crs("EPSG:3832").union_all()
    return _layer(tmp_path, "t", [ring], "EPSG:3832")


def test_a_target_torn_by_web_mercators_seam_is_refused(tmp_path):
    """Web Mercator has its seam at 180 degrees too, in metres: the degrees-only check missed it.

    Before: the target received 5000 from a zone 59 degrees away, and the
    zone inside it was reported as covered by no target.
    """
    zones = gpd.GeoSeries([box(179.2, -9.8, 179.8, -9.2), box(120, -10, 121, -9)],
                          crs="EPSG:4326").to_crs("EPSG:3857")
    src = _layer(tmp_path, "s", list(zones), "EPSG:3857", population=[100, 5000])
    out = tmp_path / "o.parquet"
    with pytest.raises(ValueError, match=r"target zone\(s\) \[0\].*change area by more than 0\.1%"):
        vector.apportion_by_area(src, _fiji_target(tmp_path), str(out), extensive=["population"])
    assert not out.exists()


def test_a_target_brought_across_no_seam_is_not_refused(tmp_path):
    """The same target, onto a UTM zone that holds it whole: 200 of 200."""
    zones = gpd.GeoSeries([box(179.2, -9.8, 179.8, -9.2), box(-179.8, -9.8, -179.2, -9.2)],
                          crs="EPSG:4326").to_crs("EPSG:32760")
    src = _layer(tmp_path, "s", list(zones), "EPSG:32760", population=[100, 100])
    out = tmp_path / "o.parquet"
    vector.apportion_by_area(src, _fiji_target(tmp_path), str(out), extensive=["population"])
    row = gpd.read_parquet(out).iloc[0]
    assert (row["population"], row["population_min"]) == pytest.approx((200, 200), rel=1e-6)


def test_a_densely_drawn_zone_does_not_pull_the_centre_onto_itself(tmp_path):
    """30001 vertices against 5: weighted per vertex, the layer was refused as wider than a hemisphere."""
    dense = shapely_segmentize(box(155, -5, 160, 5), 0.001)
    src = _layer(tmp_path, "s", [box(0, -5, 5, 5), dense], "EPSG:4326", population=[100, 100])
    tgt = _layer(tmp_path, "t", [box(-1, -6, 6, 6), box(154, -6, 161, 6)], "EPSG:4326")
    out = tmp_path / "o.parquet"
    vector.apportion_by_area(src, tgt, str(out), extensive=["population"])
    assert list(gpd.read_parquet(out)["population"]) == pytest.approx([100, 100])
    plane = _manifest(out)["crs_decisions"]["x-mapsmith:areas_measured_on"]
    assert 70 < float(plane.split("lon_0=")[1].split(",")[0]) < 90


def test_two_longitude_conventions_in_one_crs_are_refused(tmp_path):
    """0..360 against -180..180: the zone at 180..181 and the target at -180..-179 never met (100 / 0)."""
    src = _layer(tmp_path, "s", [box(179, -10, 180, -9), box(180, -10, 181, -9)], "EPSG:4326",
                 population=[100, 100])
    tgt = _layer(tmp_path, "t", [box(179, -10, 180, -9), box(-180, -10, -179, -9)], "EPSG:4326")
    with pytest.raises(ValueError, match="0..360, the other in -180..180"):
        vector.apportion_by_area(src, tgt, str(tmp_path / "o.parquet"), extensive=["population"])



# --- the fifth review's cases: the area check must not refuse what is whole --------------------

CYLINDRICAL_EQUAL_AREA = "+proj=cea +lon_0=0 +lat_ts=0 +datum=WGS84 +units=m +no_defs"


def _geodesic_area(geometry):
    from pyproj import Geod

    return abs(Geod(ellps="WGS84").geometry_area_perimeter(shapely_segmentize(geometry, 0.01))[0])


@pytest.mark.parametrize("lat", [0, 45, 70])
def test_a_target_on_a_sphere_is_not_refused_as_torn(tmp_path, lat):
    """A MODIS sinusoidal grid is on a sphere: 0.2-0.45% from WGS 84, refused as crossing a seam."""
    modis = "+proj=sinu +lon_0=0 +x_0=0 +y_0=0 +R=6371007.181 +units=m +no_defs"
    cell = box(10, lat, 11, lat + 1)
    zone = box(9, lat - 1, 12, lat + 2)
    tgt_frame = gpd.GeoSeries([shapely_segmentize(cell, 0.01)], crs="EPSG:4326").to_crs(modis)
    tgt = _layer(tmp_path, "t", list(tgt_frame), modis)
    src = _layer(tmp_path, "s", [zone], "EPSG:4326", population=[900])
    out = tmp_path / "o.parquet"
    vector.apportion_by_area(src, tgt, str(out), extensive=["population"])
    expected = 900 * _geodesic_area(cell) / _geodesic_area(zone)
    assert gpd.read_parquet(out)["population"].iloc[0] == pytest.approx(expected, rel=1e-3)


def test_targets_on_opposite_sides_of_the_earth_are_not_refused(tmp_path):
    """One plane for every target refused the ones near its antipode; v4 gave 50 / 50."""
    src_frame = gpd.GeoSeries([box(5, 45, 6, 46), box(-175, -45, -173, -43)],
                              crs="EPSG:4326").to_crs(CYLINDRICAL_EQUAL_AREA)
    src = _layer(tmp_path, "s", list(src_frame), CYLINDRICAL_EQUAL_AREA, population=[100, 100])
    cells = [box(5 + i / 10, 45 + j / 10, 5 + (i + 1) / 10, 45 + (j + 1) / 10)
             for i in range(10) for j in range(10)]
    halves = [box(-175, -45, -174, -43), box(-174, -45, -173, -43)]
    tgt = _layer(tmp_path, "t", cells + halves, "EPSG:4326")
    out = tmp_path / "o.parquet"
    vector.apportion_by_area(src, tgt, str(out), extensive=["population"])
    written = gpd.read_parquet(out)["population"]
    assert list(written.iloc[-2:]) == pytest.approx([50, 50], rel=1e-6)
    assert written.sum() == pytest.approx(200, rel=1e-6)


def test_a_torn_target_is_refused_even_when_another_balances_it(tmp_path):
    """An antipodal target cancelled the targets' mean, and the check was skipped: 5000 for 100."""
    src_frame = gpd.GeoSeries([box(179.2, -9.8, 179.8, -9.2), box(120, -10, 121, -9)],
                              crs="EPSG:4326").to_crs(CYLINDRICAL_EQUAL_AREA)
    src = _layer(tmp_path, "s", list(src_frame), CYLINDRICAL_EQUAL_AREA, population=[100, 5000])
    ring = gpd.GeoSeries([box(179, -10, 180, -9), box(-180, -10, -179, -9)],
                         crs="EPSG:4326").to_crs("EPSG:3832").union_all()
    antipode = gpd.GeoSeries([box(-1, 9, 1, 10)], crs="EPSG:4326").to_crs("EPSG:3832").iloc[0]
    tgt = _layer(tmp_path, "t", [ring, antipode], "EPSG:3832")
    with pytest.raises(ValueError, match=r"target zone\(s\) \[0\]"):
        vector.apportion_by_area(src, tgt, str(tmp_path / "o.parquet"), extensive=["population"])


def test_many_small_zones_at_one_end_do_not_make_a_hemisphere(tmp_path):
    """A zone 120 degrees wide and 200 tiny ones at its end: the mean sat at 119E and refused them."""
    tiny = [box(119 + i * 0.004, 0, 119 + i * 0.004 + 0.003, 0.003) for i in range(200)]
    src = _layer(tmp_path, "s", [box(0, -5, 120, 5), *tiny], "EPSG:4326",
                 population=[1000] + [1] * 200)
    tgt = _layer(tmp_path, "t", [box(-1, -6, 121, 6)], "EPSG:4326")
    out = tmp_path / "o.parquet"
    vector.apportion_by_area(src, tgt, str(out), extensive=["population"])
    assert gpd.read_parquet(out)["population"].iloc[0] == pytest.approx(1200)



# --- the sixth review's case: the geodesic check must not read a ring's winding ------------------


def _winding_case(tmp_path, target_geometry, source_crs):
    from shapely.geometry import box as _box

    x0, y0 = 500_000.0, 5_000_000.0
    zone = gpd.GeoSeries([shapely_segmentize(_box(x0 - 10, y0 - 10, x0 + 3010, y0 + 1010), 10.0)],
                         crs=CRS).to_crs(source_crs)
    src = _layer(tmp_path, "s", list(zone), source_crs, population=[1000])
    tgt = _layer(tmp_path, "t", [target_geometry(x0, y0)], CRS)
    out = tmp_path / "o.parquet"
    vector.apportion_by_area(src, tgt, str(out), extensive=["population"])
    return gpd.read_parquet(out)["population"].iloc[0]


# In degrees the plane's first pass clears the target; equal-area, the geodesic reads every one.
WINDING_SOURCES = pytest.mark.parametrize("source_crs", ["EPSG:4326", CYLINDRICAL_EQUAL_AREA])


@WINDING_SOURCES
def test_a_hole_wound_like_its_shell_is_not_read_as_torn(tmp_path, source_crs):
    """Valid to shapely either way; the signed geodesic sum refused it as crossing a seam."""
    from shapely.geometry import Polygon

    def holed(x0, y0):
        shell = [(x0, y0), (x0 + 1000, y0), (x0 + 1000, y0 + 1000), (x0, y0 + 1000)]
        hole = [(x0 + 400, y0 + 400), (x0 + 600, y0 + 400), (x0 + 600, y0 + 600), (x0 + 400, y0 + 600)]
        return Polygon(shell, [hole])  # both counter-clockwise

    assert _winding_case(tmp_path, holed, source_crs) == pytest.approx(1000 * 960_000 / 3_080_400, rel=1e-3)


@WINDING_SOURCES
def test_parts_wound_opposite_ways_are_not_read_as_torn(tmp_path, source_crs):
    from shapely.geometry import MultiPolygon, Polygon

    def mixed(x0, y0):
        ccw = Polygon([(x0, y0), (x0 + 1000, y0), (x0 + 1000, y0 + 1000), (x0, y0 + 1000)])
        cw = Polygon([(x0 + 2000, y0), (x0 + 2000, y0 + 1000), (x0 + 3000, y0 + 1000), (x0 + 3000, y0)])
        return MultiPolygon([ccw, cw])

    assert _winding_case(tmp_path, mixed, source_crs) == pytest.approx(1000 * 2_000_000 / 3_080_400, rel=1e-3)



# --- the seventh review's case: a tear that keeps its area ---------------------------------------


@pytest.mark.parametrize("source_crs", ["EPSG:6933", "EPSG:3857"])
def test_a_tear_with_the_right_area_is_caught_by_position(tmp_path, source_crs):
    """A band 179.95 degrees wide centred on 180, torn, is the other half of its band.

    Its area is then what it should be to 0.03%, the area check passed, and the
    target received a zone in Africa: 5000 where 200 is right.
    """
    from pyproj import Transformer

    pacific = "+proj=cea +lon_0=180 +datum=WGS84 +units=m +no_defs"
    to_pacific = Transformer.from_crs("EPSG:4326", pacific, always_xy=True)
    half = abs(to_pacific.transform(180 - 179.95 / 2, 0)[0])
    top = to_pacific.transform(180, 10)[1]
    tgt = _layer(tmp_path, "t", [box(-half, 0, half, top)], pacific)
    zones = gpd.GeoSeries([box(175, 1, 180, 9), box(-180, 1, -175, 9), box(0, 1, 10, 9)],
                          crs="EPSG:4326").to_crs(source_crs)
    src = _layer(tmp_path, "s", list(zones), source_crs, population=[100, 100, 5000])
    with pytest.raises(ValueError, match="no longer hold a point of their own interior"):
        vector.apportion_by_area(src, tgt, str(tmp_path / "o.parquet"), extensive=["population"])



@pytest.mark.parametrize("source_crs", ["EPSG:4326", "EPSG:6933"])
def test_a_sliver_nanometres_wide_is_not_read_as_torn(tmp_path, source_crs):
    """At 1e-8 m, rounding the reprojected coordinates moves the area by percent: not a tear."""
    x0, y0 = 500_000.0, 5_000_000.0
    zone = gpd.GeoSeries([box(x0 - 100, y0 - 100, x0 + 1100, y0 + 1100)], crs=CRS).to_crs(source_crs)
    src = _layer(tmp_path, "s", list(zone), source_crs, population=[1000])
    tgt = _layer(tmp_path, "t", [box(x0, y0, x0 + 1000, y0 + 1e-8)], CRS)
    out = tmp_path / "o.parquet"
    vector.apportion_by_area(src, tgt, str(out), extensive=["population"])
    assert gpd.read_parquet(out)["population"].iloc[0] == pytest.approx(1000 * 1e-5 / 1_440_000, rel=0.2)



# --- the eighth review's cases: where the inner point is taken -----------------------------------


@pytest.mark.parametrize("neck", [1.0, 0.2])
def test_a_target_with_a_narrow_neck_is_not_read_as_torn(tmp_path, neck):
    """Two blocks joined by a neck under a metre wide: a point on the surface fell in the neck.

    The chord of a reprojected edge there passed half a metre from it, and the
    target was refused as crossing a seam it does not cross.
    """
    import shapely
    from pyproj import Transformer

    to_laea = Transformer.from_crs("EPSG:4326", "EPSG:3035", always_xy=True)
    cx, cy = to_laea.transform(25, 70)
    blocks = [box(cx - 10_000, cy - 30_000, cx + 10_000, cy - 10_000),
              box(cx + 20_000, cy + 10_000, cx + 40_000, cy + 30_000)]
    neck_line = shapely.LineString([(cx + 5_000, cy - 15_000), (cx + 25_000, cy + 15_000)])
    target = shapely.union_all([*blocks, neck_line.buffer(neck / 2, cap_style="flat")])
    far = box(*to_laea.transform(-20, 35), *to_laea.transform(-19, 36))
    tgt = _layer(tmp_path, "t", [target, far], "EPSG:3035")
    src = _layer(tmp_path, "s", [box(-25, 30, 45, 75)], "EPSG:4326", population=[1000])
    out = tmp_path / "o.parquet"
    vector.apportion_by_area(src, tgt, str(out), extensive=["population"])
    assert gpd.read_parquet(out)["population"].iloc[0] > 0


@pytest.mark.parametrize("source_crs", ["EPSG:3857", "EPSG:6933"])
def test_a_torn_strip_a_micrometre_wide_is_refused(tmp_path, source_crs):
    """Under the area floor, and with a tolerance read off the torn image's own extent, it passed."""
    import shapely
    from pyproj import Transformer

    to_pacific = Transformer.from_crs("EPSG:4326", "EPSG:3832", always_xy=True)
    strip = shapely.LineString([to_pacific.transform(179.0, -10.0),
                                to_pacific.transform(-179.0, -9.0)]).buffer(0.5e-6, cap_style="flat")
    tgt = _layer(tmp_path, "t", [strip], "EPSG:3832")
    zones = gpd.GeoSeries([box(178.5, -11, 180, -8.5), box(-180, -11, -178.5, -8.5), box(0, -11, 10, -8.5)],
                          crs="EPSG:4326").to_crs(source_crs)
    src = _layer(tmp_path, "s", list(zones), source_crs, population=[100, 100, 5000])
    with pytest.raises(ValueError, match="no longer hold a point of their own interior"):
        vector.apportion_by_area(src, tgt, str(tmp_path / "o.parquet"), extensive=["population"])
