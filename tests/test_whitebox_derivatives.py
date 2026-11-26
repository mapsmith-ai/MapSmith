"""Curvature, flow direction, Euclidean distance and IDW: closed-form values.

Every expected number here is derivable by hand from the fixture, which is the
only kind of expectation that can fail for the right reason. The pointer-table
test is the important one: it does not check that the manifest CARRIES a table,
it checks that the table the manifest carries is the one the raster uses.
"""

import json
import math
from pathlib import Path

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import Point

pytest.importorskip("whitebox_workflows")
rasterio = pytest.importorskip("rasterio")

import numpy as np
from rasterio.transform import from_origin

from mapsmith.engines import whitebox_engine as engine


def _write(path, data, *, crs="EPSG:32632", resolution=10.0):
    height, width = data.shape
    with rasterio.open(
        path, "w", driver="GTiff", height=height, width=width, count=1,
        dtype="float32", crs=crs, nodata=-9999.0,
        transform=from_origin(500000.0, 4500000.0, resolution, resolution),
    ) as dst:
        dst.write(data.astype("float32"), 1)
    return str(path)


def _read(path):
    with rasterio.open(path) as src:
        return src.read(1, masked=True)


def _manifest(result):
    return json.loads(Path(result["provenance"]).read_text(encoding="utf-8"))


def _checks(result):
    return {c["name"]: c["passed"] for c in _manifest(result)["verification"]}


@pytest.fixture
def plane(tmp_path):
    """z = column * 10 on a 10 m grid: a plane of unit gradient rising eastward.

    Closed form on such a surface: curvature is exactly zero, and the downslope
    direction is due west in every cell. The outermost two rings are clamped
    windows and only approximate, so the assertions read the interior.
    """
    return _write(tmp_path / "plane.tif", np.fromfunction(lambda r, c: c * 10.0, (8, 8)))


# --------------------------------------------------------------- curvature


def test_curvature_of_a_plane_is_exactly_zero(plane, tmp_path):
    out = str(tmp_path / "curv.tif")
    engine.curvature(plane, out, kind="profile")
    interior = _read(out)[2:-2, 2:-2]
    assert float(np.abs(interior).max()) == 0.0


def test_every_curvature_kind_runs_and_is_recorded(plane, tmp_path):
    for kind, tool in engine.CURVATURE_KINDS.items():
        result = engine.curvature(plane, str(tmp_path / f"c-{kind}.tif"), kind=kind)
        parameters = _manifest(result)["parameters"]
        assert parameters["kind"] == kind
        # The tool name too, not only the kind: 'plan' and 'tangential' are one
        # rename apart upstream, and the manifest should say which ran.
        assert parameters["tool"] == tool


def test_curvature_refuses_an_unknown_kind_and_names_the_valid_ones(plane, tmp_path):
    with pytest.raises(ValueError, match="kind must be one of"):
        engine.curvature(plane, str(tmp_path / "x.tif"), kind="planar")


def test_curvature_refuses_a_geographic_dem(tmp_path):
    dem = _write(
        tmp_path / "geo.tif",
        np.fromfunction(lambda r, c: c * 10.0, (8, 8)),
        crs="EPSG:4326",
        resolution=0.001,
    )
    with pytest.raises(ValueError, match="geographic CRS"):
        engine.curvature(dem, str(tmp_path / "x.tif"), kind="profile")


# ---------------------------------------------------------- flow_direction

# Measured one direction at a time on whitebox-workflows 2.0.6 and asserted here
# so a library upgrade that changes the table fails loudly instead of mirroring
# every drainage network the day it lands.
_NEIGHBOURS = {
    "east": (0, 1), "northeast": (-1, 1), "north": (-1, 0), "northwest": (-1, -1),
    "west": (0, -1), "southwest": (1, -1), "south": (1, 0), "southeast": (1, 1),
}


@pytest.mark.parametrize("encoding", sorted(engine.POINTER_ENCODINGS))
def test_the_pointer_table_in_the_manifest_is_the_table_the_raster_uses(
    encoding, tmp_path
):
    """One low neighbour, so the code at the centre names it and nothing else.

    This is the check the operation exists for. A test that asserted the
    manifest CONTAINS a direction table would pass on a table that describes a
    different engine — which is exactly the failure mode, since nothing in a
    GeoTIFF says which convention it holds.
    """
    table = engine.POINTER_ENCODINGS[encoding]
    for direction, (dr, dc) in _NEIGHBOURS.items():
        grid = np.full((5, 5), 200.0)
        grid[2, 2] = 100.0
        grid[2 + dr, 2 + dc] = 0.0
        dem = _write(tmp_path / f"{encoding}-{direction}.tif", grid)
        out = str(tmp_path / f"{encoding}-{direction}-d8.tif")
        result = engine.flow_direction(dem, out, method="d8", encoding=encoding)
        code = int(_read(out)[2, 2])
        declared = _manifest(result)["parameters"]["direction_codes"]
        assert code == declared[direction], (
            f"{encoding}: the cell flows {direction} and holds {code}, while the "
            f"manifest says {direction} is {declared[direction]}"
        )
        assert declared == table


