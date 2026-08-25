"""Every catalog entry declares what it applies to.

The applicability block exists so that a future deterministic filter can
narrow the catalog to the operations that are *meaningful* for the data in
hand (vector vs raster, projected CRS required) before any ranking runs — and
so that the choice is recordable in a manifest. Declared per entry at
writing time, because retrofitting sixty entries later is how schemas rot.
"""

from __future__ import annotations

import pytest

from mapsmith import catalog

ALLOWED_INPUTS = {"vector", "raster", "dataset", "plan"}


@pytest.mark.parametrize("op", catalog.OPERATIONS, ids=lambda op: op["name"])
def test_every_entry_declares_its_applicability(op):
    block = op.get("applicability")
    assert block, f"{op['name']} has no applicability block"
    assert set(block) == {"inputs", "requires_projected_crs"}
    assert block["inputs"], "inputs must not be empty"
    assert set(block["inputs"]) <= ALLOWED_INPUTS
    assert isinstance(block["requires_projected_crs"], bool)


def test_the_projected_crs_requirement_matches_the_engines_that_refuse():
    """slope and aspect refuse geographic-CRS DEMs at the engine level; the
    catalog must say so, or the filter would offer them anyway."""
    demanding = {
        op["name"] for op in catalog.OPERATIONS
        if op["applicability"]["requires_projected_crs"]
    }
    assert demanding == {"slope", "aspect"}


def test_the_filter_narrows_before_ranking_deterministically():
    """Closed-form: a geographic raster must never be offered slope or aspect,
    and must still be offered hillshade and zonal_statistics."""
    names = {op["name"] for op in catalog.applicable("raster", projected=False)}
    assert "slope" not in names and "aspect" not in names
    assert {"hillshade", "zonal_statistics", "describe_dataset"} <= names
    assert "buffer_layer" not in names  # vector-only does not apply to a raster

    projected_raster = {op["name"] for op in catalog.applicable("raster", projected=True)}
    assert {"slope", "aspect"} <= projected_raster

    vector_names = {op["name"] for op in catalog.applicable("vector")}
    assert {"buffer_layer", "overlay_layers", "dissolve_layer", "run_sql"} <= vector_names
    assert "hillshade" not in vector_names

    with pytest.raises(ValueError, match="input_kind must be"):
        catalog.applicable("tabular")


def test_search_applies_the_filter_before_bm25():
    hits = catalog.search("slope in degrees", input_kind="raster", projected=False)
    assert all(entry["name"] != "slope" for entry in hits)
    hits_projected = catalog.search("slope in degrees", input_kind="raster", projected=True)
    assert hits_projected and hits_projected[0]["name"] == "slope"
