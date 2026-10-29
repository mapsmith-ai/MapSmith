"""Deterministic vector/raster conversion on declared grids."""

from __future__ import annotations

import math
from typing import Any

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import shape

from .. import grid, readers, verify
from ..provenance import InputRecord, ProvenanceRecord

POLYGON_CONNECTIVITIES = {4, 8}


def _require():
    try:
        import rasterio
        from rasterio import features
    except ImportError as exc:
        raise ImportError(
            "vector/raster conversion requires the raster extra: pip install mapsmith[raster]"
        ) from exc
    return rasterio, features


def rasterize_layer(
    input_path: str,
    reference_raster_path: str,
    output_path: str,
    field_name: str | None = None,
    all_touched: bool = False,
    nodata: float | None = None,
) -> dict[str, Any]:
    """Burn vector values onto the reference raster's exact grid."""
    rasterio, features = _require()
    grid.refuse_ambiguous_georeferencing(reference_raster_path, "rasterize_layer")
    if nodata is not None and not math.isfinite(nodata):
        raise ValueError("nodata must be finite")
    if (
        not field_name
        and nodata is not None
        and (nodata != int(nodata) or not 0 <= nodata <= 255)
    ):
        raise ValueError("a presence mask uses uint8, so nodata must be an integer from 0 to 255")
    layer = readers.read_vector(input_path)
    if layer.crs is None:
        raise ValueError(readers.no_crs_message(layer, f"{input_path} has no CRS"))
    with rasterio.open(reference_raster_path) as reference:
        if reference.crs is None:
            raise ValueError(f"{reference_raster_path} declares no CRS")
        if not verify.same_crs(layer.crs, reference.crs):
            raise ValueError(
                "input and reference raster have different CRSs; reproject the vector "
                "to the reference CRS before rasterizing"
            )
        registration = grid.registration(reference)
        profile = reference.profile.copy()
        profile.pop("blockxsize", None)
        profile.pop("blockysize", None)
        profile.update(
            driver="GTiff", count=1, dtype="float64" if field_name else "uint8",
            nodata=nodata,
        )
        width, height, transform = reference.width, reference.height, reference.transform

    values = None
    if field_name:
        if field_name not in layer.columns:
            raise ValueError(f"field {field_name!r} is not present in {input_path}")
        if not pd.api.types.is_numeric_dtype(layer[field_name].dtype):
            raise ValueError(f"field {field_name!r} must be numeric to rasterize it")
        if layer[field_name].isna().any():
            raise ValueError(f"field {field_name!r} contains null values")
        if pd.api.types.is_integer_dtype(layer[field_name].dtype):
            integers = [int(value) for value in layer[field_name]]
            if any(abs(value) > 2**53 for value in integers):
                raise ValueError(
                    f"field {field_name!r} contains an integer outside ±2^53, which cannot "
                    "be preserved exactly by rasterize_layer's float64 output"
                )
            if pd.api.types.is_unsigned_integer_dtype(layer[field_name].dtype):
                if not integers or max(integers) <= 2**32 - 1:
                    profile["dtype"] = "uint32"
            elif not integers or min(integers) >= -2**31 and max(integers) <= 2**31 - 1:
                profile["dtype"] = "int32"
        values = [float(value) for value in layer[field_name]]
        if not all(math.isfinite(value) for value in values):
            raise ValueError(f"field {field_name!r} contains non-finite values")
    if nodata is not None and np.issubdtype(profile["dtype"], np.integer):
        limits = np.iinfo(profile["dtype"])
        if nodata != int(nodata) or not limits.min <= nodata <= limits.max:
            raise ValueError(
                f"nodata {nodata} is not exactly representable by the output dtype "
                f"{profile['dtype']}"
            )
    valid = [
        (geometry, value)
        for geometry, value in zip(
            layer.geometry, values or [None] * len(layer), strict=True
        )
        if geometry is not None and not geometry.is_empty
    ]
    shapes = [
        (geometry.__geo_interface__, value) if values is not None else geometry.__geo_interface__
        for geometry, value in valid
    ]
    geometry_shapes = [geometry.__geo_interface__ for geometry, _ in valid]
    pre = verify.verify_loaded_inputs("rasterize_layer", input_path=layer)
    record = ProvenanceRecord(
        operation="rasterize_layer",
        parameters={
            "field_name": field_name,
            "all_touched": all_touched,
            "nodata": nodata,
            "fill": nodata if nodata is not None else 0,
            "grid": {"width": width, "height": height, "transform": list(transform)[:6]},
        },
        inputs=[
            InputRecord.from_path(
                input_path, crs=verify.crs_label(layer.crs), argument="input_path"
            ),
            InputRecord.from_path(
                reference_raster_path, crs=verify.crs_label(layer.crs),
                argument="reference_raster_path",
            ),
        ],
        engine={"name": "rasterio", "version": rasterio.__version__},
    )
    record.crs_decisions = {
        "analysis_crs": verify.crs_label(layer.crs),
        "reason": "vector and reference raster share a CRS; the reference grid is used unchanged",
        **grid.manifest_decisions(registration),
    }
    with verify.audit_on_failure(record, output_path, pre):
        burned = features.rasterize(
            shapes, out_shape=(height, width), transform=transform,
            fill=nodata if nodata is not None else 0,
            all_touched=all_touched, dtype=profile["dtype"], skip_invalid=False,
        )
        with rasterio.open(output_path, "w", **profile) as output:
            grid.preserve(registration, output)
            output.write(burned, 1)

    def checks():
        with rasterio.open(output_path) as output:
            mask = features.rasterize(
                geometry_shapes, out_shape=(height, width), transform=transform,
                fill=0, all_touched=all_touched, dtype="uint8", skip_invalid=False,
            )
            cells = int(np.count_nonzero(mask))
            return [
                verify.Check(
                    "x-mapsmith:rasterized_cells_present",
                    cells > 0,
                    f"{cells} cells contain an input geometry",
                    critical=False,
                    argument="input_path",
                    hint="check that the input geometries intersect the reference raster extent",
                ),
                verify.Check(
                    "x-mapsmith:shape_matches_reference",
                    (output.height, output.width) == (height, width),
                    f"expected {(height, width)}, got {(output.height, output.width)}",
                    argument="reference_raster_path",
                ),
                verify.Check(
                    "crs_matches",
                    verify.same_crs(output.crs, layer.crs),
                    verify.crs_label(output.crs),
                    argument="output_path",
                ),
                verify.Check(
                    "x-mapsmith:grid_transform_matches_reference",
                    output.transform == transform,
                    f"expected {transform}, got {output.transform}",
                    argument="reference_raster_path",
                ),
                verify.Check(
                    "x-mapsmith:nodata_matches_declaration",
                    output.nodata == nodata,
                    f"expected {nodata}, got {output.nodata}",
                    argument="output_path",
                ),
            ]

    manifest, extras = verify.audited(
        record, output_path, operation="rasterize_layer",
        preconditions=pre, checks_fn=checks, repair=False,
    )
    return {
        "output": output_path,
        "shape": [height, width],
        "crs": verify.crs_label(layer.crs),
        "provenance": manifest,
        "verified": True,
        **extras,
    }


