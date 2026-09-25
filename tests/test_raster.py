"""Zonal statistics: exact deterministic values, CRS discipline, provenance."""

import json
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import box

exactextract = pytest.importorskip("exactextract")
rasterio = pytest.importorskip("rasterio")

import numpy as np
from rasterio.transform import from_origin

from mapsmith.engines import raster


@pytest.fixture()
def dem(tmp_path):
    """10x10 raster, 1-unit pixels, UL corner (0,10), value = row*10+col."""
    data = np.arange(100, dtype=np.float32).reshape(10, 10)
    path = tmp_path / "dem.tif"
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=10,
        width=10,
        count=1,
        dtype="float32",
        crs="EPSG:32631",
        transform=from_origin(0.0, 10.0, 1.0, 1.0),
    ) as dst:
        dst.write(data, 1)
    return str(path)


@pytest.fixture()
def zone(tmp_path):
    """One zone covering exactly the top-left 5x5 pixel block (values 0..44)."""
    gdf = gpd.GeoDataFrame(
        {"zone_id": ["A"]}, geometry=[box(0.0, 5.0, 5.0, 10.0)], crs="EPSG:32631"
    )
    path = tmp_path / "zones.gpkg"
    gdf.to_file(path)
    return str(path)


def test_zonal_exact_values(dem, zone, tmp_path):
    out = tmp_path / "stats.parquet"
    result = raster.zonal_statistics(dem, zone, str(out), ["count", "mean", "min", "max", "sum"])
    assert result["verified"] is True
    gdf = gpd.read_parquet(out)
    row = gdf.iloc[0]
    # top-left 5x5 block: values r*10+c for r,c in 0..4 → known closed-form results
    assert row["count"] == pytest.approx(25.0)
    assert row["mean"] == pytest.approx(22.0)
    assert row["min"] == pytest.approx(0.0)
    assert row["max"] == pytest.approx(44.0)
    assert row["sum"] == pytest.approx(550.0)
    assert row["zone_id"] == "A"  # original attributes carried through


def test_zonal_crs_realignment_recorded(dem, zone, tmp_path):
    # declare zones in EPSG:4326: the engine must reproject and record the decision
    zones_4326 = gpd.read_file(zone).to_crs("EPSG:4326")
    z_path = tmp_path / "zones4326.gpkg"
    zones_4326.to_file(z_path)
    out = tmp_path / "stats2.parquet"
    result = raster.zonal_statistics(dem, str(z_path), str(out), ["count", "mean"])
    manifest = json.loads((tmp_path / "stats2.parquet.provenance.json").read_text())
    # The zones carry the pixel footprints, so a ballpark here weights the
    # wrong cells — named, with its transformation, rather than described.
    decisions = manifest["crs_decisions"]
    assert decisions["x-mapsmith:inputs_reprojected"] == [
        {"argument": "zones_path", "from": "EPSG:4326"}
    ]
    assert decisions["transformation"]["is_ballpark"] is False
    gdf = gpd.read_parquet(out)
    assert gdf.iloc[0]["mean"] == pytest.approx(22.0, abs=0.5)  # round-trip tolerance
    assert result["feature_count"] == 1


def test_zonal_rejects_unknown_stat(dem, zone, tmp_path):
    with pytest.raises(ValueError, match="stdev"):
        raster.zonal_statistics(dem, zone, str(tmp_path / "x.parquet"), ["std"])


def _weights(tmp_path, data, *, name="w.tif", crs="EPSG:32631", transform=None, nodata=-1.0):
    data = np.asarray(data, dtype=np.float32)
    bands = data if data.ndim == 3 else data[None]
    path = tmp_path / name
    with rasterio.open(
        path, "w", driver="GTiff", height=bands.shape[1], width=bands.shape[2],
        count=bands.shape[0], dtype="float32", crs=crs,
        transform=transform or from_origin(0.0, 10.0, 1.0, 1.0), nodata=nodata,
    ) as dst:
        dst.write(bands)
    return str(path)


def _column_weights():
    """weight = column + 1, so the right of the zone counts more than the left."""
    return np.tile(np.arange(1, 11, dtype=np.float32), (10, 1))


