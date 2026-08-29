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
    extra_checks: Any = None,
) -> dict[str, Any]:
    """Shared body of the local terrain derivatives (slope, aspect, curvature, flow direction).

    `extra_checks` receives the written output path and returns more checks. It
    exists for `flow_direction`, whose codes are a fixed SET rather than a range:
    a value of 3 in a D8 pointer is not out of bounds, it is not a direction at
    all, and a range check would pass it.
    """
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
        if extra_checks is not None:
            checks.extend(extra_checks(output_path))
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


# The sixteen curvature tools whitebox-workflows exposes, of which these six are
# the ones with an established meaning in terrain analysis. Verified against the
# installed package rather than the documentation (D-048).
#
# Call them through the CATEGORY path with keyword arguments, never through the
# flat `wbe.<tool>(...)` name -- and not because the flat form is deprecated: on
# this build it is disabled for SOME tools and works for others. `wbe.slope(dem)`
# runs; `wbe.d8_pointer(dem)` raises "Flat WbEnvironment tool methods are
# disabled in this build"; `dir(wbe)` lists both identically. A per-tool
# inconsistency cannot be learned once and reapplied, so the rule here is the
# category path everywhere, which worked for every tool measured. The category
# form also refuses positional arguments, hence `input=` (`points=` for IDW).
#
# One operation with a `kind`, not six tools: two tools that both apply to a DEM
# and return different numbers for the same question are exactly what invariant 6
# is about.
CURVATURE_KINDS = {
    "profile": "profile_curvature",
    "plan": "plan_curvature",
    "tangential": "tangential_curvature",
    "mean": "mean_curvature",
    "gaussian": "gaussian_curvature",
    "total": "total_curvature",
}

# D8 and Rho8 encode directions as powers of two; a value outside the set is not
# a direction. Dinf and Fd8 write continuous aspect-like values instead, so the
# set check does not apply to them.
FLOW_DIRECTION_METHODS = {"d8": "d8_pointer", "rho8": "rho8_pointer",
                          "dinf": "dinf_pointer", "fd8": "fd8_pointer"}
_POINTER_CODES = {0.0, 1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0, 128.0}

# The two direction tables a pointer raster can hold, named after what they ARE
# rather than after who uses them -- which is also the more useful name, since a
# consumer needs the table and not the brand.
#
# Both were MEASURED on whitebox-workflows 2.0.6, one direction at a time: a 5x5
# grid at 200 with the centre at 100 and exactly one neighbour at 0, so the code
# the centre receives names that neighbour and nothing else.
#
# `northeast_first` is what the engine writes by default, and it is NOT the table
# the WhiteboxTools manual documents for its own default. The manual says east=1,
# northeast=2, north=4 -- counter-clockwise from east. The engine writes
# northeast=1, east=2, southeast=4 -- clockwise from northeast. The two agree on
# no direction at all, so reading a default pointer with the documented table
# mirrors the whole drainage network about the NE-SW axis. Nothing raises, and a
# mirrored network still looks like a drainage network.
#
# `east_first` is the table most desktop GIS software uses, and the engine's
# alternate mode reproduces it exactly as that software documents it -- so the
# mismatch above is a defect in whitebox's documentation of its own default, not
# in its code. It is the second measured whitebox doc/behaviour mismatch after
# the TIFF predictor bug (issue #32), hence D-048: on this library the installed
# object is the source, never the manual.
POINTER_ENCODINGS = {
    "northeast_first": {
        "northeast": 1, "east": 2, "southeast": 4, "south": 8,
        "southwest": 16, "west": 32, "northwest": 64, "north": 128,
    },
    "east_first": {
        "east": 1, "southeast": 2, "south": 4, "southwest": 8,
        "west": 16, "northwest": 32, "north": 64, "northeast": 128,
    },
}


