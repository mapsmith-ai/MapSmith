"""Workspace jail: uniform path containment at the MCP tool boundary."""

import os

import pytest

from mapsmith import workspace

# --- non-local path forms are rejected ALWAYS, workspace set or not --------

@pytest.mark.parametrize(
    "bad",
    [
        r"\\evil-host\share\data.parquet",  # UNC: SMB/NTLM leak on first touch
        "//evil-host/share/data.parquet",
        "/vsicurl/https://evil/x.parquet",  # GDAL virtual fs reaches the network
        "https://evil.example/x.parquet",
        r"C:\data\out.parquet:stream",      # NTFS alternate data stream
    ],
)
def test_nonlocal_paths_rejected_without_workspace(bad, monkeypatch):
    monkeypatch.delenv("MAPSMITH_WORKSPACE", raising=False)
    with pytest.raises(ValueError):
        workspace.guard(bad, "input_path")


def test_plain_local_path_passes_without_workspace(monkeypatch, tmp_path):
    monkeypatch.delenv("MAPSMITH_WORKSPACE", raising=False)
    p = str(tmp_path / "data.parquet")
    assert workspace.guard(p, "input_path") == p


# --- containment when MAPSMITH_WORKSPACE is set ----------------------------

def test_inside_workspace_passes(monkeypatch, tmp_path):
    monkeypatch.setenv("MAPSMITH_WORKSPACE", str(tmp_path))
    inside = str(tmp_path / "sub" / "out.parquet")
    assert workspace.guard(inside, "output_path") == inside
    assert workspace.guard(str(tmp_path), "output_path") == str(tmp_path)


def test_outside_workspace_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("MAPSMITH_WORKSPACE", str(tmp_path / "ws"))
    with pytest.raises(ValueError, match="MAPSMITH_WORKSPACE"):
        workspace.guard(str(tmp_path / "elsewhere" / "x.parquet"), "output_path")


def test_traversal_escape_rejected(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    monkeypatch.setenv("MAPSMITH_WORKSPACE", str(ws))
    sneaky = str(ws / ".." / "outside.parquet")
    with pytest.raises(ValueError, match="MAPSMITH_WORKSPACE"):
        workspace.guard(sneaky, "output_path")


def test_prefix_sibling_is_not_inside(monkeypatch, tmp_path):
    # /ws-evil must not pass containment for workspace /ws (prefix != subpath)
    ws = tmp_path / "ws"
    monkeypatch.setenv("MAPSMITH_WORKSPACE", str(ws))
    with pytest.raises(ValueError, match="MAPSMITH_WORKSPACE"):
        workspace.guard(str(tmp_path / "ws-evil" / "x.parquet"), "output_path")


def test_case_variant_is_inside_on_case_insensitive_fs(monkeypatch, tmp_path):
    if os.path.normcase("A") != "a":
        pytest.skip("case-sensitive filesystem")
    ws = tmp_path / "ws"
    monkeypatch.setenv("MAPSMITH_WORKSPACE", str(ws))
    variant = str(ws / "OUT.PARQUET").upper()
    assert workspace.guard(variant, "output_path") == variant


# --- enforcement is wired into the MCP tool layer --------------------------

def test_server_tools_guard_paths(monkeypatch, tmp_path):
    from mapsmith import server

    monkeypatch.setenv("MAPSMITH_WORKSPACE", str(tmp_path))
    outside = str(tmp_path.parent / "leak.parquet")
    with pytest.raises(ValueError, match="MAPSMITH_WORKSPACE"):
        server.buffer_layer(outside, 100, str(tmp_path / "out.parquet"))
    with pytest.raises(ValueError, match="MAPSMITH_WORKSPACE"):
        server.run_sql("SELECT 1", output_path=outside)
    with pytest.raises(ValueError, match="not allowed"):
        server.describe_dataset(r"\\evil-host\share\x.gpkg")
    with pytest.raises(ValueError, match="not allowed"):
        server.preview_map([r"/vsicurl/https://evil/x.parquet"])
