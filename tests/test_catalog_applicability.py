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


def test_search_names_its_engine_and_refuses_an_unknown_one():
    """Every result says which engine ranked it: a score of 10.03 and one of
    0.38 are not comparable, and a caller reading both needs to know which
    scale it is on."""
    hits = catalog.search("area of a parcel", limit=3)
    assert hits and all(entry["engine"] == "lexical" for entry in hits)
    with pytest.raises(ValueError, match="engine must be one of"):
        catalog.search("area", engine="nonsense")


def test_auto_engine_always_answers_whatever_is_installed():
    """auto is the deployment switch: it must never fail for want of an extra.
    With the extra it ranks by vectors, without it by BM25 — either way the
    caller gets results and is told which."""
    hits = catalog.search("how big is this field really", limit=3, engine="auto")
    assert hits
    engines = {entry["engine"] for entry in hits}
    assert engines in ({"vector"}, {"lexical"}), engines


def test_the_vector_engine_also_narrows_before_it_ranks():
    """The applicability filter is not a property of BM25: it runs first for
    both engines, or the guarantee is only true of the default one."""
    pytest.importorskip("model2vec")
    hits = catalog.search(
        "slope in degrees", input_kind="raster", projected=False, engine="vector"
    )
    assert hits and all(entry["name"] not in ("slope", "aspect") for entry in hits)
    assert all(entry["engine"] == "vector" for entry in hits)
    projected = catalog.search(
        "steepness of the terrain", input_kind="raster", projected=True, engine="vector"
    )
    assert {"slope", "aspect"} & {entry["name"] for entry in projected}


def test_every_operation_declares_a_workload_the_dispatcher_knows():
    """D-041: declaring is not routing, but a declaration the router cannot read
    is decoration. Every entry carries a workload, and every workload is a value
    of the enum the dispatcher switches on — so the day routing is extended, the
    catalog is already the input it needs."""
    from mapsmith.engines.dispatch import Workload

    valid = {w.value for w in Workload}
    for op in catalog.OPERATIONS:
        assert "workload" in op, f"{op['name']} declares no workload"
        assert op["workload"] in valid, (
            f"{op['name']} declares workload {op['workload']!r}, "
            f"which the dispatcher does not know: {sorted(valid)}"
        )


def test_a_raster_operation_is_never_filed_as_a_vector_workload():
    """Closed-form: the raster category and the raster workload must agree.
    A GeoTIFF operation filed as heavy_join is how a router eventually sends a
    grid to a spatial-join engine."""
    for op in catalog.OPERATIONS:
        if op["category"] == "raster" or op["applicability"]["inputs"] == ["raster"]:
            assert op["workload"] == "raster", (
                f"{op['name']} is a raster operation declared as {op['workload']!r}"
            )
