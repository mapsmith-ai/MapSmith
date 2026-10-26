"""The coordinate system a dataset declares is read before GDAL, for every format MapSmith accepts.

Measured on 2026-10-26 with a named pipe standing in for a grid file, under a
workspace: a GeoJSON ``crs`` member, a GML ``srsName`` and a FlatGeobuf header
made GDAL open the "grid" 21 to 32 times before MapSmith refused the file. The
path gate now reads the declared SRS itself, from the bytes, and under a
workspace refuses a format it does not recognise. These tests use a grid path
outside the workspace that does not exist: what matters is that the gate
refuses on the text, before anything parses it.
"""
from __future__ import annotations

import json
import zipfile

import pytest

from mapsmith import crs_policy, srs_sources, workspace

gpd = pytest.importorskip("geopandas")
from shapely.geometry import Point

GRID = "C:/evil/x.gsb"
PROJ_HOSTILE = f"+proj=longlat +ellps=clrk66 +nadgrids={GRID} +type=crs"
WKT_HOSTILE = (
    'GEOGCS["evil",DATUM["x",SPHEROID["clrk66",6378206.4,294.9786982]],PRIMEM["Greenwich",0],'
    f'UNIT["degree",0.0174532925199433],EXTENSION["PROJ4_GRIDS","{GRID}"]]'
)