def test_the_two_encodings_produce_different_rasters(plane, tmp_path):
    """Otherwise `encoding` would be a parameter that records a choice it did
    not make — the manifest would be true about nothing."""
    first = str(tmp_path / "ne.tif")
    second = str(tmp_path / "e.tif")
    engine.flow_direction(plane, first, method="d8", encoding="northeast_first")
    engine.flow_direction(plane, second, method="d8", encoding="east_first")
    assert not np.array_equal(_read(first), _read(second))
    # And each says west with its own code: 32 clockwise-from-northeast, 16
    # clockwise-from-east.
    assert set(np.unique(_read(first)[1:-1, 1:-1].compressed())) == {32}
    assert set(np.unique(_read(second)[1:-1, 1:-1].compressed())) == {16}


def test_pointer_codes_are_verified_as_a_set_not_a_range(plane, tmp_path):
    result = engine.flow_direction(plane, str(tmp_path / "d8.tif"), method="d8")
    assert _checks(result)["values_in_expected_range"] is True
    detail = next(
        c["detail"] for c in _manifest(result)["verification"]
        if c["name"] == "values_in_expected_range"
    )
    assert "powers of two" in detail


@pytest.mark.parametrize("method", sorted(engine.FLOW_DIRECTION_METHODS))
def test_every_flow_direction_method_runs(method, plane, tmp_path):
    result = engine.flow_direction(plane, str(tmp_path / f"fd-{method}.tif"), method=method)
    parameters = _manifest(result)["parameters"]
    assert parameters["method"] == method
    # dinf and fd8 write continuous values: a direction table would be a lie.
    assert ("direction_codes" in parameters) == (method in ("d8", "rho8"))


def test_an_encoding_is_refused_where_there_is_no_table(plane, tmp_path):
    with pytest.raises(ValueError, match="meaningless for method"):
        engine.flow_direction(
            plane, str(tmp_path / "x.tif"), method="dinf", encoding="east_first"
        )


# ------------------------------------------------------ euclidean_distance


def test_euclidean_distance_from_one_source_cell_is_closed_form(tmp_path):
    grid = np.zeros((5, 5))
    grid[2, 2] = 1.0
    source = _write(tmp_path / "mask.tif", grid)
    out = str(tmp_path / "dist.tif")
    result = engine.euclidean_distance(source, out)
    values = _read(out)
    assert float(values[2, 2]) == 0.0
    assert float(values[2, 0]) == pytest.approx(20.0)
    assert float(values[0, 0]) == pytest.approx(math.hypot(20.0, 20.0))
    assert _checks(result)["values_in_expected_range"] is True


def test_euclidean_distance_refuses_a_geographic_crs(tmp_path):
    grid = np.zeros((5, 5))
    grid[2, 2] = 1.0
    source = _write(tmp_path / "geo.tif", grid, crs="EPSG:4326", resolution=0.001)
    with pytest.raises(ValueError, match="not a length"):
        engine.euclidean_distance(source, str(tmp_path / "x.tif"))


# ------------------------------------------------------ idw_interpolation


@pytest.fixture
def four_equal_points(tmp_path):
    path = tmp_path / "points.parquet"
    gpd.GeoDataFrame(
        {"height": [7.0, 7.0, 7.0, 7.0]},
        geometry=[Point(0, 0), Point(100, 0), Point(0, 100), Point(100, 100)],
        crs="EPSG:32632",
    ).to_parquet(path)
    return str(path)


def test_idw_of_equal_values_is_that_value_everywhere(four_equal_points, tmp_path):
    """Closed form regardless of the weighting: a weighted mean of identical
    values is the value, so any deviation is the interpolation misbehaving."""
    out = str(tmp_path / "idw.tif")
    result = engine.idw_interpolation(
        four_equal_points, out, field_name="height", cell_size=10.0
    )
    values = _read(out)
    assert float(values.min()) == pytest.approx(7.0)
    assert float(values.max()) == pytest.approx(7.0)
    parameters = _manifest(result)["parameters"]
    # The exponent travels with the surface: an IDW raster without it cannot be
    # reproduced, and 2 versus 3 is a visibly different map.
    assert parameters["weight"] == 2.0
    assert parameters["field_name"] == "height"
    assert parameters["cell_size"] == 10.0


def test_idw_requires_the_field_because_the_library_default_is_row_numbers(
    four_equal_points, tmp_path
):
    with pytest.raises(ValueError, match="field_name is required"):
        engine.idw_interpolation(
            four_equal_points, str(tmp_path / "x.tif"), field_name="", cell_size=10.0
        )


def test_idw_refuses_a_non_positive_cell_size(four_equal_points, tmp_path):
    with pytest.raises(ValueError, match="cell_size must be positive"):
        engine.idw_interpolation(
            four_equal_points, str(tmp_path / "x.tif"), field_name="height", cell_size=0.0
        )


