"""Native Parquet CRS arbitration when the GeoParquet 1.x `geo.crs` is absent."""

from __future__ import annotations

import json

import pytest

from mapsmith import readers, verify

duckdb = pytest.importorskip("duckdb")
gpd = pytest.importorskip("geopandas")
pq = pytest.importorskip("pyarrow.parquet")


def _write_v2(path, sql):
    con = duckdb.connect()
    con.execute("LOAD spatial")
    con.execute(
        f"COPY ({sql}) TO '{str(path).replace(chr(92), '/')}' "
        "(FORMAT parquet, geoparquet_version 'V2')"
    )
    return path


def _geo_spec(path):
    metadata = pq.read_schema(path).metadata or {}
    geo = json.loads(metadata[b"geo"])
    spec = geo["columns"][geo.get("primary_column", "geometry")]
    assert "crs" not in spec, "fixture must exercise the missing geo.crs branch"
    return spec


def _valid_v2(tmp_path):
    target = _write_v2(
        tmp_path / "valid-v2.parquet",
        "SELECT ST_SetCRS(ST_Point(9, 45), 'srid:5070') AS geom",
    )
    raw = target.read_bytes()
    assert raw.count(b"srid:5070") == 1, "fixture must mutate only the native declaration"
    target.write_bytes(raw.replace(b"srid:5070", b"EPSG:4326"))
    _geo_spec(target)
    return target


def _unresolved_v2(tmp_path):
    target = _write_v2(
        tmp_path / "unresolved-v2.parquet",
        "SELECT ST_SetCRS(ST_Buffer(ST_Point(9, 45), 0.1), 'srid:5070') AS geom",
    )
    _geo_spec(target)
    return target


def test_a_valid_native_crs_is_used_when_geo_crs_is_absent(tmp_path):
    target = _valid_v2(tmp_path)
    frame = readers.read_vector(str(target))
    assert frame.crs is not None and frame.crs.to_epsg() == 4326
    assert verify.probe_crs(str(target)) == "EPSG:4326"


def test_an_unresolved_native_crs_does_not_become_crs84(tmp_path):
    target = _unresolved_v2(tmp_path)
    frame = readers.read_vector(str(target))
    assert frame.crs is None
    assert "srid:5070" in readers.crs_reason(frame)
    assert verify.probe_crs(str(target)) == verify.UNKNOWN_CRS


def test_a_consumer_refuses_an_unresolved_native_crs_with_its_reason(tmp_path):
    from mapsmith.engines import vector

    target = _unresolved_v2(tmp_path)
    output = tmp_path / "out.parquet"
    with pytest.raises(ValueError, match="srid:5070"):
        vector.buffer(str(target), 10.0, str(output))
    assert not output.exists()


def test_no_crs_declaration_anywhere_keeps_the_spec_default(tmp_path):
    target = _write_v2(tmp_path / "default-v2.parquet", "SELECT ST_Point(9, 45) AS geom")
    _geo_spec(target)
    frame = readers.read_vector(str(target))
    assert frame.crs is not None and frame.crs.equals(__import__("pyproj").CRS("OGC:CRS84"))
    assert verify.probe_crs(str(target)) == "OGC:CRS84"


def test_primary_geometry_column_controls_native_crs_arbitration(tmp_path):
    target = _write_v2(
        tmp_path / "multi-v2.parquet",
        "SELECT ST_Point(0, 0) AS first, ST_SetCRS(ST_Point(9, 45), 'srid:5070') AS other",
    )
    raw = target.read_bytes()
    assert raw.count(b'"primary_column":"first"') == 1, "fixture must change only the primary column"
    target.write_bytes(raw.replace(b'"primary_column":"first"', b'"primary_column":"other"'))
    _geo_spec(target)

    frame = readers.read_vector(str(target))
    assert frame.geometry.name == "other"
    assert frame.crs is None
    assert "srid:5070" in readers.crs_reason(frame)
    assert verify.probe_crs(str(target)) == verify.UNKNOWN_CRS
