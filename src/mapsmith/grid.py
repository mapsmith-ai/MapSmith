"""Where a raster's values actually are — the one place that decides.

A grid of numbers is not a map until something says where each number sits, and
GeoTIFF says it in two different ways that differ by half a pixel:

* **`RasterPixelIsArea`** — a value describes the cell it fills. The tie point in
  the header is that cell's upper-left corner, and the value's position is the
  cell's centre. The default, and what most data ships as.
* **`RasterPixelIsPoint`** — a value is a sample *at a grid node*. The tie point
  IS the position of the first value, and there is no half cell to add.

The choice is recorded in the file, GDAL reads it faithfully and reports it as
the `AREA_OR_POINT` metadata item, and its documentation is explicit that the
geotransform is **not** adjusted for it. So `dataset.tags()` tells you the
convention and `dataset.xy()` ignores it, from the same open dataset — which is
how a fifteen-metre systematic shift on a 30 m DEM gets into an analysis without
a single warning. The USGS elevation products are point-registered, so this is
not a corner case: it is most of the free elevation data in North America.

This module exists because MapSmith had that defect everywhere at once. Every
place that turned a cell index into a coordinate did what rasterio does, and
none of them had asked the question — which is the same shape as #28, where
"open a vector file" existed as six copies of one decision and four of them were
missing a branch. So the decision lives here, once, and a test fails if a second
copy appears.

## The one idea

For cell `(row, col)`, the value's position in *array space* is

    (col + OFFSET, row + OFFSET)

where `OFFSET` is 0.5 for area registration and 0.0 for point registration.
Everything else in this module is that sentence applied: forward to get a
coordinate, backward to get an index, and fractionally to interpolate between
samples.

## What it does not fix

Nothing here changes what an operation MEANS. Reprojecting or resampling a
point-registered grid still has to decide what the output represents, and
`preserve` carries the declaration onto the output rather than answering that
question — an output that quietly became area-registered is the same silent
error one step downstream.
"""

from __future__ import annotations

from typing import Any

#: How far the value sits from the cell's upper-left corner, in cells, for each
#: registration. The whole module is this table plus arithmetic.
OFFSET = {"area": 0.5, "point": 0.0}

#: The metadata item GDAL reports the GeoTIFF raster type as. Written once so
#: that a search for it finds every use.
TAG = "AREA_OR_POINT"


def registration(dataset: Any) -> str:
    """`"area"` or `"point"` for an open rasterio dataset.

    Anything other than a declared `Point` is area: the default is area, most
    formats cannot express anything else, and treating an unreadable tag as
    point would move every position on files that are fine.
    """
    try:
        declared = (dataset.tags() or {}).get(TAG)
    except Exception:  # noqa: BLE001 - a driver that cannot report tags is area
        return "area"
    return "point" if str(declared).strip().lower() == "point" else "area"


def offset(dataset: Any) -> float:
    """The half cell, or not, for this dataset."""
    return OFFSET[registration(dataset)]


def describe(dataset: Any) -> dict[str, Any]:
    """The registration as a manifest entry: what it is and what it changed.

    Recorded on every operation that converts between cells and coordinates,
    including the ordinary case. A manifest that mentions the convention only
    when it is unusual leaves a reader unable to tell "area" from "nobody
    looked", and those are different claims.
    """
    kind = registration(dataset)
    return {
        "raster_registration": kind,
        # NOT "reason". This dict is merged into `crs_decisions`, which already
        # has one, and the first version of this function silently replaced the
        # sentence explaining a reprojection with a sentence about cell
        # registration. An existing test caught it; two different reasons under
        # one key is a defect wherever it happens.
        "raster_registration_reason": (
            "the file declares AREA_OR_POINT=Point, so each value is a sample at a "
            "grid node and the tie point is the position of the first value — no "
            "half cell is added"
            if kind == "point"
            else "the file does not declare point registration, so each value "
            "describes the cell it fills and its position is the cell's centre"
        ),
    }


