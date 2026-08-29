"""Reading a surface at points, along a line, and between two positions.

Every expected value here is arithmetic done on paper first. The DEM is
`value = row * 10 + column` on 10 m cells, so the value at any position follows
from the position; the profile DEM is a linear ramp, so an elevation at distance
d is a straight-line function of d. Either the code returns those numbers or it
is wrong — there is no "close enough" to hide in.
"""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import LineString, Point

from mapsmith.engines import sampling


@pytest.fixture
def grid(tmp_path: Path) -> Path:
    """10x10 cells of 10 m, origin at (0, 100), value = row * 10 + column.

    Cell (row, col) covers x in [col*10, col*10+10) and y in
    (100 - row*10 - 10, 100 - row*10]. Its centre is at
    (col*10 + 5, 95 - row*10).
    """
    path = tmp_path / "grid.tif"
    values = np.arange(100, dtype="float32").reshape(10, 10)
    with rasterio.open(
        path, "w", driver="GTiff", height=10, width=10, count=1, dtype="float32",
        crs="EPSG:32632", transform=from_origin(0, 100, 10, 10), nodata=-9999.0,
    ) as dst:
        dst.write(values, 1)
    return path


@pytest.fixture
def ramp(tmp_path: Path) -> Path:
    """Elevation rising 1 m per metre eastward: value = x at every cell centre.

    Cells are 1 m, origin (0, 20), so the centre of column c is at x = c + 0.5
    and carries the value c + 0.5. A bilinear read at any x inside the grid
    therefore returns x exactly, which makes a profile along a horizontal line a
    closed-form check of the sampling AND of the distance stepping at once.
    """
    path = tmp_path / "ramp.tif"
    row = np.arange(20, dtype="float32") + 0.5
    values = np.tile(row, (20, 1))
    with rasterio.open(
        path, "w", driver="GTiff", height=20, width=20, count=1, dtype="float32",
        crs="EPSG:32632", transform=from_origin(0, 20, 1, 1),
    ) as dst:
        dst.write(values, 1)
    return path


def _manifest(output: Path) -> dict:
    return json.loads(Path(f"{output}.provenance.json").read_text(encoding="utf-8"))


# --------------------------------------------------------- sample at points

def test_nearest_reads_the_cell_the_point_falls_in(grid, tmp_path):
    """Three points whose cells can be worked out from the transform."""
    points = tmp_path / "points.gpkg"
    gpd.GeoDataFrame(
        {"name": ["top-left", "row2-col3", "bottom-right"]},
        geometry=[Point(5, 95), Point(35, 75), Point(95, 5)],
        crs="EPSG:32632",
    ).to_file(points, layer="p", driver="GPKG")

    out = tmp_path / "sampled.parquet"
    result = sampling.sample_raster_at_points(str(grid), str(points), str(out), "nearest")

    got = gpd.read_parquet(out)["value"].tolist()
    # row*10 + col: (0,0) -> 0, (2,3) -> 23, (9,9) -> 99
    assert got == [0.0, 23.0, 99.0]
    assert result["sampled"] == 3 and result["unreadable"] == 0


def test_bilinear_interpolates_between_cell_centres_exactly(grid, tmp_path):
    """Halfway between two horizontally adjacent centres is their mean.

    Centres of (0,0) and (0,1) are at x=5 and x=15, carrying 0 and 1. At x=10,
    y=95 the answer is exactly 0.5 — and if the half-cell offset in the
    interpolation is dropped, it comes back 1.0 instead, which is the kind of
    error that looks like data.
    """
    points = tmp_path / "mid.gpkg"
    gpd.GeoDataFrame(
        {"n": [1, 2]}, geometry=[Point(10, 95), Point(5, 90)], crs="EPSG:32632"
    ).to_file(points, layer="p", driver="GPKG")

    out = tmp_path / "bilinear.parquet"
    sampling.sample_raster_at_points(str(grid), str(points), str(out), "bilinear")

    got = gpd.read_parquet(out)["value"].tolist()
    # between (0,0)=0 and (0,1)=1 -> 0.5 ; between (0,0)=0 and (1,0)=10 -> 5.0
    assert got == pytest.approx([0.5, 5.0])


