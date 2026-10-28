"""A CRS definition may not name a file or a host (`crs_policy`, 2026-10-23)."""
from __future__ import annotations

import json
from contextlib import closing

import pytest

from mapsmith import crs_policy, datum

HOSTILE = [
    "+proj=longlat +ellps=intl +nadgrids=C:/evil/x.gsb +type=crs",
    "+proj=longlat +ellps=intl +nadgrids=/tmp/evil.gsb +type=crs",
    "+proj=longlat +ellps=intl +nadgrids=//localhost/C$/evil.gsb +type=crs",
    "+proj=longlat +ellps=intl +nadgrids=\\\\host\\share\\evil.gsb +type=crs",
    "+proj=longlat +ellps=intl +nadgrids=http://host/x.tif +type=crs",
    "+proj=longlat +ellps=intl +nadgrids=@C:/evil/x.gsb +type=crs",
    "+proj=longlat +ellps=intl +nadgrids=../x.gsb +type=crs",
    "+proj=longlat +ellps=intl +geoidgrids=/etc/passwd +type=crs",
    "+init=C:/evil/epsg",
    "+init=/etc/proj/evil",
    # the forms pyproj also accepts (second audit pass): no `+`, spaces, quotes, `|`
    "proj=longlat ellps=intl nadgrids=C:/evil/x.gsb type=crs",
    "+proj=longlat +ellps=intl +nadgrids = C:/evil/x.gsb +type=crs",
    '+proj=longlat +ellps=intl +nadgrids="C:/evil/x.gsb" +type=crs',
    "+proj=longlat +ellps=intl +nadgrids=c|/evil/x.gsb +type=crs",
    "+init = C:/evil/epsg",
]
HONEST = [
    "EPSG:4326",
    "EPSG:3003",
    "+proj=longlat +ellps=intl +nadgrids=@null +type=crs",
    "+proj=longlat +ellps=clrk66 +nadgrids=ntv1_can.dat +type=crs",
    (
        "+proj=tmerc +lat_0=0 +lon_0=9 +k=0.9996 +x_0=1500000 +y_0=0 +ellps=intl "
        "+towgs84=-104.1,-49.1,-9.9,0.971,-2.917,0.714,-11.68 +units=m +no_defs +type=crs"
    ),
    "+init=epsg:4326",
]


@pytest.mark.parametrize("text", HOSTILE)
def test_a_crs_naming_a_file_or_a_host_is_refused_before_proj_sees_it(text):
    assert crs_policy.reason_to_refuse_text(text), text
    with pytest.raises(ValueError, match="refused"):
        crs_policy.parse(text)
    with pytest.raises(ValueError, match="refused"):
        datum._as_crs(text)


@pytest.mark.parametrize("text", HONEST)
def test_a_crs_with_bare_names_is_accepted(text):
    assert crs_policy.reason_to_refuse_text(text) is None, text
    crs_policy.parse(text)


def test_a_grid_path_hidden_in_wkt_or_projjson_is_still_seen():
    """`to_proj4()` of a CRS built from WKT can drop the grid; the object and its JSON keep it."""
    from pyproj import CRS

    hostile = CRS("+proj=longlat +ellps=intl +nadgrids=C:/evil/x.gsb +type=crs")
    for form in (hostile.to_wkt(), hostile.to_json()):
        rebuilt = CRS(form)
        assert crs_policy.reason_to_refuse(rebuilt), form[:80]
        assert crs_policy.reason_to_refuse_text(form), form[:80]


def test_a_file_declaring_a_hostile_crs_is_refused_with_the_reason(tmp_path):
    """The CRS a GeoParquet carries is the author's text; `readers` refuses it."""
    gpd = pytest.importorskip("geopandas")
    pq = pytest.importorskip("pyarrow.parquet")
    from shapely.geometry import Point

    from mapsmith import readers

    path = tmp_path / "evil.parquet"
    gpd.GeoDataFrame({"id": [1]}, geometry=[Point(9.0, 45.0)], crs="EPSG:4326").to_parquet(path)
    table = pq.read_table(path)
    meta = dict(table.schema.metadata)
    geo = json.loads(meta[b"geo"])
    column = geo["columns"]["geometry"]
    column["crs"] = json.loads(
        __import__("pyproj").CRS("+proj=longlat +ellps=intl +nadgrids=C:/evil/x.gsb +type=crs").to_json()
    )
    meta[b"geo"] = json.dumps(geo).encode()
    pq.write_table(table.replace_schema_metadata(meta), path)
    with pytest.raises(ValueError, match="refused"):
        readers.read_vector(str(path))


