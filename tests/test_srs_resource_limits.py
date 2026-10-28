"""Resource bounds for reading a declared SRS before GDAL opens the dataset."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from mapsmith import srs_sources, workspace

_OVER_32_MIB = 33 * 1024 * 1024


@pytest.fixture
def ws(monkeypatch, tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setenv("MAPSMITH_WORKSPACE", str(root))
    return root


@pytest.mark.parametrize(
    ("name", "start"),
    [
        ("large.geojson", b'{"type":"FeatureCollection","features":[]}'),
        ("large.txt", b"id,WKT\n1,POINT (12 45)\n"),
    ],
)
def test_a_large_textual_input_is_refused_before_it_is_read_whole(ws, monkeypatch, name, start):
    """A 33 MiB input must meet the 32 MiB refusal, not a whole-file read."""
    path = ws / name
    path.write_bytes(start + b" " * _OVER_32_MIB)
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("the file was read whole before its size was refused")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    with pytest.raises(ValueError, match="larger than MapSmith reads"):
        workspace.guard(str(path), "input_path")
    assert calls == []


def test_a_large_zip_member_is_refused_before_it_is_read_whole(ws, monkeypatch):
    """The member-size check must precede ZipFile.read, not follow it."""
    path = ws / "large.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("large.geojson", b'{"type":"FeatureCollection","features":[]}' + b" " * _OVER_32_MIB)
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("the zip member was read whole before its size was refused")

    monkeypatch.setattr(zipfile.ZipFile, "read", forbidden)
    with pytest.raises(ValueError, match="larger than MapSmith reads"):
        workspace.guard(str(path), "input_path")
    assert calls == []


def test_a_large_prj_is_refused_before_it_is_read_whole(ws, monkeypatch):
    path = ws / "large.prj"
    path.write_text('GEOGCS["WGS 84"]' + " " * (70 * 1024), encoding="utf-8")
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("the .prj was read whole before its size was refused")

    monkeypatch.setattr(Path, "read_text", forbidden)
    with pytest.raises(ValueError, match="larger than MapSmith reads"):
        workspace.guard(str(path), "input_path")
    assert calls == []


def test_a_large_aux_xml_is_refused_before_it_is_read_whole(ws, monkeypatch):
    path = ws / "r.tif"
    path.write_bytes(b"II*\x00" + bytes(16))
    aux = ws / "r.tif.aux.xml"
    aux.write_text("<PAMDataset><SRS>" + "x" * (70 * 1024) + "</SRS></PAMDataset>", encoding="utf-8")
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("the .aux.xml was read whole before its size was refused")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    with pytest.raises(ValueError, match="larger than MapSmith reads"):
        workspace.guard(str(path), "input_path")
    assert calls == []


def test_an_oversized_srsname_element_is_refused_during_streaming(ws):
    path = ws / "large.gml"
    path.write_text(
        '<?xml version="1.0"?><ogr:FeatureCollection xmlns:ogr="http://ogr.maptools.org/" '
        'xmlns:gml="http://www.opengis.net/gml"><gml:featureMember><ogr:e><gml:Point srsName="EPSG:4326">'
        "<gml:SRSName>" + "x" * (70 * 1024) + "</gml:SRSName>"
        "</gml:Point></ogr:e></gml:featureMember></ogr:FeatureCollection>",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="larger than MapSmith reads"):
        srs_sources.read_xml(str(path), str(path))
