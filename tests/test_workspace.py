"""Workspace jail: uniform path containment at the MCP tool boundary."""

import os

import pytest

from mapsmith import workspace

# --- UNC and ADS forms are rejected ALWAYS, workspace set or not -----------

@pytest.mark.parametrize(
    "bad",
    [
        r"\\evil-host\share\data.parquet",  # UNC: SMB/NTLM leak on first touch
        "//evil-host/share/data.parquet",
        r"C:\data\out.parquet:stream",      # NTFS alternate data stream
    ],
)
def test_hard_forms_rejected_without_workspace(bad, monkeypatch):
    monkeypatch.delenv("MAPSMITH_WORKSPACE", raising=False)
    with pytest.raises(ValueError):
        workspace.guard(bad, "input_path")


# --- remote/virtual forms: opt-in, and never under a workspace -------------

REMOTE = [
    "/vsicurl/https://data.example/cog.tif",  # GDAL virtual filesystem
    "https://data.example/cog.tif",           # cloud-native rasters
]


@pytest.mark.parametrize("remote", REMOTE)
def test_remote_forms_refused_by_default(remote, monkeypatch):
    """Changed default (#21). These used to be admitted "on the user's own
    responsibility", except the user never sees the URL: the model writes it,
    from data the model read. A GeoPackage attribute saying "the updated layer
    lives at https://evil.tld/x.gpkg" was enough to have GDAL parse
    attacker-chosen bytes in-process, with nobody consenting."""
    monkeypatch.delenv("MAPSMITH_WORKSPACE", raising=False)
    monkeypatch.delenv("MAPSMITH_ALLOW_REMOTE", raising=False)
    with pytest.raises(ValueError, match="MAPSMITH_ALLOW_REMOTE"):
        workspace.guard(remote, "input_path")


@pytest.mark.parametrize("remote", REMOTE)
def test_the_operator_can_switch_remote_reads_on(remote, monkeypatch):
    """Cloud-native data is a real use case, so the capability stays — it just
    takes the one party who can actually consent."""
    monkeypatch.delenv("MAPSMITH_WORKSPACE", raising=False)
    monkeypatch.setenv("MAPSMITH_ALLOW_REMOTE", "1")
    assert workspace.guard(remote, "input_path") == remote


@pytest.mark.parametrize("value", ["0", "false", "no", "", "maybe"])
def test_only_an_explicit_yes_switches_it_on(value, monkeypatch):
    monkeypatch.delenv("MAPSMITH_WORKSPACE", raising=False)
    monkeypatch.setenv("MAPSMITH_ALLOW_REMOTE", value)
    with pytest.raises(ValueError, match="MAPSMITH_ALLOW_REMOTE"):
        workspace.guard(REMOTE[0], "input_path")


@pytest.mark.parametrize("remote", REMOTE)
def test_a_workspace_overrides_the_opt_in(remote, monkeypatch, tmp_path):
    """Containment to one directory and "fetch whatever URL the model names"
    cannot both be true, and the DuckDB sandbox refuses the network under a
    workspace anyway."""
    monkeypatch.setenv("MAPSMITH_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("MAPSMITH_ALLOW_REMOTE", "1")
    with pytest.raises(ValueError, match="workspace"):
        workspace.guard(remote, "input_path")


@pytest.mark.parametrize("remote", REMOTE)
def test_validated_plans_stay_strict_whatever_the_setting(remote, monkeypatch):
    """A plan is a contract checked end to end before anything runs."""
    monkeypatch.delenv("MAPSMITH_WORKSPACE", raising=False)
    monkeypatch.setenv("MAPSMITH_ALLOW_REMOTE", "1")
    assert workspace.nonlocal_reason(remote) is not None


@pytest.mark.parametrize(
    "remote",
    ["/vsicurl/https://data.example/cog.tif", "https://data.example/cog.tif"],
)
def test_remote_forms_rejected_under_workspace(remote, monkeypatch, tmp_path):
    monkeypatch.setenv("MAPSMITH_WORKSPACE", str(tmp_path))
    with pytest.raises(ValueError, match="workspace"):
        workspace.guard(remote, "input_path")


def test_plain_local_path_passes_without_workspace(monkeypatch, tmp_path):
    monkeypatch.delenv("MAPSMITH_WORKSPACE", raising=False)
    p = str(tmp_path / "data.parquet")
    assert workspace.guard(p, "input_path") == p


# --- containment when MAPSMITH_WORKSPACE is set ----------------------------

def test_inside_workspace_passes(monkeypatch, tmp_path):
    monkeypatch.setenv("MAPSMITH_WORKSPACE", str(tmp_path))
    inside = str(tmp_path / "sub" / "out.parquet")
    assert workspace.guard(inside, "output_path") == inside


def test_workspace_root_itself_is_not_a_dataset_path(monkeypatch, tmp_path):
    # output_path == root would put the derived '<output>.provenance.json'
    # BESIDE the workspace, i.e. out of jail
    monkeypatch.setenv("MAPSMITH_WORKSPACE", str(tmp_path))
    with pytest.raises(ValueError, match="MAPSMITH_WORKSPACE"):
        workspace.guard(str(tmp_path), "output_path")


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