def polygonize_raster(
    input_path: str,
    output_path: str,
    band: int = 1,
    connectivity: int = 4,
    field_name: str = "value",
    max_features: int = 100_000,
) -> dict[str, Any]:
    """Vectorize connected equal-valued valid cells in a stable feature order."""
    rasterio, features = _require()
    grid.refuse_ambiguous_georeferencing(input_path, "polygonize_raster")
    if connectivity not in POLYGON_CONNECTIVITIES:
        raise ValueError("connectivity must be 4 or 8")
    if max_features < 1:
        raise ValueError("max_features must be positive")
    if not field_name or field_name == "geometry":
        raise ValueError("field_name must be non-empty and different from 'geometry'")
    with rasterio.open(input_path) as source:
        if source.crs is None:
            raise ValueError(f"{input_path} declares no CRS")
        if not 1 <= band <= source.count:
            raise ValueError(f"band must be between 1 and {source.count}")
        data = source.read(band)
        valid_mask = source.read_masks(band)
        if data.dtype in (np.dtype("float64"), np.dtype("uint64")):
            raise ValueError(
                f"polygonize_raster cannot preserve {data.dtype} values exactly; "
                "convert the raster to an exactly representable dtype first"
            )
        if data.dtype == np.dtype("int64") and any(
            abs(int(value)) > 2**53 for value in data[valid_mask != 0]
        ):
            raise ValueError("polygonize_raster cannot preserve int64 values outside ±2^53")
        crs = source.crs
        registration = grid.registration(source)
        record = ProvenanceRecord(
            operation="polygonize_raster",
            parameters={
                "band": band,
                "connectivity": connectivity,
                "field_name": field_name,
                "max_features": max_features,
                "nodata": "masked cells are excluded",
            },
            inputs=[InputRecord.from_path(
                input_path, crs=verify.crs_label(crs), argument="input_path"
            )],
            engine={"name": "rasterio", "version": rasterio.__version__},
        )
        record.crs_decisions = {
            "analysis_crs": verify.crs_label(crs),
            "reason": "pixel polygons are transformed with the source raster's affine grid",
            **grid.manifest_decisions(registration),
        }
        polygons = []
        with verify.audit_on_failure(record, output_path, []):
            for geometry, value in features.shapes(
                data, mask=valid_mask, connectivity=connectivity,
                transform=source.transform,
            ):
                if not math.isfinite(value):
                    raise ValueError("polygonize_raster cannot preserve a non-finite cell value")
                polygons.append((float(value), shape(geometry)))
                if len(polygons) > max_features:
                    raise ValueError(
                        f"polygonize_raster would create more than {max_features} features; "
                        "reclassify the raster first or raise max_features deliberately"
                    )
            polygons.sort(key=lambda item: (item[0], item[1].bounds, item[1].wkb))
            output = gpd.GeoDataFrame(
                {field_name: [value for value, _ in polygons]},
                geometry=[geometry for _, geometry in polygons], crs=crs,
            )
            from .vector import _write

            _write(output, output_path)

    def checks():
        return [
            *verify.verify_vector_output(
                output_path, expect_crs=verify.crs_label(crs),
                expect_count=len(polygons), on_empty="warn",
                empty_hint="The raster has no valid connected regions to polygonize.",
            ),
            verify.Check(
                "x-mapsmith:polygon_values_are_finite",
                bool(np.isfinite(output[field_name].to_numpy(dtype="float64")).all()),
                "all polygon values are finite",
                argument="output_path",
            ),
        ]

    manifest, extras = verify.audited(
        record, output_path, operation="polygonize_raster",
        checks_fn=checks, repair=True,
    )
    return {
        "output": output_path,
        "feature_count": len(polygons),
        "crs": verify.crs_label(crs),
        "provenance": manifest,
        "verified": True,
        **extras,
    }
