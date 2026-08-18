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
from pathlib import Path
from typing import Any

from .. import verify
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


def _read_dem(wbe: Any, dem_path: str) -> tuple[Any, str]:
    """Read a DEM and return (raster, crs_string). Rejects rasters without a CRS."""
    dem = wbe.read_raster(str(dem_path))
    epsg = dem.crs_epsg()
    wkt = dem.crs_wkt()
    if not epsg and not wkt:
        raise ValueError(
            f"{dem_path} has no CRS — terrain analysis needs one to be meaningful. "
            "Assign the correct CRS to the source raster first."
        )
    return dem, (f"EPSG:{epsg}" if epsg else str(wkt))


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
    checks = [
        verify.Check(
            "crs_present",
            bool(out.crs_epsg() or out.crs_wkt()),
            f"EPSG:{out.crs_epsg()}" if out.crs_epsg() else "output has no CRS",
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
    valid = arr[arr != meta.nodata]
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
    dem, crs = _read_dem(wbe, dem_path)
    record = ProvenanceRecord(
        operation="hillshade",
        parameters={"azimuth": azimuth, "altitude": altitude, "z_factor": z_factor},
        inputs=[InputRecord.from_path(dem_path, crs=crs)],
        engine=_engine_info(),
    )
    record.crs_decisions = {
        "analysis_crs": crs,
        "reason": "hillshade computed in the DEM's native CRS; no reprojection needed",
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
    dem, crs = _read_dem(wbe, dem_path)
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
        "reason": "flow routing computed in the DEM's native CRS; no reprojection needed",
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
    dem, crs = _read_dem(wbe, dem_path)
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
    with tempfile.TemporaryDirectory() as tmp:
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
    manifest = record.add_verification(checks).finish().write_for(output_path)
    verify.enforce(checks, "watershed")
    return {
        "output": str(output_path),
        "n_pour_points": len(points),
        "provenance": str(manifest),
        "verified": True,
    }
