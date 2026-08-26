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

from .. import readers, verify, workspace
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
        cumsum(whitebox row) == GDAL row  ->  exactly True, on a STRIPED file

    The condition on that last line matters, and leaving it implicit misleads
    the next reader: the horizontal predictor differences within each strip or
    *tile*, so on a tiled GeoTIFF the series restarts at every tile boundary
    and a cumulative sum across the full row stops agreeing after the first
    tile column. Measured on ``examples/fixtures/mount_st_helens_dem.tif``
    (tiled 256, DEFLATE, predictor=2): columns 0-255 match, column 256 is the
    first mismatch, 50.8% of pixels differ — while the same cumulative sum
    *reset at each tile boundary* reproduces the truth exactly. Anyone checking
    with a COG, which is tiled by definition, would otherwise see the identity
    fail and conclude the bug is not there.

    The bug itself does not care about tiling: it happens at any block size,
    and on this tiled fixture whitebox reads elevations of -63..2531 m where
    the truth is 652..2534 m. predictor=3 (the floating-point predictor) yields
    garbage and NaNs the same way. Compression is NOT the trigger: DEFLATE, LZW
    and PACKBITS all read correctly without a predictor. The damage is silent —
    CRS, shape and value range all stay plausible, and a hillshade still looks
    like terrain while differing from the truth on 99% of pixels — so no
    postcondition can catch it.

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
            "shape_preserved",
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
            "result_not_empty",
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


SLOPE_UNITS = {"degrees", "percent", "radians"}


def slope(
    dem_path: str,
    output_path: str,
    units: str = "degrees",
    z_factor: float = 1.0,
) -> dict[str, Any]:
    """Slope gradient from a DEM (Zevenbergen-Thorne; degrees, percent or radians).

    Geographic-CRS DEMs are refused: with degree cells and meter elevations the
    gradient is wrong everywhere while looking plausible — the caller must
    reproject first. Edge behaviour, measured on 2.0.6: the outermost TWO cell
    rings are approximated (clamped windows), values from the third ring inward
    are exact on an ideal plane.
    """
    if units not in SLOPE_UNITS:
        raise ValueError(f"units must be one of {sorted(SLOPE_UNITS)}, got '{units}'")
    bounds = {
        "degrees": (0.0, 90.0),
        "radians": (0.0, math.pi / 2),
        "percent": None,  # tan of the angle: unbounded near vertical
    }[units]
    return _derivative(
        "slope",
        dem_path,
        output_path,
        parameters={"units": units, "z_factor": z_factor},
        call=lambda wbe, dem: wbe.terrain.derivatives.slope(
            input=dem, units=units, z_factor=z_factor
        ),
        value_range=bounds,
    )


def aspect(
    dem_path: str,
    output_path: str,
    z_factor: float = 1.0,
) -> dict[str, Any]:
    """Aspect from a DEM: azimuth of the downslope direction, degrees, 0 = north.

    Convention pinned by measurement on 2.0.6: a plane rising eastward yields
    270 (the downslope faces west). Flat cells are encoded as -1, NOT as
    nodata — a consumer averaging aspect over an area that contains flats gets
    a plausible wrong number unless it masks the -1 first, which is why the
    value is stated here and in the tool docs. Geographic-CRS DEMs are refused
    (see slope).
    """
    return _derivative(
        "aspect",
        dem_path,
        output_path,
        parameters={"z_factor": z_factor},
        call=lambda wbe, dem: wbe.terrain.derivatives.aspect(input=dem, z_factor=z_factor),
        value_range=(-1.0, 360.0),  # -1 is the flat-cell marker, by upstream design
    )


