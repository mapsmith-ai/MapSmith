"""Engine dispatcher: route each workload class to the fastest available engine.

The 2025-2026 published engine benchmarks are unambiguous (upstream figures,
not ours — MapSmith's own measurements live in docs/benchmarks.md):
- SedonaDB wins heavy joins/KNN by 10-180x, in-process, optional dependency.
- DuckDB spatial wins filters/aggregations/point-in-polygon (~2M rows/sec).
- GeoPandas/Shapely stays as the long-tail lane (<~1M features).

Engines are optional imports: the dispatcher degrades gracefully and the
provenance manifest always records which engine actually ran.
"""

from __future__ import annotations

from enum import Enum
from functools import cache


class Workload(str, Enum):
    SQL = "sql"                # filters, aggregations, ad-hoc SQL
    HEAVY_JOIN = "heavy_join"  # polygon overlay, distance joins, KNN
    SMALL_VECTOR = "small_vector"  # long-tail ops on small data


# Preference order per workload class; first available wins.
_PREFERENCES: dict[Workload, list[str]] = {
    Workload.SQL: ["duckdb", "geopandas"],
    Workload.HEAVY_JOIN: ["sedonadb", "duckdb", "geopandas"],
    Workload.SMALL_VECTOR: ["geopandas"],
}


@cache
def available_engines() -> dict[str, bool]:
    """Probe optional engines once per process."""
    status: dict[str, bool] = {"geopandas": True}  # hard dependency
    try:
        import duckdb  # noqa: F401

        status["duckdb"] = True
    except ImportError:
        status["duckdb"] = False
    try:
        import sedona.db  # noqa: F401

        status["sedonadb"] = True
    except ImportError:
        status["sedonadb"] = False
    try:
        import exactextract  # noqa: F401
        import rasterio  # noqa: F401

        status["exactextract"] = True
    except ImportError:
        status["exactextract"] = False
    try:
        import whitebox_workflows  # noqa: F401

        status["whitebox"] = True
    except ImportError:
        status["whitebox"] = False
    return status


def pick(workload: Workload, requested: str = "auto") -> str:
    """Pick the engine for a workload. `requested` may force a specific engine."""
    engines = available_engines()
    if requested != "auto":
        if not engines.get(requested, False):
            raise RuntimeError(
                f"Engine '{requested}' is not available in this installation. "
                f"Available: {[k for k, v in engines.items() if v]}"
            )
        return requested
    for name in _PREFERENCES[workload]:
        if engines.get(name, False):
            return name
    raise RuntimeError(f"No engine available for workload {workload}")


def spatial_join_routed(
    left_path: str,
    right_path: str,
    output_path: str,
    predicate: str = "intersects",
    engine: str = "auto",
) -> dict:
    """Run a spatial join on the fastest available engine (single routing source).

    Used by both the MCP tool and the plan executor so routing policy cannot
    drift between the two entry points. The fast engines (SedonaDB, DuckDB)
    require both inputs in the same CRS: with engine='auto' mismatched or
    unknown CRS falls back to the GeoPandas path, which aligns the right layer
    and records the decision; an explicitly requested fast engine raises a
    helpful error instead of failing mid-query.
    """
    from .. import verify
    from . import duckdb_engine, vector

    chosen = pick(Workload.HEAVY_JOIN, engine)
    if chosen == "duckdb" and not duckdb_engine.supports_inputs(left_path, right_path):
        chosen = "geopandas"  # the DuckDB fast path is GeoParquet-only
    if chosen in ("sedonadb", "duckdb"):
        left_crs = verify.probe_crs(left_path)
        right_crs = verify.probe_crs(right_path)
        if left_crs == verify.UNKNOWN_CRS or left_crs != right_crs:
            if engine != "auto":
                raise ValueError(
                    f"engine '{chosen}' requires both inputs in the same CRS "
                    f"(got {left_crs} vs {right_crs}). Reproject first, or use "
                    "engine='auto' for the aligning GeoPandas path."
                )
            chosen = "geopandas"
    if chosen == "sedonadb":
        from . import sedona_engine

        fn = sedona_engine.spatial_join
    elif chosen == "duckdb":
        fn = duckdb_engine.spatial_join
    else:
        fn = vector.spatial_join
    result = fn(left_path, right_path, output_path, predicate)
    result["engine_used"] = chosen
    return result