def curvature(
    dem_path: str,
    output_path: str,
    kind: str,
    z_factor: float = 1.0,
) -> dict[str, Any]:
    """Surface curvature from a DEM. The kind is REQUIRED: they mean different things.

    `profile` is curvature along the slope (where flow accelerates), `plan` is
    curvature across it (where flow converges), and they answer opposite
    questions about the same cell — a hillslope can be convex in profile and
    concave in plan at once. `mean`, `gaussian`, `total` and `tangential` are
    the standard invariants. There is no default because a caller who wanted
    convergence and got acceleration receives a plausible raster of the wrong
    quantity, with no way to tell.

    Geographic-CRS DEMs are refused, for the same reason as `slope`: degree cells
    with metre elevations make the second derivative wrong everywhere.
    """
    if kind not in CURVATURE_KINDS:
        raise ValueError(
            f"kind must be one of {sorted(CURVATURE_KINDS)}, got {kind!r}. There is no "
            "default: profile curvature is along the slope and plan curvature is across "
            "it, and they answer opposite questions about the same cell."
        )
    tool = CURVATURE_KINDS[kind]
    return _derivative(
        "curvature",
        dem_path,
        output_path,
        parameters={"kind": kind, "z_factor": z_factor, "tool": tool},
        call=lambda wbe, dem: getattr(wbe.terrain.derivatives, tool)(
            input=dem, z_factor=z_factor
        ),
        # Curvature is a second derivative in units of 1/length: unbounded, and
        # a range check would either pass everything or reject real terrain.
        value_range=None,
    )


def flow_direction(
    dem_path: str,
    output_path: str,
    method: str = "d8",
    encoding: str = "northeast_first",
) -> dict[str, Any]:
    """Flow direction from a DEM, as a pointer raster with its direction table.

    `d8` sends all of a cell's water to its steepest neighbour and `dinf`
    splits it between two, which is the difference between a drainage network
    that looks like a line and one that looks like a fan. `rho8` is D8 with a
    stochastic tie-break; `fd8` spreads over all downslope neighbours.

    A pointer raster is a grid of small integers whose MEANING lives outside the
    file, and the two conventions in use disagree on every direction: in
    `northeast_first` (this engine's default) 1 is northeast, in `east_first`
    (what most desktop GIS software writes) 1 is east. Read a raster with the
    wrong table and every cell points somewhere else -- the network stays
    connected, stays plausible, and drains the wrong way. Nothing in a GeoTIFF
    says which table it holds, so the manifest carries the whole table, by
    direction name, for the encoding actually used: a consumer never has to know
    which engine wrote the file, and never has to trust a manual. This engine's
    own manual documents its default table backwards (see POINTER_ENCODINGS).

    `dinf` and `fd8` do not use the table at all, so for those it is not
    recorded and `encoding` is refused rather than ignored.
    """
    if method not in FLOW_DIRECTION_METHODS:
        raise ValueError(
            f"method must be one of {sorted(FLOW_DIRECTION_METHODS)}, got {method!r}"
        )
    if encoding not in POINTER_ENCODINGS:
        raise ValueError(
            f"encoding must be one of {sorted(POINTER_ENCODINGS)}, got {encoding!r}"
        )
    tool = FLOW_DIRECTION_METHODS[method]
    coded = method in ("d8", "rho8")
    if encoding != "northeast_first" and not coded:
        raise ValueError(
            f"encoding={encoding!r} is meaningless for method={method!r}: {method} writes "
            "continuous aspect-like values, not direction codes, so there is no table to "
            "choose. Use method='d8' or 'rho8', or leave the default encoding."
        )

    def codes_are_directions(written: str) -> list[verify.Check]:
        if not coded:
            return []
        wb = _require()
        wbe = wb.WbEnvironment()
        wbe.verbose = False
        out = wbe.read_raster(str(written))
        meta = out.metadata()
        arr = out.to_numpy(dtype="float64")
        if math.isnan(meta.nodata):
            valid = arr[~np.isnan(arr)]
        else:
            valid = arr[(arr != meta.nodata) & ~np.isnan(arr)]
        seen = {float(v) for v in np.unique(valid)}
        stray = sorted(seen - _POINTER_CODES)
        return [
            verify.Check(
                # A range check would pass a 3: it is between 0 and 128 and it is
                # not a direction. The valid codes are a SET.
                "values_in_expected_range",
                not stray,
                f"codes {sorted(seen)} are powers of two" if not stray
                else f"{stray} are not D8 direction codes",
            )
        ]

    parameters: dict[str, Any] = {"method": method, "tool": tool}
    if coded:
        # The table itself, not its name: a name is a pointer into documentation
        # that may be wrong, and on this engine it demonstrably is.
        parameters["encoding"] = encoding
        parameters["direction_codes"] = dict(POINTER_ENCODINGS[encoding])

    def run_tool(wbe, dem):
        tool_fn = getattr(wbe.hydrology.flow_routing, tool)
        if coded:
            # The engine's own flag name is its vendor's; the value is what matters.
            return tool_fn(input=dem, esri_pntr=(encoding == "east_first"))
        return tool_fn(input=dem)

    return _derivative(
        "flow_direction",
        dem_path,
        output_path,
        parameters=parameters,
        call=run_tool,
        value_range=None,
        extra_checks=codes_are_directions,
    )