def sample_xy(dataset: Any, row: int, column: int) -> tuple[float, float]:
    """Where the value of this cell IS, as a coordinate.

    The replacement for `dataset.xy(row, col)`, which always answers as if the
    file were area-registered.
    """
    shift = offset(dataset)
    return dataset.transform * (column + shift, row + shift)


def sample_index(dataset: Any, x: float, y: float) -> tuple[int, int]:
    """Which cell's value is the one at this position.

    Area: the cell the position falls inside. Point: the node it is nearest to.
    They are the same question asked of two different grids, and rounding rather
    than flooring is the whole difference.

    Returns **(row, column)**, in that order, because that is the order every
    array index is written in. Unpacking it the other way round is the axis-order
    defect this project has a whole Argleton family for, and it happened here
    once already — the existing sampling tests caught it in the same minute.

    Ties go up in each axis rather than to even: `floor(v + 0.5)` instead of
    `round`, because Python rounds 2.5 to 2 and 3.5 to 4, and a lookup whose
    tie-breaking alternates is worse than one whose rule can be stated.
    """
    import math

    column, row = ~dataset.transform * (x, y)
    if registration(dataset) == "point":
        return math.floor(row + 0.5), math.floor(column + 0.5)
    return math.floor(row), math.floor(column)


def sample_space(dataset: Any, x: float, y: float) -> tuple[float, float]:
    """The position in *sample space*: (column, row) where integers are samples.

    What bilinear interpolation needs. Under area registration a coordinate at
    array position 3.5 is exactly on the sample of cell 3, so sample space is
    array space minus a half; under point registration array position 3.0 is the
    sample, so the two spaces coincide.
    """
    column, row = ~dataset.transform * (x, y)
    shift = offset(dataset)
    return column - shift, row - shift


def bounds_of_samples(dataset: Any) -> tuple[float, float, float, float]:
    """The envelope of the sample POSITIONS, which is not the dataset's extent.

    Under point registration the outermost samples sit on the file's declared
    boundary rather than half a cell inside it, so a caller asking "is this
    position within the data" gets a different answer. Returned as
    (left, bottom, right, top).
    """
    shift = offset(dataset)
    left, top = dataset.transform * (shift, shift)
    right, bottom = dataset.transform * (
        dataset.width - 1 + shift,
        dataset.height - 1 + shift,
    )
    return min(left, right), min(top, bottom), max(left, right), max(top, bottom)


def shift_for_area_tools(dataset: Any) -> tuple[float, float]:
    """How far to move a GEOMETRY so an area-registered tool gets it right.

    Some engines take the cell footprint from the transform and cannot be told
    otherwise — exact coverage fractions, rasterisation. Against a
    point-registered file they compute the footprint half a cell south-east of
    where the samples are.

    Moving the raster is expensive and moving the question is free: coverage of
    a polygon against a grid shifted by d equals coverage of the same polygon
    shifted by -d against the unshifted grid. This returns that -d, in map
    units, and it is (0, 0) for an ordinary file.

    The result of such a call must be attached to the ORIGINAL geometry. The
    shifted copy exists only to ask the question.
    """
    if registration(dataset) == "area":
        return 0.0, 0.0
    return abs(dataset.transform.a) / 2.0, -abs(dataset.transform.e) / 2.0


def preserve(source: Any, destination: Any) -> str | None:
    """Carry the registration from an input raster onto an output one.

    `profile.copy()` does not include tags, so every raster MapSmith wrote used
    to come back area-registered whatever went in. That is the same silent error
    one step downstream: a point-registered DEM in, an area-registered file out,
    and every position derived from it afterwards half a cell wrong with nothing
    in the file to say so.

    Returns a note for the manifest when something was carried, None otherwise.
    """
    if registration(source) != "point":
        return None
    destination.update_tags(**{TAG: "Point"})
    return (
        "the input declares AREA_OR_POINT=Point and the output declares it too. "
        "Dropping it would have made every position derived from the output half "
        "a cell wrong, with nothing in the file to say so."
    )


#: GDAL variables that change which georeferencing a raster is read with. Only
#: these: a manifest that listed forty variables would bury the one that
#: mattered, and its reader would learn to skip the field.
GEOREF_VARIABLES = ("GDAL_PAM_ENABLED", "GDAL_GEOREF_SOURCES")