def _derivative(
    operation: str,
    dem_path: str,
    output_path: str,
    *,
    parameters: dict[str, Any],
    call: Any,
    value_range: tuple[float, float] | None,
) -> dict[str, Any]:
    """Shared body of the local terrain derivatives (slope, aspect)."""
    wb = _require()
    wbe = wb.WbEnvironment()
    wbe.verbose = False
    with _read_dem(wbe, dem_path) as (dem, crs, geographic, input_note):
        if geographic:
            raise ValueError(
                f"{operation} on a geographic CRS mixes degree cells with meter "
                "elevations and returns plausible but wrong values everywhere. "
                "Reproject the DEM to a projected CRS first (e.g. its UTM zone)."
            )
        record = ProvenanceRecord(
            operation=operation,
            parameters=parameters,
            inputs=[InputRecord.from_path(dem_path, crs=crs)],
            engine=_engine_info(),
        )
        record.crs_decisions = {
            "analysis_crs": crs,
            "reason": (
                f"{operation} computed in the DEM's native projected CRS; geographic-CRS "
                "DEMs are refused because horizontal units (degrees) would not match "
                "vertical units (meters)"
            ),
        }
        result = call(wbe, dem)
        wbe.write_raster(result, str(output_path))

        meta = dem.metadata()
        checks = _raster_checks(
            wbe,
            output_path,
            expect_epsg=dem.crs_epsg(),
            expect_shape=(meta.rows, meta.columns),
            value_range=value_range,
        )
        if input_note:
            record.notes.append(input_note)
        manifest = record.add_verification(checks).finish().write_for(output_path)
        verify.enforce(checks, operation)
        return {
            "output": str(output_path),
            **parameters,
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
    points = readers.read_vector(pour_points_path)
    if points.crs is None:
        raise ValueError(readers.no_crs_message(
            points, f"{pour_points_path} has no CRS — cannot place pour points on the DEM."
        ))
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
                InputRecord.from_path(pour_points_path, crs=verify.crs_label(points.crs)),
            ],
            engine=_engine_info(),
        )
        # Compare the coordinate systems, not their spellings: `str(crs)` is
        # PROJJSON for any GeoParquet input, so this was true even when the
        # points were already on the DEM's grid — and the manifest then recorded
        # a reprojection that never happened.
        if not verify.same_crs(points.crs, crs):
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


FOCAL_STATISTICS = {
    "mean": "mean_filter",
    "median": "median_filter",
    "maximum": "maximum_filter",
    "minimum": "minimum_filter",
    "range": "range_filter",
    "standard_deviation": "standard_deviation_filter",
    "majority": "majority_filter",
    "diversity": "diversity_filter",
    "total": "total_filter",
}


def focal_statistics(
    input_path: str,
    output_path: str,
    statistic: str,
    window: int,
) -> dict[str, Any]:
    """A moving-window statistic over a raster, with the window size required.

    Whitebox's filters default to an 11 x 11 window. On a 1 m DEM that is a
    5.5 m radius, so a "local" statistic quietly stops being local — and the
    result is a perfectly ordinary-looking smoothed surface. The window is
    therefore a required argument here, and must be odd: an even window has no
    centre cell, so the output is offset by half a cell from its input, which
    is a shift nothing downstream can see.

    For class codes use `majority` or `diversity`; `mean` on a land-cover map
    invents codes, the same way an interpolating resample does.
    """
    wb = _require()
    if statistic not in FOCAL_STATISTICS:
        raise ValueError(
            f"statistic must be one of {sorted(FOCAL_STATISTICS)}, got {statistic!r}"
        )
    if window < 3 or window % 2 == 0:
        raise ValueError(
            f"window must be an odd number of cells, at least 3, got {window}. "
            "An even window has no centre cell and shifts the result by half a "
            "cell against its input."
        )
    wbe = wb.WbEnvironment()
    wbe.verbose = False
    with _read_dem(wbe, input_path) as (raster, crs, _geographic, input_note):
        record = ProvenanceRecord(
            operation="focal_statistics",
            parameters={"statistic": statistic, "window": window, "shape": "square"},
            inputs=[InputRecord.from_path(input_path, crs=crs)],
            engine=_engine_info(),
        )
        record.crs_decisions = {
            "analysis_crs": crs,
            "reason": "a moving window is measured in CELLS, not in ground units: "
            "the same window covers a different distance on a different grid, and "
            "no reprojection would change that",
        }
        record.notes.append(
            f"window {window}x{window} cells; at this raster's resolution that is "
            f"{window} cells across, and the statistic is not comparable with one "
            "computed at another resolution"
        )
        if statistic in ("mean", "median", "total", "standard_deviation", "range"):
            record.notes.append(
                f"'{statistic}' derives values that need not exist in the input: "
                "correct for a continuous surface, wrong for class codes, where "
                "'majority' or 'diversity' are the ones that keep the alphabet"
            )
        # `wbe.remote_sensing`, not `wbe.raster`: the typed stub files them
        # under raster and the runtime does not have them there. Third time
        # a Whitebox tool is not where its documentation says (after
        # terrain.general vs terrain.derivatives), so this path was found by
        # introspecting the installed package.
        method = getattr(wbe.remote_sensing, FOCAL_STATISTICS[statistic])
        result = method(input=raster, filter_size_x=window, filter_size_y=window)
        wbe.write_raster(result, str(output_path))

        meta = raster.metadata()
        checks = _raster_checks(
            wbe,
            output_path,
            expect_epsg=raster.crs_epsg(),
            expect_shape=(meta.rows, meta.columns),
            value_range=None,
        )
        if input_note:
            record.notes.append(input_note)
        manifest = record.add_verification(checks).finish().write_for(output_path)
        verify.enforce(checks, "focal_statistics")
        return {
            "output": str(output_path),
            "statistic": statistic,
            "window": window,
            "provenance": str(manifest),
            "verified": True,
        }