def test_a_point_outside_the_raster_is_null_and_counted_not_silent(grid, tmp_path):
    """The failure this module exists to prevent: a null that reads as a value.

    `rasterio.sample` would return the nodata value, -9999, which averages into
    a profile and produces a number nobody can defend.
    """
    points = tmp_path / "outside.gpkg"
    gpd.GeoDataFrame(
        {"n": [1, 2]}, geometry=[Point(5, 95), Point(500, 500)], crs="EPSG:32632"
    ).to_file(points, layer="p", driver="GPKG")

    out = tmp_path / "partial.parquet"
    result = sampling.sample_raster_at_points(str(grid), str(points), str(out), "nearest")

    got = gpd.read_parquet(out)["value"].tolist()
    assert got[0] == 0.0
    assert got[1] is None or (isinstance(got[1], float) and np.isnan(got[1]))
    assert result["unreadable"] == 1

    named = {c["name"]: c for c in _manifest(out)["verification"]}
    check = named["x-mapsmith:every_position_had_a_value"]
    assert check["passed"] is False, "the null was not reported"
    assert check.get("critical") is False, (
        "sampling outside the raster is legitimate on purpose — making this "
        "critical would break the survey-comparison case it is best at"
    )
    assert "1 of 2" in check["detail"]


def test_a_nodata_cell_reads_as_null_rather_than_as_minus_9999(tmp_path):
    holed = tmp_path / "holed.tif"
    values = np.full((3, 3), 5.0, dtype="float32")
    values[1, 1] = -9999.0
    with rasterio.open(
        holed, "w", driver="GTiff", height=3, width=3, count=1, dtype="float32",
        crs="EPSG:32632", transform=from_origin(0, 30, 10, 10), nodata=-9999.0,
    ) as dst:
        dst.write(values, 1)

    points = tmp_path / "hole.gpkg"
    gpd.GeoDataFrame(
        {"n": [1]}, geometry=[Point(15, 15)], crs="EPSG:32632"
    ).to_file(points, layer="p", driver="GPKG")

    out = tmp_path / "hole.parquet"
    result = sampling.sample_raster_at_points(str(holed), str(points), str(out), "nearest")
    assert result["unreadable"] == 1, "the nodata cell came back as a number"


def test_the_points_are_reprojected_and_the_decision_is_recorded(grid, tmp_path):
    """A point layer in degrees still lands on the right cell, and says so."""
    points = tmp_path / "wgs84.gpkg"
    in_utm = gpd.GeoDataFrame(
        {"n": [1]}, geometry=[Point(35, 75)], crs="EPSG:32632"
    )
    in_utm.to_crs("EPSG:4326").to_file(points, layer="p", driver="GPKG")

    out = tmp_path / "reprojected.parquet"
    sampling.sample_raster_at_points(str(grid), str(points), str(out), "nearest")

    assert gpd.read_parquet(out)["value"].tolist() == [23.0]
    decisions = _manifest(out)["crs_decisions"]
    assert "32632" in decisions["analysis_crs"]
    assert "reprojected" in decisions["reason"]


def test_the_method_has_to_be_stated(grid, tmp_path):
    points = tmp_path / "p.gpkg"
    gpd.GeoDataFrame(
        {"n": [1]}, geometry=[Point(5, 95)], crs="EPSG:32632"
    ).to_file(points, layer="p", driver="GPKG")
    with pytest.raises(ValueError, match="nearest"):
        sampling.sample_raster_at_points(
            str(grid), str(points), str(tmp_path / "x.parquet"), "cubic"
        )


def test_an_existing_column_is_not_overwritten_silently(grid, tmp_path):
    points = tmp_path / "clash.gpkg"
    gpd.GeoDataFrame(
        {"value": [42]}, geometry=[Point(5, 95)], crs="EPSG:32632"
    ).to_file(points, layer="p", driver="GPKG")
    with pytest.raises(ValueError, match="already has a column"):
        sampling.sample_raster_at_points(
            str(grid), str(points), str(tmp_path / "x.parquet"), "nearest"
        )


def test_polygons_are_refused_and_pointed_at_the_right_operation(grid, tmp_path):
    from shapely.geometry import box

    polys = tmp_path / "polys.gpkg"
    gpd.GeoDataFrame(
        {"n": [1]}, geometry=[box(0, 0, 10, 10)], crs="EPSG:32632"
    ).to_file(polys, layer="p", driver="GPKG")
    with pytest.raises(ValueError, match="zonal_statistics"):
        sampling.sample_raster_at_points(
            str(grid), str(polys), str(tmp_path / "x.parquet"), "nearest"
        )


# ------------------------------------------------------------------ profile