def _points(tmp_path, name, values, locations, *, field="mm", crs="EPSG:32632"):
    path = tmp_path / f"{name}.parquet"
    gpd.GeoDataFrame(
        {field: values}, geometry=[Point(*xy) for xy in locations], crs=crs
    ).to_parquet(path)
    return str(path)


_SQUARE = [(0, 0), (100, 0), (0, 100), (100, 100)]


@pytest.mark.parametrize(
    ("operation", "values", "asked", "said"),
    [
        # A typo in the field name: whitebox answers with the surface of FID.
        ("idw_interpolation", [800.0, 900.0, 1000.0, 1100.0], "mn", "is not present"),
        ("tin_interpolation", [800.0, 900.0, 1000.0, 1100.0], "mn", "is not present"),
        # Digits stored as text: same surface of FID.
        ("idw_interpolation", ["800", "900", "1000", "1100"], "mm", "not a number"),
        ("tin_interpolation", ["800", "900", "1000", "1100"], "mm", "not a number"),
        # One gauge without a reading: its ROW NUMBER goes in its place.
        ("idw_interpolation", [800.0, None, 1000.0, 1100.0], "mm", "1 of 4 points"),
        ("tin_interpolation", [800.0, None, 1000.0, 1100.0], "mm", "1 of 4 points"),
        ("tin_interpolation", [800.0, float("inf"), 1000.0, 1100.0], "mm", "1 of 4 points"),
    ],
)
def test_an_interpolator_refuses_where_the_library_would_answer_with_row_numbers(
    operation, values, asked, said, tmp_path
):
    """Each case ran to `verified: True` through idw_interpolation up to 0.12.2.

    Measured on whitebox-workflows 2.0.6: the typo and the text field returned a
    surface spanning 0..3, the row numbers of four points, and the null returned
    one spanning 1..1100 — the reading of the second gauge replaced by its
    position in the file.
    """
    source = _points(tmp_path, "gauges", values, _SQUARE)
    out = tmp_path / "surface.tif"
    with pytest.raises(ValueError, match=said):
        getattr(engine, operation)(source, str(out), field_name=asked, cell_size=10.0)
    assert not out.exists()


@pytest.mark.parametrize("operation", ["idw_interpolation", "tin_interpolation"])
def test_an_interpolator_refuses_a_grid_over_the_operators_limit(
    operation, tmp_path, monkeypatch
):
    """The grid is invented from an extent and a cell size the caller chose."""
    source = _points(tmp_path, "gauges", [1.0, 2.0, 3.0, 4.0], _SQUARE)
    monkeypatch.setenv("MAPSMITH_MAX_GRID_CELLS", "100")
    with pytest.raises(ValueError, match="MAPSMITH_MAX_GRID_CELLS"):
        # 11 x 11 by the limit's own count, over 100.
        getattr(engine, operation)(
            source, str(tmp_path / "x.tif"), field_name="mm", cell_size=10.0
        )
    # 6 x 6 = 36 passes: the limit refuses the size, not the operation.
    getattr(engine, operation)(
        source, str(tmp_path / "ok.tif"), field_name="mm", cell_size=20.0
    )


@pytest.mark.parametrize("operation", ["idw_interpolation", "tin_interpolation"])
def test_a_second_run_to_the_same_path_is_verified_against_itself(operation, tmp_path):
    """The read-back used to come from the engine's cache, keyed by path.

    Written, read, overwritten, read again: whitebox-workflows 2.0.6 returns the
    FIRST file on the second read, from any environment in the process. A server
    is one process and an agent retries to the same output, so the second run
    was checked against the raster of the first: here a 5 x 5 surface refused
    with "expected (5, 5), got (10, 10)" while the file on disk was right.
    """
    source = _points(tmp_path, "sq", [1.0, 2.0, 3.0, 4.0], _SQUARE)
    out = str(tmp_path / "same.tif")
    run = getattr(engine, operation)
    assert run(source, out, field_name="mm", cell_size=10.0)["shape"] == [10, 10]
    second = run(source, out, field_name="mm", cell_size=20.0)
    assert second["shape"] == [5, 5]
    assert _read(out).shape == (5, 5)
    assert all(_checks(second).values())


def test_a_bad_second_run_is_not_passed_on_the_first_runs_raster(tmp_path):
    """The dangerous direction of the same defect: a failure read as a success.

    First a good surface, then, to the same path, one with no valid cell. The
    second returned `verified: True` with a check reading "100/100 valid
    cells" beside a note reading "0 of 100" — the check had looked at the
    first run's file.
    """
    from mapsmith import verify

    source = _points(tmp_path, "sparse", [1.0, 2.0, 3.0, 4.0, 5.0], [*_SQUARE, (50, 50)])
    out = str(tmp_path / "same.tif")
    engine.tin_interpolation(source, out, field_name="mm", cell_size=10.0)
    with pytest.raises(verify.VerificationError, match="result_not_empty"):
        engine.tin_interpolation(
            source, out, field_name="mm", cell_size=10.0, max_triangle_edge_length=60.0
        )


