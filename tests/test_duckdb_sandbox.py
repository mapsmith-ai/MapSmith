"""DuckDB connection sandbox: run_sql confinement when MAPSMITH_WORKSPACE is set.

SQL text can name any file, so the tool-boundary path jail cannot see it —
the engine connection itself must refuse anything outside the workspace.
These tests probe the real DuckDB behavior, including the corners the docs
leave open (prefix matching vs traversal), so a duckdb version bump that
weakens the sandbox fails CI here.
"""

import pytest

duckdb = pytest.importorskip("duckdb")

from mapsmith.engines import duckdb_engine


@pytest.fixture
def ws(monkeypatch, tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setenv("MAPSMITH_WORKSPACE", str(root))
    return root


def _plain_write_parquet(path, value: int) -> None:
    """Write a parquet OUTSIDE the sandbox, with a plain unrestricted connection."""
    con = duckdb.connect()
    con.execute(f"COPY (SELECT {value} AS v) TO '{str(path).replace(chr(92), '/')}' (FORMAT parquet)")
    con.close()


def test_io_inside_workspace_works(ws):
    con = duckdb_engine._connect()
    target = str(ws / "t.parquet").replace("\\", "/")
    con.execute(f"COPY (SELECT 42 AS v) TO '{target}' (FORMAT parquet)")
    assert con.sql(f"SELECT v FROM read_parquet('{target}')").fetchone()[0] == 42


def test_read_outside_workspace_refused(ws, tmp_path):
    outside = tmp_path / "outside.parquet"
    _plain_write_parquet(outside, 7)
    con = duckdb_engine._connect()
    with pytest.raises(duckdb.Error, match="(?i)permission"):
        con.sql(f"SELECT * FROM read_parquet('{str(outside).replace(chr(92), '/')}')").fetchall()


def test_write_outside_workspace_refused(ws, tmp_path):
    con = duckdb_engine._connect()
    target = str(tmp_path / "leak.parquet").replace("\\", "/")
    with pytest.raises(duckdb.Error, match="(?i)permission"):
        con.execute(f"COPY (SELECT 1 AS v) TO '{target}' (FORMAT parquet)")


def test_traversal_does_not_escape_the_prefix(ws, tmp_path):
    """allowed_directories matches by prefix: 'C:/ws/../evil' STARTS with
    'C:/ws/', so if DuckDB compared raw strings this would slip through."""
    evil = tmp_path / "evil.parquet"
    _plain_write_parquet(evil, 13)
    con = duckdb_engine._connect()
    sneaky = f"{str(ws).replace(chr(92), '/')}/../evil.parquet"
    with pytest.raises(duckdb.Error, match="(?i)permission"):
        con.sql(f"SELECT * FROM read_parquet('{sneaky}')").fetchall()


def test_sibling_prefix_is_not_inside(ws, tmp_path):
    evil_dir = tmp_path / (ws.name + "-evil")
    evil_dir.mkdir()
    evil = evil_dir / "evil.parquet"
    _plain_write_parquet(evil, 13)
    con = duckdb_engine._connect()
    with pytest.raises(duckdb.Error, match="(?i)permission"):
        con.sql(f"SELECT * FROM read_parquet('{str(evil).replace(chr(92), '/')}')").fetchall()


def test_extension_install_and_load_refused(ws):
    con = duckdb_engine._connect()
    with pytest.raises(duckdb.Error):
        con.execute("INSTALL httpfs")
    with pytest.raises(duckdb.Error):
        con.execute("LOAD httpfs")


def test_configuration_is_locked(ws):
    con = duckdb_engine._connect()
    with pytest.raises(duckdb.Error):
        con.execute("SET enable_external_access = true")
    with pytest.raises(duckdb.Error):
        con.execute("SET allowed_directories = ['/']")
    with pytest.raises(duckdb.Error):
        con.execute("SET memory_limit = '100GB'")


def test_gdal_vsi_paths_refused(ws):
    con = duckdb_engine._connect()
    with pytest.raises(duckdb.Error):
        con.sql("SELECT * FROM ST_Read('/vsicurl/https://example.invalid/x.shp')").fetchall()


def test_no_workspace_means_no_sandbox(monkeypatch, tmp_path):
    monkeypatch.delenv("MAPSMITH_WORKSPACE", raising=False)
    con = duckdb_engine._connect()
    target = str(tmp_path / "free.parquet").replace("\\", "/")
    con.execute(f"COPY (SELECT 1 AS v) TO '{target}' (FORMAT parquet)")
    assert con.sql(f"SELECT v FROM read_parquet('{target}')").fetchone()[0] == 1


def test_run_sql_end_to_end_inside_workspace(ws):
    out = str(ws / "result.parquet")
    result = duckdb_engine.run_sql("SELECT 5 AS v", output_path=out)
    assert result["row_count"] == 1
    assert (ws / "result.parquet").exists()
