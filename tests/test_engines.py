"""Engine dispatcher and DuckDB engine tests. SedonaDB is optional and skipped if absent."""

import geopandas as gpd
import pytest
from shapely.geometry import Point, box

from mapsmith.engines import dispatch


def test_dispatch_geopandas_always_available():
    engines = dispatch.available_engines()
    assert engines["geopandas"] is True


def test_dispatch_forced_unavailable_engine_raises():
    with pytest.raises(RuntimeError, match="not available"):
        dispatch.pick(dispatch.Workload.SQL, "nonexistent-engine")


def test_dispatch_auto_returns_available():
    name = dispatch.pick(dispatch.Workload.HEAVY_JOIN, "auto")
    assert dispatch.available_engines()[name] is True


@pytest.fixture()
def duck():
    duckdb_engine = pytest.importorskip("mapsmith.engines.duckdb_engine")
    try:
        duckdb_engine._connect()
    except Exception as exc:  # noqa: BLE001 — extension download can fail offline
        pytest.skip(f"duckdb spatial extension unavailable: {exc}")
    return duckdb_engine


def test_duckdb_run_sql_preview(duck):
    result = duck.run_sql("SELECT 41 + 1 AS answer")
    assert result["columns"] == ["answer"]
    assert result["rows"][0][0] == 42


@pytest.fixture()
def parquet_layers(tmp_path):
    points = gpd.GeoDataFrame(
        {"name": ["a", "b"]},
        geometry=[Point(9.19, 45.46), Point(12.49, 41.89)],  # Milan, Rome
        crs="EPSG:4326",
    )
    zones = gpd.GeoDataFrame(
        {"zone": ["north"]},
        geometry=[box(8.0, 44.0, 11.0, 47.0)],  # covers Milan only
        crs="EPSG:4326",
    )
    p1 = tmp_path / "points.parquet"
    p2 = tmp_path / "zones.parquet"
    points.to_parquet(p1)
    zones.to_parquet(p2)
    return str(p1), str(p2)


def test_duckdb_spatial_join(duck, parquet_layers, tmp_path):
    left, right = parquet_layers
    out = tmp_path / "joined.parquet"
    result = duck.spatial_join(left, right, str(out), "intersects")
    assert result["feature_count"] == 1  # only Milan falls in the zone
    assert out.exists()
    assert (tmp_path / "joined.parquet.provenance.json").exists()


def test_duckdb_supports_inputs(duck):
    assert duck.supports_inputs("a.parquet", "b.PARQUET")
    assert not duck.supports_inputs("a.parquet", "b.gpkg")