def euclidean_distance(input_path: str, output_path: str) -> dict[str, Any]:
    """Distance from every cell to the nearest non-zero cell, in the CRS's units.

    The unit is the raster's own horizontal unit, which is why a geographic CRS
    is refused: a distance in degrees is not a distance, it varies with latitude,
    and it comes back as a number that looks like metres.

    Whitebox treats the NON-ZERO cells as the sources, so a mask where features
    are 1 and background is 0 behaves as expected. A mask whose background is
    nodata rather than 0 does not, and that is worth knowing before reading the
    output as a proximity surface.
    """
    wb = _require()
    wbe = wb.WbEnvironment()
    wbe.verbose = False
    with _read_dem(wbe, input_path) as (source, crs, geographic, input_note):
        if geographic:
            raise ValueError(
                "euclidean_distance on a geographic CRS would measure in degrees, which "
                "is not a length: a degree of longitude is 111 km at the equator and "
                "83 km in Rome. Reproject to a projected CRS first."
            )
        record = ProvenanceRecord(
            operation="euclidean_distance",
            parameters={"source_cells": "non-zero"},
            inputs=[InputRecord.from_path(input_path, crs=crs)],
            engine=_engine_info(),
        )
        record.crs_decisions = {
            "analysis_crs": crs,
            "reason": "distance is measured in the raster's own projected units; a "
            "geographic CRS is refused because degrees are not a length",
        }
        meta = source.metadata()
        result = wbe.raster.distance_cost.euclidean_distance(input=source)
        wbe.write_raster(result, str(output_path))
        checks = _raster_checks(
            wbe,
            output_path,
            expect_epsg=source.crs_epsg(),
            expect_shape=(meta.rows, meta.columns),
            # Both ends are closed form from the grid: a distance cannot be
            # negative, and nothing in the raster can be farther from a source
            # than the grid's own diagonal.
            value_range=(
                0.0,
                math.hypot(
                    meta.rows * abs(meta.resolution_y),
                    meta.columns * abs(meta.resolution_x),
                ),
            ),
        )
        if input_note:
            record.notes.append(input_note)
        manifest = record.add_verification(checks).finish().write_for(output_path)
        verify.enforce(checks, "euclidean_distance")
        return {
            "output": str(output_path),
            "units": "the raster's own horizontal units",
            "provenance": str(manifest),
            "verified": True,
        }


