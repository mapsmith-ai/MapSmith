"""Zonal statistics: exact deterministic values, CRS discipline, provenance."""

import json

import geopandas as gpd
import pytest
from shapely.geometry import box

exactextract = pytest.importorskip("exactextract")
rasterio = pytest.importorskip("rasterio")

import numpy as np  # noqa: E402
from rasterio.transform import from_origin  # noqa: E402

from mapsmith.engines import raster  # noqa: E402


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
    assert "reprojected" in manifest["crs_decisions"]["reason"]
    gdf = gpd.read_parquet(out)
    assert gdf.iloc[0]["mean"] == pytest.approx(22.0, abs=0.5)  # round-trip tolerance
    assert result["feature_count"] == 1


def test_zonal_rejects_unknown_stat(dem, zone, tmp_path):
    with pytest.raises(ValueError, match="stdev"):
        raster.zonal_statistics(dem, zone, str(tmp_path / "x.parquet"), ["std"])


def test_zonal_rejects_zones_without_crs(dem, tmp_path):
    gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[box(0, 0, 1, 1)], crs=None)
    z = tmp_path / "nocrs.gpkg"
    gdf.to_file(z)
    with pytest.raises(ValueError, match="no CRS"):
        raster.zonal_statistics(dem, str(z), str(tmp_path / "y.parquet"))
