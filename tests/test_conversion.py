"""Closed-form vector/raster conversion and plan integration."""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import box

from mapsmith.engines import conversion
from mapsmith.plans import Plan, execute

CRS = "EPSG:32632"


def reference(path, values=None, nodata=-9999):
    array = np.asarray(values if values is not None else np.zeros((2, 2)), dtype="int16")
    with rasterio.open(
        path, "w", driver="GTiff", width=array.shape[1], height=array.shape[0],
        count=1, dtype=array.dtype, crs=CRS, transform=from_origin(0, 20, 10, 10),
        nodata=nodata,
    ) as output:
        output.write(array, 1)
    return str(path)


def polygons(path, crs=CRS):
    gpd.GeoDataFrame(
        {"code": [7, 3]},
        geometry=[box(0, 0, 10, 20), box(10, 0, 20, 20)], crs=crs,
    ).to_file(path)
    return str(path)


def test_rasterize_burns_each_field_on_the_reference_grid(tmp_path):
    grid = reference(tmp_path / "reference.tif")
    layer = polygons(tmp_path / "zones.gpkg")
    output = tmp_path / "zones.tif"
    result = conversion.rasterize_layer(layer, grid, str(output), field_name="code", nodata=-1)

    with rasterio.open(output) as dataset:
        assert dataset.read(1).tolist() == [[7, 3], [7, 3]]
        assert dataset.dtypes == ("int32",)
        assert dataset.nodata == -1
        assert dataset.transform == rasterio.open(grid).transform
        assert dataset.crs.to_epsg() == 32632
    assert result["shape"] == [2, 2]
    assert result["verified"] is True
    record = json.loads((tmp_path / "zones.tif.provenance.json").read_text(encoding="utf-8"))
    assert record["crs_decisions"]["x-mapsmith:raster_registration"] == "area"
    assert record["crs_decisions"]["x-mapsmith:raster_registration_reason"]


def test_rasterize_refuses_mismatched_crs_and_null_fields(tmp_path):
    grid = reference(tmp_path / "reference.tif")
    layer = polygons(tmp_path / "zones.gpkg", "EPSG:4326")
    with pytest.raises(ValueError, match="different CRSs"):
        conversion.rasterize_layer(layer, grid, str(tmp_path / "no.tif"))

    layer = polygons(tmp_path / "valid.gpkg")
    frame = gpd.read_file(layer)
    frame.loc[0, "code"] = None
    frame.to_file(tmp_path / "nulls.gpkg")
    with pytest.raises(ValueError, match="null values"):
        conversion.rasterize_layer(
            str(tmp_path / "nulls.gpkg"), grid, str(tmp_path / "nulls.tif"),
            field_name="code",
        )


def test_rasterize_refuses_unrepresentable_integers_and_mask_nodata(tmp_path):
    grid = reference(tmp_path / "reference.tif")
    layer = tmp_path / "large.gpkg"
    gpd.GeoDataFrame(
        {"code": [9007199254740993]}, geometry=[box(0, 0, 10, 20)], crs=CRS
    ).to_file(layer)
    with pytest.raises(ValueError, match=r"2\^53"):
        conversion.rasterize_layer(
            str(layer), grid, str(tmp_path / "large.tif"), field_name="code"
        )
    with pytest.raises(ValueError, match="uint8"):
        conversion.rasterize_layer(
            str(layer), grid, str(tmp_path / "mask.tif"), nodata=0.5
        )
    assert not (tmp_path / "large.tif").exists()
    assert not (tmp_path / "mask.tif").exists()

    small = polygons(tmp_path / "small.gpkg")
    with pytest.raises(ValueError, match="output dtype int32"):
        conversion.rasterize_layer(
            small, grid, str(tmp_path / "fractional.tif"), field_name="code", nodata=0.5
        )
    unsigned = tmp_path / "unsigned.parquet"
    gpd.GeoDataFrame(
        {"code": np.asarray([1], dtype="uint32")},
        geometry=[box(0, 0, 10, 20)], crs=CRS,
    ).to_parquet(unsigned)
    with pytest.raises(ValueError, match="output dtype uint32"):
        conversion.rasterize_layer(
            str(unsigned), grid, str(tmp_path / "negative.tif"),
            field_name="code", nodata=-1,
        )
    assert not (tmp_path / "fractional.tif").exists()
    assert not (tmp_path / "negative.tif").exists()


@pytest.mark.parametrize("dtype", ["float64", "uint64", "int64"])
def test_polygonize_refuses_dtypes_it_cannot_preserve(tmp_path, dtype):
    path = tmp_path / f"{dtype}.tif"
    value = 9007199254740993 if dtype == "int64" else 1
    with rasterio.open(
        path, "w", driver="GTiff", width=1, height=1, count=1, dtype=dtype,
        crs=CRS, transform=from_origin(0, 20, 10, 10),
    ) as output:
        output.write(np.asarray([[value]], dtype=dtype), 1)
    with pytest.raises(ValueError, match="preserve"):
        conversion.polygonize_raster(str(path), str(tmp_path / f"{dtype}.parquet"))