def viewshed(
    dem_path: str,
    stations_path: str,
    output_path: str,
    station_height: float,
) -> dict[str, Any]:
    """How many observing stations can see each cell.

    **The output is a COUNT, not a yes/no**, and that sentence is here because
    the tool's own documentation says the opposite. The Whitebox help for
    Viewshed states "The output image will be a Boolean raster, containing 1's
    and 0's"; measured on the installed 2.0.6 with two stations on flat ground,
    every cell comes back `2.0`. The classic Rust source settles it — it calls
    `output.increment(...)`, not `set_value`. A caller who trusted the manual
    and thresholded at `> 0` would be right by accident, and one who summed the
    raster expecting an area would be wrong by a factor of the station count.
    This is the second place where this library's prose describes the reverse of
    what its code does; the first was the D8 pointer table (see
    `POINTER_ENCODINGS`), and the lesson both times was to measure.

    `station_height` has no default even though the library defaults it to 2.0,
    because **the unit is the DEM's Z unit, not metres**: on a DEM in US survey
    feet, 2.0 is two feet, and an eye height of 0.6 m would be a silent error
    with a plausible-looking viewshed to show for it. There is no target height
    in this tool — only the observer is raised — so a radio mast at the far end
    is not modelled. Use `line_of_sight` for a two-ended check.

    A geographic CRS is refused: the height is in Z units while the cell size is
    in degrees, so the vertical and horizontal are on different scales and the
    horizon comes out at the wrong distance.
    """
    wb = _require()
    if station_height < 0:
        raise ValueError(f"station_height must be zero or positive, got {station_height}")

    stations = readers.read_vector(stations_path)
    if stations.crs is None:
        raise ValueError(
            readers.no_crs_message(
                stations, f"{stations_path} has no CRS — cannot place the observing "
                "stations on the DEM."
            )
        )
    kinds = set(stations.geom_type.dropna().unique())
    if not kinds.issubset({"Point"}):
        raise ValueError(
            f"observing stations must be Point geometries, got {sorted(kinds)}"
        )
    if stations.empty:
        raise ValueError(
            f"{stations_path} holds no stations, so there is nothing to see from. "
            "The output would be a grid of zeros, which is not the same answer as "
            "'nothing is visible'."
        )

    wbe = wb.WbEnvironment()
    wbe.verbose = False
    with _read_dem(wbe, dem_path) as (dem, crs, geographic, input_note):
        if geographic:
            raise ValueError(
                "viewshed on a geographic CRS would compare a station height in the "
                "DEM's Z unit against cell sizes in degrees, so the horizon lands at "
                "the wrong distance. Reproject the DEM to a projected CRS first."
            )
        record = ProvenanceRecord(
            operation="viewshed",
            parameters={
                "station_height": station_height,
                "height_unit": "the DEM's Z unit, not necessarily metres",
                "n_stations": len(stations),
                "output_meaning": "number of stations that can see the cell",
            },
            inputs=[
                InputRecord.from_path(dem_path, crs=crs),
                InputRecord.from_path(
                    stations_path, crs=verify.crs_label(stations.crs)
                ),
            ],
            engine=_engine_info(),
        )
        if not verify.same_crs(stations.crs, crs):
            stations = stations.to_crs(crs)
            record.crs_decisions = {
                "analysis_crs": crs,
                "reason": "stations reprojected to the DEM CRS so each one stands on "
                "the cell it actually occupies",
            }
        else:
            record.crs_decisions = {
                "analysis_crs": crs,
                "reason": "stations and DEM share the same CRS",
            }

        ws = workspace.root()
        with tempfile.TemporaryDirectory(dir=str(ws) if ws else None) as tmp:
            shp = Path(tmp) / "stations.shp"
            stations.to_file(shp)
            vec = wbe.read_vector(str(shp))
            seen = wbe.terrain.visibility.viewshed(
                input=dem, stations=vec, height=station_height
            )
            wbe.write_raster(seen, str(output_path))

        meta = dem.metadata()
        checks = _raster_checks(
            wbe,
            output_path,
            expect_epsg=dem.crs_epsg(),
            expect_shape=(meta.rows, meta.columns),
            # 0..n, because it counts. Written as the station count rather than
            # 1.0 precisely because the documentation says 1.0: if a future
            # version really does turn it into a boolean, this range still
            # passes and the note below stops being true — so there is also a
            # test that asserts the count semantics directly.
            value_range=(0.0, float(len(stations))),
        )
        if input_note:
            record.notes.append(input_note)
        record.notes.append(
            "each cell holds the NUMBER of stations that can see it, not a 0/1 flag: "
            "measured on whitebox-workflows 2.0.6, against its own documentation"
        )
        manifest = record.add_verification(checks).finish().write_for(output_path)
        verify.enforce(checks, "viewshed")
        return {
            "output": str(output_path),
            "stations": len(stations),
            "station_height": station_height,
            "shape": [meta.rows, meta.columns],
            "provenance": str(manifest),
            "verified": True,
        }


