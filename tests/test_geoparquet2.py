"""GeoParquet 2.0 storage: geometry in Parquet's own logical types (issue #23).

2.0 (`v2.0.0-rc.1`, 2026-07-19) drops the WKB-in-a-plain-binary-column layout
and requires Parquet's native `GEOMETRY`/`GEOGRAPHY` logical types, with the
`geo` metadata key demoted to optional. So a valid Parquet file full of geometry
may carry no `geo` key at all, and MapSmith used to treat exactly that file as
CRS-less — while `run_sql` read it happily, because DuckDB understands the native
types. One tool refusing what another accepts, on the same file, is the failure
this module pins down.

The fixtures are written by DuckDB rather than vendored: `geoparquet_version
'NONE'` writes the native type with no `geo` key, and `'BOTH'` writes both
layers. That keeps the expected values in the test instead of in a binary blob.
"""

import pytest

from mapsmith import verify

duckdb = pytest.importorskip("duckdb")
gpd = pytest.importorskip("geopandas")


def _write(path, sql, version, crs_clause=""):
    con = duckdb.connect()
    con.execute("LOAD spatial")
    con.execute(
        f"COPY ({sql}) TO '{str(path).replace(chr(92), '/')}' "
        f"(FORMAT parquet, geoparquet_version '{version}')"
    )
    return path


def test_native_geometry_is_recognised_without_geo_metadata(tmp_path):
    import pyarrow.parquet as pq

    target = _write(tmp_path / "native.parquet", "SELECT 1 AS a, ST_Point(9, 45) AS geom", "NONE")
    assert b"geo" not in (pq.read_schema(target).metadata or {}), "fixture should have no geo key"

    found = verify.native_geometry_column(str(target))
    assert found is not None, "the native logical type was not detected"
    assert found[0] == "geom"


def test_the_crs_probe_no_longer_reports_unknown(tmp_path):
    """The regression that mattered: a file that states its CRS was read as
    CRS-less, so the CRS precondition refused valid work for a wrong reason."""
    source = tmp_path / "utm.parquet"
    gpd.GeoDataFrame(
        {"a": [1]}, geometry=gpd.GeoSeries.from_wkt(["POINT (500000 5000000)"]), crs="EPSG:32632"
    ).to_parquet(source)
    target = _write(
        tmp_path / "native_utm.parquet",
        f"SELECT * FROM read_parquet('{str(source).replace(chr(92), '/')}')",
        "NONE",
    )
    assert verify.probe_crs(str(target)) == "EPSG:32632"


def test_describe_reads_a_native_file(tmp_path):
    """`describe_dataset` used to surface a raw GeoPandas ValueError here."""
    from mapsmith.engines import vector

    target = _write(
        tmp_path / "native.parquet",
        "SELECT * FROM (VALUES (1, ST_Point(9, 45)), (2, ST_Point(10, 46))) t(a, geom)",
        "NONE",
    )
    described = vector.describe(str(target))
    assert described["feature_count"] == 2
    assert described["geometry_types"] == ["Point"]
    assert described["extent"]["minx"] == pytest.approx(9.0)
    assert described["extent"]["maxy"] == pytest.approx(46.0)


def test_both_layers_are_read_the_old_way(tmp_path):
    """`geoparquet_version 'BOTH'` carries native types *and* `geo` metadata.
    The 1.x path must keep winning there, so the two layers cannot disagree."""
    import pyarrow.parquet as pq

    target = _write(tmp_path / "both.parquet", "SELECT ST_Point(9, 45) AS geom", "BOTH")
    assert b"geo" in (pq.read_schema(target).metadata or {})
    assert verify.native_geometry_column(str(target)) is not None
    assert verify.probe_crs(str(target)) != verify.UNKNOWN_CRS


@pytest.mark.parametrize(
    ("declaration", "expected"),
    [
        # The spec's default is OGC:CRS84, and its label is NOT "EPSG:4326":
        # same datum, opposite axis order, and pyproj refuses to give it that
        # code. Flattening the two would be the axis-order bug that eats a
        # working pipeline once the coordinates leave this file.
        ("", "WGS 84 (CRS84)"),
        ("OGC:CRS84", "WGS 84 (CRS84)"),
        ("EPSG:3857", "EPSG:3857"),
        # NOT resolved on purpose: the spec defines srid:<n> as a numeric
        # identifier and names no authority, so EPSG:<n> would be a guess
        # recorded as fact — the 0.2.1 `crs: null` bug in a new costume
        ("srid:5070", verify.UNKNOWN_CRS),
        ("projjson:absent_key", verify.UNKNOWN_CRS),
    ],
)
def test_crs_declaration_forms(tmp_path, declaration, expected):
    target = _write(tmp_path / "x.parquet", "SELECT ST_Point(9, 45) AS geom", "NONE")
    assert verify.native_crs_declaration(str(target), declaration) == expected


def test_an_inline_projjson_declaration_resolves(tmp_path):
    """What DuckDB actually writes is not a `projjson:key` reference but the
    whole document inline, which the first draft of the resolver did not expect."""
    import json

    import pyarrow.parquet as pq

    source = tmp_path / "utm.parquet"
    gpd.GeoDataFrame(
        {"a": [1]}, geometry=gpd.GeoSeries.from_wkt(["POINT (500000 5000000)"]), crs="EPSG:32632"
    ).to_parquet(source)
    target = _write(
        tmp_path / "inline.parquet",
        f"SELECT * FROM read_parquet('{str(source).replace(chr(92), '/')}')",
        "NONE",
    )
    schema = pq.ParquetFile(target).schema
    declaration = next(
        json.loads(schema.column(i).logical_type.to_json()).get("crs")
        for i in range(len(schema))
        if json.loads(schema.column(i).logical_type.to_json()).get("Type") in verify.NATIVE_GEO_TYPES
    )
    assert declaration.lstrip().startswith("{"), "expected an inline PROJJSON document"
    assert verify.native_crs_declaration(str(target), declaration) == "EPSG:32632"


def test_a_plain_parquet_without_geometry_keeps_its_original_error(tmp_path):
    """The fallback must not turn "this file has no geometry" into a confusing
    success or a different exception."""
    from mapsmith.engines import vector

    target = tmp_path / "tabular.parquet"
    duckdb.connect().execute(
        f"COPY (SELECT 1 AS a) TO '{str(target).replace(chr(92), '/')}' (FORMAT parquet)"
    )
    with pytest.raises(ValueError, match="geo metadata"):
        vector.describe(str(target))
