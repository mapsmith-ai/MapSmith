"""Where a raster's values are, on the fixture that proved we did not know.

Argleton trap 024 plants a DEM that declares `AREA_OR_POINT=Point` — every value
a sample at a grid node rather than an average over a cell — and asks where the
lowest one is. MapSmith reported `unsupported` twice: it had no operation that
answers *where*, and no line of code anywhere that read the tag.

The fixtures here are the trap's, rebuilt: the same 8×8 surface at 30 m spacing
with a strict minimum at row 2, column 3, written once as `Point` and once as
`Area`. The two correct answers are 412090 and 412105, and each is the other's
failure. Every test below asserts one of them, so a system that hard-codes
either convention fails half of this file.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

rasterio = pytest.importorskip("rasterio")

from rasterio.transform import from_origin

from mapsmith import grid
from mapsmith.engines import raster, sampling

EAST0, NORTH0, SPACING, SIZE = 412000.0, 5108000.0, 30.0, 8
LOW_ROW, LOW_COLUMN = 2, 3

#: The node, and the cell centre. Half a cell apart, in each axis.
NODE = (EAST0 + LOW_COLUMN * SPACING, NORTH0 - LOW_ROW * SPACING)
CENTRE = (NODE[0] + SPACING / 2, NODE[1] - SPACING / 2)


def hollow(tmp_path: Path, tag: str) -> str:
    """The trap's surface: z = 300 + 0.5((c-3)² + (r-2)²), strict minimum at (2,3)."""
    rows, columns = np.mgrid[0:SIZE, 0:SIZE]
    surface = 300.0 + 0.5 * ((columns - LOW_COLUMN) ** 2 + (rows - LOW_ROW) ** 2)
    path = tmp_path / f"hollow_{tag.lower()}.tif"
    with rasterio.open(
        path, "w", driver="GTiff", height=SIZE, width=SIZE, count=1,
        dtype="float32", crs="EPSG:32632",
        transform=from_origin(EAST0, NORTH0, SPACING, SPACING),
    ) as dst:
        dst.write(surface.astype("float32"), 1)
        dst.update_tags(AREA_OR_POINT=tag)
    return str(path)


# --- the module that decides ------------------------------------------------


def test_the_tag_decides_and_anything_else_is_area(tmp_path):
    """Area is the default and the safe reading: treating an unreadable tag as
    point would move every position on files that are perfectly fine."""
    with rasterio.open(hollow(tmp_path, "Point")) as src:
        assert grid.registration(src) == "point"
        assert grid.offset(src) == 0.0
    with rasterio.open(hollow(tmp_path, "Area")) as src:
        assert grid.registration(src) == "area"
        assert grid.offset(src) == 0.5

    plain = tmp_path / "untagged.tif"
    with rasterio.open(
        plain, "w", driver="GTiff", height=2, width=2, count=1, dtype="float32",
        crs="EPSG:32632", transform=from_origin(0, 10, 5, 5),
    ) as dst:
        dst.write(np.zeros((2, 2), dtype="float32"), 1)
    with rasterio.open(plain) as src:
        assert grid.registration(src) == "area"


def test_the_position_of_a_cell_is_the_node_or_the_centre(tmp_path):
    with rasterio.open(hollow(tmp_path, "Point")) as src:
        assert grid.sample_xy(src, LOW_ROW, LOW_COLUMN) == pytest.approx(NODE)
        # And rasterio's own helper does not agree, which is the whole point.
        assert src.xy(LOW_ROW, LOW_COLUMN) == pytest.approx(CENTRE)
    with rasterio.open(hollow(tmp_path, "Area")) as src:
        assert grid.sample_xy(src, LOW_ROW, LOW_COLUMN) == pytest.approx(CENTRE)


def test_which_cell_a_position_belongs_to_flips_with_the_registration(tmp_path):
    """Under point registration a coordinate belongs to the nearest NODE, so a
    position 2 m east of a node is that node's, where under area registration
    the same offset from a centre can be a different cell entirely."""
    with rasterio.open(hollow(tmp_path, "Point")) as src:
        assert grid.sample_index(src, NODE[0] + 2, NODE[1] - 2) == (LOW_ROW, LOW_COLUMN)
        # Row first, column second. Unpacking it the other way is the axis-order
        # defect, and it happened here once: the sampling tests caught it.
        row, column = grid.sample_index(src, NODE[0], NODE[1])
        assert (row, column) == (LOW_ROW, LOW_COLUMN)
    with rasterio.open(hollow(tmp_path, "Area")) as src:
        assert grid.sample_index(src, CENTRE[0], CENTRE[1]) == (LOW_ROW, LOW_COLUMN)