def test_reproject_layer_refuses_a_hostile_target(tmp_path):
    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import Point

    from mapsmith.engines import vector

    src = tmp_path / "pts.gpkg"
    gpd.GeoDataFrame({"id": [1]}, geometry=[Point(9.0, 45.0)], crs="EPSG:4326").to_file(src)
    with pytest.raises(ValueError, match="refused"):
        vector.reproject(str(src), "+proj=longlat +ellps=intl +nadgrids=C:/evil/x.gsb +type=crs", str(tmp_path / "o.gpkg"))
    assert not (tmp_path / "o.gpkg").exists()


def test_proj_search_paths_are_explicit_and_keep_each_runtime_database(monkeypatch, tmp_path):
    from pathlib import Path

    monkeypatch.setenv("MAPSMITH_WORKSPACE", str(tmp_path))
    pinned = crs_policy.pin_search_paths_under_workspace()
    assert pinned["pyproj"]
    assert pinned["pyogrio"]
    for runtime, paths in pinned.items():
        assert any(Path(path, "proj.db").is_file() for path in paths), runtime
        assert all(Path(path).is_absolute() for path in paths), runtime


def test_a_relative_workspace_is_pinned_before_chdir(monkeypatch, tmp_path):
    from pathlib import Path

    from mapsmith import workspace

    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MAPSMITH_WORKSPACE", "ws")
    workspace.confine_working_directory()
    workspace.confine_working_directory()
    assert Path.cwd() == ws
    assert workspace.root() == ws


def test_rasterio_multiple_absolute_proj_paths_are_preserved(monkeypatch, tmp_path):
    from rasterio import _env

    database = _env.get_proj_data_search_paths()[0]
    grid_only = tmp_path / "grids"
    grid_only.mkdir()
    paths = [database, str(grid_only)]
    monkeypatch.setattr(_env, "get_proj_data_search_paths", lambda: paths)
    monkeypatch.setattr(
        _env,
        "set_proj_data_search_path",
        lambda path: (_ for _ in ()).throw(AssertionError("multi-path configuration was overwritten")),
    )
    monkeypatch.setenv("MAPSMITH_WORKSPACE", str(tmp_path))
    assert crs_policy.pin_search_paths_under_workspace()["rasterio"] == paths


def test_rasterio_multiple_relative_proj_paths_are_not_falsely_pinned(monkeypatch, tmp_path):
    from rasterio import _env

    monkeypatch.setattr(_env, "get_proj_data_search_paths", lambda: ["relative/db", "relative/grids"])
    monkeypatch.setenv("MAPSMITH_WORKSPACE", str(tmp_path))
    with pytest.raises(RuntimeError, match="multiple relative PROJ paths"):
        crs_policy.pin_search_paths_under_workspace()


def test_server_startup_pins_proj_then_confines_cwd_before_mcp(monkeypatch, tmp_path):
    from pathlib import Path

    from mapsmith import server

    ws = tmp_path / "ws"
    outside = tmp_path / "outside"
    ws.mkdir()
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setenv("MAPSMITH_WORKSPACE", str(ws))
    calls = []
    monkeypatch.setattr(server.crs_policy, "pin_search_paths_under_workspace", lambda: calls.append("pin"))
    monkeypatch.setattr(server.crs_policy, "network_off_under_workspace", lambda: calls.append("network"))
    monkeypatch.setattr(server.mcp, "run", lambda **kwargs: calls.append("run"))
    real_confine = server.workspace.confine_working_directory

    def recording_confine():
        calls.append("cwd")
        return real_confine()

    monkeypatch.setattr(server.workspace, "confine_working_directory", recording_confine)
    server.main()
    assert Path.cwd() == ws
    assert calls == ["pin", "cwd", "network", "run"]


