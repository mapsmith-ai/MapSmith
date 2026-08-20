"""Terrain and hydrology on the Whitebox Workflows engine (Whitebox Next Gen).

whitebox_workflows 2.x is the PyO3 successor of WhiteboxTools: rasters stay
in memory (no CLI round-trips) and every tool wrapped here lives in the open
tier (MIT/Apache-2.0 dual license — verified against the upstream taxonomy).
Optional extra: ``pip install mapsmith[whitebox]``.

API notes pinned by runtime verification (2.0.6): category tool calls accept
keyword arguments only; CRS is read via ``Raster.crs_epsg()``/``crs_wkt()``
(``metadata().epsg_code`` is not populated); hillshade output is scaled
0-32767; nodata passes through ``to_numpy`` as the raw nodata value.

Raster verification lives here rather than in ``verify.py`` because reading
the output back requires this engine; the checks land in the provenance
manifest before any error is raised, like every other MapSmith writer.
"""

from __future__ import annotations

import math
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any

import numpy as np
from pyproj import CRS

from .. import verify, workspace
from ..provenance import InputRecord, ProvenanceRecord

HILLSHADE_MAX = 32767  # upstream scales hillshade to 0-32767 (basic_terrain_tools.rs)


def _require():
    try:
        import whitebox_workflows as wb
    except ImportError as exc:
        raise ImportError(
            "terrain/hydrology operations require the whitebox extra: "
            "pip install mapsmith[whitebox]"
        ) from exc
    return wb


def _engine_info() -> dict[str, str]:
    from importlib.metadata import version

    return {"name": "whitebox-workflows", "version": version("whitebox-workflows")}


def _needs_plain_copy(path: str) -> str | None:
    """Why this GeoTIFF must be rewritten before whitebox may read it, or None.

    whitebox_workflows 2.x never undoes the TIFF predictor (tag 317), so
    ``read_raster`` hands back the *undifferenced* values and everything
    computed from them is wrong. The mechanism is unambiguous — for
    predictor=2 the rows come back as the horizontal differences themselves:

        row as whitebox returns it : [100,   7,   7,   7,   7,   7]
        row as GDAL decodes it     : [100, 107, 114, 121, 128, 135]
        cumsum(whitebox row) == GDAL row  ->  exactly True

    predictor=3 (the floating-point predictor) yields garbage and NaNs the
    same way. Compression is NOT the trigger: DEFLATE, LZW and PACKBITS all
    read correctly without a predictor, and tiling is irrelevant at any block
    size. The damage is silent — CRS, shape and value range all stay
    plausible, and a hillshade still looks like terrain while differing from
    the truth on 99% of pixels — so no postcondition can catch it.

    PREDICTOR=2 is the standard recommendation for integer rasters, so this
    is a normal encoding rather than an exotic one.

    Reported upstream: https://github.com/jblindsay/whitebox_next_gen/issues/32
    Drop this workaround once a release fixes it — the read-level test in
    tests/test_whitebox_encoding.py fails when that happens, on purpose.
    """
    try:
        import rasterio
    except ImportError:  # rasterio ships with the whitebox extra; be defensive
        return None
    try:
        with rasterio.open(path) as ds:
            predictor = ds.tags(ns="IMAGE_STRUCTURE").get("PREDICTOR")
    except Exception:  # noqa: BLE001 — unreadable here means whitebox will complain
        return None
    if predictor in ("2", "3"):
        return f"stored with TIFF predictor {predictor}"
    return None


def _plain_copy(path: str, into: Path) -> str:
    """A copy with byte-identical values and no predictor.

    Compression is kept (whitebox decodes it correctly on its own); only the
    predictor is dropped, so the copy stays roughly the size of the original.
    """
    import rasterio

    with rasterio.open(path) as src:
        profile = src.profile
        data = src.read(1)
    profile.pop("predictor", None)
    plain = into / f"{Path(path).stem}.no-predictor.tif"
    with rasterio.open(plain, "w", **profile, predictor=1) as dst:
        dst.write(data, 1)
    return str(plain)