def test_the_registration_is_recorded_under_its_own_key(tmp_path):
    """It is merged into `crs_decisions`, which already has a `reason`.

    The first version used `reason` and silently replaced the sentence
    explaining a reprojection with one about cell registration. An existing test
    caught it. Two different reasons under one key is a defect wherever it
    happens, so the key is checked here rather than left to luck.
    """
    with rasterio.open(hollow(tmp_path, "Point")) as src:
        described = grid.describe(src)
    assert "reason" not in described
    assert described["raster_registration"] == "point"
    assert "AREA_OR_POINT=Point" in described["raster_registration_reason"]


# --- the operation that answers where ---------------------------------------


def test_the_lowest_cell_is_at_the_node_on_a_point_registered_dem(tmp_path):
    """Argleton trap 024, answered from this side.

    412090, not 412105. The engine that says 412105 is reading the file as if
    the tag were not there, and the engine that says 412120 is whitebox.
    """
    answer = raster.locate_extreme_cell(hollow(tmp_path, "Point"), "min")
    assert answer["x"] == pytest.approx(NODE[0])
    assert answer["y"] == pytest.approx(NODE[1])
    assert answer["value"] == pytest.approx(300.0)
    assert (answer["row"], answer["column"]) == (LOW_ROW, LOW_COLUMN)
    assert answer["raster_registration"] == "point"


def test_the_same_surface_area_registered_answers_half_a_cell_away(tmp_path):
    """The clean twin. Its correct answer is the trap's wrong one.

    A fix that subtracts half a cell unconditionally passes the test above and
    fails this one, which is precisely why Argleton ships the pair.
    """
    answer = raster.locate_extreme_cell(hollow(tmp_path, "Area"), "min")
    assert answer["x"] == pytest.approx(CENTRE[0])
    assert answer["y"] == pytest.approx(CENTRE[1])
    assert answer["raster_registration"] == "area"


def test_nodata_does_not_win_the_search_for_a_minimum(tmp_path):
    """A nodata of -9999 beats every real elevation, and the answer would be the
    position of a hole reported as the bottom of a valley."""
    path = tmp_path / "holed.tif"
    surface = np.full((4, 4), 100.0, dtype="float32")
    surface[1, 1] = 50.0
    surface[3, 3] = -9999.0
    with rasterio.open(
        path, "w", driver="GTiff", height=4, width=4, count=1, dtype="float32",
        crs="EPSG:32632", transform=from_origin(0, 40, 10, 10), nodata=-9999.0,
    ) as dst:
        dst.write(surface, 1)

    answer = raster.locate_extreme_cell(str(path), "min")
    assert answer["value"] == pytest.approx(50.0)
    assert (answer["row"], answer["column"]) == (1, 1)
    assert answer["nodata_cells"] == 1


def test_a_tie_is_reported_rather_than_broken_in_silence(tmp_path):
    """A plateau is a fact about the data. Reporting one of its cells as *the*
    position, with nothing said, turns it into a confident single answer."""
    path = tmp_path / "plateau.tif"
    surface = np.full((3, 3), 5.0, dtype="float32")
    surface[0, 0] = 1.0
    surface[2, 2] = 1.0
    with rasterio.open(
        path, "w", driver="GTiff", height=3, width=3, count=1, dtype="float32",
        crs="EPSG:32632", transform=from_origin(0, 30, 10, 10),
    ) as dst:
        dst.write(surface, 1)

    answer = raster.locate_extreme_cell(str(path), "min")
    assert answer["tied_cells"] == 2
    assert "plateau" in answer["note"]


def test_a_raster_without_a_crs_is_refused(tmp_path):
    path = tmp_path / "nocrs.tif"
    with rasterio.open(
        path, "w", driver="GTiff", height=2, width=2, count=1, dtype="float32",
        transform=from_origin(0, 10, 5, 5),
    ) as dst:
        dst.write(np.zeros((2, 2), dtype="float32"), 1)
    with pytest.raises(ValueError, match="no CRS"):
        raster.locate_extreme_cell(str(path), "min")


# --- everything that samples ------------------------------------------------


def test_sampling_reads_the_value_at_the_node_not_half_a_cell_away(tmp_path):
    """`sample_raster_at_points`, `elevation_profile` and `line_of_sight` all
    go through the same reader, so this covers the three of them.

    A point exactly on a node must read that node's value exactly, under either
    interpolation. Under the area reading the same coordinate falls on a cell
    boundary and bilinear returns the average of two neighbours instead.
    """
    import geopandas as gpd
    from shapely.geometry import Point

    points = tmp_path / "at_the_node.parquet"
    gpd.GeoDataFrame(
        {"n": [1]}, geometry=[Point(*NODE)], crs="EPSG:32632"
    ).to_parquet(points)

    for method in ("nearest", "bilinear"):
        out = tmp_path / f"sampled_{method}.parquet"
        sampling.sample_raster_at_points(
            hollow(tmp_path, "Point"), str(points), str(out), method
        )
        got = gpd.read_parquet(out)["value"].iloc[0]
        assert got == pytest.approx(300.0), (
            f"{method} read {got} at the node whose value is 300.0 — the sample "
            "positions are half a cell from where the file says they are"
        )


