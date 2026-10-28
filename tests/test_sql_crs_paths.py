"""Under a workspace, run_sql's coordinate systems and dataset paths pass the tools' own policies.

DuckDB's spatial extension carries its own PROJ, which opens the files a CRS
names without going through DuckDB's filesystem, so `allowed_directories` never
saw them: measured 2026-10-25, `ST_Transform(g, 'EPSG:4326', '+init=<a file
outside>:1')` read that file while `read_text` on it was refused. The statement
is now read with DuckDB's own parser and each CRS and path is held to
`crs_policy.parse` and `workspace.guard` before it runs.
"""
from __future__ import annotations

import json

import pytest

duckdb = pytest.importorskip("duckdb")

from mapsmith.engines import duckdb_engine


@pytest.fixture
def ws(monkeypatch, tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setenv("MAPSMITH_WORKSPACE", str(root))
    return root


@pytest.fixture
def init_outside(tmp_path):
    """A PROJ init file outside the workspace whose false easting is a number nobody else writes."""
    folder = tmp_path / "outside"
    folder.mkdir()
    path = folder / "evil"
    path.write_text(
        "<1> +proj=tmerc +lat_0=0 +lon_0=0 +k=1 +x_0=777777 +y_0=0 +ellps=WGS84 +units=m +no_defs <>\n"
    )
    return str(path).replace("\\", "/")


def _sql_path(path) -> str:
    return str(path).replace("\\", "/")


def _parquet(path, crs="EPSG:4326"):
    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import Point

    gpd.GeoDataFrame({"id": [1]}, geometry=[Point(15.0, 0.0)], crs=crs).to_parquet(path)
    return path


def _hostile_parquet(path):
    """A GeoParquet whose declared CRS names a grid outside the workspace."""
    pq = pytest.importorskip("pyarrow.parquet")
    from pyproj import CRS

    _parquet(path)
    table = pq.read_table(path)
    meta = dict(table.schema.metadata)
    geo = json.loads(meta[b"geo"])
    geo["columns"]["geometry"]["crs"] = json.loads(
        CRS("+proj=longlat +ellps=intl +nadgrids=C:/evil/x.gsb +type=crs").to_json()
    )
    meta[b"geo"] = json.dumps(geo).encode()
    pq.write_table(table.replace_schema_metadata(meta), path)
    return path


# --- the hole, and that the refusal below is the policy's -----------------------


def test_the_bare_sandbox_lets_proj_read_a_file_outside_the_workspace(ws, init_outside):
    """Why the policy exists, and proof that the init file is real and readable
    by PROJ: without this the refusals below could pass on a typo."""
    con = duckdb_engine._connect()
    x = con.sql(
        f"SELECT ST_X(ST_Transform(ST_Point(0, 0), 'EPSG:4326', '+init={init_outside}:1', always_xy := true))"
    ).fetchone()[0]
    assert x == pytest.approx(777777.0)
    with pytest.raises(duckdb.Error, match="(?i)permission"):
        con.sql(f"SELECT * FROM read_text('{init_outside}')").fetchall()


@pytest.mark.parametrize(
    "template",
    [
        "SELECT ST_Transform(ST_Point(0, 0), 'EPSG:4326', '+init={f}:1', always_xy := true)",
        "SELECT ST_Transform(ST_SetCRS(ST_Point(0, 0), '+init={f}:1'), 'EPSG:4326')",
        "SELECT ST_Point(0, 0)::GEOMETRY('+init={f}:1')",
        "SELECT ST_Transform(ST_Point(0, 0), 'EPSG:4326', '+proj=longlat +nadgrids={f} +type=crs')",
        # nested where a walk of the top level would not look
        "WITH c AS (SELECT ST_Transform(ST_Point(0, 0), 'EPSG:4326', '+init={f}:1') AS g) SELECT * FROM c",
        "SELECT 1 WHERE EXISTS (SELECT ST_SetCRS(ST_Point(0, 0), '+init={f}:1'))",
    ],
)
def test_a_crs_naming_a_file_is_refused_before_proj_opens_it(ws, init_outside, template):
    with pytest.raises(ValueError, match="refused"):
        duckdb_engine.run_sql(template.format(f=init_outside))


@pytest.mark.parametrize(
    "query",
    [
        "WITH c AS (SELECT 'EPSG:3857' AS crs) SELECT ST_Transform(ST_Point(0, 0), 'EPSG:4326', crs) FROM c",
        "SELECT ST_Transform(ST_Point(0, 0), 'EPSG:4326', 'EPSG:' || '3857')",
        "SELECT ST_SetCRS(ST_Point(0, 0), lower('EPSG:4326'))",
    ],
)
def test_a_crs_computed_in_sql_is_refused_because_it_cannot_be_checked(ws, query):
    with pytest.raises(ValueError, match="literals"):
        duckdb_engine.run_sql(query)


def test_a_bare_grid_is_refused_because_duckdb_proj_can_use_the_server_cwd(ws):
    query = (
        "SELECT ST_Transform(ST_Point(12, 45), 'EPSG:4326', "
        "'+proj=longlat +ellps=clrk66 +nadgrids=shift.gsb +type=crs', always_xy := true)"
    )
    with pytest.raises(ValueError, match="external grid files"):
        duckdb_engine.run_sql(query)


def test_a_dataset_declaring_a_bare_grid_is_refused_by_run_sql(ws):
    path = _native_parquet(ws / "bare-grid.parquet", "shift.gsb")
    with pytest.raises(ValueError, match="external grid files"):
        duckdb_engine.run_sql(_LATITUDE.format(g="geometry", source=f"read_parquet('{path}')"))


def test_the_null_grid_is_not_an_external_file(ws):
    result = duckdb_engine.run_sql(
        "SELECT ST_Y(ST_Transform(ST_Point(12, 45), 'EPSG:4326', "
        "'+proj=longlat +ellps=clrk66 +nadgrids=@null +type=crs', always_xy := true))"
    )
    assert result["rows"][0][0] == pytest.approx(45.0, abs=1e-6)


def test_a_honest_transform_still_answers(ws):
    """Longitude 15 on the equator is the central meridian of UTM 33N: x = 500000, y = 0."""
    result = duckdb_engine.run_sql(
        "SELECT ST_X(g) AS x, ST_Y(g) AS y FROM (SELECT ST_Transform(ST_Point(15, 0), "
        "'EPSG:4326', 'EPSG:32633', always_xy := true) AS g)"
    )
    x, y = result["rows"][0]
    assert x == pytest.approx(500000.0, abs=1e-6)
    assert y == pytest.approx(0.0, abs=1e-6)
    positional = duckdb_engine.run_sql(
        "SELECT ST_X(ST_Transform(ST_Point(15, 0), 'EPSG:4326', 'EPSG:32633', true))"
    )
    assert positional["rows"][0][0] == pytest.approx(500000.0, abs=1e-6)


# --- statements the walk cannot read ---------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "CREATE TABLE t AS SELECT 1 AS v",
        "COPY (SELECT 1 AS v) TO 'x.parquet'",
        "SELECT 1; CREATE TABLE t AS SELECT 2 AS v",
    ],
)
def test_a_statement_that_is_not_a_select_is_refused_under_a_workspace(ws, query):
    with pytest.raises(ValueError, match="SELECT"):
        duckdb_engine.run_sql(query)