def _plain_copy_note(reason: str) -> str:
    return (
        f"input GeoTIFF is {reason}; the engine was given a copy with identical "
        "values and no predictor, because whitebox_workflows 2.x does not undo "
        "the TIFF predictor when decompressing (see _needs_plain_copy for the "
        "measurements)"
    )


@contextmanager
def _read_dem(wbe: Any, dem_path: str):
    """Yield (raster, crs, is_geographic, disclosure note) for a DEM.

    A context manager because whitebox reads lazily: when the predictor
    defect (see :func:`_needs_plain_copy`) forces us to hand the engine a
    converted copy, that copy has to stay on disk until the operation ends.
    Rasters without a CRS are rejected — terrain analysis on unknown units is
    meaningless.
    """
    with ExitStack() as stack:
        reason = _needs_plain_copy(dem_path)
        if reason:
            ws = workspace.root()
            tmp = stack.enter_context(
                tempfile.TemporaryDirectory(dir=str(ws) if ws else None)
            )
            source, note = _plain_copy(dem_path, Path(tmp)), _plain_copy_note(reason)
        else:
            source, note = str(dem_path), None
        dem = wbe.read_raster(source)
        epsg = dem.crs_epsg()
        wkt = dem.crs_wkt()
        if not epsg and not wkt:
            raise ValueError(
                f"{dem_path} has no CRS — terrain analysis needs one to be meaningful. "
                "Assign the correct CRS to the source raster first."
            )
        crs_obj = CRS.from_epsg(epsg) if epsg else CRS.from_wkt(str(wkt))
        yield dem, (f"EPSG:{epsg}" if epsg else str(wkt)), crs_obj.is_geographic, note


def _raster_checks(
    wbe: Any,
    output_path: str,
    *,
    expect_epsg: int,
    expect_shape: tuple[int, int],
    value_range: tuple[float, float] | None = None,
) -> list[verify.Check]:
    """Deterministic postconditions on a raster output, read back from disk."""
    out = wbe.read_raster(str(output_path))
    out_wkt = out.crs_wkt()
    if out.crs_epsg():
        crs_detail = f"EPSG:{out.crs_epsg()}"
    elif out_wkt:
        crs_detail = f"WKT: {str(out_wkt)[:60]}"
    else:
        crs_detail = "output has no CRS"
    checks = [
        verify.Check(
            "crs_present",
            bool(out.crs_epsg() or out_wkt),
            crs_detail,
        )
    ]
    if expect_epsg:
        checks.append(
            verify.Check(
                "crs_matches",
                out.crs_epsg() == expect_epsg,
                f"expected EPSG:{expect_epsg}, got EPSG:{out.crs_epsg()}",
            )
        )
    meta = out.metadata()
    shape = (meta.rows, meta.columns)
    checks.append(
        verify.Check(
            "dimensions_match_input",
            shape == expect_shape,
            f"expected {expect_shape}, got {shape}",
        )
    )
    arr = out.to_numpy(dtype="float64")
    # NaN is a common float nodata: NaN != NaN, so an equality mask alone would
    # let nodata cells leak into the range check as NaN min/max.
    if math.isnan(meta.nodata):
        valid = arr[~np.isnan(arr)]
    else:
        valid = arr[(arr != meta.nodata) & ~np.isnan(arr)]
    checks.append(
        verify.Check(
            "has_valid_cells",
            valid.size > 0,
            f"{valid.size}/{arr.size} valid cells",
        )
    )
    if value_range is not None and valid.size:
        lo, hi = value_range
        vmin, vmax = float(valid.min()), float(valid.max())
        checks.append(
            verify.Check(
                "values_in_expected_range",
                lo <= vmin and vmax <= hi,
                f"valid cells in [{vmin:.4g}, {vmax:.4g}], expected [{lo:.4g}, {hi:.4g}]",
            )
        )
    return checks