def georeferencing_source(path: str) -> dict[str, str]:
    """Which georeferencing produced the numbers, when more than one exists.

    A `.aux.xml` beside a raster georeferences it too, and GDAL prefers the
    sidecar over the file's own tags by documented design — a sidecar is how
    somebody corrects georeferencing they know to be wrong, so an override that
    lost to the thing it overrides would not be an override.

    Both readings are the library behaving exactly as written, and that is what
    makes this a field in a record rather than a bug to file. Measured by
    Argleton trap 030: the same file gives 40 000 m² or 160 000 m² and an origin
    a hundred kilometres apart, and until this function existed nothing MapSmith
    wrote could say which.

    Returns the entries for the manifest's `environment` (specification section
    3.8), and **an empty dict when there is nothing to say** — one georeferencing
    means nothing outside the data and the call influenced the answer, and a
    field that fires on every operation is a field nobody reads.

    Costs one extra open, and only on the rare file that has a sidecar at all.
    """
    import os
    from pathlib import Path

    import rasterio

    sidecar = Path(f"{path}.aux.xml")
    setting = {
        name: os.environ[name] for name in GEOREF_VARIABLES if os.environ.get(name)
    }
    if not sidecar.exists():
        # Nothing overrides the file. Whatever the variables say, they changed
        # nothing here, and recording them would be noise dressed as diligence.
        return {}

    with rasterio.open(path) as used:
        effective = used.transform
    with rasterio.Env(GDAL_GEOREF_SOURCES="INTERNAL"), rasterio.open(path) as own:
        internal = own.transform

    from_sidecar = effective != internal
    entry = {
        "georeferencing_source": "sidecar (.aux.xml)" if from_sidecar else "internal",
        "georeferencing_sidecar_present": sidecar.name,
        **setting,
    }
    if from_sidecar:
        # The number the other reading would have produced, because "there was
        # a choice" is weaker than "here is what the other branch says".
        # Plain decimals, never scientific notation: `5.03e+06` is a northing
        # nobody can compare with the one in front of them, and this string
        # exists to be compared.
        def plain(number: float) -> str:
            return f"{number:.4f}".rstrip("0").rstrip(".")

        entry["georeferencing_internal_would_give"] = (
            f"cell {plain(abs(internal.a))} x {plain(abs(internal.e))} at "
            f"({plain(internal.c)}, {plain(internal.f)})"
        )
    return entry


def refuse_ambiguous_georeferencing(path: str, operation: str) -> dict[str, str]:
    """Raise when two georeferencings claim the same raster and nobody chose.

    The twin of `readers.refuse_ambiguous_container` (issue #29), on a different
    axis and for the same reason. There, GDAL's default is the first layer of a
    container; here it is the sidecar over the file's own tags. Both defaults
    answer a question the caller never asked, and in both cases a manifest
    could not honestly say which data produced the numbers.

    Returns the `environment` entry when there is nothing to refuse, so a caller
    writes one line and gets either the refusal or the record.

    **Describe does not call this**, on purpose. Its whole job is to say what a
    file is, and a file with two georeferencings is a thing to be told about,
    not a thing to be refused. Refusing where a number is computed and reporting
    where a file is described is the same principle applied twice, not two
    policies.
    """
    source = georeferencing_source(path)
    if not source or source.get("georeferencing_source") == "internal":
        return source
    raise ValueError(
        f"{path} is georeferenced twice and nobody chose: the GeoTIFF's own tags "
        f"say {source['georeferencing_internal_would_give']}, and "
        f"{source['georeferencing_sidecar_present']} beside it says something else. "
        "GDAL prefers the sidecar, which is correct — that is how an override "
        f"works — but {operation} would then report numbers from a file you did "
        "not name, and this record could not say which. describe_dataset lists "
        "both. To choose, either remove the sidecar or set "
        "GDAL_GEOREF_SOURCES=INTERNAL for a run that must use the file's own."
    )
