"""Which operations refuse a geographic CRS — executed, not read off the source.

`test_catalog_applicability.py` asserts the SET of operations declaring
`requires_projected_crs`. That assertion is a declaration compared with another
declaration, and on 27/08/2026 it nearly went in wrong: deriving the set by
looking for the word "geographic" near a `raise` in the engine source reported
five operations that do not refuse at all, because the word also appears in
their notes and warnings. Running them was what settled it.

So this file runs every whitebox operation on a geographic DEM and compares the
outcome with what the catalog claims. It is the slow half of a pair, and the
half that cannot be fooled by prose.
"""

import geopandas as gpd
import pytest
from shapely.geometry import Point

pytest.importorskip("whitebox_workflows")
rasterio = pytest.importorskip("rasterio")

import numpy as np
from rasterio.transform import from_origin

from mapsmith import catalog
from mapsmith.engines import whitebox_engine as engine

GEOGRAPHIC = "EPSG:4326"


@pytest.fixture(scope="module")
def geographic_inputs(tmp_path_factory):
    """A DEM, a mask, pour points and value points, all in EPSG:4326."""
    directory = tmp_path_factory.mktemp("geographic")
    dem = directory / "dem.tif"
    rows = cols = 10
    surface = np.fromfunction(lambda r, c: (r + c) * 3.0, (rows, cols)).astype("float32")
    with rasterio.open(
        dem, "w", driver="GTiff", height=rows, width=cols, count=1, dtype="float32",
        crs=GEOGRAPHIC, nodata=-9999.0, transform=from_origin(12.0, 42.0, 0.001, 0.001),
    ) as dst:
        dst.write(surface, 1)

    pour = directory / "pour.gpkg"
    gpd.GeoDataFrame(geometry=[Point(12.004, 41.996)], crs=GEOGRAPHIC).to_file(
        pour, driver="GPKG"
    )
    values = directory / "values.gpkg"
    gpd.GeoDataFrame(
        {"v": [1.0, 2.0, 3.0, 4.0]},
        geometry=[
            Point(12.001, 41.999), Point(12.008, 41.999),
            Point(12.001, 41.992), Point(12.008, 41.992),
        ],
        crs=GEOGRAPHIC,
    ).to_file(values, driver="GPKG")
    return directory, str(dem), str(pour), str(values)


def _calls(directory, dem, pour, values):
    def out(name):
        return str(directory / name)

    return {
        "hillshade": lambda: engine.hillshade(dem, out("hs.tif")),
        "slope": lambda: engine.slope(dem, out("slope.tif")),
        "aspect": lambda: engine.aspect(dem, out("aspect.tif")),
        "flow_accumulation": lambda: engine.flow_accumulation(dem, out("facc.tif")),
        "watershed": lambda: engine.watershed(dem, pour, out("ws.tif")),
        "focal_statistics": lambda: engine.focal_statistics(
            dem, out("focal.tif"), statistic="mean", window=3
        ),
        "extract_streams": lambda: engine.extract_streams(
            dem, out("streams.tif"), threshold=5.0
        ),
        "curvature": lambda: engine.curvature(dem, out("curv.tif"), kind="profile"),
        "flow_direction": lambda: engine.flow_direction(dem, out("d8.tif")),
        "euclidean_distance": lambda: engine.euclidean_distance(dem, out("dist.tif")),
        "idw_interpolation": lambda: engine.idw_interpolation(
            values, out("idw.tif"), field_name="v", cell_size=0.001
        ),
    }


def test_the_catalog_declaration_matches_what_the_engines_actually_do(
    geographic_inputs,
):
    directory, dem, pour, values = geographic_inputs
    declared = {
        entry["name"]: entry["applicability"]["requires_projected_crs"]
        for entry in catalog.OPERATIONS
    }
    disagreements = []
    for name, call in _calls(directory, dem, pour, values).items():
        try:
            call()
            refuses = False
        except ValueError as error:
            refuses = "geographic" in str(error).lower()
        if refuses != declared[name]:
            disagreements.append(
                f"{name}: the engine refuses={refuses}, the catalog declares "
                f"{declared[name]}"
            )
    assert not disagreements, (
        "the catalog and the engines disagree about which operations need a "
        f"projected CRS: {disagreements}. A declaration of False on an operation "
        "that refuses means the applicability filter offers an operation that "
        "will raise; a declaration of True on one that does not means it is "
        "hidden from a caller who could have used it."
    )


def test_at_least_one_operation_in_each_group_is_covered():
    """A sweep that measured nothing would pass. This says the sweep has both
    kinds in it, so the comparison above is doing work in both directions."""
    declared = {
        entry["name"]: entry["applicability"]["requires_projected_crs"]
        for entry in catalog.OPERATIONS
    }
    covered = set(_calls("", "", "", ""))
    assert any(declared[n] for n in covered), "no refusing operation is exercised"
    assert any(not declared[n] for n in covered), "no accepting operation is exercised"