def test_a_terrain_writer_rerun_on_a_different_grid_is_verified_against_itself(tmp_path):
    """The same read-back serves every writer in this engine, not the two above."""
    out = str(tmp_path / "shade.tif")
    small = _write(tmp_path / "small.tif", np.fromfunction(lambda r, c: c * 10.0, (8, 8)))
    large = _write(tmp_path / "large.tif", np.fromfunction(lambda r, c: c * 10.0, (12, 12)))
    engine.hillshade(small, out)
    assert all(_checks(engine.hillshade(large, out)).values())
    assert _read(out).shape == (12, 12)


@pytest.mark.parametrize("operation", ["idw_interpolation", "tin_interpolation"])
def test_an_interpolator_refuses_a_row_the_library_would_drop(operation, tmp_path):
    """Three rows holding 1, 2 and 3, the third a polygon.

    Handed to the library, the polygon is dropped with its value and the surface
    is built from the other two — or, for a row with no geometry at all, from
    the rest — and nothing says a measurement went missing.
    """
    from shapely.geometry import Polygon

    mixed = tmp_path / "mixed.parquet"
    gpd.GeoDataFrame(
        {"mm": [1.0, 2.0, 3.0, 4.0]},
        geometry=[Point(0, 0), Point(100, 0), Point(0, 100),
                  Polygon([(90, 90), (110, 90), (110, 110), (90, 110)])],
        crs="EPSG:32632",
    ).to_parquet(mixed)
    with pytest.raises(ValueError, match="reads point layers"):
        getattr(engine, operation)(
            str(mixed), str(tmp_path / "x.tif"), field_name="mm", cell_size=10.0
        )
    missing = tmp_path / "missing.parquet"
    gpd.GeoDataFrame(
        {"mm": [1.0, 2.0, 3.0, 4.0]},
        geometry=[Point(0, 0), Point(100, 0), Point(0, 100), None],
        crs="EPSG:32632",
    ).to_parquet(missing)
    with pytest.raises(ValueError, match="no point geometry"):
        getattr(engine, operation)(
            str(missing), str(tmp_path / "y.tif"), field_name="mm", cell_size=10.0
        )


@pytest.mark.parametrize("operation", ["idw_interpolation", "tin_interpolation"])
@pytest.mark.parametrize(
    ("values", "said"),
    [
        ([True, False, True, False], "not a number"),
        (pd.array([1, None, 3, 4], dtype="Int64"), "1 of 4 points"),
    ],
)
def test_an_interpolator_refuses_booleans_and_nullable_gaps(operation, values, said, tmp_path):
    source = tmp_path / "typed.parquet"
    gpd.GeoDataFrame(
        {"mm": values}, geometry=[Point(*xy) for xy in _SQUARE], crs="EPSG:32632"
    ).to_parquet(source)
    with pytest.raises(ValueError, match=said):
        getattr(engine, operation)(
            str(source), str(tmp_path / "x.tif"), field_name="mm", cell_size=10.0
        )


@pytest.mark.parametrize("operation", ["idw_interpolation", "tin_interpolation"])
def test_an_interpolator_reads_a_nullable_integer_field_with_no_gaps(operation, tmp_path):
    source = tmp_path / "ints.parquet"
    gpd.GeoDataFrame(
        {"mm": pd.array([7, 7, 7, 7], dtype="Int64")},
        geometry=[Point(*xy) for xy in _SQUARE], crs="EPSG:32632",
    ).to_parquet(source)
    out = str(tmp_path / "ok.tif")
    getattr(engine, operation)(str(source), out, field_name="mm", cell_size=10.0)
    values = _read(out)
    assert float(values.min()) == pytest.approx(7.0)
    assert float(values.max()) == pytest.approx(7.0)


def test_idw_refuses_a_search_that_turns_the_grid_into_cells_times_points(tmp_path):
    """Measured: a radius wider than the layer makes every cell weigh every sample.

    A hundred samples on a 1000 x 1000 extent at a cell of 0.5 is four million
    cells: as they are it runs, with a radius of a kilometre it is four hundred
    million pairs, over the hundred million this server allows by default.
    """
    rng = np.random.default_rng(3)
    locations = [(0, 0), (1000, 1000), *((float(x), float(y)) for x, y in rng.random((98, 2)) * 1000)]
    source = _points(tmp_path, "many", [1.0] * 100, locations)
    with pytest.raises(ValueError, match="cell-sample pairs"):
        engine.idw_interpolation(
            source, str(tmp_path / "x.tif"), field_name="mm", cell_size=0.5, radius=1000.0
        )
    with pytest.raises(ValueError, match="more samples than the 100"):
        engine.idw_interpolation(
            source, str(tmp_path / "y.tif"), field_name="mm", cell_size=50.0, min_points=101
        )
    with pytest.raises(ValueError, match="radius must be zero or a positive finite"):
        engine.idw_interpolation(
            source, str(tmp_path / "z.tif"), field_name="mm", cell_size=50.0,
            radius=float("nan"),
        )
    # The same radius on a grid it does not blow up: 441 cells, 44 100 pairs.
    engine.idw_interpolation(
        source, str(tmp_path / "ok.tif"), field_name="mm", cell_size=50.0, radius=1000.0
    )


