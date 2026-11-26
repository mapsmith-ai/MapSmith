"""tin_interpolation: closed-form values on GEOS and GDAL.

Every expected number here is derivable by hand from the fixture. The operation
needs the raster extra and nothing else, so this file does too: until it was
split out, these tests sat in a module that skips without whitebox, and an
installation with only `[raster]` ran none of them.
"""

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
from shapely.geometry import Point

rasterio = pytest.importorskip("rasterio")

from mapsmith.engines import interpolation


def _points(tmp_path, name, values, locations, *, field="mm", crs="EPSG:32632"):
    path = tmp_path / f"{name}.parquet"
    gpd.GeoDataFrame(
        {field: values}, geometry=[Point(*xy) for xy in locations], crs=crs
    ).to_parquet(path)
    return str(path)


_SQUARE = [(0, 0), (100, 0), (0, 100), (100, 100)]


def _read(path):
    with rasterio.open(path) as src:
        return src.read(1, masked=True)


def _manifest(result):
    return json.loads(Path(result["provenance"]).read_text(encoding="utf-8"))


def _checks(result):
    return {c["name"]: c["passed"] for c in _manifest(result)["verification"]}


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
    result = interpolation.tin_interpolation(source, out, field_name="z", cell_size=10.0)
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
    result = interpolation.tin_interpolation(source, out, field_name="z", cell_size=10.0)
    values = _read(out)
    xs, ys = _cell_centres(out)
    inside = xs + ys <= 100
    assert (np.ma.getmaskarray(values) == ~inside).all()
    assert (result["valid_cells"], result["total_cells"]) == (55, 100)
    assert float(np.abs(values - _plane(xs, ys))[inside].max()) < 1e-9
    assert any("does not extrapolate" in note for note in _manifest(result)["notes"])

    # IDW fills the rectangle; it lives on another engine, so only where that is installed.
    pytest.importorskip("whitebox_workflows")
    from mapsmith.engines import whitebox_engine

    filled = _read(
        whitebox_engine.idw_interpolation(
            source, str(tmp_path / "idw.tif"), field_name="z", cell_size=10.0
        )["output"]
    )
    assert not np.ma.getmaskarray(filled).any()


def test_a_tin_never_leaves_the_range_of_its_samples(tmp_path):
    locations = [*_SQUARE, (50, 50)]
    source = _points(tmp_path, "peak", [10.0, 10.0, 10.0, 10.0, 90.0], locations)
    out = str(tmp_path / "tin.tif")
    result = interpolation.tin_interpolation(source, out, field_name="mm", cell_size=10.0)
    values = _read(out)
    assert 10.0 <= float(values.min()) and float(values.max()) <= 90.0
    # The four cells around the peak have centres 5 m from it in x and in y. On
    # a triangle with the peak and one side of the square, the height falls
    # linearly from 90 at the centre to 10 at 50 m: 90 - 80 * 5 / 50 = 82.
    assert float(values.max()) == pytest.approx(82.0)
    assert _checks(result)["values_in_expected_range"] is True


def test_a_tin_refuses_a_geographic_layer(tmp_path):
    """Which samples make a triangle is decided by distances, and degrees are not."""
    source = _points(
        tmp_path, "degrees", [1.0, 2.0, 3.0, 4.0],
        [(12.001, 41.999), (12.008, 41.999), (12.001, 41.992), (12.008, 41.992)],
        crs="EPSG:4326",
    )
    out = tmp_path / "x.tif"
    with pytest.raises(ValueError, match="geographic CRS"):
        interpolation.tin_interpolation(
            source, str(out), field_name="mm", cell_size=0.001
        )
    assert not out.exists()


