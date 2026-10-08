"""A record written on a failing path describes only bytes its own run wrote.

Found by the manifest-conformity review on 2026-10-08: with a dataset and its
manifest already at the output path from an earlier run, a run refused by its
preconditions -- or one that crashed before writing -- computed the digest of
the EARLIER bytes into its own record and overwrote theirs. A lineage walk by
digest (section 6 of the specification) then attributed those bytes to the
inputs of the run that failed.
"""

from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import box

from mapsmith import verify
from mapsmith.engines import vector


def _layer(path, crs="EPSG:32632"):
    gpd.GeoDataFrame({"n": [1]}, geometry=[box(500_000, 5_000_000, 500_100, 5_000_100)], crs=crs).to_file(path)
    return str(path)


def _manifest(out):
    return json.loads(Path(str(out) + ".provenance.json").read_text(encoding="utf-8"))


@pytest.fixture
def earlier_run(tmp_path):
    """A good clip at out.parquet: dataset and manifest, digest recorded."""
    layer = _layer(tmp_path / "layer.gpkg")
    mask = _layer(tmp_path / "mask.gpkg")
    out = tmp_path / "out.parquet"
    vector.clip(layer, mask, str(out))
    assert _manifest(out)["output"]["sha256"]
    return layer, mask, out


def _failed(out):
    return json.loads(Path(str(out) + ".failed.provenance.json").read_text(encoding="utf-8"))


def test_a_refused_run_does_not_claim_the_earlier_bytes_nor_replace_their_record(earlier_run, tmp_path):
    layer, _, out = earlier_run
    before = _manifest(out)
    no_crs = tmp_path / "no_crs.gpkg"
    gpd.GeoDataFrame({"n": [1]}, geometry=[box(0, 0, 1, 1)]).to_file(no_crs)
    with pytest.raises(verify.VerificationError):
        vector.clip(layer, str(no_crs), str(out))
    # The earlier record still describes the bytes at the path...
    assert _manifest(out) == before
    # ...and the refusal's own trail sits beside it, claiming nothing.
    failed = _failed(out)
    assert "output" not in failed
    assert any("belong to an earlier run" in note and "left in place" in note for note in failed["notes"])


def test_a_run_that_crashes_before_writing_does_not_claim_the_earlier_bytes(earlier_run, monkeypatch):
    layer, mask, out = earlier_run

    def boom(*args, **kwargs):
        raise RuntimeError("engine died before writing")

    monkeypatch.setattr(vector.gpd, "clip", boom)
    before = _manifest(out)
    with pytest.raises(RuntimeError):
        vector.clip(layer, mask, str(out))
    assert _manifest(out) == before
    failed = _failed(out)
    assert "output" not in failed
    assert sum("belong to an earlier run" in note for note in failed["notes"]) == 1


def test_a_run_that_crashes_after_writing_still_records_its_own_bytes(earlier_run, monkeypatch):
    """The other side: bytes this run DID write keep their digest, partial or not."""
    layer, mask, out = earlier_run
    real_write = vector._write

    def write_then_die(frame, path):
        real_write(frame.iloc[:0], path)
        raise RuntimeError("engine died after writing")

    monkeypatch.setattr(vector, "_write", write_then_die)
    with pytest.raises(RuntimeError):
        vector.clip(layer, mask, str(out))
    manifest = _manifest(out)
    assert manifest["output"]["sha256"]
    assert not any("belong to an earlier run" in note for note in manifest.get("notes", []))


def test_every_write_under_a_refusal_says_it_is_one():
    """Derived from source: a record written under `has_critical_failure` passes `refused=True`.

    Eighteen sites on 2026-10-08. A new operation copying the old line would
    bring the defect back without a test of its own noticing.
    """
    import ast

    import mapsmith

    root = Path(mapsmith.__file__).parent
    seen, missing = 0, []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.If) and "has_critical_failure" in ast.unparse(node.test)):
                continue
            for call in ast.walk(ast.Module(body=node.body, type_ignores=[])):
                if isinstance(call, ast.Call) and getattr(call.func, "attr", None) == "write_for":
                    seen += 1
                    if not any(k.arg == "refused" for k in call.keywords):
                        missing.append(f"{path.name}:{call.lineno}")
    assert seen >= 15, f"only {seen} refusal writes found: the sweep has stopped seeing them"
    assert not missing, missing



@pytest.mark.parametrize("earlier", [True, False])
def test_a_crash_in_a_check_of_the_output_keeps_the_new_bytes_digest(tmp_path, monkeypatch, earlier):
    """`audited` runs after the write: a fingerprint taken there was of this run's own bytes.

    A check of the output that crashed then dropped the digest of the bytes
    just written and said they were an earlier run's -- with or without a file
    there before (review, 2026-10-08).
    """
    layer = _layer(tmp_path / "layer.gpkg")
    mask = _layer(tmp_path / "mask.gpkg")
    out = tmp_path / "out.parquet"
    if earlier:
        vector.clip(layer, mask, str(out))

    def check_dies(*args, **kwargs):
        raise RuntimeError("a check of the output crashed")

    monkeypatch.setattr(verify, "verify_vector_output", check_dies)
    with pytest.raises(RuntimeError):
        vector.clip(layer, mask, str(out))
    manifest = _manifest(out)
    assert manifest["output"]["sha256"]
    assert not any("earlier run" in note for note in manifest.get("notes", []))
    assert not Path(str(out) + ".failed.provenance.json").exists()