def test_a_profile_along_a_ramp_is_the_ramp(ramp, tmp_path):
    """Closed form twice over: the count and every value.

    The line runs from x=2 to x=18 at y=10, so its length is 16 m; sampled every
    4 m that is floor(16/4) + 1 = 5 points, at x = 2, 6, 10, 14, 18. The ramp
    reads value = x, so the elevations are those same numbers.
    """
    line = tmp_path / "line.gpkg"
    gpd.GeoDataFrame(
        {"n": [1]}, geometry=[LineString([(2, 10), (18, 10)])], crs="EPSG:32632"
    ).to_file(line, layer="l", driver="GPKG")

    out = tmp_path / "profile.parquet"
    result = sampling.elevation_profile(str(ramp), str(line), str(out), spacing=4.0)

    got = gpd.read_parquet(out)
    assert result["points"] == 5
    assert got["distance"].tolist() == [0.0, 4.0, 8.0, 12.0, 16.0]
    assert got["value"].tolist() == pytest.approx([2.0, 6.0, 10.0, 14.0, 18.0])
    assert result["total_length"] == pytest.approx(16.0)

    named = {c["name"]: c["passed"] for c in _manifest(out)["verification"]}
    assert named["x-mapsmith:point_count_follows_length_and_spacing"] is True


def test_a_profile_includes_both_ends_even_when_the_step_does_not_divide(ramp, tmp_path):
    """15 m at 4 m is three whole steps and a remainder. The far end still
    appears, clamped to the line's length, because a profile that silently stops
    short of the summit is the worst kind of nearly-right."""
    line = tmp_path / "odd.gpkg"
    gpd.GeoDataFrame(
        {"n": [1]}, geometry=[LineString([(2, 10), (17, 10)])], crs="EPSG:32632"
    ).to_file(line, layer="l", driver="GPKG")

    out = tmp_path / "odd.parquet"
    sampling.elevation_profile(str(ramp), str(line), str(out), spacing=4.0)
    got = gpd.read_parquet(out)
    assert got["distance"].tolist() == [0.0, 4.0, 8.0, 12.0]
    assert got["value"].tolist() == pytest.approx([2.0, 6.0, 10.0, 14.0])


def test_two_lines_profile_separately_and_say_which_is_which(ramp, tmp_path):
    line = tmp_path / "two.gpkg"
    gpd.GeoDataFrame(
        {"n": [1, 2]},
        geometry=[
            LineString([(2, 10), (10, 10)]),
            LineString([(2, 15), (6, 15)]),
        ],
        crs="EPSG:32632",
    ).to_file(line, layer="l", driver="GPKG")

    out = tmp_path / "two.parquet"
    result = sampling.elevation_profile(str(ramp), str(line), str(out), spacing=4.0)
    got = gpd.read_parquet(out)
    assert result["points"] == 5  # 3 on the first line, 2 on the second
    assert got["line_index"].tolist() == [0, 0, 0, 1, 1]
    assert got.groupby("line_index")["distance"].max().tolist() == [8.0, 4.0]


def test_a_geographic_line_is_refused_because_the_spacing_would_be_degrees(tmp_path):
    dem = tmp_path / "geo.tif"
    with rasterio.open(
        dem, "w", driver="GTiff", height=4, width=4, count=1, dtype="float32",
        crs="EPSG:4326", transform=from_origin(11.0, 45.0, 0.01, 0.01),
    ) as dst:
        dst.write(np.ones((4, 4), dtype="float32"), 1)

    line = tmp_path / "geo.gpkg"
    gpd.GeoDataFrame(
        {"n": [1]}, geometry=[LineString([(11.001, 44.99), (11.02, 44.99)])],
        crs="EPSG:4326",
    ).to_file(line, layer="l", driver="GPKG")

    with pytest.raises(ValueError, match="degrees"):
        sampling.elevation_profile(
            str(dem), str(line), str(tmp_path / "x.parquet"), spacing=20.0
        )


# ------------------------------------------------------------- line of sight

@pytest.fixture
def ridge(tmp_path: Path) -> Path:
    """Flat ground at 0 with a single 100 m ridge in the middle column.

    100 cells of 10 m from x=0 to x=1000; the ridge is the column whose centre
    is at x=505. An observer at x=5 and a target at x=995 are 990 m apart with
    the ridge exactly halfway, so a sight line between two points at ground
    level is blocked and one from 200 m up at both ends is not.
    """
    path = tmp_path / "ridge.tif"
    values = np.zeros((3, 100), dtype="float32")
    values[:, 50] = 100.0
    with rasterio.open(
        path, "w", driver="GTiff", height=3, width=100, count=1, dtype="float32",
        crs="EPSG:32632", transform=from_origin(0, 30, 10, 10),
    ) as dst:
        dst.write(values, 1)
    return path


