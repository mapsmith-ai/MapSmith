"""SedonaDB engine: heavy overlays, distance joins, KNN — 10-180x on benchmarks.

Optional dependency (`pip install mapsmith[sedona]`). In-process Rust engine
(Arrow/DataFusion). API is pre-1.0: this wrapper stays deliberately thin.
"""

from __future__ import annotations

from typing import Any

from ..provenance import InputRecord, ProvenanceRecord


def _engine_info() -> dict[str, str]:
    import sedonadb  # the wheel behind apache-sedona[db]

    return {"name": "sedonadb", "version": getattr(sedonadb, "__version__", "unknown")}


def spatial_join(
    left_path: str, right_path: str, output_path: str, predicate: str = "intersects"
) -> dict[str, Any]:
    """Spatial join on SedonaDB. Keeps left columns (right attributes omitted).

    Note: inner join semantics — a left feature is repeated once per match,
    like GeoPandas sjoin. Recorded in the provenance manifest.
    """
    import sedona.db

    predicates = {"intersects": "ST_Intersects", "within": "ST_Within", "contains": "ST_Contains"}
    if predicate not in predicates:
        raise ValueError(f"predicate must be one of {sorted(predicates)}, got {predicate!r}")

    sd = sedona.db.connect()
    record = ProvenanceRecord(
        operation="spatial_join",
        parameters={"predicate": predicate, "engine": "sedonadb", "columns": "left-only"},
        inputs=[InputRecord.from_path(left_path), InputRecord.from_path(right_path)],
        engine=_engine_info(),
    )

    def _load(path: str, view: str) -> None:
        if str(path).lower().endswith(".parquet"):
            sd.read_parquet(path).to_view(view)
        else:
            sd.read_pyogrio(path).to_view(view)

    _load(left_path, "l")
    _load(right_path, "r")
    fn = predicates[predicate]
    result = sd.sql(f"SELECT l.* FROM l JOIN r ON {fn}(l.geometry, r.geometry)")
    result.to_parquet(output_path)
    count = sd.sql(
        "SELECT count(*) FROM l JOIN r ON " + f"{fn}(l.geometry, r.geometry)"
    ).to_pandas()
    feature_count = int(count.iloc[0, 0])
    manifest = record.finish().write_for(output_path)
    return {
        "output": str(output_path),
        "feature_count": feature_count,
        "provenance": str(manifest),
    }