def test_a_tin_over_the_operators_point_limit_is_refused(tmp_path, monkeypatch):
    source = _points(tmp_path, "five", [1.0, 2.0, 3.0, 4.0, 5.0], [*_SQUARE, (50, 50)])
    monkeypatch.setenv("MAPSMITH_MAX_TRIANGULATION_POINTS", "4")
    with pytest.raises(ValueError, match="MAPSMITH_MAX_TRIANGULATION_POINTS"):
        engine.tin_interpolation(
            source, str(tmp_path / "x.tif"), field_name="mm", cell_size=10.0
        )


def test_a_refusal_does_not_quote_every_column_a_file_brought(tmp_path):
    """The names are the file's: 1 500 of them made one message 104 000 characters."""
    path = tmp_path / "wide.parquet"
    columns = {f"column_{index:04d}": [1.0, 2.0, 3.0, 4.0] for index in range(300)}
    gpd.GeoDataFrame(
        columns, geometry=[Point(*xy) for xy in _SQUARE], crs="EPSG:32632"
    ).to_parquet(path)
    with pytest.raises(ValueError, match="and 280 more") as refused:
        engine.tin_interpolation(
            str(path), str(tmp_path / "x.tif"), field_name="nope", cell_size=10.0
        )
    assert "column_0019" in str(refused.value)
    assert "column_0020" not in str(refused.value)


# ------------------------------------------------------ tin_interpolation


def _plane(x, y):
    return 2.0 * x + 3.0 * y + 5.0


def _cell_centres(path):
    with rasterio.open(path) as src:
        rows, cols = np.indices((src.height, src.width))
        xs, ys = rasterio.transform.xy(src.transform, rows, cols, offset="center")
        return np.asarray(xs).reshape(rows.shape), np.asarray(ys).reshape(rows.shape)


def test_a_tin_gives_back_the_plane_its_samples_lie_on(tmp_path):
    """Closed form: linear interpolation of samples of a plane IS that plane.

    Read at the cell centres, so this is also the registration test: a surface
    placed half a cell off — as this library places its contours — would be wrong
    by 2 * 5 + 3 * 5 = 25 on a 10 m grid, not by rounding.
    """
    locations = [*_SQUARE, (50, 50), (30, 80)]
    source = _points(
        tmp_path, "survey", [_plane(x, y) for x, y in locations], locations, field="z"
    )
    out = str(tmp_path / "tin.tif")
    result = engine.tin_interpolation(source, out, field_name="z", cell_size=10.0)
    values = _read(out)
    xs, ys = _cell_centres(out)
    assert values.shape == (10, 10)
    assert not np.ma.getmaskarray(values).any()
    assert float(np.abs(values - _plane(xs, ys)).max()) < 1e-9
    assert result["valid_cells"] == result["total_cells"] == 100
    assert _checks(result)["values_in_expected_range"] is True
    manifest = _manifest(result)
    assert manifest["parameters"] == {
        "field_name": "z", "cell_size": 10.0, "max_triangle_edge_length": None,
    }
    assert manifest["crs_decisions"]["analysis_crs"] == "EPSG:32632"


def test_a_tin_does_not_extrapolate_beyond_its_points(tmp_path):
    """Three samples: the triangle below the diagonal is the whole surface.

    On a 10 x 10 grid of 10 m cells the centres with x + y <= 100 number
    10 + 9 + ... + 1 = 55: the ten that lie exactly on the hypotenuse are on
    the hull, and the hull is part of the surface. IDW fills all 100.
    """
    locations = [(0, 0), (100, 0), (0, 100)]
    source = _points(
        tmp_path, "three", [_plane(x, y) for x, y in locations], locations, field="z"
    )
    out = str(tmp_path / "tin.tif")
    result = engine.tin_interpolation(source, out, field_name="z", cell_size=10.0)
    values = _read(out)
    xs, ys = _cell_centres(out)
    inside = xs + ys <= 100
    assert (np.ma.getmaskarray(values) == ~inside).all()
    assert (result["valid_cells"], result["total_cells"]) == (55, 100)
    assert float(np.abs(values - _plane(xs, ys))[inside].max()) < 1e-9
    assert any("does not extrapolate" in note for note in _manifest(result)["notes"])

    filled = _read(
        engine.idw_interpolation(
            source, str(tmp_path / "idw.tif"), field_name="z", cell_size=10.0
        )["output"]
    )
    assert not np.ma.getmaskarray(filled).any()


