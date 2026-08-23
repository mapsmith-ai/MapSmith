"""Deterministic verification: checks recorded in manifests, critical failures raise."""

import json

import geopandas as gpd
import pytest
from shapely.geometry import Point, Polygon

from mapsmith import verify
from mapsmith.engines import vector


@pytest.fixture()
def points_gpkg(tmp_path):
    gdf = gpd.GeoDataFrame(
        {"name": ["a", "b"]},
        geometry=[Point(9.19, 45.46), Point(9.20, 45.47)],
        crs="EPSG:4326",
    )
    path = tmp_path / "points.gpkg"
    gdf.to_file(path)
    return path


def test_buffer_manifest_contains_passed_verification(points_gpkg, tmp_path):
    out = tmp_path / "buffered.gpkg"
    result = vector.buffer(str(points_gpkg), 250.0, str(out))
    assert result["verified"] is True
    manifest = json.loads((tmp_path / "buffered.gpkg.provenance.json").read_text())
    names = {c["name"] for c in manifest["verification"]}
    assert {"crs_present", "crs_matches", "geometry_valid", "feature_count_exact"} <= names
    assert all(c["passed"] for c in manifest["verification"])


def test_invalid_geometry_fails_critical_check(tmp_path):
    bowtie = Polygon([(0, 0), (1, 1), (1, 0), (0, 1)])  # self-intersecting
    gdf = gpd.GeoDataFrame({"id": [1]}, geometry=[bowtie], crs="EPSG:4326")
    path = tmp_path / "bowtie.gpkg"
    gdf.to_file(path)
    checks = verify.verify_vector_output(str(path))
    failed = [c for c in checks if c.name == "geometry_valid" and not c.passed]
    assert failed, "the invalid geometry must be detected"
    with pytest.raises(verify.VerificationError, match="geometry_valid"):
        verify.enforce(checks, "test_op")


def test_count_mismatch_raises(points_gpkg):
    checks = verify.verify_vector_output(str(points_gpkg), expect_count=99)
    with pytest.raises(verify.VerificationError, match="feature_count_exact"):
        verify.enforce(checks, "test_op")


def test_non_critical_failure_does_not_raise(points_gpkg):
    # extent check is non-critical: a failing bounds check alone must not raise
    checks = verify.verify_vector_output(
        str(points_gpkg), within_bounds=(0.0, 0.0, 0.1, 0.1)
    )
    extent = [c for c in checks if c.name == "extent_within_expected"]
    assert extent and not extent[0].passed
    verify.enforce(checks, "test_op")  # no exception


def test_reproject_verifies_target_crs(points_gpkg, tmp_path):
    out = tmp_path / "utm.gpkg"
    result = vector.reproject(str(points_gpkg), "EPSG:32632", str(out))
    assert result["verified"] is True
    manifest = json.loads((tmp_path / "utm.gpkg.provenance.json").read_text())
    crs_checks = [c for c in manifest["verification"] if c["name"] == "crs_matches"]
    assert crs_checks and crs_checks[0]["passed"]


def test_a_manifest_path_does_not_depend_on_the_host(tmp_path):
    """Two correct manifests for the same run must differ in no field but time.

    `str(path)` recorded the host's separator, so the same operation on the same
    bytes produced a backslash path on Windows and a forward-slash one elsewhere
    — a difference that describes nothing about the computation, and one that
    gives any consumer keying on the path two entries for one file (#30). The
    manifest is meant to be held next to someone else's; this is the field that
    stopped that working.
    """
    from pathlib import PureWindowsPath

    from mapsmith.provenance import InputRecord, posix_path

    assert posix_path(PureWindowsPath(r"data\wells.gpkg")) == "data/wells.gpkg"
    assert posix_path(PureWindowsPath(r"C:\work\dem.tif")) == "C:/work/dem.tif"
    # Only the separator is normalised: rewriting an absolute path to a relative
    # one would misstate what actually ran.
    assert posix_path("/srv/data/dem.tif") == "/srv/data/dem.tif"
    assert posix_path("data/wells.gpkg") == "data/wells.gpkg"

    sorgente = tmp_path / "wells.gpkg"
    sorgente.write_bytes(b"not really a geopackage")
    registrato = InputRecord.from_path(sorgente)
    assert "\\" not in registrato.path