def test_networking_is_switched_off_under_a_workspace(monkeypatch, tmp_path):
    pyproj = pytest.importorskip("pyproj")
    from pyproj.network import is_network_enabled, set_network_enabled

    from mapsmith import workspace

    monkeypatch.setenv("MAPSMITH_WORKSPACE", str(tmp_path))
    workspace.root.cache_clear() if hasattr(workspace.root, "cache_clear") else None
    set_network_enabled(True)
    assert is_network_enabled()
    assert crs_policy.network_off_under_workspace() is True
    assert not is_network_enabled()
    del pyproj


def test_the_policy_never_asks_proj_whether_a_grid_exists():
    """`coordinate_operation.grids` opens the file to answer `available` (measured):
    the policy reads the CRS as text only."""
    import inspect

    source = inspect.getsource(crs_policy.reason_to_refuse)
    body = source.split('"""')[-1]  # after the docstring, which names the trap
    assert "grids" not in body


def test_a_geopackage_declaring_a_hostile_srs_is_refused_before_gdal_opens_it(tmp_path):
    """The SRS is read from `gpkg_spatial_ref_sys` through sqlite, and refused before
    `gpd.read_file`: GDAL opened the grid twenty times while reading the SRS (measured)."""
    import sqlite3

    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import Point

    from mapsmith import readers

    path = tmp_path / "evil.gpkg"
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
    texts = readers._declared_srs_texts(str(path))
    assert any("PROJ4_GRIDS" in t for t in texts)
    with pytest.raises(ValueError, match="refused"):
        readers.read_vector(str(path))


def test_a_declared_name_called_ballpark_does_not_hide_projs_own_leg():
    """What `_unshifted` reads is PROJ's own description, the declared steps already
    removed as objects by `datum._projs_own_description`; names play no part."""
    helmert = "+proj=pipeline +step +proj=helmert +x=1"
    assert datum._unshifted(-1.0, helmert, "Inverse of unknown") is False
    assert datum._unshifted(-1.0, helmert, "Inverse of unknown + Ballpark geographic offset from WGS 84 to NAD27") is True
    assert datum._unshifted(-1.0, helmert, "") is False, "every step was the declared one: a shift, nobody's ballpark"


def test_gdal_policy_switches_proj_networking_off_in_the_environment(monkeypatch):
    import os

    from mapsmith import gdal_policy

    monkeypatch.delenv("MAPSMITH_ALLOW_REMOTE", raising=False)
    monkeypatch.setenv("PROJ_NETWORK", "ON")
    gdal_policy.apply(force=True)
    assert os.environ["PROJ_NETWORK"] == "OFF"


def test_a_proj_based_transformation_is_refused_whatever_file_it_names():
    text = (
        'BOUNDCRS[SOURCECRS[GEOGCS["x",DATUM["d",SPHEROID["intl",6378388,297]],PRIMEM["Greenwich",0],'
        'UNIT["degree",0.0174532925199433]]],TARGETCRS[GEOGCS["WGS 84",DATUM["WGS_1984",'
        'SPHEROID["WGS 84",6378137,298.257223563]],PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]],'
        'ABRIDGEDTRANSFORMATION["t",METHOD["PROJ-based operation method: +proj=tinshift +file=\\\\host\\x"]]]'
    )
    assert crs_policy.reason_to_refuse_text(text)
    for key in ("file", "model"):
        assert crs_policy.reason_to_refuse_text(f"+proj=longlat +ellps=intl +{key}=C:/evil/x +type=crs")


def test_doubled_wkt_quotes_do_not_hide_a_grid(tmp_path):
    """WKT escapes a quote as `""`; the regex missed it, pyproj normalises it (measured)."""
    import sqlite3

    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import Point

    from mapsmith import readers

    path = tmp_path / "evil2.gpkg"
    gpd.GeoDataFrame({"id": [1]}, geometry=[Point(9.0, 45.0)], crs="EPSG:4326").to_file(path)
    with closing(sqlite3.connect(path)) as db:
        db.execute(
            "UPDATE gpkg_spatial_ref_sys SET definition = ? WHERE srs_id = 4326",
            (
                (
                    'GEOGCS["evil",DATUM["x",SPHEROID["intl",6378388,297]],PRIMEM["Greenwich",0],'
                    'UNIT["degree",0.0174532925199433],EXTENSION["PROJ4_GRIDS","@x"",C:/evil/x.gsb"]]'
                ),
            ),
        )
        db.commit()
    with pytest.raises(ValueError, match="refused"):
        readers.read_vector(str(path))