def test_a_tin_never_leaves_the_range_of_its_samples(tmp_path):
    locations = [*_SQUARE, (50, 50)]
    source = _points(tmp_path, "peak", [10.0, 10.0, 10.0, 10.0, 90.0], locations)
    out = str(tmp_path / "tin.tif")
    result = engine.tin_interpolation(source, out, field_name="mm", cell_size=10.0)
    values = _read(out)
    assert 10.0 <= float(values.min()) and float(values.max()) <= 90.0
    # The four cells around the peak have centres 5 m from it in x and in y. On
    # a triangle with the peak and one side of the square, the height falls
    # linearly from 90 at the centre to 10 at 50 m: 90 - 80 * 5 / 50 = 82.
    assert float(values.max()) == pytest.approx(82.0)
    assert _checks(result)["values_in_expected_range"] is True


def test_a_tin_refuses_two_values_at_one_location(tmp_path):
    """Measured: the library keeps 1 and drops 9, and says nothing."""
    source = _points(
        tmp_path, "twice", [1.0, 9.0, 3.0, 4.0], [(0, 0), (0, 0), (100, 0), (0, 100)]
    )
    with pytest.raises(ValueError, match="more than one value"):
        engine.tin_interpolation(
            source, str(tmp_path / "x.tif"), field_name="mm", cell_size=10.0
        )


def test_a_tin_accepts_a_repeated_measurement_and_says_so(tmp_path):
    source = _points(
        tmp_path, "same", [1.0, 1.0, 3.0, 4.0], [(0, 0), (0, 0), (100, 0), (0, 100)]
    )
    result = engine.tin_interpolation(
        source, str(tmp_path / "tin.tif"), field_name="mm", cell_size=10.0
    )
    assert any("repeat a location" in note for note in _manifest(result)["notes"])


def test_a_multipoint_lends_its_value_to_each_of_its_parts(tmp_path):
    """Two rows, four locations: the bottom edge at 10 and the top edge at 40.

    The surface depends on y alone, 10 + 30 * y / 100, whichever diagonal the
    triangulation takes: 11.5 at the lowest row of centres (y = 5) and 38.5 at
    the highest (y = 95).
    """
    from shapely.geometry import MultiPoint

    path = tmp_path / "pairs.parquet"
    gpd.GeoDataFrame(
        {"mm": [10.0, 40.0]},
        geometry=[MultiPoint([(0, 0), (100, 0)]), MultiPoint([(0, 100), (100, 100)])],
        crs="EPSG:32632",
    ).to_parquet(path)
    out = str(tmp_path / "tin.tif")
    engine.tin_interpolation(str(path), out, field_name="mm", cell_size=10.0)
    values = _read(out)
    _, ys = _cell_centres(out)
    assert float(np.abs(values - (10.0 + 30.0 * ys / 100.0)).max()) < 1e-9
    assert float(values.min()) == pytest.approx(11.5)
    assert float(values.max()) == pytest.approx(38.5)


@pytest.mark.parametrize(
    ("locations", "said"),
    [
        ([(0, 0), (100, 0)], "2 distinct location"),
        ([(0, 0), (50, 0), (100, 0)], "all on one line"),
    ],
)
def test_a_tin_refuses_points_that_make_no_triangle(locations, said, tmp_path):
    source = _points(tmp_path, "line", [float(i) for i in range(len(locations))], locations)
    with pytest.raises(ValueError, match=said):
        engine.tin_interpolation(
            source, str(tmp_path / "x.tif"), field_name="mm", cell_size=10.0
        )


def test_a_tin_refuses_a_layer_that_is_not_points(tmp_path):
    path = tmp_path / "plots.parquet"
    gpd.GeoDataFrame(
        {"mm": [1.0]}, geometry=[Point(50, 50).buffer(40)], crs="EPSG:32632"
    ).to_parquet(path)
    with pytest.raises(ValueError, match="reads point layers"):
        engine.tin_interpolation(
            str(path), str(tmp_path / "x.tif"), field_name="mm", cell_size=10.0
        )


def test_a_surface_with_no_valid_cell_fails_with_its_record_on_disk(tmp_path):
    """Every triangle of the square-with-centre has a 100 m side or two of 70.7.

    Capping the edge at 60 m leaves no triangle, so every cell is nodata. That
    is a result nobody asked for: the check fails, and the manifest that says
    so is written before the error is raised.
    """
    from mapsmith import verify

    locations = [*_SQUARE, (50, 50)]
    source = _points(tmp_path, "sparse", [1.0, 2.0, 3.0, 4.0, 5.0], locations)
    out = tmp_path / "tin.tif"
    with pytest.raises(verify.VerificationError):
        engine.tin_interpolation(
            source, str(out), field_name="mm", cell_size=10.0,
            max_triangle_edge_length=60.0,
        )
    manifests = list(tmp_path.glob("tin.tif*provenance.json"))
    assert len(manifests) == 1
    recorded = json.loads(manifests[0].read_text(encoding="utf-8"))
    failed = [c["name"] for c in recorded["verification"] if not c["passed"]]
    assert failed == ["result_not_empty"]