def test_a_tin_keeps_a_crs_that_has_no_epsg_code(tmp_path):
    """Written on whitebox, a custom CRS was lost on the way to the raster."""
    custom = (
        "+proj=tmerc +lat_0=41.9 +lon_0=12.5 +k=1 +x_0=1000 +y_0=2000 "
        "+ellps=GRS80 +units=m +no_defs"
    )
    source = _points(tmp_path, "local", [1.0, 2.0, 3.0, 4.0], _SQUARE, crs=custom)
    out = str(tmp_path / "tin.tif")
    result = interpolation.tin_interpolation(source, out, field_name="mm", cell_size=10.0)
    assert _checks(result)["crs_matches"] is True
    with rasterio.open(out) as written:
        assert written.crs.to_dict()["proj"] == "tmerc"
        assert written.crs.to_dict()["x_0"] == 1000


def test_a_tin_refuses_two_values_at_one_location(tmp_path):
    """Measured: the library keeps 1 and drops 9, and says nothing."""
    source = _points(
        tmp_path, "twice", [1.0, 9.0, 3.0, 4.0], [(0, 0), (0, 0), (100, 0), (0, 100)]
    )
    with pytest.raises(ValueError, match="more than one value"):
        interpolation.tin_interpolation(
            source, str(tmp_path / "x.tif"), field_name="mm", cell_size=10.0
        )