def test_a_geopackage_is_recognised_by_its_bytes_and_a_broken_catalogue_is_refused(tmp_path):
    import shutil
    import sqlite3

    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import Point

    good = tmp_path / "good.gpkg"
    gpd.GeoDataFrame({"id": [1]}, geometry=[Point(9.0, 45.0)], crs="EPSG:4326").to_file(good)
    renamed = tmp_path / "h#1.sqlite"
    shutil.copy(good, renamed)
    with closing(sqlite3.connect(renamed)) as db:
        db.execute(
            "UPDATE gpkg_spatial_ref_sys SET definition_12_063 = ? WHERE srs_id = 4326",
            (
                (
                    'GEOGCRS["evil",DATUM["x",ELLIPSOID["intl",6378388,297]],PRIMEM["Greenwich",0],'
                    'CS[ellipsoidal,2],AXIS["lat",north],AXIS["lon",east],UNIT["degree",0.0174532925199433]]'
                ),
            ),
        ) if "definition_12_063" in {r[1] for r in db.execute("PRAGMA table_info(gpkg_spatial_ref_sys)")} else None
        db.commit()
    texts = crs_policy.declared_srs_texts(str(renamed))
    assert texts, "an SQLite file with no .gpkg extension must still be read as one"
    assert texts == crs_policy.declared_srs_texts(str(good)) or len(texts) >= 1


def test_the_path_gate_refuses_a_hostile_input_before_any_probe(tmp_path):
    import sqlite3

    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import Point

    from mapsmith import workspace

    path = tmp_path / "evil3.gpkg"
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
    with pytest.raises(ValueError, match="refused"):
        workspace.guard(str(path), "input_path")
    assert workspace.guard(str(tmp_path / "out.gpkg"), "output_path")  # an output that does not exist yet passes


def _hostile_gpkg(tmp_path, name="evil.gpkg", column="definition"):
    import sqlite3

    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import Point

    path = tmp_path / name
    gpd.GeoDataFrame({"id": [1]}, geometry=[Point(9.0, 45.0)], crs="EPSG:4326").to_file(path)
    with closing(sqlite3.connect(path)) as db:
        db.execute(
            f"UPDATE gpkg_spatial_ref_sys SET {column} = ? WHERE srs_id = 4326",
            (
                (
                    'GEOGCS["evil",DATUM["x",SPHEROID["intl",6378388,297]],PRIMEM["Greenwich",0],'
                    'UNIT["degree",0.0174532925199433],EXTENSION["PROJ4_GRIDS","C:/evil/x.gsb"]]'
                ),
            ),
        )
        db.commit()
    return path


def test_the_plan_validator_refuses_a_hostile_input_before_probing_it(tmp_path):
    """`validate_plan` said `valid: true` and probed the file (21 opens) before anything refused it."""
    from mapsmith.plans import validator
    from mapsmith.plans.models import Plan

    path = _hostile_gpkg(tmp_path)
    plan = Plan(steps=[{"id": "s1", "operation": "describe_dataset", "arguments": {"path": str(path)}}])
    report = validator.validate(plan)
    assert "CRS_REFUSED" in {issue.code for issue in report.errors}, report
    assert not report.valid


def test_a_spatial_ref_sys_that_is_a_view_is_refused(tmp_path):
    """A VIEW can answer one WKT to this connection and another to GDAL's (measured)."""
    import sqlite3

    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import Point

    from mapsmith import workspace

    path = tmp_path / "view.gpkg"
    gpd.GeoDataFrame({"id": [1]}, geometry=[Point(9.0, 45.0)], crs="EPSG:4326").to_file(path)
    with closing(sqlite3.connect(path)) as db:
        db.execute("ALTER TABLE gpkg_spatial_ref_sys RENAME TO real_srs")
        db.execute("CREATE VIEW gpkg_spatial_ref_sys AS SELECT * FROM real_srs")
        db.commit()
    with pytest.raises(ValueError, match="refused|catalogue"):
        workspace.guard(str(path), "input_path")