@pytest.fixture
def ws(monkeypatch, tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setenv("MAPSMITH_WORKSPACE", str(root))
    return root


def _feature_collection(**extra):
    return {
        "type": "FeatureCollection",
        **extra,
        "features": [{"type": "Feature", "properties": {"id": 1}, "geometry": {"type": "Point", "coordinates": [12, 45]}}],
    }


def _write_json(path, document):
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _gml(path, srs_name):
    path.write_text(
        '<?xml version="1.0"?><ogr:FeatureCollection xmlns:ogr="http://ogr.maptools.org/" '
        'xmlns:gml="http://www.opengis.net/gml"><gml:featureMember><ogr:e><ogr:geometryProperty>'
        f'<gml:Point srsName="{srs_name}"><gml:coordinates>12,45</gml:coordinates></gml:Point>'
        '</ogr:geometryProperty><ogr:id>1</ogr:id></ogr:e></gml:featureMember></ogr:FeatureCollection>',
        encoding="utf-8",
    )
    return path


def _points(crs="EPSG:4326"):
    return gpd.GeoDataFrame({"id": [1]}, geometry=[Point(12.0, 45.0)], crs=crs)


# --- the formats GDAL used to read first ---------------------------------------------------


def hostile_geojson_name(d):
    return _write_json(d / "a.geojson", _feature_collection(crs={"type": "name", "properties": {"name": PROJ_HOSTILE}}))


def hostile_geojson_link(d):
    return _write_json(d / "a.json", _feature_collection(crs={"type": "link", "properties": {"href": GRID, "type": "proj4"}}))


def hostile_json_fg(d):
    return _write_json(d / "a.json", _feature_collection(coordRefSys=PROJ_HOSTILE))


def hostile_esri_json(d):
    return _write_json(d / "a.json", {"spatialReference": {"wkt": WKT_HOSTILE}, "features": []})


def hostile_geojson_escaped_key(d):
    # `\u0063rs` is "crs" to any JSON parser, GDAL's included; a scan for the
    # literal key would not see it.
    text = json.dumps(_feature_collection(crs={"type": "name", "properties": {"name": PROJ_HOSTILE}}))
    path = d / "a.geojson"
    path.write_text(text.replace('"crs"', '"\\u0063rs"'), encoding="utf-8")
    return path


def hostile_geojson_seq(d):
    record = json.dumps({"type": "Feature", "crs": {"type": "name", "properties": {"name": PROJ_HOSTILE}},
                         "properties": {}, "geometry": {"type": "Point", "coordinates": [12, 45]}})
    path = d / "a.geojsons"
    path.write_text("\x1e" + record + "\n\x1e" + record + "\n", encoding="utf-8")
    return path


def hostile_gml(d):
    return _gml(d / "a.gml", PROJ_HOSTILE)


def hostile_gml_gfs(d):
    path = _gml(d / "a.gml", "EPSG:4326")
    (d / "a.gfs").write_text(f"<GMLFeatureClassList><GMLFeatureClass><SRSName>{PROJ_HOSTILE}</SRSName>"
                             "</GMLFeatureClass></GMLFeatureClassList>", encoding="utf-8")
    return path


def hostile_flatgeobuf(d):
    from pyproj import CRS

    path = d / "a.fgb"
    _points(CRS(PROJ_HOSTILE)).to_file(path, driver="FlatGeobuf")
    return path


def hostile_csv_prj(d):
    (d / "a.csv").write_text("id,WKT\n1,POINT (12 45)\n", encoding="utf-8")
    (d / "a.prj").write_text(WKT_HOSTILE, encoding="utf-8")
    return d / "a.csv"


def hostile_zip(d):
    staging = d.parent / "staging"
    staging.mkdir()
    _points().to_file(staging / "a.shp")
    (staging / "a.prj").write_text(WKT_HOSTILE, encoding="utf-8")
    path = d / "a.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for member in staging.iterdir():
            archive.write(member, member.name)
    return path


def hostile_folder(d):
    folder = d / "layers"
    folder.mkdir()
    _points().to_file(folder / "a.shp")
    (folder / "a.prj").write_text(WKT_HOSTILE, encoding="utf-8")
    return folder


def hostile_geotiff(d):
    rasterio = pytest.importorskip("rasterio")
    import numpy as np
    from pyproj import CRS
    from rasterio.transform import from_origin

    path = d / "a.tif"
    with rasterio.open(path, "w", driver="GTiff", width=2, height=2, count=1, dtype="uint8",
                       crs=CRS(PROJ_HOSTILE), transform=from_origin(12, 45, 0.1, 0.1)) as dst:
        dst.write(np.ones((1, 2, 2), dtype="uint8"))
    for aux in d.glob("*.aux.xml"):
        aux.unlink()  # the geokeys alone, not the sidecar MapSmith already read
    return path


HOSTILE = [
    hostile_geojson_name, hostile_geojson_link, hostile_json_fg, hostile_esri_json, hostile_geojson_escaped_key,
    hostile_geojson_seq, hostile_gml, hostile_gml_gfs, hostile_flatgeobuf, hostile_csv_prj, hostile_zip,
    hostile_folder, hostile_geotiff,
]


@pytest.mark.parametrize("make", HOSTILE, ids=lambda f: f.__name__.removeprefix("hostile_"))
def test_a_hostile_declared_crs_is_refused_by_the_path_gate(ws, make):
    path = make(ws)
    with pytest.raises(ValueError, match="refused"):
        workspace.guard(str(path), "input_path")


@pytest.mark.parametrize("make", [hostile_geojson_name, hostile_gml, hostile_flatgeobuf],
                         ids=["geojson", "gml", "flatgeobuf"])
def test_gdal_really_reads_these_crs_so_the_refusal_is_the_gates(tmp_path, make):
    """Counterpart: GDAL, left alone, hands PROJ a CRS that names the grid."""
    pyogrio = pytest.importorskip("pyogrio")
    path = make(tmp_path)
    crs = pyogrio.read_info(str(path)).get("crs") or ""
    from pyproj import CRS

    assert GRID in CRS(crs).to_wkt() or "x.gsb" in CRS(crs).to_wkt()


# --- honest files of the same formats still pass ---------------------------------------------


def honest_files(d):
    _write_json(d / "a.geojson", _feature_collection())
    _write_json(d / "b.geojson", _feature_collection(crs={"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}}))
    _write_json(d / "c.json", _feature_collection(coordRefSys="[EPSG:4326]"))
    # A feature attribute called "crs" is data, not a coordinate system.
    _write_json(d / "d.geojson", {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"crs": "survey notes: +nadgrids=C:/x"}, "geometry": None}]})
    _gml(d / "e.gml", "http://www.opengis.net/gml/srs/epsg.xml#4326")
    _gml(d / "f.gml", "urn:ogc:def:crs:EPSG::4326")
    _points().to_file(d / "g.fgb", driver="FlatGeobuf")
    (d / "h.csv").write_text("id,WKT\n1,POINT (12 45)\n", encoding="utf-8")
    (d / "i.kml").write_text('<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"><Placemark>'
                             "<Point><coordinates>12,45</coordinates></Point></Placemark></kml>", encoding="utf-8")
    folder = d / "layers"
    folder.mkdir()
    _points().to_file(folder / "j.shp")
    staging = d.parent / "staging_ok"
    staging.mkdir()
    _points().to_file(staging / "k.shp")
    with zipfile.ZipFile(d / "k.zip", "w") as archive:
        for member in staging.iterdir():
            archive.write(member, member.name)
    rasterio = pytest.importorskip("rasterio")
    import numpy as np
    from rasterio.transform import from_origin

    with rasterio.open(d / "l.tif", "w", driver="GTiff", width=2, height=2, count=1, dtype="uint8",
                       crs="EPSG:32633", transform=from_origin(500000, 4500000, 10, 10)) as dst:
        dst.write(np.ones((1, 2, 2), dtype="uint8"))
    return [p for p in d.iterdir() if p.suffix not in (".prj", ".shx", ".dbf", ".cpg")]


def test_honest_files_of_the_same_formats_still_pass(ws):
    for path in honest_files(ws):
        assert workspace.guard(str(path), "input_path") == str(path), path


# --- what MapSmith does not recognise ---------------------------------------------------------


def test_an_unrecognised_format_is_refused_under_a_workspace_and_passes_without(monkeypatch, tmp_path):
    path = tmp_path / "ws" / "scene.img"
    path.parent.mkdir()
    path.write_bytes(b"\x89HFA\x00\x01\x02\x03" + bytes(range(256)) * 4)
    monkeypatch.setenv("MAPSMITH_WORKSPACE", str(path.parent))
    with pytest.raises(ValueError, match="Convert the file"):
        workspace.guard(str(path), "input_path")
    monkeypatch.delenv("MAPSMITH_WORKSPACE")
    assert workspace.guard(str(path), "input_path") == str(path)


def test_a_zip_holding_a_geopackage_is_refused_under_a_workspace(ws):
    staging = ws.parent / "staging_gpkg"
    staging.mkdir()
    _points().to_file(staging / "a.gpkg")
    path = ws / "a.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.write(staging / "a.gpkg", "a.gpkg")
    with pytest.raises(ValueError, match="unzip it"):
        workspace.guard(str(path), "input_path")


def test_an_xml_doctype_is_refused_under_a_workspace(ws):
    path = ws / "a.gml"
    path.write_text('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e "y">]><x srsName="EPSG:4326"/>', encoding="utf-8")
    with pytest.raises(ValueError, match="DOCTYPE"):
        workspace.guard(str(path), "input_path")


def test_the_content_decides_the_format_not_the_name(ws):
    """A GeoJSON called `.parquet` or `.dbf` is opened by GDAL as GeoJSON."""
    for name in ("a.parquet", "a.dbf", "a.tif"):
        path = _write_json(ws / name, _feature_collection(crs={"type": "name", "properties": {"name": PROJ_HOSTILE}}))
        assert srs_sources.sniff(path) == "json"
        with pytest.raises(ValueError, match="refused"):
            workspace.guard(str(path), "input_path")


def test_the_verdict_follows_the_file_when_it_changes(ws):
    path = _write_json(ws / "a.geojson", _feature_collection())
    workspace.guard(str(path), "input_path")
    _write_json(path, _feature_collection(crs={"type": "name", "properties": {"name": PROJ_HOSTILE}}))
    with pytest.raises(ValueError, match="refused"):
        workspace.guard(str(path), "input_path")


def test_a_cached_verdict_does_not_read_the_file_again(ws, monkeypatch):
    path = _write_json(ws / "a.geojson", _feature_collection())
    crs_policy.refuse_declared_in_file(str(path))
    calls = []
    monkeypatch.setattr(srs_sources, "read_json", lambda raw: calls.append(1) or srs_sources.Declared("json"))
    crs_policy.refuse_declared_in_file(str(path))
    crs_policy.refuse_declared_in_file(str(path))
    assert calls == []