def extract_streams(
    flow_accumulation_path: str,
    output_path: str,
    threshold: float,
    zero_background: bool = False,
) -> dict[str, Any]:
    """The stream network implied by a flow-accumulation grid and a threshold.

    The threshold is required, and the manifest records which UNIT it is in,
    because that is where this operation goes wrong: `d8_flow_accum` produces
    either a cell count or a specific contributing area depending on its
    `out_type`, the two differ by orders of magnitude, and a threshold tuned for
    one applied to the other gives a stream network that is well formed, drawn
    on the map, and wrong. There is no defensible default for the threshold
    either — the literature says so plainly — so the caller states it and the
    record keeps it.
    """
    wb = _require()
    if threshold <= 0:
        raise ValueError(
            f"threshold must be positive, got {threshold}. With 0 every cell that "
            "drains anything becomes a stream, which is the whole DEM."
        )
    wbe = wb.WbEnvironment()
    wbe.verbose = False
    with _read_dem(wbe, flow_accumulation_path) as (accumulation, crs, _geographic, note):
        record = ProvenanceRecord(
            operation="extract_streams",
            parameters={
                "threshold": threshold,
                "zero_background": zero_background,
                "threshold_unit": "whatever unit the input flow accumulation is in",
            },
            inputs=[InputRecord.from_path(flow_accumulation_path, crs=crs)],
            engine=_engine_info(),
        )
        record.crs_decisions = {
            "analysis_crs": crs,
            "reason": "thresholding an existing grid changes values, not geometry",
        }
        record.notes.append(
            "the threshold is compared against the input's own values: if that grid "
            "came from d8_flow_accum with out_type='cells' the unit is a cell count, "
            "and with 'sca' it is a specific contributing area — the two differ by "
            "orders of magnitude and produce different networks from the same number"
        )
        # `wbe.streams.extract_streams`, measured: the stub nests it under a
        # `network_extraction` sub-namespace that does not exist at runtime.
        result = wbe.streams.extract_streams(
            flow_accumulation=accumulation,
            threshold=threshold,
            zero_background=zero_background,
        )
        wbe.write_raster(result, str(output_path))

        meta = accumulation.metadata()
        checks = _raster_checks(
            wbe,
            output_path,
            expect_epsg=accumulation.crs_epsg(),
            expect_shape=(meta.rows, meta.columns),
            value_range=None,
        )
        if note:
            record.notes.append(note)
        manifest = record.add_verification(checks).finish().write_for(output_path)
        verify.enforce(checks, "extract_streams")
        return {
            "output": str(output_path),
            "threshold": threshold,
            "zero_background": zero_background,
            "provenance": str(manifest),
            "verified": True,
        }