def test_weighted_mean_and_sum_in_closed_form(dem, zone, tmp_path):
    """Values r*10+c, weights c+1, the 5x5 block: sum(w) = 5*15 = 75 and
    sum(x*w) = sum_r(10r*15) + 5*sum_c(c(c+1)) = 1500 + 200 = 1700."""
    weights = _weights(tmp_path, _column_weights())
    out = tmp_path / "w.parquet"
    result = raster.zonal_statistics(
        dem, zone, str(out), ["mean", "weighted_mean", "weighted_sum"], weights_path=weights
    )
    row = gpd.read_parquet(out).iloc[0]
    assert row["mean"] == pytest.approx(22.0)
    assert row["weighted_mean"] == pytest.approx(1700 / 75)
    assert row["weighted_sum"] == pytest.approx(1700.0)
    manifest = json.loads(Path(result["provenance"]).read_text(encoding="utf-8"))
    assert [i["path"].rsplit("/", 1)[-1] for i in manifest["inputs"]] == [
        "dem.tif", "zones.gpkg", "w.tif"
    ]
    assert manifest["parameters"]["weight_of_a_cell_with_no_weight"] == 0.0
    check = next(
        c for c in manifest["verification"]
        if c["name"] == "x-mapsmith:every_valued_cell_has_a_weight"
    )
    assert check["passed"] is True


def test_a_cell_with_no_weight_counts_zero_and_the_zone_is_named(dem, zone, tmp_path):
    """exactextract's own default turns the whole zone's weighted statistics
    into NaN for ONE nodata weight (measured). The cell (0,0) has value 0 and
    weight 1: without it sum(w) = 74 and sum(x*w) is still 1700."""
    data = _column_weights()
    data[0, 0] = -1.0
    weights = _weights(tmp_path, data)
    out = tmp_path / "hole.parquet"
    result = raster.zonal_statistics(
        dem, zone, str(out), ["weighted_mean"], weights_path=weights
    )
    assert gpd.read_parquet(out).iloc[0]["weighted_mean"] == pytest.approx(1700 / 74)
    manifest = json.loads(Path(result["provenance"]).read_text(encoding="utf-8"))
    check = next(
        c for c in manifest["verification"]
        if c["name"] == "x-mapsmith:every_valued_cell_has_a_weight"
    )
    assert check["passed"] is False and check["critical"] is False
    assert "zones [0]" in check["detail"]


@pytest.mark.parametrize(
    "case, match",
    [
        ("other_grid", "its grid is"),
        ("other_crs", "is in EPSG:32632"),
        ("two_bands", "2 bands"),
        ("negative", "negative weight"),
    ],
)
def test_weights_off_the_value_grid_are_refused(dem, zone, tmp_path, case, match):
    """exactextract accepts a coarser weights grid and repeats each coarse cell
    in every fine one -- a weighted_sum inflated k*k times for a count -- and
    never compares the two CRSs. Refused, with how to align them."""
    if case == "other_grid":
        weights = _weights(
            tmp_path, np.ones((5, 5)), transform=from_origin(0.0, 10.0, 2.0, 2.0)
        )
    elif case == "other_crs":
        weights = _weights(tmp_path, _column_weights(), crs="EPSG:32632")
    elif case == "two_bands":
        weights = _weights(tmp_path, np.stack([_column_weights(), _column_weights()]))
    else:
        data = _column_weights()
        data[3, 3] = -5.0
        weights = _weights(tmp_path, data, nodata=-9999.0)
    with pytest.raises(ValueError, match=match):
        raster.zonal_statistics(
            dem, zone, str(tmp_path / "x.parquet"), ["weighted_mean"], weights_path=weights
        )


def test_a_weighted_statistic_needs_weights_and_weights_need_one(dem, zone, tmp_path):
    with pytest.raises(ValueError, match="need weights_path"):
        raster.zonal_statistics(dem, zone, str(tmp_path / "a.parquet"), ["weighted_mean"])
    weights = _weights(tmp_path, _column_weights())
    with pytest.raises(ValueError, match="would change nothing"):
        raster.zonal_statistics(
            dem, zone, str(tmp_path / "b.parquet"), ["mean"], weights_path=weights
        )


def test_zonal_rejects_zones_without_crs(dem, tmp_path):
    gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[box(0, 0, 1, 1)], crs=None)
    z = tmp_path / "nocrs.gpkg"
    gdf.to_file(z)
    with pytest.raises(ValueError, match="no CRS"):
        raster.zonal_statistics(dem, str(z), str(tmp_path / "y.parquet"))