def test_a_ridge_blocks_the_view_and_the_answer_says_where(ridge):
    result = sampling.line_of_sight(
        str(ridge), 5, 15, 995, 15, earth_curvature=False
    )
    assert result["visible"] is False
    assert result["first_obstruction_at"] == pytest.approx(490.0, abs=15.0), (
        "the ridge centre is 500 m from the observer; the obstruction should "
        "start where the ground first rises above the line"
    )
    assert result["minimum_clearance"] < 0


def test_enough_height_clears_the_same_ridge(ridge):
    result = sampling.line_of_sight(
        str(ridge), 5, 15, 995, 15, earth_curvature=False,
        observer_height=200.0, target_height=200.0,
    )
    assert result["visible"] is True
    assert result["minimum_clearance"] == pytest.approx(100.0, abs=1.0)


def test_curvature_lowers_the_sight_line_by_the_sagitta(tmp_path):
    """Closed form: over a 20 km chord the mid-line drop is
    (1 - 0.13) * d * (L - d) / 2R = 0.87 * 10000 * 10000 / (2 * 6371008.8) ≈ 6.83 m.

    Flat ground at 6 m is under a level sight line without curvature and above
    it with curvature, which is the whole point of making the caller state it.
    """
    flat = tmp_path / "flat.tif"
    values = np.zeros((3, 200), dtype="float32")
    values[:, 100] = 6.0
    with rasterio.open(
        flat, "w", driver="GTiff", height=3, width=200, count=1, dtype="float32",
        crs="EPSG:32632", transform=from_origin(0, 300, 100, 100),
    ) as dst:
        dst.write(values, 1)

    common = {"observer_height": 10.0, "target_height": 10.0}
    without = sampling.line_of_sight(
        str(flat), 50, 150, 19950, 150, earth_curvature=False, **common
    )
    with_curve = sampling.line_of_sight(
        str(flat), 50, 150, 19950, 150, earth_curvature=True, **common
    )
    assert without["visible"] is True
    assert with_curve["visible"] is False, (
        "a 6.8 m drop over 20 km did not move a 6 m obstacle above a 10 m sight "
        "line, so the curvature correction is not being applied"
    )
    assert with_curve["refraction_coefficient"] == 0.13
    drop = without["minimum_clearance"] - with_curve["minimum_clearance"]
    assert drop == pytest.approx(6.83, abs=0.15)


def test_the_curvature_question_has_to_be_answered(ridge):
    with pytest.raises(TypeError):
        sampling.line_of_sight(str(ridge), 5, 15, 995, 15)  # type: ignore[call-arg]


def test_an_observer_off_the_raster_is_refused_rather_than_assumed_to_be_at_zero(ridge):
    with pytest.raises(ValueError, match="outside the raster"):
        sampling.line_of_sight(
            str(ridge), -500, 15, 995, 15, earth_curvature=False
        )


def test_a_geographic_raster_is_refused_for_a_sight_line(tmp_path):
    dem = tmp_path / "geo.tif"
    with rasterio.open(
        dem, "w", driver="GTiff", height=4, width=4, count=1, dtype="float32",
        crs="EPSG:4326", transform=from_origin(11.0, 45.0, 0.01, 0.01),
    ) as dst:
        dst.write(np.zeros((4, 4), dtype="float32"), 1)
    with pytest.raises(ValueError, match="geographic"):
        sampling.line_of_sight(
            str(dem), 11.005, 44.995, 11.025, 44.995, earth_curvature=False
        )


def test_the_outer_half_cell_is_readable_and_outside_the_extent_is_not(grid, tmp_path):
    """Two different "outside", and conflating them made the boundary unreadable.

    A position inside the raster's extent but within the outer half-cell has a
    value; its bilinear stencil simply has no outer neighbour, so the stencil is
    clamped to the edge cell — what GDAL does. A position beyond the extent has
    no value at all. The first version refused both, and a sight line to a
    target 5 m from the edge came back "outside the raster".
    """
    points = tmp_path / "edges.gpkg"
    gpd.GeoDataFrame(
        {"where": ["inside the outer half-cell", "beyond the extent"]},
        geometry=[Point(1, 99), Point(-1, 99)],
        crs="EPSG:32632",
    ).to_file(points, layer="p", driver="GPKG")

    out = tmp_path / "edges.parquet"
    result = sampling.sample_raster_at_points(
        str(grid), str(points), str(out), "bilinear"
    )
    got = gpd.read_parquet(out)["value"].tolist()
    assert got[0] == pytest.approx(0.0), (
        "a point inside the extent came back null because its interpolation "
        "window poked over the edge"
    )
    assert got[1] is None or np.isnan(got[1])
    assert result["unreadable"] == 1