@pytest.mark.parametrize("operation", ["idw_interpolation", "tin_interpolation"])
@pytest.mark.parametrize("cell_size", [0.0, -5.0, float("nan"), float("inf")])
def test_an_interpolator_refuses_a_cell_size_that_is_not_a_size(
    operation, cell_size, tmp_path
):
    source = _points(tmp_path, "sq", [1.0, 2.0, 3.0, 4.0], _SQUARE)
    with pytest.raises(ValueError, match="cell_size must be positive and finite"):
        getattr(engine, operation)(
            source, str(tmp_path / "x.tif"), field_name="mm", cell_size=cell_size
        )


@pytest.mark.parametrize("limit", [0.0, -1.0, float("nan"), float("inf")])
def test_a_tin_refuses_an_edge_limit_that_is_not_a_length(limit, tmp_path):
    """NaN passed `<= 0` and went into the manifest as a NaN literal: not JSON."""
    source = _points(tmp_path, "sq", [1.0, 2.0, 3.0, 4.0], _SQUARE)
    with pytest.raises(ValueError, match="max_triangle_edge_length must be positive"):
        engine.tin_interpolation(
            source, str(tmp_path / "x.tif"), field_name="mm", cell_size=10.0,
            max_triangle_edge_length=limit,
        )


def test_the_edge_limit_blanks_the_long_triangles_and_keeps_the_short_ones(tmp_path):
    """Two clusters 1 km apart, each a 100 m square with its centre.

    With no limit the triangulation bridges the gap and the whole 1100 m strip
    is surface. Every triangle inside a cluster has sides of 100 m or 70.7 m;
    every triangle across the gap has a side of 900 m or more. A limit of
    150 m keeps the first kind only: 10 x 10 cells per cluster, 200 in all.
    """
    near = [*_SQUARE, (50, 50)]
    far = [(x + 1000, y) for x, y in near]
    source = _points(tmp_path, "fields", [5.0] * 10, [*near, *far])
    bridged = engine.tin_interpolation(
        source, str(tmp_path / "bridged.tif"), field_name="mm", cell_size=10.0
    )
    assert (bridged["valid_cells"], bridged["total_cells"]) == (1100, 1100)
    capped = engine.tin_interpolation(
        source, str(tmp_path / "capped.tif"), field_name="mm", cell_size=10.0,
        max_triangle_edge_length=150.0,
    )
    assert capped["valid_cells"] == 200
    xs, _ = _cell_centres(capped["output"])
    blank = np.ma.getmaskarray(_read(capped["output"]))
    assert (blank == ((xs > 100) & (xs < 1000))).all()


def test_a_tin_refuses_a_sample_it_could_not_tell_from_an_empty_cell(tmp_path):
    """-32768 is what the engine writes for a cell with no value."""
    source = _points(tmp_path, "sentinel", [-32768.0, 2.0, 3.0, 4.0], _SQUARE)
    with pytest.raises(ValueError, match="could not be told apart"):
        engine.tin_interpolation(
            source, str(tmp_path / "x.tif"), field_name="mm", cell_size=10.0
        )


def test_a_tin_reads_x_and_y_and_says_so_when_z_differs(tmp_path):
    path = tmp_path / "stacked.parquet"
    gpd.GeoDataFrame(
        {"mm": [1.0, 9.0, 3.0, 4.0]},
        geometry=[Point(0, 0, 10), Point(0, 0, 20), Point(100, 0, 0), Point(0, 100, 0)],
        crs="EPSG:32632",
    ).to_parquet(path)
    with pytest.raises(ValueError, match="a z coordinate is not read"):
        engine.tin_interpolation(
            str(path), str(tmp_path / "x.tif"), field_name="mm", cell_size=10.0
        )


# ------------------------------------------------------------------ viewshed

def test_the_viewshed_counts_stations_and_does_not_flag_them(tmp_path):
    """The tool's own documentation says the opposite, so this is measured.

    Whitebox's help for Viewshed states "the output image will be a Boolean
    raster, containing 1's and 0's". On flat ground with two stations, every
    cell here comes back 2.0 — the classic source calls `increment`, not
    `set_value`. A caller who summed the raster expecting a visible area would
    be out by a factor of the station count, and nothing would raise.

    This test is the reason that claim is safe to put in a docstring: if a
    future version really does turn the output into a flag, this fails.
    """
    pytest.importorskip("whitebox_workflows")
    import geopandas as gpd
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    from shapely.geometry import Point

    dem = tmp_path / "flat.tif"
    with rasterio.open(
        dem, "w", driver="GTiff", height=21, width=21, count=1, dtype="float32",
        crs="EPSG:32632", transform=from_origin(0, 210, 10, 10),
    ) as dst:
        dst.write(np.zeros((21, 21), dtype="float32"), 1)

    stations = tmp_path / "stations.gpkg"
    gpd.GeoDataFrame(
        {"id": [1, 2]},
        geometry=[Point(55, 105), Point(155, 105)],
        crs="EPSG:32632",
    ).to_file(stations, layer="s", driver="GPKG")

    out = tmp_path / "seen.tif"
    result = engine.viewshed(
        str(dem), str(stations), str(out), station_height=2.0
    )
    assert result["stations"] == 2

    with rasterio.open(out) as ds:
        values = ds.read(1, masked=True)
    assert float(values.max()) == 2.0, (
        "two stations on flat ground see every cell, so a count says 2 and a "
        "boolean says 1 — this is the measurement the docstring rests on"
    )

    manifest = json.loads((tmp_path / "seen.tif.provenance.json").read_text(encoding="utf-8"))
    assert manifest["parameters"]["output_meaning"].startswith("number of stations")
    assert any("NUMBER of stations" in note for note in manifest["notes"])


