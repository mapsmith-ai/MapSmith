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



def _spec_problems(record: dict) -> list[str]:
    """Every way this record fails the spec, according to BOTH implementations.

    The standalone validator alone is not enough, and until 2026-08-26 it was
    all this file used: the schema is the NORMATIVE implementation, and the two
    had drifted on every recommended field. A CI that says "conforming" using
    the lenient one of two implementations is worse than one that says nothing,
    because it is the sentence a reader trusts.
    """
    import json
    import sys
    from pathlib import Path

    data = Path(__file__).parent / "data"
    sys.path.insert(0, str(data))
    from manifest_spec_validator import problems

    found = list(problems(record))
    try:
        import jsonschema
    except ImportError:  # pragma: no cover - jsonschema is in the test extra
        return found
    schema = json.loads((data / "manifest-v1.schema.json").read_text(encoding="utf-8"))
    checker = jsonschema.Draft202012Validator(schema)
    found += [f"schema: {error.message}" for error in checker.iter_errors(record)]
    return found

def test_every_manifest_mapsmith_writes_conforms_to_the_spec(tmp_path):
    """MapSmith is an implementation of the manifest spec, not its definition.

    The day our manifest stops passing our own published validator, CI says so
    here — instead of a reader finding out. The validator is a vendored copy of
    the spec repository's stdlib-only implementation; the record under test is
    a REAL one, written by a real writer on a real file, because a hand-built
    record would validate the test's idea of a manifest rather than MapSmith's.
    """
    import json

    import geopandas as gpd

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
    assert _spec_problems(record) == [], "a MapSmith manifest no longer conforms to the spec"
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
    from pathlib import Path

    from mapsmith import catalog
    from mapsmith.plans.registry import BINDINGS

    fixtures = _spec_fixtures(tmp_path)
    writing = [
        entry["name"]
        for entry in catalog.OPERATIONS
        if entry["status"] == "available"
        and (binding := BINDINGS.get(entry["name"])) is not None
        and binding.output_arg is not None
    ]
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
        assert _spec_problems(record) == [], f"{name} writes a manifest the spec rejects"
        assert record["operation"] == name
        assert record["spec_version"].startswith("1."), name
        validated.append(name)

    # An invariant, not a threshold. `>= 32` was wrong in both directions: with
    # every extra installed there are 34 writing operations and 34 fixtures, so
    # the number let TWO fixtures disappear in silence -- exactly the defect
    # this test exists to close. And on a checkout without [raster]/[whitebox]
    # only 21 fixtures can run, so the same number FAILED while blaming missing
    # fixtures for an absent extra. Comparing the two sets says the true thing
    # in either environment, and needs no maintenance when the 35th operation
    # lands.
    assert set(validated) | set(skipped) == set(writing), (
        "every writing operation must be either validated or explicitly skipped; "
        f"unaccounted for: {sorted(set(writing) - set(validated) - set(skipped))}"
    )
    assert validated, "the sweep validated nothing at all"
    print(
        f"conformance sweep: {len(validated)} of {len(writing)} writing operations "
        f"validated, {len(skipped)} skipped for a missing extra ({sorted(skipped)})"
    )