def test_sql_held_in_a_string_is_refused(ws, init_outside):
    with pytest.raises(ValueError, match="string"):
        duckdb_engine.run_sql(
            f"SELECT * FROM query('SELECT ST_Transform(ST_Point(0, 0), ''EPSG:4326'', ''+init={init_outside}:1'')')"
        )


def test_without_a_workspace_nothing_changes(monkeypatch):
    """Unconfined mode is documented as unconfined: the walk is not run there."""
    monkeypatch.delenv("MAPSMITH_WORKSPACE", raising=False)
    result = duckdb_engine.run_sql("CREATE TABLE t AS SELECT 7 AS v; SELECT v FROM t")
    assert result["rows"] == [[7]]


# --- dataset paths ---------------------------------------------------------------


def test_a_geoparquet_declaring_a_hostile_crs_is_refused_by_every_spelling(ws):
    path = _sql_path(_hostile_parquet(ws / "evil.parquet"))
    for query in (
        f"SELECT * FROM read_parquet('{path}')",
        f"SELECT * FROM read_parquet(['{path}'])",
        f"SELECT * FROM parquet_scan('{path}')",
        f"SELECT * FROM '{path}'",
        f"SELECT * FROM read_parquet('{_sql_path(ws)}/*.parquet')",
    ):
        with pytest.raises(ValueError, match="refused"):
            duckdb_engine.run_sql(query)


