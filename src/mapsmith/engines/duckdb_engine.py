"""DuckDB spatial engine: SQL, filters, aggregations, point-in-polygon joins.

Benchmarks (2025-26): DuckDB's SPATIAL_JOIN operator does point-in-polygon at
~2M rows/sec on a laptop. The spatial extension is NOT bundled in the wheel:
we install it on first use (needs network once per environment).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb

from .. import verify
from ..provenance import InputRecord, ProvenanceRecord

_PREVIEW_ROWS = 50


def _engine_info() -> dict[str, str]:
    return {"name": "duckdb", "version": duckdb.__version__}


def _connect() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    try:
        con.install_extension("spatial")
    except duckdb.Error:
        pass  # already installed in this environment, or offline with a cached copy
    con.load_extension("spatial")
    return con


def _quote(path: str) -> str:
    return str(path).replace("'", "''")


def _rel(path: str) -> str:
    """SQL relation for a dataset file: GeoParquet natively, other formats via GDAL."""
    if str(path).lower().endswith(".parquet"):
        return f"read_parquet('{_quote(path)}')"
    return f"ST_Read('{_quote(path)}')"


def run_sql(query: str, output_path: str | None = None) -> dict[str, Any]:
    """Run spatial SQL. With output_path, materialize the result as GeoParquet."""
    con = _connect()
    record = ProvenanceRecord(
        operation="run_sql",
        parameters={"query": query},
        inputs=[],
        engine=_engine_info(),
    )
    if output_path:
        con.sql(f"COPY ({query}) TO '{_quote(output_path)}' (FORMAT parquet)")
        count = con.sql(
            f"SELECT count(*) FROM read_parquet('{_quote(output_path)}')"
        ).fetchone()[0]
        # SQL is opaque to static analysis: the cheapest honest check is whether
        # the materialized result carries georeference metadata (non-critical —
        # a purely tabular result is legitimate).
        out_crs = verify.probe_crs(output_path)
        checks = [
            verify.Check(
                "output_has_georeference",
                out_crs != verify.UNKNOWN_CRS,
                out_crs if out_crs != verify.UNKNOWN_CRS else
                "no geo metadata (non-spatial result?)",
                critical=False,
            )
        ]
        manifest = record.add_verification(checks).finish().write_for(output_path)
        return {"output": str(output_path), "row_count": int(count), "provenance": str(manifest)}
    result = con.sql(query)
    rows = result.fetchmany(_PREVIEW_ROWS)
    return {
        "columns": [d[0] for d in result.description],
        "rows": [[repr(v) if isinstance(v, (bytes, bytearray)) else v for v in r] for r in rows],
        "truncated_at": _PREVIEW_ROWS,
    }


def spatial_join(
    left_path: str, right_path: str, output_path: str, predicate: str = "intersects"
) -> dict[str, Any]:
    """Attribute join by spatial predicate. GeoParquet-native fast path."""
    predicates = {"intersects": "ST_Intersects", "within": "ST_Within", "contains": "ST_Contains"}
    if predicate not in predicates:
        raise ValueError(f"predicate must be one of {sorted(predicates)}, got {predicate!r}")
    con = _connect()
    left_crs = verify.probe_crs(left_path)
    record = ProvenanceRecord(
        operation="spatial_join",
        parameters={"predicate": predicate, "engine": "duckdb"},
        inputs=[
            InputRecord.from_path(left_path, crs=left_crs),
            InputRecord.from_path(right_path, crs=verify.probe_crs(right_path)),
        ],
        engine=_engine_info(),
    )
    record.crs_decisions = {
        "analysis_crs": left_crs,
        "reason": "DuckDB fast path joins in the shared input CRS "
        "(the router only takes this path when both inputs already match)",
    }
    fn = predicates[predicate]
    query = f"""
        SELECT l.*, r.* EXCLUDE (geometry)
        FROM {_rel(left_path)} AS l
        JOIN {_rel(right_path)} AS r
          ON {fn}(l.geometry, r.geometry)
    """
    con.sql(f"COPY ({query}) TO '{_quote(output_path)}' (FORMAT parquet)")
    count = con.sql(f"SELECT count(*) FROM read_parquet('{_quote(output_path)}')").fetchone()[0]
    checks = verify.verify_vector_output(
        output_path,
        expect_crs=left_crs if left_crs != verify.UNKNOWN_CRS else None,
    )
    manifest = record.add_verification(checks).finish().write_for(output_path)
    verify.enforce(checks, "spatial_join")
    return {
        "output": str(output_path),
        "feature_count": int(count),
        "provenance": str(manifest),
        "verified": True,
    }


def supports_inputs(*paths: str) -> bool:
    """The DuckDB fast path expects GeoParquet inputs with a 'geometry' column."""
    return all(Path(p).suffix.lower() == ".parquet" for p in paths)