def idw_interpolation(
    points_path: str,
    output_path: str,
    field_name: str,
    cell_size: float,
    weight: float = 2.0,
    radius: float = 0.0,
    min_points: int = 0,
) -> dict[str, Any]:
    """Inverse-distance-weighted surface from a point layer. The field is REQUIRED.

    `field_name` has no default here because of the library's default: whitebox
    interpolates FID when you do not say otherwise, which produces a smooth,
    plausible and perfectly meaningless surface of ROW NUMBERS. Nothing raises,
    the raster renders, and a caller who forgot the argument gets a map of the
    order the points happened to be stored in.

    `weight` is the distance exponent: 2 is the usual choice, and higher values
    make the surface flatter between points and peakier at them. It is recorded,
    because an IDW surface without its exponent cannot be reproduced.

    A geographic CRS is refused, for the same reason `euclidean_distance`
    refuses one and with more consequence: IDW weights every sample by its
    distance, and in degrees at 41 degrees north a degree of longitude covers
    0.75 of the ground a degree of latitude does. The weighting comes out
    anisotropic by a third, the surface is stretched east-west, and nothing in
    the output says so — it renders, it is smooth, and it is wrong in a way that
    looks like terrain. Until 0.3.0 this ran happily on EPSG:4326 and recorded
    "the cell size is read in that CRS's units" as though that settled it.
    """
    wb = _require()
    if cell_size <= 0:
        raise ValueError(f"cell_size must be positive, got {cell_size}")
    if not field_name or not str(field_name).strip():
        raise ValueError(
            "field_name is required: whitebox interpolates FID by default, which "
            "produces a smooth and meaningless surface of row numbers, with no warning."
        )
    # Checked BEFORE the engine runs, and named as the INPUT's problem. The
    # output-side `crs_present` check does catch a CRS-less input, but only
    # afterwards, and its message sends the caller to look at the output — the
    # wrong artifact.
    layer = readers.read_vector(points_path)
    if layer.crs is None:
        raise ValueError(
            readers.no_crs_message(
                layer,
                f"{points_path} declares no CRS, so the distances IDW weights by "
                "mean nothing.",
            )
        )
    crs = layer.crs
    if crs.is_geographic:
        raise ValueError(
            "idw_interpolation on a geographic CRS would weight samples by a distance "
            "in degrees, which is not a distance: a degree of longitude is 111 km at "
            "the equator and 83 km in Rome, so the weighting comes out anisotropic by "
            "a third and the surface is stretched east-west with nothing to show for "
            "it. Reproject to a projected CRS first."
        )

    wbe = wb.WbEnvironment()
    wbe.verbose = False
    # No `_needs_plain_copy` here, unlike the DEM path this was copied from: that
    # helper opens a path with rasterio looking for a TIFF predictor, which on a
    # vector layer raises and is swallowed, so the branch was permanently dead —
    # and had it ever fired it would have written a single-band GeoTIFF in place
    # of the point layer. Removed rather than guarded.
    points = wbe.read_vector(str(points_path))
    record = ProvenanceRecord(
        operation="idw_interpolation",
        parameters={
            "field_name": field_name,
            "cell_size": cell_size,
            "weight": weight,
            "radius": radius,
            "min_points": min_points,
        },
        inputs=[InputRecord.from_path(points_path, crs=verify.crs_label(crs))],
        engine=_engine_info(),
    )
    result = wbe.raster.general.idw_interpolation(
        points=points,
        field_name=field_name,
        weight=weight,
        radius=radius,
        min_points=min_points,
        cell_size=cell_size,
    )
    wbe.write_raster(result, str(output_path))
    meta = result.metadata()
    epsg = result.crs_epsg()
    record.crs_decisions = {
        "analysis_crs": f"EPSG:{epsg}" if epsg else verify.crs_label(crs),
        "reason": "the surface is built in the point layer's own CRS, which is "
        "projected — refused otherwise — so the cell size and the distance "
        "weighting are both in that CRS's linear unit",
    }
    checks = _raster_checks(
        wbe,
        output_path,
        expect_epsg=epsg,
        expect_shape=(meta.rows, meta.columns),
        value_range=None,
    )
    manifest = record.add_verification(checks).finish().write_for(output_path)
    verify.enforce(checks, "idw_interpolation")
    return {
        "output": str(output_path),
        "field_name": field_name,
        "cell_size": cell_size,
        "weight": weight,
        "shape": [meta.rows, meta.columns],
        "provenance": str(manifest),
        "verified": True,
    }