def test_a_geopackage_declaring_a_hostile_srs_is_refused_by_st_read(ws):
    import sqlite3
    from contextlib import closing

    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import Point

    path = ws / "evil.gpkg"
    gpd.GeoDataFrame({"id": [1]}, geometry=[Point(9.0, 45.0)], crs="EPSG:4326").to_file(path)
    with closing(sqlite3.connect(path)) as db:
        db.execute(
            "UPDATE gpkg_spatial_ref_sys SET definition = ? WHERE srs_id = 4326",
            (
                (
                    'GEOGCS["evil",DATUM["x",SPHEROID["intl",6378388,297]],PRIMEM["Greenwich",0],'
                    'UNIT["degree",0.0174532925199433],EXTENSION["PROJ4_GRIDS","C:/evil/x.gsb"]]'
                ),
            ),
        )
        db.commit()
    for query in (f"SELECT * FROM ST_Read('{_sql_path(path)}')", f"SELECT * FROM ST_Read_Meta('{_sql_path(path)}')"):
        with pytest.raises(ValueError, match="refused"):
            duckdb_engine.run_sql(query)


def test_a_path_computed_in_sql_is_refused(ws):
    _parquet(ws / "a.parquet")
    with pytest.raises(ValueError, match="literals"):
        duckdb_engine.run_sql(f"SELECT * FROM read_parquet('{_sql_path(ws)}/' || 'a.parquet')")


def test_gdal_open_options_are_refused(ws):
    pytest.importorskip("geopandas")
    path = _parquet(ws / "a.parquet")
    with pytest.raises(ValueError, match="open_options"):
        duckdb_engine.run_sql(f"SELECT * FROM ST_Read('{_sql_path(path)}', open_options := ['X=Y'])")


def test_honest_datasets_in_the_workspace_are_still_read(ws):
    a = _sql_path(_parquet(ws / "a.parquet"))
    b = _sql_path(_parquet(ws / "b.parquet"))
    for query, rows in (
        (f"SELECT count(*) FROM read_parquet('{a}')", 1),
        (f"SELECT count(*) FROM read_parquet(['{a}', '{b}'])", 2),
        (f"SELECT count(*) FROM read_parquet('{_sql_path(ws)}/*.parquet')", 2),
        (f"SELECT count(*) FROM '{a}'", 1),
    ):
        assert duckdb_engine.run_sql(query)["rows"] == [[rows]], query


def test_a_cte_named_like_a_file_is_not_a_path(ws):
    result = duckdb_engine.run_sql('WITH "x.parquet" AS (SELECT 3 AS v) SELECT v FROM "x.parquet"')
    assert result["rows"] == [[3]]


def test_a_nested_cte_does_not_hide_a_file_in_the_parent_scope(ws):
    _hostile_parquet(ws / "evil.parquet")
    query = (
        "SELECT * FROM 'evil.parquet' WHERE EXISTS "
        "(WITH \"evil.parquet\" AS (SELECT 1 AS x) SELECT 1)"
    )
    with pytest.raises(ValueError, match="refused"):
        duckdb_engine.run_sql(query)