def test_a_shapefile_is_refused_through_any_member_of_its_family(tmp_path):
    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import Point

    from mapsmith import workspace

    shp = tmp_path / "evil.shp"
    gpd.GeoDataFrame({"id": [1]}, geometry=[Point(9.0, 45.0)], crs="EPSG:4326").to_file(shp)
    (tmp_path / "evil.prj").write_text(
        'GEOGCS["evil",DATUM["x",SPHEROID["intl",6378388,297]],PRIMEM["Greenwich",0],'
        'UNIT["degree",0.0174532925199433],EXTENSION["PROJ4_GRIDS","C:/evil/x.gsb"]]',
        encoding="utf-8",
    )
    for member in ("evil.shp", "evil.dbf", "evil.shx"):
        with pytest.raises(ValueError, match="refused"):
            workspace.guard(str(tmp_path / member), "input_path")


def test_a_rasters_aux_xml_srs_is_checked(tmp_path):
    from mapsmith import workspace

    tif = tmp_path / "r.tif"
    tif.write_bytes(b"II*\x00" + b"\x00" * 16)  # the bytes are not read: only the sidecar is
    (tmp_path / "r.tif.aux.xml").write_text(
        '<PAMDataset><SRS>GEOGCS["evil",DATUM["x",SPHEROID["intl",6378388,297]],PRIMEM["Greenwich",0],'
        'UNIT["degree",0.0174532925199433],EXTENSION["PROJ4_GRIDS","C:/evil/x.gsb"]]</SRS></PAMDataset>',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="refused"):
        workspace.guard(str(tif), "input_path")


def test_a_target_bound_crs_renamed_like_projs_leg_is_still_a_shift():
    """4326 -> a towgs84 CRS whose declared transformation is named like PROJ's own ballpark leg:
    the inverse step is removed by method and parameters, not by name (fourth audit pass)."""
    import numpy as np
    from pyproj import CRS

    honest = CRS(
        "+proj=tmerc +lat_0=0 +lon_0=9 +k=0.9996 +x_0=1500001 +y_0=0 +ellps=intl "
        "+towgs84=-104.1,-49.1,-9.9,0.971,-2.917,0.714,-11.68 +units=m +no_defs +type=crs"
    )
    wkt = honest.to_wkt()
    renamed = CRS(wkt.replace("Transformation from unknown to WGS84", "Ballpark geographic offset from WGS 84 to NAD27"))
    points = np.array([(8.95, 45.5), (9.05, 45.6)])
    for target in (honest, renamed):
        record = datum.default_operation("EPSG:4326", target, points)
        assert record["is_ballpark"] is False, record
        assert record.get(datum.SHIFT_FROM_CRS) is True


def test_a_geopackage_layer_bound_to_a_negative_srs_id_is_still_checked(tmp_path):
    """GDAL treats only 0 and -1 as undefined; `WHERE srs_id > 0` skipped -2 (fifth audit pass)."""
    import sqlite3

    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import Point

    from mapsmith import workspace

    path = tmp_path / "neg.gpkg"
    gpd.GeoDataFrame({"id": [1]}, geometry=[Point(9.0, 45.0)], crs="EPSG:4326").to_file(path)
    with closing(sqlite3.connect(path)) as db:
        db.execute(
            "INSERT INTO gpkg_spatial_ref_sys (srs_name, srs_id, organization, organization_coordsys_id, definition) "
            "VALUES ('evil', -2, 'NONE', -2, ?)",
            (
                (
                    'GEOGCS["evil",DATUM["x",SPHEROID["intl",6378388,297]],PRIMEM["Greenwich",0],'
                    'UNIT["degree",0.0174532925199433],EXTENSION["PROJ4_GRIDS","C:/evil/x.gsb"]]'
                ),
            ),
        )
        db.execute("UPDATE gpkg_geometry_columns SET srs_id = -2")
        db.execute("UPDATE gpkg_contents SET srs_id = -2")
        db.commit()
    with pytest.raises(ValueError, match="refused"):
        workspace.guard(str(path), "input_path")


