"""The configuration that changed the answer, and the record that says so.

Section 3.8 of the manifest specification had no implementation until
2026-08-31: the field did not exist in `ProvenanceRecord`, so a whole section of
a published specification described something MapSmith could not produce.

Argleton trap 030 is what made that concrete. A GeoTIFF and the `.aux.xml`
beside it declare different georeferencing; GDAL prefers the sidecar by
documented design, because that is how somebody overrides georeferencing they
know to be wrong. Both readings are the library behaving as written. What was
missing is the sentence naming which one produced the numbers — and MapSmith
answered 160 000 m² where the file's own tags say 40 000, silently.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import Affine

from mapsmith import grid
from mapsmith.provenance import REDACTED, InputRecord, ProvenanceRecord

CRS = "EPSG:32632"
SIZE = 20
INTERNAL = (500000.0, 5030000.0, 10.0)
SIDECAR = (600000.0, 5040000.0, 20.0)

AUX_XML = """<PAMDataset>
  <SRS dataAxisToSRSAxisMapping="1,2">EPSG:32632</SRS>
  <GeoTransform> {ox},  {cell},  0.0,  {oy},  0.0, -{cell}</GeoTransform>
</PAMDataset>
"""


def _raster(path: Path, origin_x: float, origin_y: float, cell: float) -> None:
    values = np.tile(np.arange(SIZE, dtype="float32"), (SIZE, 1))
    with rasterio.open(
        path, "w", driver="GTiff", height=SIZE, width=SIZE, count=1,
        dtype="float32", crs=CRS,
        transform=Affine(cell, 0.0, origin_x, 0.0, -cell, origin_y),
    ) as destination:
        destination.write(values, 1)


@pytest.fixture
def two_georeferencings(tmp_path):
    """A raster whose sidecar disagrees with it — the shape of trap 030."""
    path = tmp_path / "terrain.tif"
    _raster(path, *INTERNAL)
    ox, oy, cell = SIDECAR
    Path(f"{path}.aux.xml").write_text(
        AUX_XML.format(ox=ox, oy=oy, cell=cell), encoding="utf-8", newline="\n"
    )
    return str(path)


@pytest.fixture
def one_georeferencing(tmp_path):
    path = tmp_path / "plain.tif"
    _raster(path, *INTERNAL)
    return str(path)


def test_the_sidecar_wins_and_the_source_says_so(two_georeferencings):
    """GDAL's default, and the sentence that was missing.

    Not a bug to file: a sidecar is how an override works, so it has to beat
    the file it overrides. What was missing is anywhere saying which was used.
    """
    found = grid.georeferencing_source(two_georeferencings)
    assert found["georeferencing_source"] == "sidecar (.aux.xml)"
    assert found["georeferencing_sidecar_present"] == "terrain.tif.aux.xml"
    # The other branch's numbers, because "there was a choice" is weaker than
    # "here is what the other choice says".
    assert "cell 10 x 10 at (500000, 5030000)" == found[
        "georeferencing_internal_would_give"
    ]


def test_a_coordinate_is_never_reported_in_scientific_notation(two_georeferencings):
    """`5.03e+06` is a northing nobody can compare with the one in front of
    them, and this string exists to be compared."""
    found = grid.georeferencing_source(two_georeferencings)
    assert "e+" not in found["georeferencing_internal_would_give"]


def test_one_georeferencing_says_nothing(one_georeferencing):
    """An empty `environment` claims nothing, exactly like an absent
    `crs_decisions` — and a field that fires on every raster is a field its
    reader learns to skip."""
    assert grid.georeferencing_source(one_georeferencing) == {}


def test_computing_refuses_where_describing_reports(two_georeferencings):
    """The twin of the multi-layer refusal (#29), on a different axis.

    There, GDAL's default is the first layer of a container; here it is the
    sidecar. Both answer a question the caller never asked. Describing is
    different: a file with two georeferencings is a thing to be told about, and
    `describe_dataset` is the operation whose whole job is to tell you.
    """
    from mapsmith.engines import dispatch

    described = dispatch.describe_routed(two_georeferencings)
    assert described["georeferencing"]["georeferencing_source"].startswith("sidecar")

    with pytest.raises(ValueError, match="georeferenced twice"):
        grid.refuse_ambiguous_georeferencing(two_georeferencings, "resample_raster")

    # And it says how to choose, rather than only that a choice exists.
    try:
        grid.refuse_ambiguous_georeferencing(two_georeferencings, "resample_raster")
    except ValueError as refusal:
        assert "GDAL_GEOREF_SOURCES=INTERNAL" in str(refusal)


def test_a_writer_that_bypasses_audited_still_records_it(two_georeferencings, tmp_path):
    """`resample_raster` builds its record by hand, and that is the point.

    The first version filled `environment` inside `verify.audited`, which
    `mapsmith/CLAUDE.md` describes as the place the invariants become
    unmissable. Seventeen writers of fifty-seven do not go through it, and they
    are concentrated in raster — where georeferencing decides the numbers. The
    field is filled in `write_for` instead, which is the single point where a
    manifest becomes a file.
    """
    from mapsmith.engines import raster

    source = tmp_path / "in.tif"
    shutil.copy(two_georeferencings, source)
    shutil.copy(f"{two_georeferencings}.aux.xml", f"{source}.aux.xml")

    result = raster.resample(str(source), str(tmp_path / "out.tif"), 40, "nearest")
    record = json.loads(Path(result["provenance"]).read_text(encoding="utf-8"))
    assert record["environment"]["georeferencing_source"] == "sidecar (.aux.xml)"


def test_an_ordinary_raster_leaves_the_field_empty(one_georeferencing, tmp_path):
    from mapsmith.engines import raster

    result = raster.resample(one_georeferencing, str(tmp_path / "out.tif"), 40, "nearest")
    record = json.loads(Path(result["provenance"]).read_text(encoding="utf-8"))
    assert record["environment"] == {}


def test_a_credential_in_a_dictionary_key_is_masked():
    """The gap adding this field exposed, and it was never about this field.

    `{"AWS_SECRET_ACCESS_KEY": "AKIA..."}` passed through untouched: the scanner
    looks for `name=value` INSIDE a string, and in a dictionary the name is the
    key. That was true of every dict a manifest carries; `environment` is
    merely the one that makes it obvious, holding environment variables by
    definition.
    """
    record = ProvenanceRecord(
        operation="x", parameters={"api_key": "abc123"}, inputs=[],
        environment={"AWS_SECRET_ACCESS_KEY": "AKIAnotreally", "GDAL_PAM_ENABLED": "NO"},
    )
    assert record.environment["AWS_SECRET_ACCESS_KEY"] == REDACTED
    assert record.parameters["api_key"] == REDACTED
    # And the record says a redaction happened, because a manifest that quietly
    # differs from what ran would be worse than the leak.
    assert record.parameters_redacted is True


@pytest.mark.parametrize(
    "key", ["sort_key", "primary_key", "key", "GDAL_PAM_ENABLED", "georeferencing_source"]
)
def test_an_ordinary_key_is_left_alone(key):
    """A redaction that fires on `sort_key` teaches its reader to distrust the
    mask, which leaves less protection than a narrower rule nobody doubts."""
    record = ProvenanceRecord(
        operation="x", parameters={key: "ordinary"}, inputs=[]
    )
    assert record.parameters[key] == "ordinary"
    assert record.parameters_redacted is False


def test_the_field_is_never_a_dump_of_the_environment(two_georeferencings, tmp_path):
    """A record listing forty variables would bury the one that mattered.

    The specification says a producer records what it knows influenced the
    result. This pins the discipline rather than the wording: whatever is in
    there has to be about the georeferencing decision, not about the machine.
    """
    from mapsmith.engines import raster

    source = tmp_path / "in.tif"
    shutil.copy(two_georeferencings, source)
    shutil.copy(f"{two_georeferencings}.aux.xml", f"{source}.aux.xml")
    result = raster.resample(str(source), str(tmp_path / "out.tif"), 40, "nearest")
    record = json.loads(Path(result["provenance"]).read_text(encoding="utf-8"))

    assert len(record["environment"]) <= 5, (
        "the environment field is growing into a dump; a reader who has to scan "
        "it stops reading it"
    )
    assert all(
        "georef" in key.lower() or key in grid.GEOREF_VARIABLES
        for key in record["environment"]
    )


def test_an_operation_can_say_more_than_this_does(two_georeferencings, tmp_path):
    """Anything the engine set itself is kept.

    An operation that knows something about its own environment knows it better
    than a generic sweep over the inputs does, and overwriting it would be this
    helper claiming an authority it has not got.
    """
    record = ProvenanceRecord(
        operation="x",
        parameters={},
        inputs=[InputRecord.from_path(two_georeferencings)],
        environment={"georeferencing_source": "internal, forced by the operation"},
    )
    output = tmp_path / "out.txt"
    output.write_text("x", encoding="utf-8")
    record.write_for(str(output))

    assert record.environment["georeferencing_source"] == (
        "internal, forced by the operation"
    )
    # And the rest is still added: keeping one key is not skipping the sweep.
    assert "georeferencing_sidecar_present" in record.environment


def test_the_refusal_stays_quiet_when_there_is_nothing_to_refuse(one_georeferencing):
    """A guard that refuses everything is not a guard, and nothing covered it.

    Found by sabotage: removing the early return from
    `refuse_ambiguous_georeferencing` — so that it raised on every raster —
    left all fourteen other tests green. The refusing branch was tested and the
    quiet branch was not, which is the half that every ordinary call takes.
    """
    assert grid.refuse_ambiguous_georeferencing(one_georeferencing, "resample_raster") == {}


def test_a_sidecar_that_agrees_is_not_a_conflict(tmp_path):
    """Two sources saying the same thing is not two answers.

    A `.aux.xml` often carries statistics or a colour table and repeats the
    georeferencing unchanged. Refusing there would be refusing an ordinary file,
    and this suite's own admission rule says a check that fires on the ordinary
    case gets switched off.
    """
    path = tmp_path / "agreeing.tif"
    _raster(path, *INTERNAL)
    ox, oy, cell = INTERNAL
    Path(f"{path}.aux.xml").write_text(
        AUX_XML.format(ox=ox, oy=oy, cell=cell), encoding="utf-8", newline="\n"
    )
    found = grid.georeferencing_source(str(path))
    assert found["georeferencing_source"] == "internal"
    assert grid.refuse_ambiguous_georeferencing(str(path), "resample_raster") == found