@pytest.mark.parametrize(
    "query",
    [
        "WITH \"evil.parquet\" AS (SELECT * FROM 'evil.parquet') SELECT * FROM \"evil.parquet\"",
        "WITH a AS (SELECT * FROM 'evil.parquet'), \"evil.parquet\" AS (SELECT 1 AS x) SELECT * FROM a",
        ("WITH RECURSIVE \"evil.parquet\" AS (SELECT * FROM 'evil.parquet' "
         "UNION ALL SELECT 1) SELECT * FROM \"evil.parquet\""),
    ],
)
def test_a_cte_does_not_hide_a_file_inside_its_own_or_an_earlier_definition(ws, query):
    _hostile_parquet(ws / "evil.parquet")
    with pytest.raises(ValueError, match="refused"):
        duckdb_engine.run_sql(query)


def test_a_replacement_scan_without_an_extension_is_still_checked(ws):
    import shutil

    hostile = _hostile_parquet(ws / "evil.parquet")
    shutil.copy(hostile, ws / "evil")
    with pytest.raises(ValueError, match="refused"):
        duckdb_engine.run_sql("SELECT * FROM 'evil'")


def test_the_output_path_still_materialises_a_checked_query(ws):
    a = _sql_path(_parquet(ws / "a.parquet"))
    out = ws / "out.parquet"
    result = duckdb_engine.run_sql(
        f"SELECT id, ST_Transform(geometry, 'EPSG:4326', 'EPSG:32633', always_xy := true) AS geometry "
        f"FROM read_parquet('{a}')",
        output_path=str(out),
    )
    assert result["row_count"] == 1
    assert out.exists()


# --- the two routes an audit found on 2026-10-25 ------------------------------------
#
# Both went through `crs_policy.declared_srs_texts`, which did not read the CRS
# from where DuckDB and GDAL read it. The observable is a real NTv2 grid outside
# the workspace that moves latitude by exactly 2.5 degrees: (12, 45) comes back
# at latitude 47.5 if and only if PROJ read the file.


def _ntv2_grid(path) -> str:
    """A one-cell NTv2 grid (clrk66 -> WGS84) over 11-13 E, 44-46 N shifting latitude by 9000 seconds."""
    import struct

    def text(key, value):
        return key.ljust(8).encode()[:8] + value.ljust(8).encode()[:8]

    def integer(key, value):
        return key.ljust(8).encode()[:8] + struct.pack("<i", value) + b"\x00" * 4

    def real(key, value):
        return key.ljust(8).encode()[:8] + struct.pack("<d", value)

    data = b"".join([
        integer("NUM_OREC", 11), integer("NUM_SREC", 11), integer("NUM_FILE", 1),
        text("GS_TYPE", "SECONDS"), text("VERSION", "NTv2.0"), text("SYSTEM_F", "clrk66"),
        text("SYSTEM_T", "WGS84"), real("MAJOR_F", 6378206.4), real("MINOR_F", 6356583.8),
        real("MAJOR_T", 6378137.0), real("MINOR_T", 6356752.314),
        text("SUB_NAME", "NONE"), text("PARENT", "NONE"), text("CREATED", "TEST"), text("UPDATED", "TEST"),
        real("S_LAT", 44 * 3600.0), real("N_LAT", 46 * 3600.0),
        real("E_LONG", -13 * 3600.0), real("W_LONG", -11 * 3600.0),  # NTv2 longitudes are positive west
        real("LAT_INC", 7200.0), real("LONG_INC", 7200.0), integer("GS_COUNT", 4),
        struct.pack("<ffff", 9000.0, 0.0, 0.0, 0.0) * 4,
    ])
    path.write_bytes(data)
    return str(path).replace("\\", "/")