def test_the_station_height_unit_is_stated_because_it_is_not_metres(tmp_path):
    """`station_height` is in the DEM's Z unit. On a DEM in feet, 2.0 is two
    feet, and an eye height of 0.6 m would be a silent error with a
    plausible-looking viewshed to show for it."""
    pytest.importorskip("whitebox_workflows")
    import geopandas as gpd
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin
    from shapely.geometry import Point

    dem = tmp_path / "flat.tif"
    with rasterio.open(
        dem, "w", driver="GTiff", height=5, width=5, count=1, dtype="float32",
        crs="EPSG:32632", transform=from_origin(0, 50, 10, 10),
    ) as dst:
        dst.write(np.zeros((5, 5), dtype="float32"), 1)
    stations = tmp_path / "s.gpkg"
    gpd.GeoDataFrame(
        {"id": [1]}, geometry=[Point(25, 25)], crs="EPSG:32632"
    ).to_file(stations, layer="s", driver="GPKG")

    out = tmp_path / "one.tif"
    engine.viewshed(str(dem), str(stations), str(out), station_height=1.7)
    manifest = json.loads((tmp_path / "one.tif.provenance.json").read_text(encoding="utf-8"))
    assert manifest["parameters"]["height_unit"].startswith("the DEM's Z unit")
    assert manifest["parameters"]["station_height"] == 1.7


def test_a_viewshed_with_no_stations_is_refused_not_answered_with_zeros(tmp_path):
    pytest.importorskip("whitebox_workflows")
    import geopandas as gpd
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    dem = tmp_path / "flat.tif"
    with rasterio.open(
        dem, "w", driver="GTiff", height=5, width=5, count=1, dtype="float32",
        crs="EPSG:32632", transform=from_origin(0, 50, 10, 10),
    ) as dst:
        dst.write(np.zeros((5, 5), dtype="float32"), 1)
    empty = tmp_path / "none.gpkg"
    gpd.GeoDataFrame(
        {"id": []}, geometry=gpd.GeoSeries([], dtype="geometry"), crs="EPSG:32632"
    ).to_file(empty, layer="s", driver="GPKG")

    with pytest.raises(ValueError, match="nothing to see from"):
        engine.viewshed(
            str(dem), str(empty), str(tmp_path / "x.tif"), station_height=2.0
        )


def test_confusing_the_two_pointer_tables_rotates_the_network_by_one_step():
    """The reason the manifest carries the table and not its name.

    Both conventions are clockwise and use the same set of codes -- the powers
    of two from 1 to 128 -- one position apart. So a consumer that reads a
    pointer raster under the wrong table gets a legal direction in every cell
    and a drainage network rotated 45 degrees, uniformly, in one turn. Nothing
    raises, the set check in the engine passes, and a rotated network still
    looks like a drainage network.

    Closed form, and it holds the comment above POINTER_ENCODINGS honest: until
    2026-09-22 that comment said the two tables "agree on no direction at all"
    and that reading one as the other mirrors the network about the NE-SW axis.
    Both followed from a table attributed to the WhiteboxTools manual that the
    manual does not contain -- `d8_pointer.rs` documents the engine's default
    exactly as the engine writes it. This test is what that paragraph now rests
    on, so a future edit that reintroduces the mirror claim fails here.
    """
    clockwise = [
        "north", "northeast", "east", "southeast",
        "south", "southwest", "west", "northwest",
    ]
    ne_first = engine.POINTER_ENCODINGS["northeast_first"]
    e_first = engine.POINTER_ENCODINGS["east_first"]

    assert set(ne_first.values()) == set(e_first.values()), (
        "the two tables no longer share a code set, so a wrong reading would be "
        "detectable by the set check and this whole argument changes"
    )

    for index, direction in enumerate(clockwise):
        code = ne_first[direction]
        read_as = next(k for k, v in e_first.items() if v == code)
        expected = clockwise[(index + 1) % len(clockwise)]
        assert read_as == expected, (
            f"a cell flowing {direction} holds {code}; read with the other table "
            f"that is {read_as}, and one step clockwise from {direction} is "
            f"{expected} -- the rotation is no longer uniform"
        )
