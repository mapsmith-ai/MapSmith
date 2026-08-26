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
    gives any consumer keying on the path two entries for one file (#30).

    The assertion is written with a path this host BUILT, not with a literal,
    and that is the whole subtlety. A first version asserted that a literal
    Windows string normalises everywhere, and CI on Linux disagreed — correctly:
    there, a backslash is a legal character in a filename, so rewriting one
    would corrupt a real path. Normalisation happens on the host that has
    separators, which is the only host that can produce the problem.
    """
    from pathlib import PurePath, PureWindowsPath

    from mapsmith.provenance import InputRecord, posix_path

    assert posix_path(PurePath("data") / "wells.gpkg") == "data/wells.gpkg"
    assert posix_path(PurePath("/srv/data") / "dem.tif").endswith("/srv/data/dem.tif")
    # The Windows flavour explicitly, so the behaviour is pinned from any host.
    assert PureWindowsPath(r"data\wells.gpkg").as_posix() == "data/wells.gpkg"
    assert PureWindowsPath(r"C:\work\dem.tif").as_posix() == "C:/work/dem.tif"
    # Only the separator: rewriting an absolute path to a relative one, or the
    # reverse, would misstate what actually ran.
    assert posix_path("data/wells.gpkg") == "data/wells.gpkg"

    source = tmp_path / "wells.gpkg"
    source.write_bytes(b"not really a geopackage")
    recorded = InputRecord.from_path(source)
    assert recorded.path == PurePath(source).as_posix()


def test_every_manifest_mapsmith_writes_conforms_to_the_spec(tmp_path):
    """MapSmith is an implementation of the manifest spec, not its definition.

    The day our manifest stops passing our own published validator, CI says so
    here — instead of a reader finding out. The validator is a vendored copy of
    the spec repository's stdlib-only implementation; the record under test is
    a REAL one, written by a real writer on a real file, because a hand-built
    record would validate the test's idea of a manifest rather than MapSmith's.
    """
    import json
    import sys
    from pathlib import Path

    import geopandas as gpd

    sys.path.insert(0, str(Path(__file__).parent / "data"))
    from manifest_spec_validator import problems

    from mapsmith.engines import vector

    source = tmp_path / "wells.parquet"
    gpd.GeoDataFrame(
        {"id": [1, 2]},
        geometry=gpd.GeoSeries.from_wkt(["POINT (500000 5000000)", "POINT (500100 5000100)"]),
        crs="EPSG:32632",
    ).to_parquet(source)
    out = tmp_path / "wells_100m.parquet"
    vector.buffer(str(source), 100.0, str(out))

    record = json.loads((tmp_path / "wells_100m.parquet.provenance.json")
                        .read_text(encoding="utf-8"))
    assert problems(record) == [], "a MapSmith manifest no longer conforms to the spec"
    assert record["spec_version"].startswith("1.")

    # The record must describe the bytes it sits beside — recomputed here from
    # the file, not trusted from the record.
    import hashlib

    assert record["output"]["path"].endswith("wells_100m.parquet")
    assert "\\" not in record["output"]["path"]
    assert record["output"]["sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()


def test_every_writing_operation_conforms_to_the_spec(tmp_path):
    """The test above proves one operation conforms. This one proves they all do.

    The catalog is the list, so an operation added tomorrow is covered the day
    it is added — which is the point: the previous version of this file
    validated `buffer_layer` and nothing else, and four raster operations
    shipped without anyone checking whether their manifests were still
    manifests. They were, and that was luck rather than a control.

    Operations behind an absent extra are skipped rather than silently passed,
    and the test says how many it actually validated: a conformance test that
    quietly checks nothing is worse than no conformance test.
    """
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent / "data"))
    from manifest_spec_validator import problems

    from mapsmith import catalog
    from mapsmith.plans.registry import BINDINGS

    fixtures = _spec_fixtures(tmp_path)
    validated: list[str] = []
    skipped: list[str] = []
    for entry in catalog.OPERATIONS:
        if entry["status"] != "available":
            continue
        name = entry["name"]
        binding = BINDINGS.get(name)
        if binding is None or binding.output_arg is None:
            continue  # readers write no manifest
        call = fixtures.get(name)
        if call is None:
            skipped.append(name)
            continue
        try:
            result = call()
        except ImportError:
            skipped.append(name)
            continue
        record = json.loads(Path(result["provenance"]).read_text(encoding="utf-8"))
        assert problems(record) == [], f"{name} writes a manifest the spec rejects"
        assert record["operation"] == name
        assert record["spec_version"].startswith("1."), name
        validated.append(name)

    assert len(validated) >= 32, (
        f"only {len(validated)} operations were validated ({sorted(validated)}); "
        f"no fixture for {sorted(skipped)} — add one rather than let coverage rot"
    )


def _spec_fixtures(tmp_path):
    """One real call per writing operation, for the conformance sweep.

    Real calls on real files: a hand-built record would validate the test's
    idea of a manifest rather than MapSmith's.
    """
    import geopandas as gpd
    from shapely.geometry import LineString, Point, Polygon

    from mapsmith.engines import vector

    crs = "EPSG:32632"
    square = Polygon([(0, 0), (100, 0), (100, 100), (0, 100)])
    other = Polygon([(50, 50), (150, 50), (150, 150), (50, 150)])
    layer = tmp_path / "a.parquet"
    gpd.GeoDataFrame({"k": ["x"], "v": [1]}, geometry=[square], crs=crs).to_parquet(layer)
    second = tmp_path / "b.parquet"
    gpd.GeoDataFrame({"j": [2]}, geometry=[other], crs=crs).to_parquet(second)
    points = tmp_path / "p.parquet"
    gpd.GeoDataFrame(
        {"n": [1, 2]}, geometry=[Point(10, 10), Point(90, 90)], crs=crs
    ).to_parquet(points)

    def out(name: str) -> str:
        return str(tmp_path / name)

    fixtures = {
        "buffer_layer": lambda: vector.buffer(str(layer), 10.0, out("buf.parquet")),
        "clip_layer": lambda: vector.clip(str(layer), str(second), out("clip.parquet")),
        "overlay_layers": lambda: vector.overlay(
            str(layer), str(second), out("ov.parquet")
        ),
        "dissolve_layer": lambda: vector.dissolve(str(layer), out("dis.parquet"), by="k"),
        "nearest_join": lambda: vector.nearest_join(
            str(points), str(layer), out("near.parquet")
        ),
        "explode_layer": lambda: vector.explode(str(layer), out("exp.parquet")),
        "measure_area": lambda: vector.measure_area(str(layer), out("area.parquet")),
        "merge_layers": lambda: vector.merge(
            [str(layer), str(second)], out("merge.parquet")
        ),
        "simplify_layer": lambda: vector.simplify(str(layer), 1.0, out("simp.parquet")),
        "centroid_layer": lambda: vector.centroid(str(layer), out("cent.parquet")),
        "convert_format": lambda: vector.convert(str(layer), out("conv.gpkg")),
        "reproject_layer": lambda: vector.reproject(
            str(layer), "EPSG:4326", out("rep.parquet")
        ),
        "spatial_join": lambda: vector.spatial_join(
            str(points), str(layer), out("sj.parquet")
        ),
    }

    # --- the tier-A operations, whose fixtures are the traps' own -----------
    table = tmp_path / "table.csv"
    table.write_text("k,v\nx,10\n", encoding="utf-8")
    keyed = tmp_path / "keyed.parquet"
    gpd.GeoDataFrame({"k": ["x"]}, geometry=[square], crs=crs).to_parquet(keyed)
    weighted = tmp_path / "weighted.parquet"
    gpd.GeoDataFrame(
        {"value": [20.0, 1.0], "weight": [1000, 99000]},
        geometry=[square, other],
        crs=crs,
    ).to_parquet(weighted)
    dms = tmp_path / "dms.csv"
    dms.write_text("id,lat,lon\n1,41.89,12.49\n", encoding="utf-8")
    climbing = tmp_path / "climbing.parquet"
    gpd.GeoDataFrame(
        {"i": [1]}, geometry=[LineString([(0, 0, 0), (400, 0, 300)])], crs=crs
    ).to_parquet(climbing)

    fixtures.update({
        "join_table": lambda: vector.join_table(
            str(keyed), str(table), out("joined.parquet"), on="k"
        ),
        "measure_length": lambda: vector.measure_length(
            str(climbing), out("length.parquet"), method="3d"
        ),
        "aggregate_weighted": lambda: vector.aggregate_weighted(
            str(weighted), out("agg.parquet"),
            value_column="value", weight_column="weight",
        ),
        "parse_coordinates": lambda: vector.parse_coordinates(
            str(dms), out("parsed.parquet"),
            latitude_columns="lat", longitude_columns="lon",
        ),
        "point_on_surface": lambda: vector.point_on_surface(
            str(layer), out("pos.parquet")
        ),
        "hull_layer": lambda: vector.hull(str(layer), out("hull.parquet")),
        "validate_geometry": lambda: vector.validate_geometry(
            str(layer), out("valid.parquet")
        ),
        "count_in_polygons": lambda: vector.count_in_polygons(
            str(points), str(layer), out("counts.parquet")
        ),
    })

    try:
        import numpy as np
        import rasterio
        from rasterio.transform import from_origin

        from mapsmith.engines import raster
    except ImportError:
        return fixtures

    grid = tmp_path / "grid.tif"
    with rasterio.open(
        grid, "w", driver="GTiff", height=4, width=4, count=2, dtype="int16",
        crs=crs, transform=from_origin(0, 100, 25, 25), nodata=-9999,
    ) as ds:
        ds.write(np.arange(16, dtype="int16").reshape(4, 4), 1)
        ds.write(np.arange(16, 32, dtype="int16").reshape(4, 4), 2)
    query = (
        f"SELECT * FROM read_parquet('{str(layer).replace(chr(92), '/')}')"
    )
    from mapsmith.engines import duckdb_engine

    fixtures["run_sql"] = lambda: duckdb_engine.run_sql(query, out("sql.parquet"))

    fixtures.update({
        "resample_raster": lambda: raster.resample(
            str(grid), out("res.tif"), 50, "nearest"
        ),
        "clip_raster": lambda: raster.clip_raster(
            str(grid), str(layer), out("clipr.tif")
        ),
        "reclassify_raster": lambda: raster.reclassify(
            str(grid), out("rc.tif"), ["0:8:1", "8:40:2"]
        ),
        "band_math": lambda: raster.band_math(str(grid), out("bm.tif"), "b2 - b1"),
        "zonal_statistics": lambda: raster.zonal_statistics(
            str(grid), str(layer), out("zs.parquet"), stats=["mean"]
        ),
    })

    try:
        from mapsmith.engines import whitebox_engine
    except ImportError:
        return fixtures

    # A tilted plane with a single low corner: enough terrain for the
    # derivatives and the hydrology to have something to route.
    rows, cols = 24, 24
    yy, xx = np.mgrid[0:rows, 0:cols]
    surface = (100.0 + xx * 2.0 + yy * 1.0).astype("float32")
    dem = tmp_path / "dem.tif"
    with rasterio.open(
        dem, "w", driver="GTiff", height=rows, width=cols, count=1, dtype="float32",
        crs=crs, transform=from_origin(0, rows * 10.0, 10, 10), nodata=-9999.0,
    ) as ds:
        ds.write(surface, 1)
    pour = tmp_path / "pour.parquet"
    gpd.GeoDataFrame(
        {"id": [1]}, geometry=[Point(5.0, 5.0)], crs=crs
    ).to_parquet(pour)
    fixtures.update({
        "hillshade": lambda: whitebox_engine.hillshade(str(dem), out("hs.tif")),
        "slope": lambda: whitebox_engine.slope(str(dem), out("slope.tif")),
        "aspect": lambda: whitebox_engine.aspect(str(dem), out("aspect.tif")),
        "flow_accumulation": lambda: whitebox_engine.flow_accumulation(
            str(dem), out("facc.tif")
        ),
        "watershed": lambda: whitebox_engine.watershed(
            str(dem), str(pour), out("ws.tif")
        ),
        "focal_statistics": lambda: whitebox_engine.focal_statistics(
            str(dem), out("focal.tif"), statistic="mean", window=3
        ),
        "extract_streams": lambda: whitebox_engine.extract_streams(
            whitebox_engine.flow_accumulation(str(dem), out("facc_for_streams.tif"))
            and out("facc_for_streams.tif"),
            out("streams.tif"),
            threshold=5.0,
        ),
    })
    return fixtures
