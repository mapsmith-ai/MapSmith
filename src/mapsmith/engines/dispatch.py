"""Engine dispatcher: route each workload class to the fastest available engine.

The 2025-2026 benchmarks are unambiguous (see docs in the repo wiki):
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