def test_every_check_name_in_the_source_obeys_the_vocabulary():
    """Not only the names a fixture happens to trigger.

    The conformance sweep above sees the checks that actually fire on its
    fixtures, which on 2026-08-26 was 12 of the 16 extension names in the
    source: four live on conditional branches -- the axis-order probe in
    `run_sql`, the invented-class-code guard, the flat-length warning on 3D
    geometries, the repair-path input check -- and no fixture reaches them. All
    four turned out to be well formed, checked by hand. The next one might not
    be, and "checked by hand" is not a control.

    Read from the source with `ast` rather than by executing anything: a name on
    a branch nothing takes is exactly the case that matters here.
    """
    import ast
    import sys
    from pathlib import Path

    import mapsmith

    # The rule is read from the vendored spec copy, not from a local
    # restatement of it: two copies of one mistake agree perfectly.
    sys.path.insert(0, str(Path(__file__).parent / "data"))
    from manifest_spec_validator import CORE_CHECK_NAMES, EXTENSION_CHECK_NAME

    root = Path(mapsmith.__file__).parent
    found: dict[str, str] = {}
    dynamic: list[str] = []
    for module in sorted(root.rglob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        # Module-level tables of literal check names, so a lookup at the call
        # site is still statically readable. Only all-string dicts count.
        tables: dict[str, list[str]] = {}
        for node in tree.body:
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
                continue
            values = node.value.values
            if not values or not all(
                isinstance(v, ast.Constant) and isinstance(v.value, str) for v in values
            ):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    tables[target.id] = [v.value for v in values]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = node.func
            name = (
                callee.attr if isinstance(callee, ast.Attribute)
                else callee.id if isinstance(callee, ast.Name)
                else None
            )
            if name != "Check" or not node.args:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.setdefault(first.value, module.name)
            elif isinstance(first, ast.Subscript) and isinstance(first.value, ast.Name):
                # A lookup into a module-level table of literal names. The names
                # ARE written out, just not at the call, so the sweep reads the
                # table instead of giving up — and if the table holds anything
                # that is not a plain string, this falls through to `dynamic`.
                table = tables.get(first.value.id)
                if table:
                    for literal in table:
                        found.setdefault(literal, module.name)
                else:
                    dynamic.append(f"{module.name}:{first.lineno}")
            else:
                # A name this cannot read is a name it cannot police, and it used
                # to skip them in silence: `f"x-mapsmith:{name}_is_close_to_..."`
                # in network.py was invisible to the whole sweep, and legal by
                # luck. Refusing outright is stronger than half-evaluating an
                # f-string, and it costs nothing: a check name is short.
                dynamic.append(f"{module.name}:{getattr(first, 'lineno', '?')}")

    assert not dynamic, (
        f"these check names are built at runtime rather than written out: {dynamic}. "
        "Write them literally — a name assembled from an f-string is invisible to "
        "this sweep, so the vocabulary rule stops applying to exactly the newest "
        "code, which is where it is most needed."
    )
    assert len(found) >= 25, f"only {len(found)} check names found in the source: {sorted(found)}"
    offenders = {
        check: where
        for check, where in found.items()
        if check not in CORE_CHECK_NAMES and not EXTENSION_CHECK_NAME.fullmatch(check)
    }
    assert not offenders, (
        f"these check names are neither a core name from section 3.6 of the spec nor an "
        f"extension `x-<producer>:<name>`: {offenders}. An unconstrained vocabulary makes "
        "two records incomparable, which is the point of having a format."
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
    quad = tmp_path / "quad.parquet"
    gpd.GeoDataFrame(
        {"n": [1, 2, 3, 4], "v": [7.0, 7.0, 7.0, 7.0]},
        geometry=[Point(0, 0), Point(100, 0), Point(0, 100), Point(100, 100)],
        crs=crs,
    ).to_parquet(quad)

    # A small connected network and a strip of touching areas: everything the
    # network and statistics operations need, built once here rather than in
    # each lambda.
    streets = tmp_path / "streets.parquet"
    gpd.GeoDataFrame(
        {"id": [0, 1, 2]},
        geometry=[
            LineString([(0, 0), (100, 0)]),
            LineString([(100, 0), (200, 0)]),
            LineString([(100, 0), (100, 100)]),
        ],
        crs=crs,
    ).to_parquet(streets)
    areas = tmp_path / "areas.parquet"
    gpd.GeoDataFrame(
        {"cases": [1.0, 1.0, 5.0, 1.0], "pop": [100.0, 200.0, 300.0, 400.0]},
        geometry=[
            Polygon([(i * 10, 0), (i * 10 + 10, 0), (i * 10 + 10, 10), (i * 10, 10)])
            for i in range(4)
        ],
        crs=crs,
    ).to_parquet(areas)
    crowd = tmp_path / "crowd.parquet"
    gpd.GeoDataFrame(
        {"weight": [1.0, 5.0, 2.0, 4.0]},
        geometry=[Point(x, 0) for x in (0, 10, 20, 30)],
        crs=crs,
    ).to_parquet(crowd)

    # For the linework operations: a line a few centimetres off its reference,
    # a pair that cross in a plus sign, and two control points describing a
    # quarter turn about the origin followed by a shift.
    nearly = tmp_path / "nearly.parquet"
    gpd.GeoDataFrame(
        {"id": [0]}, geometry=[LineString([(0, 0.03), (100, 0.03)])], crs=crs
    ).to_parquet(nearly)
    crossing = tmp_path / "crossing.parquet"
    gpd.GeoDataFrame(
        {"id": [0]}, geometry=[LineString([(50, -50), (50, 50)])], crs=crs
    ).to_parquet(crossing)
    baseline = tmp_path / "baseline.parquet"
    gpd.GeoDataFrame(
        {"id": [0]}, geometry=[LineString([(0, 0), (100, 0)])], crs=crs
    ).to_parquet(baseline)
    control = tmp_path / "control.parquet"
    gpd.GeoDataFrame(
        {"source_x": [0.0, 10.0], "source_y": [0.0, 0.0]},
        geometry=[Point(100, 200), Point(100, 210)],
        crs=crs,
    ).to_parquet(control)

    # A container with two layers, which nothing else here needs: extract_layer
    # has nothing to extract without one. Written with the GPKG driver because
    # single-dataset formats cannot hold two layers at all — which is the
    # refusal the operation exists to resolve.
    container = tmp_path / "container.gpkg"
    gpd.GeoDataFrame({"k": ["x"], "v": [1]}, geometry=[square], crs=crs).to_file(
        container, layer="parcels", driver="GPKG"
    )
    gpd.GeoDataFrame(
        {"id": [0]}, geometry=[LineString([(0, 0), (100, 0)])], crs=crs
    ).to_file(container, layer="roads", driver="GPKG")

    def out(name: str) -> str:
        return str(tmp_path / name)

    from mapsmith.engines import linework, network, spatial_stats, whitebox_engine

    fixtures = {
        "network_shortest_path": lambda: network.network_shortest_path(
            str(streets), out("route.parquet"), 0, 0, 200, 0, tolerance=0.01
        ),
        "service_area": lambda: network.service_area(
            str(streets), out("reach.parquet"), 0, 0, budget=150.0, tolerance=0.01
        ),
        "hot_spots": lambda: spatial_stats.hot_spots(
            str(areas), out("gi.parquet"), value_field="cases", weights="contiguity"
        ),
        "smooth_rates": lambda: spatial_stats.smooth_rates(
            str(areas), out("eb.parquet"), count_field="cases", population_field="pop"
        ),
        "aggregate_to_threshold": lambda: spatial_stats.aggregate_to_threshold(
            str(areas), out("merged.parquet"), count_field="cases", minimum=2
        ),
        "thin_points": lambda: spatial_stats.thin_points(
            str(crowd), out("thin.parquet"), min_distance=15.0
        ),
        "snap_layer": lambda: linework.snap_layer(
            str(nearly), str(baseline), out("snapped.parquet"), tolerance=0.05
        ),
        "points_along_lines": lambda: linework.points_along_lines(
            str(baseline), out("chainage.parquet"), spacing=20.0
        ),
        "line_intersections": lambda: linework.line_intersections(
            str(baseline), str(crossing), out("nodes.parquet")
        ),
        "transform_by_control_points": lambda: linework.transform_by_control_points(
            str(baseline), str(control), out("placed.parquet"), target_crs=crs
        ),
        "contour_lines": lambda: whitebox_engine.contour_lines(
            _ramp(tmp_path), out("contours.parquet"), interval=3.0
        ),
        "least_cost_path": lambda: network.least_cost_path(
            _uniform_cost(tmp_path),
            _one_point(tmp_path, "lcp_start", 0.5, 9.5),
            _one_point(tmp_path, "lcp_end", 9.5, 9.5),
            out("cheapest.parquet"),
        ),
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
        # `streets` and not `layer`: the single-polygon layer kept 1 of 1, so the
        # conformance sweep validated the manifest of a filter that filtered
        # nothing — output bytes identical to input, no `SUBSET` note, no
        # `feature_count_bounded` with anything to bound. Here two of three
        # survive, so the interesting branches are the ones checked.
        "select_features": lambda: vector.select_features(
            str(streets),
            out("selected.parquet"),
            by="field_in",
            field="id",
            values=[0, 1],
        ),
        "extract_layer": lambda: vector.extract_layer(
            str(container), "roads", out("extracted.parquet")
        ),
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
        # Four points, because two collinear ones give two cells and no corner.
        "voronoi_polygons": lambda: vector.voronoi_polygons(
            str(quad), out("vor.parquet")
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
        "reproject_raster": lambda: raster.reproject_raster(
            str(grid), out("repr.tif"), "EPSG:4326", "nearest"
        ),
        "extract_band": lambda: raster.extract_band(str(grid), out("band2.tif"), 2),
    })

    try:
        from mapsmith.engines import sampling, whitebox_engine
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
    mask = tmp_path / "mask.tif"
    features = np.zeros((rows, cols), dtype="float32")
    features[rows // 2, cols // 2] = 1.0
    with rasterio.open(
        mask, "w", driver="GTiff", height=rows, width=cols, count=1, dtype="float32",
        crs=crs, transform=from_origin(0, rows * 10.0, 10, 10), nodata=-9999.0,
    ) as ds:
        ds.write(features, 1)
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
        "curvature": lambda: whitebox_engine.curvature(
            str(dem), out("curv.tif"), kind="profile"
        ),
        "flow_direction": lambda: whitebox_engine.flow_direction(
            str(dem), out("d8.tif"), method="d8"
        ),
        # A mask, not the DEM: euclidean_distance measures from the NON-ZERO
        # cells, and every cell of the DEM is non-zero, so it would be all zeros.
        "euclidean_distance": lambda: whitebox_engine.euclidean_distance(
            str(mask), out("dist.tif")
        ),
        "idw_interpolation": lambda: whitebox_engine.idw_interpolation(
            str(quad), out("idw.tif"), field_name="v", cell_size=10.0
        ),
        "viewshed": lambda: whitebox_engine.viewshed(
            str(dem), str(pour), out("seen.tif"), station_height=2.0
        ),
        "sample_raster_at_points": lambda: sampling.sample_raster_at_points(
            str(dem), str(points), out("sampled.parquet"), "bilinear"
        ),
        "elevation_profile": lambda: sampling.elevation_profile(
            str(dem), str(streets), out("profile.parquet"), spacing=25.0
        ),
    })
    return fixtures



def _ramp(tmp_path) -> str:
    """A planar DEM: z = column index, 10 m cells. Contours land on cell centres."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    path = tmp_path / "ramp.tif"
    if not path.exists():
        values = np.tile(np.arange(12, dtype="float32"), (12, 1))
        with rasterio.open(
            path, "w", driver="GTiff", height=12, width=12, count=1,
            dtype="float32", crs="EPSG:32632",
            transform=from_origin(1000.0, 5000.0, 10.0, 10.0),
        ) as dst:
            dst.write(values, 1)
    return str(path)


def _uniform_cost(tmp_path) -> str:
    """Every cell costs 1, so the cheapest route is the straight one."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    path = tmp_path / "cost.tif"
    if not path.exists():
        with rasterio.open(
            path, "w", driver="GTiff", height=10, width=10, count=1,
            dtype="float32", crs="EPSG:32632",
            transform=from_origin(0, 10, 1, 1),
        ) as dst:
            dst.write(np.ones((10, 10), dtype="float32"), 1)
    return str(path)


def _one_point(tmp_path, name: str, x: float, y: float) -> str:
    import geopandas as gpd
    from shapely.geometry import Point

    path = tmp_path / f"{name}.parquet"
    if not path.exists():
        gpd.GeoDataFrame(
            {"id": [1]}, geometry=[Point(x, y)], crs="EPSG:32632"
        ).to_parquet(path)
    return str(path)


def test_a_crash_writes_a_manifest_the_published_validator_accepts(tmp_path):
    """Four call sites passed an empty precondition list, so an engine crash
    wrote `verification: []` — which the specification rejects, in its own
    words because a record with no checks is "a log entry wearing a manifest's
    clothes".

    Two of the four were operations added in 0.4.0: the module that arrived in
    this release did not inherit the pattern. The conformance sweep could not
    see it because it exercises only the paths that succeed.
    """
    import importlib.util
    import json
    from pathlib import Path

    from mapsmith.provenance import ProvenanceRecord

    out = tmp_path / "out.parquet"
    record = ProvenanceRecord(
        operation="run_sql",
        parameters={"query": "SELECT 1"},
        inputs=[],
        engine={"name": "duckdb", "version": "1.5.5"},
    )
    with pytest.raises(RuntimeError), verify.audit_on_failure(record, str(out), []):
        raise RuntimeError("the engine blew up")

    manifest = json.loads(
        Path(f"{out}.provenance.json").read_text(encoding="utf-8")
    )
    assert manifest["verification"], "a crash wrote a record with no checks"
    assert manifest["verification"][0]["passed"] is False

    # Against the specification's own validator, vendored from the published
    # repository rather than reimplemented here.
    spec = importlib.util.spec_from_file_location(
        "spec_validator", Path(__file__).parent / "data" / "manifest_spec_validator.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.problems(manifest) == [], module.problems(manifest)


def test_an_operation_that_verifies_nothing_raises_rather_than_writing(tmp_path):
    """The success-path half of the same rule.

    An operation whose checks come back empty has verified nothing, and a
    manifest is not the place for a caller to discover that.
    """
    from mapsmith.provenance import ProvenanceRecord

    record = ProvenanceRecord(
        operation="pretend_operation",
        parameters={},
        inputs=[],
        engine={"name": "test", "version": "0"},
    )
    (tmp_path / "out.parquet").write_bytes(b"")
    with pytest.raises(verify.VerificationError, match="no verification at all"):
        verify.audited(
            record,
            str(tmp_path / "out.parquet"),
            operation="pretend_operation",
            checks_fn=list,
        )