def hillshade(
    dem_path: str,
    output_path: str,
    azimuth: float = 315.0,
    altitude: float = 30.0,
    z_factor: float = 1.0,
) -> dict[str, Any]:
    """Shaded relief from a DEM (values scaled 0-32767, nodata preserved)."""
    wb = _require()
    if not 0.0 <= azimuth <= 360.0:
        raise ValueError(f"azimuth must be in [0, 360] degrees, got {azimuth}")
    if not 0.0 <= altitude <= 90.0:
        raise ValueError(f"altitude must be in [0, 90] degrees, got {altitude}")

    wbe = wb.WbEnvironment()
    wbe.verbose = False
    with _read_dem(wbe, dem_path) as (dem, crs, geographic, input_note):
        record = ProvenanceRecord(
            operation="hillshade",
            parameters={"azimuth": azimuth, "altitude": altitude, "z_factor": z_factor},
            inputs=[InputRecord.from_path(dem_path, crs=crs)],
            engine=_engine_info(),
        )
        record.crs_decisions = {
            "analysis_crs": crs,
            "reason": (
                "DEM is in a geographic CRS (degree cells vs meter elevations): shading "
                "is computed on native cells and relief may be exaggerated — reproject "
                "to a projected CRS or tune z_factor for metrically faithful shading"
                if geographic
                else "hillshade computed in the DEM's native projected CRS; "
                "no reprojection needed"
            ),
        }
        result = wbe.terrain.general.hillshade(
            input=dem, azimuth=azimuth, altitude=altitude, z_factor=z_factor
        )
        wbe.write_raster(result, str(output_path))

        meta = dem.metadata()
        checks = _raster_checks(
            wbe,
            output_path,
            expect_epsg=dem.crs_epsg(),
            expect_shape=(meta.rows, meta.columns),
            value_range=(0, HILLSHADE_MAX),
        )
        if input_note:
            record.notes.append(input_note)
        manifest = record.add_verification(checks).finish().write_for(output_path)
        verify.enforce(checks, "hillshade")
        return {
            "output": str(output_path),
            "azimuth": azimuth,
            "altitude": altitude,
            "provenance": str(manifest),
            "verified": True,
        }


def flow_accumulation(
    dem_path: str,
    output_path: str,
    out_type: str = "cells",
    log_transform: bool = False,
) -> dict[str, Any]:
    """D8 flow accumulation from a DEM (depressions filled first, decision recorded)."""
    wb = _require()
    valid_types = {"cells", "sca"}
    if out_type not in valid_types:
        raise ValueError(f"out_type must be one of {sorted(valid_types)}, got '{out_type}'")

    wbe = wb.WbEnvironment()
    wbe.verbose = False
    with _read_dem(wbe, dem_path) as (dem, crs, geographic, input_note):
        if out_type == "sca" and geographic:
            raise ValueError(
                "specific catchment area needs a projected CRS (it divides by cell width, "
                "which is degrees here). Reproject the DEM to a projected CRS first "
                "(e.g. a UTM zone via reproject workflows), or use out_type='cells'."
            )
        record = ProvenanceRecord(
            operation="flow_accumulation",
            parameters={
                "method": "d8",
                "out_type": out_type,
                "log_transform": log_transform,
                "preprocessing": "fill_depressions",
            },
            inputs=[InputRecord.from_path(dem_path, crs=crs)],
            engine=_engine_info(),
        )
        record.crs_decisions = {
            "analysis_crs": crs,
            "reason": (
                "cell-count accumulation is independent of cell units; computed in the "
                "DEM's native geographic CRS"
                if geographic
                else "flow routing computed in the DEM's native projected CRS; "
                "no reprojection needed"
            ),
        }
        filled = wbe.hydrology.depressions_storage.fill_depressions(dem=dem)
        pointer = wbe.hydrology.flow_routing.d8_pointer(dem=filled)
        accum = wbe.hydrology.flow_routing.d8_flow_accum(
            input=pointer, out_type=out_type, log_transform=log_transform, input_is_pointer=True
        )
        wbe.write_raster(accum, str(output_path))

        meta = dem.metadata()
        cells = meta.rows * meta.columns
        # 'cells' accumulation counts each cell itself, so valid values live in [1, n_cells]
        # (or their natural log when log-transformed).
        bounds = (1.0, float(cells)) if out_type == "cells" else None
        if bounds and log_transform:
            bounds = (0.0, math.log(cells))
        checks = _raster_checks(
            wbe,
            output_path,
            expect_epsg=dem.crs_epsg(),
            expect_shape=(meta.rows, meta.columns),
            value_range=bounds,
        )
        if input_note:
            record.notes.append(input_note)
        manifest = record.add_verification(checks).finish().write_for(output_path)
        verify.enforce(checks, "flow_accumulation")
        return {
            "output": str(output_path),
            "method": "d8",
            "out_type": out_type,
            "provenance": str(manifest),
            "verified": True,
        }