def test_polygonize_excludes_nodata_and_returns_closed_form_areas(tmp_path):
    grid = reference(
        tmp_path / "classes.tif",
        [[1, 1, 2], [1, 2, 2], [3, -9999, 2]],
    )
    output = tmp_path / "classes.parquet"
    result = conversion.polygonize_raster(grid, str(output))

    frame = gpd.read_parquet(output)
    assert result["feature_count"] == 3
    assert frame["value"].tolist() == [1, 2, 3]
    assert frame.area.tolist() == pytest.approx([300, 400, 100])
    assert frame.crs.to_epsg() == 32632
    assert -9999 not in frame["value"].tolist()
    record = json.loads(Path(result["provenance"]).read_text(encoding="utf-8"))
    assert record["crs_decisions"]["x-mapsmith:raster_registration"] == "area"


def test_polygonize_connectivity_and_feature_limit(tmp_path):
    grid = reference(tmp_path / "diagonal.tif", [[1, 0, 1], [0, 1, 0]], nodata=0)
    four = conversion.polygonize_raster(
        grid, str(tmp_path / "four.parquet"), connectivity=4
    )
    eight = conversion.polygonize_raster(
        grid, str(tmp_path / "eight.parquet"), connectivity=8
    )
    assert four["feature_count"] == 3
    assert eight["feature_count"] == 1

    with pytest.raises(ValueError, match="more than 1 features"):
        conversion.polygonize_raster(
            grid, str(tmp_path / "limited.parquet"), max_features=1
        )
    assert list(tmp_path.glob("limited.parquet*.json"))


def test_polygonize_repairs_bowtie_geometries_and_records_the_repair(tmp_path):
    grid = reference(tmp_path / "bowtie.tif", [[1, 0], [0, 1]], nodata=-1)
    result = conversion.polygonize_raster(
        grid, str(tmp_path / "valid.parquet"), connectivity=8
    )
    frame = gpd.read_parquet(tmp_path / "valid.parquet")
    assert frame.geometry.is_valid.all()
    assert result["repairs"]


def test_round_trip_rasterize_then_polygonize_in_a_validated_plan(tmp_path):
    layer = polygons(tmp_path / "zones.gpkg")
    grid = reference(tmp_path / "reference.tif")
    current = Plan.model_validate({"goal": "vector raster vector", "steps": [
        {"id": "grid", "operation": "rasterize_layer", "arguments": {
            "input_path": layer, "reference_raster_path": grid,
            "field_name": "code", "nodata": -1,
            "output_path": str(tmp_path / "grid.tif"),
        }},
        {"id": "polygons", "operation": "polygonize_raster", "arguments": {
            "input_path": "$grid", "output_path": str(tmp_path / "round.parquet"),
        }},
    ]})
    result = execute(current)
    assert result["executed"] is True, result
    frame = gpd.read_parquet(tmp_path / "round.parquet")
    assert frame["value"].tolist() == [3, 7]
    assert frame.area.tolist() == pytest.approx([200, 200])
    assert result["plan_manifest"]


def test_rasterize_warns_when_the_layer_misses_the_reference_grid(tmp_path):
    grid = reference(tmp_path / "reference.tif")
    layer = tmp_path / "disjoint.gpkg"
    gpd.GeoDataFrame(
        geometry=[box(1000, 1000, 1010, 1010)], crs=CRS,
    ).to_file(layer)
    output = tmp_path / "empty.tif"

    result = conversion.rasterize_layer(str(layer), grid, str(output))

    with rasterio.open(output) as dataset:
        assert dataset.read(1).tolist() == [[0, 0], [0, 0]]
    warning = next(
        item for item in result["warnings"]
        if item["check"] == "x-mapsmith:rasterized_cells_present"
    )
    assert warning["detail"] == "0 cells contain an input geometry"
    record = json.loads((tmp_path / "empty.tif.provenance.json").read_text(encoding="utf-8"))
    check = next(
        item for item in record["verification"]
        if item["name"] == "x-mapsmith:rasterized_cells_present"
    )
    assert check["passed"] is False and check["critical"] is False


def test_rasterize_warns_when_every_input_geometry_is_empty(tmp_path):
    from shapely.geometry import Polygon

    grid = reference(tmp_path / "reference.tif")
    layer = tmp_path / "empty-geometries.gpkg"
    gpd.GeoDataFrame(geometry=[Polygon()], crs=CRS).to_file(layer)

    result = conversion.rasterize_layer(
        str(layer), grid, str(tmp_path / "empty-geometries.tif")
    )

    assert any(
        item["check"] == "x-mapsmith:rasterized_cells_present"
        for item in result["warnings"]
    )