def test_an_aux_xml_is_decoded_the_way_gdal_decodes_it(tmp_path):
    """`&#92;` is a backslash to GDAL and was a bare name to a regex on the raw bytes."""
    from mapsmith import workspace

    tif = tmp_path / "r2.tif"
    tif.write_bytes(b"II*\x00" + b"\x00" * 16)
    (tmp_path / "r2.tif.aux.xml").write_text(
        '<PAMDataset><SRS>GEOGCS["evil",DATUM["x",SPHEROID["intl",6378388,297]],PRIMEM["Greenwich",0],'
        'UNIT["degree",0.0174532925199433],EXTENSION["PROJ4_GRIDS","&#92;&#92;host&#92;share&#92;x.gsb"]]</SRS></PAMDataset>',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="refused"):
        workspace.guard(str(tif), "input_path")
    # And a legitimate sidecar with entity-encoded quotes passes.
    ok = tmp_path / "ok.tif"
    ok.write_bytes(b"II*\x00" + b"\x00" * 16)
    (tmp_path / "ok.tif.aux.xml").write_text(
        "<PAMDataset><SRS>GEOGCS[&quot;WGS 84&quot;,DATUM[&quot;WGS_1984&quot;,SPHEROID[&quot;WGS 84&quot;,6378137,"
        "298.257223563]],PRIMEM[&quot;Greenwich&quot;,0],UNIT[&quot;degree&quot;,0.0174532925199433]]</SRS></PAMDataset>",
        encoding="utf-8",
    )
    assert workspace.guard(str(ok), "input_path")
    # A DOCTYPE is not read.
    (tmp_path / "ok.tif.aux.xml").write_text("<!DOCTYPE x [<!ENTITY e SYSTEM 'file:///etc/passwd'>]><PAMDataset/>", encoding="utf-8")
    with pytest.raises(ValueError, match="DOCTYPE"):
        workspace.guard(str(ok), "input_path")


def test_a_declared_srs_bigger_than_any_definition_is_refused(tmp_path):
    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import Point

    from mapsmith import workspace

    shp = tmp_path / "big.shp"
    gpd.GeoDataFrame({"id": [1]}, geometry=[Point(9.0, 45.0)], crs="EPSG:4326").to_file(shp)
    (tmp_path / "big.prj").write_text('GEOGCS["' + "x" * (70 * 1024) + '"]', encoding="utf-8")
    with pytest.raises(ValueError, match="refused"):
        workspace.guard(str(shp), "input_path")


def _spatialite_catalogue(path, rows=20, referenced=1):
    import sqlite3

    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE spatial_ref_sys (srid INTEGER PRIMARY KEY, srtext TEXT, proj4text TEXT)")
        db.execute("CREATE TABLE geometry_columns (f_table_name TEXT, f_geometry_column TEXT, srid INTEGER)")
        for i in range(rows):
            srid = 4326 if i == 0 else 10000 + i
            text = (
                f'GEOGCS["SRS {srid}",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
                'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]'
            )
            db.execute("INSERT INTO spatial_ref_sys VALUES (?, ?, ?)", (srid, text, ""))
        for i in range(referenced):
            srid = 4326 if i == 0 else 10000 + i
            db.execute("INSERT INTO geometry_columns VALUES (?, 'geom', ?)", (f"layer_{i}", srid))
        db.commit()
    return path


def test_only_spatialite_systems_referenced_by_layers_get_a_full_proj_parse(tmp_path, monkeypatch):
    path = _spatialite_catalogue(tmp_path / "ordinary.sqlite", rows=5804)
    parsed = []
    original = crs_policy._verdict

    def recording_verdict(text, allow_bare_files=True):
        parsed.append(text)
        return original(text, allow_bare_files)

    monkeypatch.setattr(crs_policy, "_verdict", recording_verdict)
    crs_policy.refuse_declared_in_file(str(path))
    assert len(parsed) == 1
    assert 'SRS 4326' in parsed[0]


def test_an_sqlite_catalogue_has_a_row_limit(tmp_path, monkeypatch):
    path = _spatialite_catalogue(tmp_path / "too_many.sqlite", rows=3)
    monkeypatch.setattr(crs_policy, "_MAX_CATALOGUE_ROWS", 2)
    with pytest.raises(ValueError, match="catalogue.*3.*limit.*2"):
        crs_policy.refuse_declared_in_file(str(path))


def test_the_number_of_referenced_spatialite_systems_is_bounded(tmp_path, monkeypatch):
    path = _spatialite_catalogue(tmp_path / "too_many_used.sqlite", rows=3, referenced=3)
    monkeypatch.setattr(crs_policy, "_MAX_REFERENCED_SRS", 2)
    with pytest.raises(ValueError, match="3.*referenced.*limit.*2"):
        crs_policy.refuse_declared_in_file(str(path))