def test_a_tin_accepts_a_repeated_measurement_and_says_so(tmp_path):
    source = _points(
        tmp_path, "same", [1.0, 1.0, 3.0, 4.0], [(0, 0), (0, 0), (100, 0), (0, 100)]
    )
    result = interpolation.tin_interpolation(
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
    interpolation.tin_interpolation(str(path), out, field_name="mm", cell_size=10.0)
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
        interpolation.tin_interpolation(
            source, str(tmp_path / "x.tif"), field_name="mm", cell_size=10.0
        )


def test_a_tin_refuses_a_layer_that_is_not_points(tmp_path):
    path = tmp_path / "plots.parquet"
    gpd.GeoDataFrame(
        {"mm": [1.0]}, geometry=[Point(50, 50).buffer(40)], crs="EPSG:32632"
    ).to_parquet(path)
    with pytest.raises(ValueError, match="reads point layers"):
        interpolation.tin_interpolation(
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
        interpolation.tin_interpolation(
            source, str(out), field_name="mm", cell_size=10.0,
            max_triangle_edge_length=60.0,
        )
    manifests = list(tmp_path.glob("tin.tif*provenance.json"))
    assert len(manifests) == 1
    recorded = json.loads(manifests[0].read_text(encoding="utf-8"))
    failed = [c["name"] for c in recorded["verification"] if not c["passed"]]
    assert failed == ["result_not_empty"]


@pytest.mark.parametrize("cell_size", [0.0, -5.0, float("nan"), float("inf"), True])
def test_a_tin_refuses_a_cell_size_that_is_not_a_size(cell_size, tmp_path):
    """`True > 0` holds: a cell size of True made a grid of one-unit cells."""
    source = _points(tmp_path, "sq", [1.0, 2.0, 3.0, 4.0], _SQUARE)
    with pytest.raises(ValueError, match="cell_size must be positive and finite"):
        interpolation.tin_interpolation(
            source, str(tmp_path / "x.tif"), field_name="mm", cell_size=cell_size
        )


@pytest.mark.parametrize("limit", [0.0, -1.0, float("nan"), float("inf"), True])
def test_a_tin_refuses_an_edge_limit_that_is_not_a_length(limit, tmp_path):
    """NaN passed `<= 0` and went into the manifest as a NaN literal: not JSON."""
    source = _points(tmp_path, "sq", [1.0, 2.0, 3.0, 4.0], _SQUARE)
    with pytest.raises(ValueError, match="max_triangle_edge_length must be positive"):
        interpolation.tin_interpolation(
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
    bridged = interpolation.tin_interpolation(
        source, str(tmp_path / "bridged.tif"), field_name="mm", cell_size=10.0
    )
    assert (bridged["valid_cells"], bridged["total_cells"]) == (1100, 1100)
    capped = interpolation.tin_interpolation(
        source, str(tmp_path / "capped.tif"), field_name="mm", cell_size=10.0,
        max_triangle_edge_length=150.0,
    )
    assert capped["valid_cells"] == 200
    xs, _ = _cell_centres(capped["output"])
    blank = np.ma.getmaskarray(_read(capped["output"]))
    assert (blank == ((xs > 100) & (xs < 1000))).all()


def test_a_sample_of_minus_32768_is_a_measurement_and_stays_one(tmp_path):
    """Written on whitebox this was refused: -32768 is what that engine puts in an empty cell.

    Here an empty cell is NaN, which no sample can be, so the value is kept.
    The corner cell's centre is 5 m in from the sample in x and in y, on a
    surface that climbs 32770, 32771 and 32772 over 100 m from it.
    """
    source = _points(tmp_path, "low", [-32768.0, 2.0, 3.0, 4.0], _SQUARE)
    out = str(tmp_path / "tin.tif")
    result = interpolation.tin_interpolation(source, out, field_name="mm", cell_size=10.0)
    values = _read(out)
    assert result["valid_cells"] == 100
    assert float(values.min()) < -29000
    assert float(values.max()) <= 4.0
    with rasterio.open(out) as written:
        assert np.isnan(written.nodata)


def test_a_tin_reads_x_and_y_and_says_so_when_z_differs(tmp_path):
    path = tmp_path / "stacked.parquet"
    gpd.GeoDataFrame(
        {"mm": [1.0, 9.0, 3.0, 4.0]},
        geometry=[Point(0, 0, 10), Point(0, 0, 20), Point(100, 0, 0), Point(0, 100, 0)],
        crs="EPSG:32632",
    ).to_parquet(path)
    with pytest.raises(ValueError, match="a z coordinate is not read"):
        interpolation.tin_interpolation(
            str(path), str(tmp_path / "x.tif"), field_name="mm", cell_size=10.0
        )


# ------------------------------------------------- the edge of the surface


@pytest.mark.parametrize(
    ("locations", "valid", "given_back"),
    [
        # The hypotenuse x + y = 100 passes through ten cell centres. Below it
        # 9 + 8 + ... + 1 = 45 centres are strictly inside either triangle. This
        # one faces the way the rasteriser keeps, so nothing has to be given back.
        ([(0, 0), (100, 0), (0, 100)], 55, 0),
        ([(100, 0), (100, 100), (0, 100)], 55, 10),
        # A square on its corner, |x - 50| + |y - 50| <= 50: 40 centres strictly
        # inside and five on each of its four sides, two of which face west.
        ([(50, 0), (100, 50), (50, 100), (0, 50)], 60, 10),
    ],
)
def test_a_centre_on_the_edge_of_the_surface_is_part_of_it_whichever_way_it_faces(
    locations, valid, given_back, tmp_path
):
    """The rasteriser alone kept such a centre on an edge facing east and dropped
    it on one facing west: the second triangle, the mirror of the first, came out
    with 45 cells, and the diamond with 50."""
    source = _points(tmp_path, "shape", [float(i) for i in range(len(locations))], locations)
    result = interpolation.tin_interpolation(
        source, str(tmp_path / "tin.tif"), field_name="mm", cell_size=10.0
    )
    assert result["valid_cells"] == valid
    said = [note for note in _manifest(result)["notes"] if "exactly on the edge" in note]
    expected = (
        f"{given_back} cell(s) have their centre exactly on the edge of the "
        "surface and are part of it"
    )
    assert said == ([expected] if given_back else [])


def test_a_grid_taller_than_one_block_at_utm_coordinates_is_still_the_plane(tmp_path):
    """Six hundred rows, so the surface is read off in more than one block, at a
    northing of 4.5 million: the plane form written from the origin would
    subtract numbers of 1e12 to get one of 1e2."""
    west, south = 500_000.0, 4_500_000.0
    locations = [
        (west, south), (west + 400, south), (west, south + 600), (west + 400, south + 600),
        (west + 137, south + 251), (west + 300, south + 500),
    ]
    source = _points(
        tmp_path, "utm", [_plane(x, y) for x, y in locations], locations, field="z"
    )
    out = str(tmp_path / "tin.tif")
    result = interpolation.tin_interpolation(source, out, field_name="z", cell_size=1.0)
    values = _read(out)
    xs, ys = _cell_centres(out)
    assert values.shape == (600, 400)
    assert result["valid_cells"] == 240_000
    assert float(np.abs(values - _plane(xs, ys)).max()) < 1e-6
    assert any(note == "all 240000 cells hold a value" for note in _manifest(result)["notes"])


# ----------------------------------------------- where the data decides


def test_a_lattice_of_samples_is_said_to_have_two_triangulations(tmp_path):
    """Four samples on one circle have two Delaunay triangulations, and every
    square of a lattice is that case. Mirroring a 6 x 6 lattice changed cells by
    up to 37 on values of 0 to 100: not a parameter, but not nothing either."""
    lattice = [(20.0 * i, 20.0 * j) for i in range(6) for j in range(6)]
    rng = np.random.default_rng(4)
    source = _points(tmp_path, "lattice", list(rng.random(36) * 100), lattice)
    result = interpolation.tin_interpolation(
        source, str(tmp_path / "tin.tif"), field_name="mm", cell_size=5.0
    )
    # One pair of triangles for each of the 25 squares, at least.
    assert result["ambiguous_quadrilaterals"] >= 25
    assert any("two equally valid diagonals" in note for note in _manifest(result)["notes"])

    scattered = [(float(x), float(y)) for x, y in rng.random((36, 2)) * 100]
    plain = interpolation.tin_interpolation(
        _points(tmp_path, "scattered", list(rng.random(36)), scattered),
        str(tmp_path / "plain.tif"), field_name="mm", cell_size=5.0,
    )
    assert plain["ambiguous_quadrilaterals"] == 0


# ------------------------------------------------------------- refusals


def test_a_tin_refuses_a_system_that_is_neither_projected_nor_geographic(tmp_path):
    """Geocentric coordinates passed the geographic gate and made a raster."""
    source = _points(
        tmp_path, "ecef", [1.0, 2.0, 3.0, 4.0],
        [(4_600_000, 1_000_000), (4_600_100, 1_000_000), (4_600_000, 1_000_100),
         (4_600_100, 1_000_100)],
        crs="EPSG:4978",
    )
    out = tmp_path / "x.tif"
    with pytest.raises(ValueError, match="needs a projected CRS"):
        interpolation.tin_interpolation(source, str(out), field_name="mm", cell_size=10.0)
    assert not out.exists()


def test_a_tin_refuses_a_layer_with_no_crs(tmp_path):
    path = tmp_path / "bare.parquet"
    gpd.GeoDataFrame({"mm": [1.0, 2.0, 3.0]}, geometry=[Point(0, 0), Point(9, 0), Point(0, 9)]).to_parquet(path)
    with pytest.raises(ValueError, match="CRS"):
        interpolation.tin_interpolation(
            str(path), str(tmp_path / "x.tif"), field_name="mm", cell_size=1.0
        )


def test_a_tin_refuses_two_multipoints_that_disagree_at_one_location(tmp_path):
    from shapely.geometry import MultiPoint

    path = tmp_path / "overlap.parquet"
    gpd.GeoDataFrame(
        {"mm": [10.0, 40.0]},
        geometry=[MultiPoint([(0, 0), (100, 0)]), MultiPoint([(0, 0), (0, 100)])],
        crs="EPSG:32632",
    ).to_parquet(path)
    with pytest.raises(ValueError, match="more than one value"):
        interpolation.tin_interpolation(
            str(path), str(tmp_path / "x.tif"), field_name="mm", cell_size=10.0
        )


def test_a_layer_far_over_the_point_limit_is_refused_before_it_is_sorted(
    tmp_path, monkeypatch
):
    """The limit is on distinct locations, which costs a sort to count; sixteen
    times over it, the duplicates are not what the caller is waiting for."""
    locations = [(float(i), float(i * i % 7)) for i in range(20)]
    source = _points(tmp_path, "many", [1.0] * 20, locations)
    monkeypatch.setenv("MAPSMITH_MAX_TRIANGULATION_POINTS", "1")
    sorted_anyway = []
    monkeypatch.setattr(
        interpolation, "_distinct_samples", lambda *args: sorted_anyway.append(args)
    )
    with pytest.raises(ValueError, match="MAPSMITH_MAX_TRIANGULATION_POINTS"):
        interpolation.tin_interpolation(
            source, str(tmp_path / "x.tif"), field_name="mm", cell_size=1.0
        )
    assert not sorted_anyway


def test_a_cell_larger_than_the_triangles_fails_with_a_hint_that_names_it(tmp_path):
    from mapsmith import verify

    source = _points(tmp_path, "tiny", [1.0, 2.0, 3.0], [(0, 0), (4, 0), (0, 4)])
    out = tmp_path / "tin.tif"
    with pytest.raises(verify.VerificationError):
        interpolation.tin_interpolation(source, str(out), field_name="mm", cell_size=100.0)
    record = json.loads(next(tmp_path.glob("tin.tif*provenance.json")).read_text(encoding="utf-8"))
    empty = next(c for c in record["verification"] if c["name"] == "result_not_empty")
    assert empty["passed"] is False
    assert "cell size is larger than the triangles" in empty["hint"]


# -------------------------------------------- what a file may ask for


def test_a_multipoint_of_a_million_parts_is_refused_from_its_header(tmp_path, monkeypatch):
    """One row, one geometry, and the coordinates counted before any array of
    them exists: four million parts in a 69 MB file cost 2.8 GB to sort before
    the limit on distinct locations was looked at."""
    from shapely.geometry import MultiPoint

    rng = np.random.default_rng(8)
    path = tmp_path / "one_row.parquet"
    gpd.GeoDataFrame(
        {"mm": [1.0]}, geometry=[MultiPoint(rng.random((2000, 2)) * 100)], crs="EPSG:32632"
    ).to_parquet(path)
    monkeypatch.setenv("MAPSMITH_MAX_SAMPLES", "1000")
    looked = []
    monkeypatch.setattr(interpolation.shapely, "get_coordinates", lambda *a, **k: looked.append(a))
    with pytest.raises(ValueError, match="2,000 coordinates, over this server's limit of 1,000"):
        interpolation.tin_interpolation(
            str(path), str(tmp_path / "x.tif"), field_name="mm", cell_size=1.0
        )
    assert not looked


@pytest.mark.parametrize("suffix", [".aux.xml", ".msk", ".ovr"])
def test_a_tin_refuses_to_write_beside_a_file_gdal_would_read_first(suffix, tmp_path):
    """A `.aux.xml` placed beside the output path answered the read-back in the
    raster's place: a NoDataValue of the placer's choosing, every check green."""
    source = _points(tmp_path, "sq", [1.0, 2.0, 3.0, 4.0], _SQUARE)
    out = tmp_path / "tin.tif"
    Path(f"{out}{suffix}").write_text("<PAMDataset/>", encoding="utf-8")
    with pytest.raises(ValueError, match="beside it"):
        interpolation.tin_interpolation(str(source), str(out), field_name="mm", cell_size=10.0)
    assert not out.exists()


def test_the_read_back_is_of_the_raster_and_not_of_a_sidecar_that_arrives_later(tmp_path):
    """The check reads with PAM off: what it reports is in the file whose digest
    the record carries."""
    source = _points(tmp_path, "sq", [1.0, 2.0, 3.0, 4.0], _SQUARE)
    out = tmp_path / "tin.tif"
    interpolation.tin_interpolation(source, str(out), field_name="mm", cell_size=10.0)
    Path(f"{out}.aux.xml").write_text(
        "<PAMDataset><PAMRasterBand band=\"1\"><NoDataValue>2.5</NoDataValue>"
        "</PAMRasterBand></PAMDataset>",
        encoding="utf-8",
    )
    with rasterio.open(out) as through_the_sidecar:
        assert through_the_sidecar.nodata == 2.5
    with rasterio.Env(GDAL_PAM_ENABLED="NO"), rasterio.open(out) as the_file:
        assert np.isnan(the_file.nodata)
