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


def test_the_same_file_is_readable_without_a_workspace(monkeypatch, tmp_path):
    """Counterpart to the ST_Read test above: the file is fine, it is the
    sandbox that refuses it. Without this, that test could pass on a typo."""
    import geopandas as gpd
    from shapely.geometry import Point

    target = tmp_path / "readable.gpkg"
    gpd.GeoDataFrame({"id": [1]}, geometry=[Point(0, 0)], crs="EPSG:4326").to_file(target)

    monkeypatch.delenv("MAPSMITH_WORKSPACE", raising=False)
    con = duckdb_engine._connect()
    sql_path = str(target).replace(chr(92), "/")
    assert con.sql(f"SELECT count(*) FROM ST_Read('{sql_path}')").fetchone()[0] == 1


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


def test_st_read_cannot_reach_a_real_file_outside_the_workspace(ws, tmp_path):
    """ST_Read goes through GDAL, so it needs its own check — and it has to use
    a file that really exists, or the test would pass on the read error alone."""
    import geopandas as gpd
    from shapely.geometry import Point

    outside = tmp_path / "outside.gpkg"
    gpd.GeoDataFrame({"id": [1]}, geometry=[Point(0, 0)], crs="EPSG:4326").to_file(outside)
    assert outside.exists()

    target = str(outside).replace(chr(92), "/")
    con = duckdb_engine._connect()
    with pytest.raises(duckdb.Error):
        # surfaces as a GDAL open failure: the driver is denied by the
        # filesystem layer rather than by DuckDB's own path check
        con.sql(f"SELECT * FROM ST_Read('{target}')").fetchall()


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


# --- hardening that applies even without a workspace -----------------------

def test_community_extensions_and_autoload_are_off_without_a_workspace(monkeypatch):
    """Unconfined file access without a workspace is deliberate; escalation to
    shell commands and network egress is not. `shellfs` runs a shell command
    for a filename ending in '|', and autoload pulls httpfs on demand — both
    reachable from one LLM-written statement under DuckDB's defaults."""
    monkeypatch.delenv("MAPSMITH_WORKSPACE", raising=False)
    con = duckdb_engine._connect()

    settings = dict(
        con.sql(
            "SELECT name, value FROM duckdb_settings() WHERE name IN "
            "('allow_community_extensions', 'autoload_known_extensions', "
            "'autoinstall_known_extensions')"
        ).fetchall()
    )
    assert settings == {
        "allow_community_extensions": "false",
        "autoload_known_extensions": "false",
        "autoinstall_known_extensions": "false",
    }

    with pytest.raises(duckdb.Error):
        con.execute("INSTALL shellfs FROM community")
    with pytest.raises(duckdb.Error):
        con.execute("LOAD httpfs")


def test_spatial_still_works_without_a_workspace(tmp_path, monkeypatch):
    """The hardening must not cost the one extension we actually use."""
    monkeypatch.delenv("MAPSMITH_WORKSPACE", raising=False)
    con = duckdb_engine._connect()
    assert con.sql("SELECT ST_AsText(ST_Point(1, 2)) AS wkt").fetchone()[0] == "POINT (1 2)"

    target = str(tmp_path / "free.parquet").replace("\\", "/")
    con.execute(f"COPY (SELECT 1 AS v) TO '{target}' (FORMAT parquet)")
    assert con.sql(f"SELECT v FROM read_parquet('{target}')").fetchone()[0] == 1
