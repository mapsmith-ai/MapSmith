"""Deterministic output verification — MapSmith's core promise.

Every operation's output is checked against explicit pre/postconditions
(CRS discipline, geometry validity, count and extent invariants). Results are
recorded in the provenance manifest; critical failures raise instead of
silently returning wrong data. External deterministic signals beat LLM
self-critique — so none of this involves a model.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import geopandas as gpd


class VerificationError(RuntimeError):
    """An output failed a critical deterministic check. The manifest still records it."""


UNKNOWN_CRS = "unknown"


def probe_crs(path: str) -> str:
    """CRS of an existing dataset from metadata only (no data scan, never raises).

    GeoParquet via the pyarrow ``geo`` schema metadata (missing/null crs means
    OGC:CRS84 per spec), rasters via rasterio when installed, everything else
    via pyogrio. Returns ``"unknown"`` whenever the CRS cannot be determined.
    """
    lower = str(path).lower()
    try:
        if lower.endswith(".parquet"):
            return _probe_geoparquet_crs(str(path))
        if lower.endswith((".tif", ".tiff")):
            try:
                import rasterio
            except ImportError:
                return UNKNOWN_CRS
            with rasterio.open(path) as ds:
                return str(ds.crs) if ds.crs else UNKNOWN_CRS
        import pyogrio

        crs = pyogrio.read_info(str(path)).get("crs")
        return str(crs) if crs else UNKNOWN_CRS
    except Exception:  # noqa: BLE001 — probing must never break the caller
        return UNKNOWN_CRS


def _probe_geoparquet_crs(path: str) -> str:
    import json

    import pyarrow.parquet as pq

    metadata = pq.read_schema(path).metadata or {}
    geo = metadata.get(b"geo")
    if not geo:
        return UNKNOWN_CRS
    geo_meta = json.loads(geo)
    column = geo_meta.get("primary_column", "geometry")
    # Per the GeoParquet spec a missing/null "crs" means OGC:CRS84.
    crs = geo_meta.get("columns", {}).get(column, {}).get("crs") or "OGC:CRS84"
    from pyproj import CRS

    parsed = CRS.from_user_input(crs)
    epsg = parsed.to_epsg()
    return f"EPSG:{epsg}" if epsg else parsed.name


@dataclass
class Check:
    name: str
    passed: bool
    detail: str
    critical: bool = True

    def as_dict(self) -> dict:
        return asdict(self)


def verify_vector_output(
    output_path: str,
    *,
    expect_crs: str | None = None,
    expect_count: int | None = None,
    max_count: int | None = None,
    expect_geometry: set[str] | None = None,
    within_bounds: tuple[float, float, float, float] | None = None,
    bounds_margin: float = 1e-6,
) -> list[Check]:
    """Run postcondition checks on a vector output. Returns all checks (pass and fail)."""
    checks: list[Check] = []
    gdf = gpd.read_file(output_path) if not str(output_path).lower().endswith(".parquet") else (
        gpd.read_parquet(output_path)
    )

    checks.append(
        Check(
            "crs_present",
            gdf.crs is not None,
            str(gdf.crs) if gdf.crs else "output has no CRS",
        )
    )
    if expect_crs is not None and gdf.crs is not None:
        same = gdf.crs.equals(expect_crs) if hasattr(gdf.crs, "equals") else str(
            gdf.crs
        ) == str(expect_crs)
        checks.append(
            Check("crs_matches", bool(same), f"expected {expect_crs}, got {gdf.crs}")
        )

    if len(gdf) > 0:
        invalid = int((~gdf.geometry.is_valid).sum())
        checks.append(
            Check(
                "geometry_valid",
                invalid == 0,
                f"{invalid}/{len(gdf)} invalid geometries" if invalid else "all valid",
            )
        )
        empty = int(gdf.geometry.is_empty.sum())
        checks.append(
            Check(
                "geometry_not_empty",
                empty == 0,
                f"{empty}/{len(gdf)} empty geometries" if empty else "none empty",
                critical=False,
            )
        )

    if expect_count is not None:
        checks.append(
            Check(
                "feature_count_exact",
                len(gdf) == expect_count,
                f"expected {expect_count}, got {len(gdf)}",
            )
        )
    if max_count is not None:
        checks.append(
            Check(
                "feature_count_bounded",
                len(gdf) <= max_count,
                f"expected <= {max_count}, got {len(gdf)}",
            )
        )

    if expect_geometry is not None and len(gdf) > 0:
        found = set(gdf.geom_type.dropna().unique())
        checks.append(
            Check(
                "geometry_types",
                found.issubset(expect_geometry),
                f"expected subset of {sorted(expect_geometry)}, got {sorted(found)}",
            )
        )

    if within_bounds is not None and len(gdf) > 0:
        minx, miny, maxx, maxy = gdf.total_bounds
        exp = within_bounds
        m = bounds_margin
        inside = (
            minx >= exp[0] - m and miny >= exp[1] - m and maxx <= exp[2] + m and maxy <= exp[3] + m
        )
        checks.append(
            Check(
                "extent_within_expected",
                bool(inside),
                f"output bounds {[round(v, 6) for v in (minx, miny, maxx, maxy)]} "
                f"vs expected {[round(v, 6) for v in exp]} (margin {m})",
                critical=False,
            )
        )

    return checks


def enforce(checks: list[Check], operation: str) -> None:
    """Raise VerificationError if any critical check failed."""
    failed = [c for c in checks if not c.passed and c.critical]
    if failed:
        details = "; ".join(f"{c.name}: {c.detail}" for c in failed)
        raise VerificationError(
            f"{operation} output failed deterministic verification — {details}. "
            "The provenance manifest records the full check list."
        )