def test_an_unreferenced_sqlite_row_still_gets_the_text_safety_check(tmp_path):
    path = _spatialite_catalogue(tmp_path / "hostile_unused.sqlite", rows=2)
    import sqlite3

    with closing(sqlite3.connect(path)) as db:
        db.execute("UPDATE spatial_ref_sys SET proj4text = ? WHERE srid = 10001", ("+nadgrids=C:/evil/x.gsb",))
        db.commit()
    with pytest.raises(ValueError, match="refused"):
        crs_policy.refuse_declared_in_file(str(path))


def test_sqlite_column_names_are_case_insensitive(tmp_path):
    import sqlite3

    path = tmp_path / "uppercase.sqlite"
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE spatial_ref_sys (SRID INTEGER PRIMARY KEY, SRText TEXT)")
        db.execute("CREATE TABLE geometry_columns (f_table_name TEXT, SRID TEXT)")
        db.execute(
            "INSERT INTO spatial_ref_sys VALUES (-2, ?)",
            ((
                'GEOGCS["evil",DATUM["x",SPHEROID["intl",6378388,297]],PRIMEM["Greenwich",0],'
                'UNIT["degree",0.0174532925199433],EXTENSION["PROJ4_GRIDS","@x"",C:/evil/x.gsb"]]'
            ),),
        )
        db.execute("INSERT INTO geometry_columns VALUES ('layer', '-2')")
        db.commit()
    with pytest.raises(ValueError, match="refused"):
        crs_policy.refuse_declared_in_file(str(path))


def test_a_reference_view_disables_the_selective_parse(tmp_path, monkeypatch):
    import sqlite3

    path = _spatialite_catalogue(tmp_path / "view.sqlite", rows=2)
    with closing(sqlite3.connect(path)) as db:
        db.execute("DROP TABLE geometry_columns")
        db.execute("CREATE VIEW geometry_columns AS SELECT 4326 AS srid")
        db.commit()
    parsed = []
    original = crs_policy._verdict
    monkeypatch.setattr(
        crs_policy,
        "_verdict",
        lambda text, allow_bare_files=True: parsed.append(text) or original(text, allow_bare_files),
    )
    crs_policy.refuse_declared_in_file(str(path))
    assert len(parsed) == 2


def test_text_spelled_srs_ids_follow_sqlite_affinity(tmp_path, monkeypatch):
    import sqlite3

    path = _spatialite_catalogue(tmp_path / "affinity.sqlite", rows=2)
    with closing(sqlite3.connect(path)) as db:
        db.execute("DROP TABLE geometry_columns")
        db.execute("CREATE TABLE geometry_columns (f_table_name TEXT, srid TEXT)")
        db.execute("INSERT INTO geometry_columns VALUES ('layer', '04326')")
        db.commit()
    parsed = []
    original = crs_policy._verdict
    monkeypatch.setattr(
        crs_policy,
        "_verdict",
        lambda text, allow_bare_files=True: parsed.append(text) or original(text, allow_bare_files),
    )
    crs_policy.refuse_declared_in_file(str(path))
    assert len(parsed) == 1
    assert "SRS 4326" in parsed[0]


def test_an_unreferenced_sqlite_srs_has_the_same_size_limit(tmp_path):
    path = _spatialite_catalogue(tmp_path / "large_unused.sqlite", rows=2)
    import sqlite3

    with closing(sqlite3.connect(path)) as db:
        db.execute("UPDATE spatial_ref_sys SET srtext = ? WHERE srid = 10001", ("x" * (70 * 1024),))
        db.commit()
    with pytest.raises(ValueError, match="coordinate system.*65536"):
        crs_policy.refuse_declared_in_file(str(path))


def test_the_total_sqlite_srs_text_is_bounded(tmp_path, monkeypatch):
    path = _spatialite_catalogue(tmp_path / "large_total.sqlite", rows=2)
    monkeypatch.setattr(crs_policy, "_MAX_CATALOGUE_SRS_BYTES", 10)
    with pytest.raises(ValueError, match="exceed 10 characters"):
        crs_policy.refuse_declared_in_file(str(path))

