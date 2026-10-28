"""A permissive CRS verdict must not survive a same-stat content replacement."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing

import pytest

from mapsmith import crs_policy

HOSTILE = "+proj=longlat +ellps=clrk66 +nadgrids=C:/evil/x.gsb +type=crs"
HONEST = "+proj=longlat +datum=WGS84 +no_defs".ljust(len(HOSTILE))


def _gpkg_with_definition(path, definition):
    gpd = pytest.importorskip("geopandas")
    from shapely.geometry import Point

    gpd.GeoDataFrame({"id": [1]}, geometry=[Point(12.0, 45.0)], crs="EPSG:4326").to_file(path)
    with closing(sqlite3.connect(path)) as db:
        db.execute("UPDATE gpkg_spatial_ref_sys SET definition = ? WHERE srs_id = 4326", (definition,))
        db.commit()
    return path


def test_an_allowed_crs_is_rechecked_after_same_size_same_mtime_change(tmp_path, monkeypatch):
    path = _gpkg_with_definition(tmp_path / "changed.gpkg", HONEST)
    monkeypatch.setenv("MAPSMITH_WORKSPACE", str(tmp_path))
    crs_policy.refuse_declared_in_file(str(path))

    before = path.stat()
    fingerprint_before = crs_policy._fingerprint(str(path))
    with closing(sqlite3.connect(path)) as db:
        db.execute("UPDATE gpkg_spatial_ref_sys SET definition = ? WHERE srs_id = 4326", (HOSTILE,))
        db.commit()
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)

    assert crs_policy._fingerprint(str(path)) == fingerprint_before
    with pytest.raises(ValueError, match="refused"):
        crs_policy.refuse_declared_in_file(str(path))


def test_a_refusal_still_uses_the_cache(tmp_path, monkeypatch):
    path = _gpkg_with_definition(tmp_path / "hostile.gpkg", HOSTILE)
    monkeypatch.setenv("MAPSMITH_WORKSPACE", str(tmp_path))
    with pytest.raises(ValueError, match="refused"):
        crs_policy.refuse_declared_in_file(str(path))

    calls = []

    def forbidden(path):
        calls.append(path)
        raise AssertionError("a cached refusal read the catalogue again")

    monkeypatch.setattr(crs_policy, "declared", forbidden)
    with pytest.raises(ValueError, match="refused"):
        crs_policy.refuse_declared_in_file(str(path))
    assert calls == []
