"""The trap this repository measures in other people's software, on our own side.

Argleton trap 021: PROJ does not fail when it has no datum transformation for a
pair. It falls back to a *ballpark* operation that carries the coordinates
across as if the two datums coincided. The geometry is plausible, the output CRS
is genuinely the one that was asked for, `crs_matches` passes, and the numbers
are tens of metres out.

`reproject_layer` has answered that since 0.4.0. On 2026-09-02 a sweep found
that it was the ONLY answer: `transformation` appeared in one manifest out of
fifty-eight, and `reproject_raster` -- the head example of section 3.7 of the
manifest specification -- recorded two CRS labels and nothing about the
operation between them. No test in this suite mentioned `transformation` or
`accuracy_m` at all, so the fix we point at was guarded by nothing.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from mapsmith import datum

ROOT = Path(__file__).resolve().parent.parent
ENGINES = ROOT / "src" / "mapsmith" / "engines"

# Monte Mario with the Rome prime meridian. PROJ's default for this pair really
# is a ballpark, a published 44 m operation exists, and the two differ by 83 m at
# the centre of the area of use -- measured 2026-09-03. This is the trap, whole.
BALLPARK_PAIR = ("EPSG:4806", "EPSG:4326")
# Same datum, different projection: a real operation with a stated accuracy.
SHIFTLESS_PAIR = ("EPSG:4326", "EPSG:32633")
# A PROJECTED source, which is what broke the probe: its area of use is stated in
# degrees while the transformer wants metres.
PROJECTED_PAIR = ("EPSG:3003", "EPSG:4326")


def test_a_pair_with_no_installed_grid_is_reported_as_a_ballpark():
    record = datum.default_operation(*BALLPARK_PAIR)
    assert record["is_ballpark"] is True
    assert record["accuracy_m"] is None
    # The distinction a reader needs in order to act: "there is no operation for
    # this pair" and "there is one and this machine has not got it" are
    # different problems with different fixes.
    assert record["better_available_m"] > 0, (
        "a published 44 m operation exists for this pair; saying only "
        "'ballpark' hides that the fix is to install its grid"
    )


def test_a_pair_within_one_datum_is_not_reported_as_a_ballpark():
    """The false positive is worse than the silence it replaces: a manifest
    saying `is_ballpark: true` accuses the engine of something it did not do."""
    record = datum.default_operation(*SHIFTLESS_PAIR)
    assert record["is_ballpark"] is False
    assert record["accuracy_m"] == 0.0
    assert record["pipeline"]


def test_a_projected_source_is_probed_in_its_own_units():
    """The defect this module was written to fix, found while fixing another one.

    `area_of_use` is always in degrees. EPSG:3003 is Gauss-Boaga in metres, and
    its area is 5.93..12.0 by 36.53..47.04, so probing at the midpoint handed
    (8.965, 41.785) to a transformer expecting metres -- nine metres from the
    false origin, nowhere near Italy. No location-restricted operation matches
    there, so PROJ returned a ballpark.

    It is not one. The default for this pair is "Monte Mario to WGS 84 (4)",
    stated accuracy 4 m. Since 0.4.0 `reproject_layer` has therefore written
    "the transformation this library selects by default for this pair is a
    ballpark one, which applies no datum shift at all" onto manifests for
    **every projected source**, which in this domain is most of them. That is a
    false statement in a shipped manifest, and it was in the one operation the
    project points at as its answer to trap 021.
    """
    record = datum.default_operation(*PROJECTED_PAIR)
    assert record["is_ballpark"] is False, (
        "EPSG:3003 -> EPSG:4326 has a 4 m default operation; reporting a "
        "ballpark means the probe is being handed degrees as metres again"
    )
    assert record["accuracy_m"] == 4.0


def test_the_answer_does_not_depend_on_how_the_caller_holds_the_crs():
    """A string, a pyproj CRS, a rasterio CRS and a bare WKT are the same CRS.

    They were not the same answer: routed through WKT the CRS loses
    `area_of_use` -- it comes from the EPSG registry, not the WKT -- and the
    probe fell back to (0, 0). Measured on 2026-09-03, NAD27 -> WGS84 came back
    7.0 m from a string and a ballpark from a rasterio object.
    """
    rasterio = pytest.importorskip("rasterio")
    from pyproj import CRS

    plain = "EPSG:4267"
    ways = [
        plain,
        CRS.from_user_input(plain),
        rasterio.crs.CRS.from_epsg(4267),
        CRS.from_user_input(rasterio.crs.CRS.from_epsg(4267).to_wkt()),
    ]
    answers = {
        (r["is_ballpark"], r["accuracy_m"])
        for r in (datum.default_operation(w, "EPSG:4326") for w in ways)
    }
    assert len(answers) == 1, (
        f"the same pair gives {len(answers)} different answers depending on how "
        f"the CRS was held: {answers}"
    )


def test_a_rasterio_crs_does_not_get_reported_as_a_ballpark():
    """The bug this nearly shipped with.

    `accuracy_of` probes a point inside the CRS's `area_of_use`, and a
    `rasterio.crs.CRS` has no such attribute. Left alone it falls back to
    (0, 0) -- the Gulf of Guinea -- which is outside almost every real
    transformation's extent, so an ordinary pair comes back `is_ballpark: true`.
    Caught by running it, not by reading it.
    """
    rasterio = pytest.importorskip("rasterio")

    native = rasterio.crs.CRS.from_epsg(4326)
    assert not hasattr(native, "area_of_use"), (
        "rasterio grew area_of_use: this test now proves nothing and the "
        "conversion in datum._as_crs needs re-justifying, not deleting"
    )
    assert datum.default_operation(native, "EPSG:32633")["is_ballpark"] is False


def test_choosing_and_reporting_are_different_answers():
    """`best_operation` may substitute a better transformation; the record then
    describes what ran. `default_operation` never substitutes, because the
    caller that uses it (rasterio's warp) will not use our choice -- and a
    manifest describing an operation that never ran is worse than one that says
    nothing."""
    _, chosen = datum.best_operation(*BALLPARK_PAIR)
    reported = datum.default_operation(*BALLPARK_PAIR)

    assert reported["is_ballpark"] is True, "default_operation must not substitute"
    if not chosen["is_ballpark"]:
        # It found a better route and says so out loud rather than quietly
        # producing a different number from the one PROJ would have produced.
        assert chosen.get("default_was_ballpark") is True
    assert "chosen_by" in reported and "MapSmith" in reported["chosen_by"]


def test_reproject_raster_records_the_operation_and_not_only_the_two_crs_labels(tmp_path):
    """The head example of section 3.7, which recorded neither until 2026-09-03."""
    rasterio = pytest.importorskip("rasterio")
    pytest.importorskip("numpy")
    import numpy as np
    from rasterio.transform import from_origin

    from mapsmith.engines import raster

    source = tmp_path / "in.tif"
    profile = {
        "driver": "GTiff", "height": 4, "width": 4, "count": 1,
        # Monte Mario / Rome meridian: longitudes here are relative to 12.45E,
        # so -3.0 is roughly the middle of Italy. The CRS is the point of the
        # fixture; the pixels are not.
        "dtype": "float32", "crs": "EPSG:4806", "nodata": -9999.0,
        "transform": from_origin(-3.0, 43.0, 0.01, 0.01),
    }
    with rasterio.open(source, "w", **profile) as dst:
        dst.write(np.arange(16, dtype="float32").reshape(4, 4), 1)

    out = tmp_path / "out.tif"
    raster.reproject_raster(str(source), str(out), target_crs="EPSG:4326",
                            resampling="nearest")

    manifest = json.loads(Path(f"{out}.provenance.json").read_text(encoding="utf-8"))
    shift = manifest["crs_decisions"].get("transformation")
    assert shift is not None, (
        "reproject_raster recorded the two CRS labels and nothing about the "
        "operation between them - which is trap 021 with our name on it"
    )
    assert shift["is_ballpark"] is True

    named = {c["name"]: c for c in manifest["verification"]}
    assert named["crs_matches"]["passed"] is True, (
        "the point of the trap: the output really is in the CRS that was asked for"
    )
    shifted = named["x-mapsmith:datum_shift_applied"]
    assert shifted["passed"] is False
    assert shifted.get("critical") is not True, (
        "a ballpark is legitimate when the caller knows the datums coincide; "
        "refusing would break them. Saying nothing is what this fixes."
    )
    assert any("ballpark" in n.lower() for n in manifest.get("notes", [])), (
        "the note is where a human reads what happened"
    )


def _transforming_functions() -> dict[str, set[str]]:
    """Public engine functions that hand two CRSs to PROJ, derived from source.

    Derived rather than listed, for the reason 2026-09-02 made expensive four
    times over: a hand-written list is worth exactly what somebody remembered to
    put in it, and stops guarding the moment a new operation is added.
    """
    found: dict[str, set[str]] = {}
    for path in sorted(ENGINES.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
                continue
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "to_crs"
                ):
                    found.setdefault(path.name, set()).add(node.name)
    return found


# Measured on 2026-09-03. These reproject a layer internally to align it with
# another CRS and do not yet record the operation, so a ballpark inside them is
# still silent. The list may only SHRINK: it is a ratchet, not a permission.
# `datum.default_operation` and `datum.best_operation` are the two answers; the
# work is wiring each call site to the one that matches who chooses.
STILL_SILENT = {
    "linework.py": {"line_intersections", "snap_layer", "transform_by_control_points"},
    "network.py": {"least_cost_path"},
    "raster.py": {"clip_raster", "zonal_statistics"},
    "sampling.py": {"elevation_profile", "sample_raster_at_points"},
    "summaries.py": {"compare_layers", "nearest_neighbour_index"},
    "vector.py": {
        "buffer", "centroid", "clip", "count_in_polygons", "merge",
        "nearest_join", "overlay", "simplify", "spatial_join",
    },
    "whitebox_engine.py": {"viewshed", "watershed"},
}


def test_no_new_operation_transforms_coordinates_in_silence():
    """A ratchet over the operations that still reproject without recording.

    The assertion that matters is the first one: a derivation that finds nothing
    passes every subset test ever written, which is the failure this suite spent
    2026-09-02 finding in four disguises.
    """
    found = _transforming_functions()
    assert found, (
        "the derivation matched no call sites at all - `.to_crs` was renamed or "
        "the engines moved, and this guard is now vacuous"
    )
    new = {
        module: sorted(names - STILL_SILENT.get(module, set()))
        for module, names in found.items()
        if names - STILL_SILENT.get(module, set())
    }
    assert not new, (
        f"these operations reproject without recording the transformation: {new}. "
        "Wire them to datum.best_operation (the caller chooses) or "
        "datum.default_operation (the engine chooses), and record it under "
        "crs_decisions.transformation - do not add them to STILL_SILENT."
    )
    gone = {
        module: sorted(names - found.get(module, set()))
        for module, names in STILL_SILENT.items()
        if names - found.get(module, set())
    }
    assert not gone, (
        f"these are listed as silent but no longer reproject: {gone}. "
        "Remove them from STILL_SILENT - a ratchet that is not tightened is a list."
    )
