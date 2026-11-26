"""The sample limit: counted from the lengths, refused before anything is allocated."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

rasterio = pytest.importorskip("rasterio")

import geopandas as gpd
from rasterio.transform import from_origin
from shapely.geometry import LineString

from mapsmith import limits
from mapsmith.engines import linework, sampling


@pytest.fixture
def line(tmp_path) -> str:
    path = tmp_path / "line.gpkg"
    gpd.GeoDataFrame(geometry=[LineString([(0, 50), (1000, 50)])], crs="EPSG:32632").to_file(path)
    return str(path)


@pytest.fixture
def dem(tmp_path) -> str:
    path = tmp_path / "dem.tif"
    with rasterio.open(
        path, "w", driver="GTiff", height=10, width=110, count=1, dtype="float32",
        crs="EPSG:32632", transform=from_origin(-50.0, 100.0, 10.0, 10.0),
    ) as dst:
        dst.write(np.ones((1, 10, 110), dtype="float32"))
    return str(path)


def test_a_profile_over_the_limit_is_refused_with_the_count(monkeypatch, dem, line, tmp_path):
    """1000 m at 10 m is 101 points plus one for the end: over a limit of 50."""
    monkeypatch.setenv(limits.MAX_SAMPLES_ENV, "50")
    out = tmp_path / "p.parquet"
    with pytest.raises(ValueError, match=r"about 102 points, over this server's limit of 50"):
        sampling.elevation_profile(dem, line, str(out), spacing=10.0)
    assert not out.exists()


def test_under_the_limit_nothing_changes(monkeypatch, dem, line, tmp_path):
    monkeypatch.setenv(limits.MAX_SAMPLES_ENV, "200")
    result = sampling.elevation_profile(dem, line, str(tmp_path / "p.parquet"), spacing=10.0)
    assert result["points"] == 101


def test_points_along_lines_is_limited_too(monkeypatch, line, tmp_path):
    monkeypatch.setenv(limits.MAX_SAMPLES_ENV, "50")
    with pytest.raises(ValueError, match="points_along_lines .* over this server's limit"):
        linework.points_along_lines(line, str(tmp_path / "pts.parquet"), spacing=10.0)


def test_the_default_admits_an_ordinary_profile_and_stops_a_micrometre_spacing(dem, line, tmp_path):
    with pytest.raises(ValueError, match="MAPSMITH_MAX_SAMPLES"):
        sampling.elevation_profile(dem, line, str(tmp_path / "p.parquet"), spacing=1e-6)


@pytest.mark.parametrize("raw", ["lots", "0", "-3"])
def test_a_limit_that_is_not_a_positive_whole_number_is_refused(monkeypatch, raw):
    monkeypatch.setenv(limits.MAX_SAMPLES_ENV, raw)
    with pytest.raises(ValueError, match=limits.MAX_SAMPLES_ENV):
        limits.max_samples()


def test_a_grid_is_counted_from_its_extent_and_refused_before_it_is_allocated(monkeypatch):
    """1000 x 500 at 10 is 101 x 51 by the limit's count: one more than strictly needed."""
    assert limits.refuse_too_many_cells(1000.0, 500.0, 10.0, "op") == 101 * 51
    monkeypatch.setenv(limits.MAX_GRID_CELLS_ENV, "5000")
    with pytest.raises(
        ValueError, match=r"about 5,151 cells \(101 x 51\), over this server's limit of 5,000"
    ):
        limits.refuse_too_many_cells(1000.0, 500.0, 10.0, "op")


def test_the_default_grid_limit_admits_a_city_and_stops_a_millimetre_cell():
    # 15 km x 15 km at 5 m: 3001 x 3001, a little over nine million cells.
    assert limits.refuse_too_many_cells(15_000.0, 15_000.0, 5.0, "op") == 3001 * 3001
    with pytest.raises(ValueError, match="MAPSMITH_MAX_GRID_CELLS"):
        limits.refuse_too_many_cells(20_000.0, 20_000.0, 0.001, "op")


def test_a_raster_may_be_regridded_to_a_multiple_of_itself_whatever_the_limit(monkeypatch):
    """The limit is for an invented grid, not for an input that is already large."""
    monkeypatch.setenv(limits.MAX_GRID_CELLS_ENV, "100")
    with pytest.raises(ValueError, match="MAPSMITH_MAX_GRID_CELLS"):
        limits.refuse_too_many_cells(1000.0, 1000.0, 10.0, "op")
    assert limits.refuse_too_many_cells(1000.0, 1000.0, 10.0, "op", entitled=20_000) == 101 * 101


def test_a_triangulation_over_the_operators_limit_is_refused(monkeypatch):
    limits.refuse_too_many_points(limits.DEFAULT_MAX_TRIANGULATION_POINTS, "op")
    monkeypatch.setenv(limits.MAX_TRIANGULATION_POINTS_ENV, "1000")
    with pytest.raises(ValueError, match="MAPSMITH_MAX_TRIANGULATION_POINTS"):
        limits.refuse_too_many_points(1001, "op")


def test_a_search_is_counted_as_the_cells_each_sample_reaches():
    """The measured cases: 250 000 cells, 1 000 samples, cells of 2 on an extent of 1000.

    As they are, nothing to count. A radius of 50 reaches pi * 25^2 = 1 963 cells
    per sample, two million pairs: allowed. A radius wider than the layer reaches
    every cell, 250 million pairs: refused, and so is asking every cell for all
    thousand samples.
    """
    limits.refuse_too_much_search(250_000, 1000, 2.0, 0.0, 0, "op")
    limits.refuse_too_much_search(250_000, 1000, 2.0, 50.0, 0, "op")
    for radius, min_points in ((1e12, 0), (0.0, 1000), (float("inf"), 0)):
        with pytest.raises(ValueError, match="cell-sample pairs"):
            limits.refuse_too_much_search(250_000, 1000, 2.0, radius, min_points, "op")


@pytest.mark.parametrize("cell_size", [1e-300, 1e-320])
def test_a_cell_size_too_small_to_count_is_refused_as_a_size_not_as_an_overflow(cell_size):
    """`math.floor(inf)` raises OverflowError; 1e-300 would print a hundred digits."""
    with pytest.raises(ValueError, match="million billion cells") as refused:
        limits.refuse_too_many_cells(100.0, 100.0, cell_size, "op")
    assert len(str(refused.value)) < 300


@pytest.mark.parametrize("raw", ["lots", "0", "-3"])
def test_a_grid_limit_that_is_not_a_positive_whole_number_is_refused(monkeypatch, raw):
    monkeypatch.setenv(limits.MAX_GRID_CELLS_ENV, raw)
    with pytest.raises(ValueError, match=limits.MAX_GRID_CELLS_ENV):
        limits.max_grid_cells()


@pytest.mark.parametrize("spacing", [float("nan"), float("inf")])
def test_points_along_lines_refuses_a_spacing_that_is_not_finite(line, tmp_path, spacing):
    with pytest.raises(ValueError, match="positive finite"):
        linework.points_along_lines(line, str(Path(tmp_path) / "pts.parquet"), spacing=spacing)