def _cwd_grid_probe(tmp_path, runtime, pin):
    import subprocess
    import sys

    ws = tmp_path / "ws"
    cwd = tmp_path / "cwd"
    ws.mkdir()
    cwd.mkdir()
    _ntv2_grid(cwd / "shift.gsb")
    source = "+proj=longlat +ellps=clrk66 +nadgrids=shift.gsb +type=crs"
    if runtime == "pyproj":
        probe = (
            "from pyproj import Transformer\n"
            f"t = Transformer.from_crs({source!r}, 'EPSG:4326', always_xy=True)\n"
            "print(t.transform(12, 45)[1])\n"
        )
    elif runtime == "rasterio":
        probe = (
            "from rasterio.warp import transform\n"
            f"print(transform({source!r}, 'EPSG:4326', [12], [45])[1][0])\n"
        )
    else:
        probe = (
            "from mapsmith.engines import duckdb_engine\n"
            f"print(duckdb_engine._connect().sql(\"SELECT ST_Y(ST_Transform(ST_Point(12, 45), {source!r}, "
            "'EPSG:4326', always_xy := true))\").fetchone()[0])\n"
        )
    code = (
        "import os\n"
        f"os.environ['MAPSMITH_WORKSPACE'] = {str(ws)!r}\n"
        f"os.chdir({str(cwd)!r})\n"
        "from mapsmith import crs_policy\n"
        + ("crs_policy.pin_search_paths_under_workspace()\n" if pin else "")
        + "try:\n"
        + "".join(f"    {line}\n" for line in probe.splitlines())
        + "except Exception:\n    print('nan')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=30,
    )
    return float(result.stdout.strip())


def test_duckdb_cannot_read_a_grid_from_the_old_server_cwd(tmp_path):
    """The same fixture moved latitude to 47.5 before the cwd was confined.
    With no grid in the workspace, DuckDB now returns infinity instead."""
    import math

    assert math.isinf(_cwd_grid_probe(tmp_path, "duckdb", pin=False))


@pytest.mark.parametrize("runtime", ["pyproj", "rasterio"])
def test_explicit_proj_search_paths_do_not_fall_back_to_the_cwd(tmp_path, runtime):
    assert _cwd_grid_probe(tmp_path, runtime, pin=True) != pytest.approx(47.5, abs=1e-5)