def watershed(
    dem_path: str,
    pour_points_path: str,
    output_path: str,
) -> dict[str, Any]:
    """Watershed of each pour point (1-based IDs in feature order; nodata elsewhere)."""
    wb = _require()
    import geopandas as gpd

    points = (
        gpd.read_parquet(pour_points_path)
        if str(pour_points_path).endswith(".parquet")
        else gpd.read_file(pour_points_path)
    )
    if points.crs is None:
        raise ValueError(
            f"{pour_points_path} has no CRS — cannot place pour points on the DEM."
        )
    geom_types = set(points.geom_type.dropna().unique())
    if not geom_types.issubset({"Point"}):
        raise ValueError(f"pour points must be Point geometries, got {sorted(geom_types)}")

    wbe = wb.WbEnvironment()
    wbe.verbose = False
    with _read_dem(wbe, dem_path) as (dem, crs, _geographic, input_note):  # topology is unit-free
        record = ProvenanceRecord(
            operation="watershed",
            parameters={"method": "d8", "preprocessing": "fill_depressions", "n_pour_points": len(points)},
            inputs=[
                InputRecord.from_path(dem_path, crs=crs),
                InputRecord.from_path(pour_points_path, crs=str(points.crs)),
            ],
            engine=_engine_info(),
        )
        if str(points.crs) != crs:
            points = points.to_crs(crs)
            record.crs_decisions = {
                "analysis_crs": crs,
                "reason": "pour points reprojected to the DEM CRS to align with the flow grid",
            }
        else:
            record.crs_decisions = {
                "analysis_crs": crs,
                "reason": "pour points and DEM share the same CRS",
            }

        filled = wbe.hydrology.depressions_storage.fill_depressions(dem=dem)
        pointer = wbe.hydrology.flow_routing.d8_pointer(dem=filled)
        # whitebox reads vectors as shapefiles: hand it the (possibly reprojected)
        # points through a temporary shapefile so any GeoPandas-readable input works.
        # Under a workspace even scratch data must not leave it (data governance).
        ws = workspace.root()
        with tempfile.TemporaryDirectory(dir=str(ws) if ws else None) as tmp:
            shp = Path(tmp) / "pour_points.shp"
            points.to_file(shp)
            vec = wbe.read_vector(str(shp))
            basins = wbe.hydrology.watersheds_basins.watershed(d8_pointer=pointer, pour_pts=vec)
            wbe.write_raster(basins, str(output_path))

        meta = dem.metadata()
        checks = _raster_checks(
            wbe,
            output_path,
            expect_epsg=dem.crs_epsg(),
            expect_shape=(meta.rows, meta.columns),
            value_range=(1.0, float(len(points))),
        )
        if input_note:
            record.notes.append(input_note)
        manifest = record.add_verification(checks).finish().write_for(output_path)
        verify.enforce(checks, "watershed")
        return {
            "output": str(output_path),
            "n_pour_points": len(points),
            "provenance": str(manifest),
            "verified": True,
        }
