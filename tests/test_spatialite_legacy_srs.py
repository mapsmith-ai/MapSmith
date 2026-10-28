"""Legacy SpatiaLite catalogues may keep the operative CRS only in `srs_wkt`."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from mapsmith import crs_policy, workspace

GRID = "C:/evil/legacy.gsb"
WKT_HOSTILE = (
    'GEOGCS["legacy",DATUM["x",SPHEROID["clrk66",6378206.4,294.9786982]],PRIMEM["Greenwich",0],'
    f'UNIT["degree",0.0174532925199433],EXTENSION["PROJ4_GRIDS","{GRID}"]]'
)


def _legacy_spatialite(path):
    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import Point

    gpd.GeoDataFrame({"id": [1]}, geometry=[Point(12.0, 45.0)], crs="EPSG:4326").to_file(
        path, driver="SQLite"
    )
    with closing(sqlite3.connect(path)) as db:
        columns = db.execute("PRAGMA table_info(spatial_ref_sys)").fetchall()
        names = {row[1] for row in columns}
        required = {"srid", "auth_name", "auth_srid", "srtext"}
        assert required <= names, f"unexpected SQLite SRS catalogue: {sorted(names)}"
        db.execute("ALTER TABLE spatial_ref_sys RENAME TO modern_spatial_ref_sys")
        db.execute(
            "CREATE TABLE spatial_ref_sys ("
            "srid INTEGER PRIMARY KEY, auth_name TEXT, auth_srid INTEGER, proj4text TEXT, srs_wkt TEXT)"
        )
        db.execute(
            "INSERT INTO spatial_ref_sys "
            "SELECT srid, 'NONE', 0, '+proj=longlat +datum=WGS84 +no_defs', NULL "
            "FROM modern_spatial_ref_sys"
        )
        db.execute("DROP TABLE modern_spatial_ref_sys")
        geometry_columns = {row[1] for row in db.execute("PRAGMA table_info(geometry_columns)")}
        if "spatial_index_enabled" in geometry_columns:
            db.execute("ALTER TABLE geometry_columns DROP COLUMN spatial_index_enabled")
        db.execute("UPDATE spatial_ref_sys SET srs_wkt = ? WHERE srid = 4326", (WKT_HOSTILE,))
        db.commit()
    return path


def test_gdal_really_uses_the_legacy_srs_wkt_column(tmp_path):
    pyogrio = pytest.importorskip("pyogrio")
    from pyproj import CRS

    path = _legacy_spatialite(tmp_path / "legacy.sqlite")
    try:
        reported = pyogrio.read_info(str(path)).get("crs") or ""
    except pyogrio.errors.CRSError as exc:
        if "no such column: srtext" in str(exc):
            pytest.skip("the installed GDAL does not read this legacy SpatiaLite layout")
        raise
    assert GRID in CRS(reported).to_wkt() or "legacy.gsb" in CRS(reported).to_wkt()


def test_the_workspace_gate_reads_and_refuses_legacy_srs_wkt(tmp_path, monkeypatch):
    path = _legacy_spatialite(tmp_path / "legacy.sqlite")
    monkeypatch.setenv("MAPSMITH_WORKSPACE", str(tmp_path))
    assert any(GRID in text for text in crs_policy.declared_srs_texts(str(path)))
    with pytest.raises(ValueError, match="refused"):
        workspace.guard(str(path), "input_path")