def test_pinned_proj_paths_still_resolve_epsg(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    import subprocess
    import sys

    code = (
        "import os\n"
        f"os.environ['MAPSMITH_WORKSPACE'] = {str(ws)!r}\n"
        "from mapsmith import crs_policy\n"
        "crs_policy.pin_search_paths_under_workspace()\n"
        "from pyproj import Transformer\n"
        "print(Transformer.from_crs('EPSG:4326', 'EPSG:32633', always_xy=True).transform(15, 0)[0])\n"
        "from rasterio.warp import transform\n"
        "print(transform('EPSG:4326', 'EPSG:32633', [15], [0])[0][0])\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=30,
    )
    assert [float(line) for line in result.stdout.splitlines()] == [
        pytest.approx(500000.0, abs=1e-6), pytest.approx(500000.0, abs=1e-6),
    ]


@pytest.fixture
def grid_outside(tmp_path):
    folder = tmp_path / "grids"
    folder.mkdir()
    return _ntv2_grid(folder / "shift.gsb")


_LATITUDE = "SELECT ST_Y(ST_Transform({g}, 'EPSG:4326', always_xy := true)) FROM {source}"


def _native_parquet(path, grid) -> str:
    """A Parquet whose CRS lives only in the column's native GEOMETRY type: no `geo` key."""
    from pyproj import CRS

    crs = CRS(f"+proj=longlat +ellps=clrk66 +nadgrids={grid} +type=crs").to_json().replace("'", "''")
    target = str(path).replace("\\", "/")
    plain = duckdb.connect()
    plain.load_extension("spatial")
    plain.execute(
        f"COPY (SELECT 1 AS id, ST_SetCRS(ST_Point(12.0, 45.0), '{crs}') AS geometry) "
        f"TO '{target}' (FORMAT parquet, geoparquet_version 'NONE')"
    )
    plain.close()
    return target


def test_a_crs_held_only_in_the_native_parquet_type_is_checked(ws, grid_outside):
    from mapsmith import crs_policy

    path = _native_parquet(ws / "native.parquet", grid_outside)
    pq = pytest.importorskip("pyarrow.parquet")
    assert b"geo" not in (pq.ParquetFile(path).metadata.metadata or {})
    # Counterpart: the grid is real and DuckDB's PROJ reads it through the bare sandbox.
    lat = duckdb_engine._connect().sql(
        _LATITUDE.format(g="geometry", source=f"read_parquet('{path}')")
    ).fetchone()[0]
    assert lat == pytest.approx(47.5, abs=1e-5)

    assert any("nadgrids" in text or "shift.gsb" in text for text in crs_policy.declared_srs_texts(path))
    for source in (f"read_parquet('{path}')", f"'{path}'"):
        with pytest.raises(ValueError, match="refused"):
            duckdb_engine.run_sql(_LATITUDE.format(g="geometry", source=source))


def test_a_hostile_crs_in_a_gdal_format_is_not_handed_to_gdal(ws, grid_outside):
    """Since 2026-10-26 the path gate reads these formats' SRS itself (srs_sources):
    the grid never reaches GDAL because the CRS is read and refused, where on
    2026-10-25 it was because the format was refused unread."""
    gpd = pytest.importorskip("geopandas")
    from pyproj import CRS
    from shapely.geometry import Point

    hostile = gpd.GeoDataFrame(
        {"id": [1]},
        geometry=[Point(12.0, 45.0)],
        crs=CRS(f"+proj=longlat +ellps=clrk66 +nadgrids={grid_outside} +type=crs"),
    )
    fgb = ws / "parcels.fgb"
    hostile.to_file(fgb, driver="FlatGeobuf")
    disguised = ws / "parcels.parquet"  # GeoJSON content under a Parquet name: GDAL goes by content
    # Written by hand: GDAL's GeoJSON writer emits no `crs` member at all, so a
    # file it writes from this frame is not hostile (the first version of this
    # test used one, and it only passed because the format was refused unread).
    (ws / "parcels.geojson").write_text(json.dumps({
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": f"+proj=longlat +ellps=clrk66 +nadgrids={grid_outside} +type=crs"}},
        "features": [{"type": "Feature", "properties": {"id": 1}, "geometry": {"type": "Point", "coordinates": [12, 45]}}],
    }), encoding="utf-8")
    disguised.write_bytes((ws / "parcels.geojson").read_bytes())

    # Counterpart: GDAL reads the FlatGeobuf's SRS, and PROJ the grid, through the bare sandbox.
    lat = duckdb_engine._connect().sql(
        _LATITUDE.format(g="geom", source=f"ST_Read('{_sql_path(fgb)}')")
    ).fetchone()[0]
    assert lat == pytest.approx(47.5, abs=1e-5)

    for source in (
        f"ST_Read('{_sql_path(fgb)}')",
        f"ST_Read('{_sql_path(ws / 'parcels.geojson')}')",
        f"ST_Read('{_sql_path(disguised)}')",
        f"'{_sql_path(fgb)}'",
        f"ST_Read('{_sql_path(ws)}/*.fgb')",
    ):
        with pytest.raises(ValueError, match="refused"):
            duckdb_engine.run_sql(_LATITUDE.format(g="geom", source=source))


def test_a_format_mapsmith_does_not_recognise_is_not_handed_to_gdal(ws):
    path = ws / "scene.img"
    path.write_bytes(b"\x89HFA\x00\x01\x02\x03" + bytes(range(256)) * 4)
    for source in (f"ST_Read('{_sql_path(path)}')", f"'{_sql_path(path)}'"):
        with pytest.raises(ValueError, match="GDAL"):
            duckdb_engine.run_sql(f"SELECT count(*) FROM {source}")


def test_open_options_are_refused_in_both_spellings(ws):
    """`open_options = [...]` serialises as a comparison and still runs as the
    named parameter: the walk must refuse it as surely as the `:=` form."""
    path = _sql_path(_parquet(ws / "a.parquet"))
    for spelling in (":=", "="):
        with pytest.raises(ValueError):
            duckdb_engine.run_sql(f"SELECT * FROM ST_Read('{path}', open_options {spelling} ['X=Y'])")


@pytest.mark.parametrize("workspace_set", [True, False])
@pytest.mark.parametrize("spelling", [":=", "="])
def test_a_missing_layer_is_refused_before_it_crashes_the_process(monkeypatch, tmp_path, workspace_set, spelling):
    """DuckDB spatial 1.5.5 dies with a segmentation fault on ST_Read of a layer
    the dataset does not have: the whole server, from one line of SQL. Not
    reproduced here -- a crash would take pytest with it -- only refused."""
    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import Point

    root = tmp_path / "ws"
    root.mkdir()
    if workspace_set:
        monkeypatch.setenv("MAPSMITH_WORKSPACE", str(root))
    else:
        monkeypatch.delenv("MAPSMITH_WORKSPACE", raising=False)
    path = root / "a.gpkg"
    gpd.GeoDataFrame({"id": [1]}, geometry=[Point(0, 0)], crs="EPSG:4326").to_file(path, layer="roads")
    with pytest.raises(ValueError, match="no layer 'nope'"):
        duckdb_engine.run_sql(f"SELECT count(*) FROM ST_Read('{_sql_path(path)}', layer {spelling} 'nope')")
    assert duckdb_engine.run_sql(
        f"SELECT count(*) FROM ST_Read('{_sql_path(path)}', layer {spelling} 'roads')"
    )["rows"] == [[1]]


@pytest.mark.parametrize(
    "template",
    [
        "CREATE TABLE t AS SELECT * FROM ST_Read('{p}', layer := 'nope')",
        "COPY (SELECT * FROM ST_Read('{p}', layer = 'nope')) TO '{o}' (FORMAT parquet)",
        "SELECT 1; CREATE TABLE t AS SELECT * FROM ST_Read('{p}', layer := 'nope')",
    ],
)
def test_a_layer_outside_a_select_is_refused_without_a_workspace(monkeypatch, tmp_path, template):
    """Without a workspace non-SELECT statements run, and the walk cannot read
    them: the same crash was one CREATE TABLE away (showcase review, 2026-10-25)."""
    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import Point

    monkeypatch.delenv("MAPSMITH_WORKSPACE", raising=False)
    path = tmp_path / "a.gpkg"
    gpd.GeoDataFrame({"id": [1]}, geometry=[Point(0, 0)], crs="EPSG:4326").to_file(path, layer="roads")
    query = template.format(p=_sql_path(path), o=_sql_path(tmp_path / "o.parquet"))
    with pytest.raises(ValueError, match="only inside a SELECT"):
        duckdb_engine.run_sql(query)


def test_gdal_still_reads_the_formats_mapsmith_checks_first(ws):
    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import Point

    honest = gpd.GeoDataFrame({"id": [1, 2]}, geometry=[Point(15, 0), Point(16, 1)], crs="EPSG:4326")
    honest.to_file(ws / "a.gpkg")
    honest.to_file(ws / "a.shp")
    (ws / "a.csv").write_text("id\n1\n2\n")
    for source in (
        f"ST_Read('{_sql_path(ws / 'a.gpkg')}')",
        f"'{_sql_path(ws / 'a.gpkg')}'",
        f"ST_Read('{_sql_path(ws / 'a.shp')}')",
        f"'{_sql_path(ws / 'a.csv')}'",
    ):
        assert duckdb_engine.run_sql(f"SELECT count(*) FROM {source}")["rows"] == [[2]], source
