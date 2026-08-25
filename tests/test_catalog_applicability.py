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
    catalog must say so, or the future filter would offer them anyway."""
    demanding = {
        op["name"] for op in catalog.OPERATIONS
        if op["applicability"]["requires_projected_crs"]
    }
    assert demanding == {"slope", "aspect"}
