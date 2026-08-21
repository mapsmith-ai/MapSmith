"""DuckDB spatial engine: SQL, filters, aggregations, point-in-polygon joins.

Benchmarks (2025-26): DuckDB's SPATIAL_JOIN operator does point-in-polygon at
~2M rows/sec on a laptop. The spatial extension is NOT bundled in the wheel:
we install it on first use (needs network once per environment).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

import duckdb

from .. import verify, workspace
from ..provenance import InputRecord, ProvenanceRecord

_PREVIEW_ROWS = 50


def _engine_info() -> dict[str, str]:
    return {"name": "duckdb", "version": duckdb.__version__}


def _sql_dir(path: Path) -> str:
    """Directory path as a DuckDB SQL string literal: forward slashes (GDAL and
    DuckDB normalize to them) and a trailing separator, because
    allowed_directories matches by PREFIX — without it 'C:/ws' would also
    admit 'C:/ws-evil'."""
    return (str(path).replace("\\", "/").rstrip("/") + "/").replace("'", "''")


def _connect() -> duckdb.DuckDBPyConnection:
    """A DuckDB connection with the spatial extension; sandboxed when
    MAPSMITH_WORKSPACE is set.

    SQL text can name any file, out of reach of the textual path jail at the
    tool boundary — so the confinement happens in the engine itself. Order is
    load-bearing (each step would be refused after the next one):
    extensions first, then the filesystem whitelist, then external access off
    (which is what ACTIVATES the whitelist: allowed_directories are the
    exceptions to it), configuration lock last. ST_Read is covered too: the
    spatial extension routes GDAL I/O through DuckDB's filesystem, and its
    /vsi* and DRIVER:https:// escapes are refused once external access is off.
    allowed_directories cannot go in the connect() config dict (VARCHAR[]
    options crash there — duckdb#17128); SET statements are the supported way.
    """
    # These apply in EVERY mode. Unconfined *file access* without a workspace is
    # a deliberate, documented choice; unconfined *code execution* is not, and
    # DuckDB's defaults allow the escalation: with autoload and community
    # extensions on, one statement can install `shellfs` (which runs a shell
    # command for a filename ending in '|') or autoload httpfs and exfiltrate
    # results in a URL. SQL written by an LLM from whatever reached its context
    # is untrusted SQL, so DuckDB's own hardening guidance applies.
    con = duckdb.connect(
        config={
            "autoinstall_known_extensions": "false",
            "autoload_known_extensions": "false",
            "allow_community_extensions": "false",  # self-locking by design
            "memory_limit": os.environ.get("MAPSMITH_DUCKDB_MEMORY", "4GB"),
        }
    )
    try:
        con.install_extension("spatial")
    except duckdb.Error:
        pass  # already installed in this environment, or offline with a cached copy
    con.load_extension("spatial")

    ws = workspace.root()
    tmp_limit = os.environ.get("MAPSMITH_DUCKDB_TEMP_LIMIT", "8GB").replace("'", "''")
    if ws is not None:
        con.execute(f"SET allowed_directories = ['{_sql_dir(ws)}']")
        con.execute(f"SET temp_directory = '{_sql_dir(ws)}.duckdb_tmp'")
        con.execute("SET enable_external_access = false")
    else:
        # No workspace means unconfined FILE access (documented). It must not
        # also mean network egress: an agent can still LOAD a signed core
        # extension like httpfs — autoload being off does not stop an explicit
        # LOAD, and enable_external_access=false would take local files with
        # it. Disabling the remote filesystems keeps local access intact and
        # closes the exfiltration channel (verified: read_csv of a URL raises
        # PermissionException while a local read still works).
        con.execute("SET disabled_filesystems = 'HTTPFileSystem,S3FileSystem'")
        # ...and unconfined disk is not part of the deal either: without a
        # limit a spill fills the volume.
        con.execute(f"SET temp_directory = '{_sql_dir(Path(tempfile.gettempdir()))}mapsmith'")
    con.execute(f"SET max_temp_directory_size = '{tmp_limit}'")
    # Locking is what makes the extension settings above a policy rather than a
    # default: without it, one multi-statement call re-enables autoload and
    # then INSTALL/LOAD of a signed extension (httpfs) opens a network egress
    # path. So it applies in EVERY mode, not just under a workspace.
    con.execute("SET lock_configuration = true")
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
    # SQL text is out of reach of the path guard at the tool boundary, and GDAL
    # brings its own HTTP client: without this the remote opt-in (#21) would be
    # decorative for exactly the statement that can reach the network.
    workspace.refuse_remote_in_sql(query)
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


def _write_empty_geoparquet(output_path: str, crs: str) -> None:
    """Rewrite a zero-row COPY output as a valid, empty GeoParquet."""
    import geopandas as gpd
    import pandas as pd

    table = pd.read_parquet(output_path)
    columns = [c for c in table.columns if c != "geometry"]
    empty = gpd.GeoDataFrame(
        {c: pd.Series(dtype=table[c].dtype) for c in columns},
        geometry=gpd.GeoSeries([], dtype="geometry"),
        crs=None if crs == verify.UNKNOWN_CRS else crs,
    )
    empty.to_parquet(output_path)


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
    with verify.audit_on_failure(record, output_path, []):
        con.sql(f"COPY ({query}) TO '{_quote(output_path)}' (FORMAT parquet)")
        count = con.sql(
            f"SELECT count(*) FROM read_parquet('{_quote(output_path)}')"
        ).fetchone()[0]
        # only worth asking when the result is empty: a non-empty join already
        # proves both inputs were populated, and counting via SQL (never a
        # GeoPandas read) keeps the fast path fast
        inputs_populated = count > 0 or all(
            con.sql(f"SELECT count(*) FROM {_rel(path)}").fetchone()[0] > 0
            for path in (left_path, right_path)
        )
    if count == 0:
        # DuckDB's COPY writes no "geo" metadata for a zero-row result, which
        # leaves an output that is not a GeoParquet at all: a downstream plan
        # step reading it would fail on a file we reported as written. An empty
        # result is a legitimate answer, so it has to be a legitimate file —
        # rewritten here with the analysis CRS and the joined schema.
        _write_empty_geoparquet(output_path, left_crs)

    # through audited() like every other writer: reading the output used to
    # raise before the manifest was written, losing the audit trail for exactly
    # the case the checks exist to explain
    manifest, extras = verify.audited(
        record,
        output_path,
        operation="spatial_join",
        checks_fn=lambda: verify.verify_vector_output(
            output_path,
            expect_crs=left_crs if left_crs != verify.UNKNOWN_CRS else None,
            on_empty="warn" if inputs_populated else "ignore",
        ),
        repair=False,  # the join's geometry comes straight from the inputs
    )
    return {
        "output": str(output_path),
        "feature_count": int(count),
        "provenance": manifest,
        "verified": True,
        **extras,
    }


def supports_inputs(*paths: str) -> bool:
    """The DuckDB fast path expects GeoParquet inputs with a 'geometry' column."""
    return all(Path(p).suffix.lower() == ".parquet" for p in paths)