def test_the_same_point_on_the_area_twin_reads_the_boundary_average(tmp_path):
    """The other half of the pair, and the reason the fix cannot be a constant.

    On the area-registered file the node coordinate sits exactly between two
    cell centres, so bilinear correctly returns their average — 300.5, not 300.
    A reader that always subtracted half a cell would answer 300 here and be
    wrong.
    """
    import geopandas as gpd
    from shapely.geometry import Point

    points = tmp_path / "at_the_node.parquet"
    gpd.GeoDataFrame(
        {"n": [1]}, geometry=[Point(*NODE)], crs="EPSG:32632"
    ).to_parquet(points)
    out = tmp_path / "area_sampled.parquet"
    sampling.sample_raster_at_points(
        hollow(tmp_path, "Area"), str(points), str(out), "bilinear"
    )
    # Between the centres of columns 2 and 3 at row 2: values 300.5 and 300.0,
    # and between rows 1 and 2 as well, so the four-corner average is 300.5.
    assert gpd.read_parquet(out)["value"].iloc[0] == pytest.approx(300.5)


# --- what gets written out --------------------------------------------------


def test_the_registration_survives_an_operation_that_writes_a_raster(tmp_path):
    """`profile.copy()` does not carry tags, so every raster MapSmith wrote came
    back area-registered whatever went in — the same silent error one step
    downstream, with nothing in the output to say so."""
    import geopandas as gpd
    from shapely.geometry import box

    source = hollow(tmp_path, "Point")
    mask = tmp_path / "mask.parquet"
    gpd.GeoDataFrame(
        {"id": [1]},
        geometry=[box(412000.0, 5107800.0, 412150.0, 5108000.0)],
        crs="EPSG:32632",
    ).to_parquet(mask)
    out = tmp_path / "clipped.tif"
    raster.clip_raster(source, str(mask), str(out))

    with rasterio.open(out) as dst:
        assert grid.registration(dst) == "point", (
            "the output lost its point registration, so every position derived "
            "from it is half a cell wrong and the file no longer says so"
        )


def test_zonal_statistics_weights_cells_around_their_own_samples(tmp_path):
    """exactextract takes the footprint from the transform and cannot be told
    otherwise, so the zones are offset for the coverage computation instead.

    The zone here is one cell wide, centred on the node. Under the correct
    reading it covers exactly that one sample and the mean is its value; read as
    area-registered it would straddle two cells and average them.
    """
    import geopandas as gpd
    from shapely.geometry import box

    zones = tmp_path / "zone.parquet"
    half = SPACING / 2
    gpd.GeoDataFrame(
        {"id": [1]},
        geometry=[box(NODE[0] - half, NODE[1] - half, NODE[0] + half, NODE[1] + half)],
        crs="EPSG:32632",
    ).to_parquet(zones)

    out = tmp_path / "zonal.parquet"
    result = raster.zonal_statistics(
        hollow(tmp_path, "Point"), str(zones), str(out), stats=["mean"]
    )
    assert gpd.read_parquet(out)["mean"].iloc[0] == pytest.approx(300.0, abs=1e-4)

    manifest = json.loads(Path(result["provenance"]).read_text(encoding="utf-8"))
    assert manifest["crs_decisions"]["raster_registration"] == "point"
    assert any("offset by" in note for note in manifest["notes"])
    # The geometry handed back is the caller's, not the shifted copy.
    assert gpd.read_parquet(out).geometry.iloc[0].bounds == pytest.approx(
        (NODE[0] - half, NODE[1] - half, NODE[0] + half, NODE[1] + half)
    )


def test_only_one_module_decides_where_a_cell_is(tmp_path):
    """The guard that keeps this from being half-applied again.

    #28 happened because "open a vector file" was six copies of one decision.
    This was the same shape: every place that turned a cell index into a
    coordinate did what rasterio does, and none had asked the question. A
    seventh copy is the only way to reintroduce it, so the seventh copy is what
    fails here.
    """
    import re

    import mapsmith

    package = Path(mapsmith.__file__).parent
    allowed = {"grid.py"}
    pattern = re.compile(r"\.xy\s*\(|\btransform\.xy\s*\(|(?<!sample_)\bindex\s*\(\s*\w+\.x")
    offenders = []
    for module in package.rglob("*.py"):
        if module.name in allowed:
            continue
        for number, line in enumerate(
            module.read_text(encoding="utf-8").splitlines(), 1
        ):
            if pattern.search(line) and "grid." not in line:
                offenders.append(f"{module.name}:{number}: {line.strip()}")
    assert not offenders, (
        "these lines turn a cell index into a coordinate without asking `grid` "
        f"which registration the file declares: {offenders}"
    )
