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
    """`"area"` or `"point"` for an open rasterio dataset, or a literal kind.

    Anything other than a declared `Point` is area: the default is area, most
    formats cannot express anything else, and treating an unreadable tag as
    point would move every position on files that are fine.

    A **closed** dataset raises instead of answering. This is not defensive
    tidiness: `reclassify`, `band_math` and `extract_band` all called `preserve`
    after their input's `with` block had ended, and asking a closed dataset for
    its tags does not raise — GDAL prints `Pointer 'hObject' is NULL` to stderr
    and returns nothing, so this function answered "area" and three writers
    shipped point-registered inputs as area-registered outputs. Half a cell,
    fifteen metres on a thirty-metre DEM, with nothing in the file or the
    manifest to say it happened. A question asked of a closed file has no
    answer, and pretending otherwise is what made it silent.
    """
    if isinstance(dataset, str):
        kind = dataset.strip().lower()
        if kind not in OFFSET:
            # The same policy as the closed-dataset guard below, and for the
            # same reason. A path is the natural mistake — every other entry
            # point in this module takes one — and so is passing "Point", the
            # literal GDAL writes and `preserve` writes back. Answering "area"
            # to either makes `preserve` a silent no-op, which is the
            # fifteen-metres-on-a-30 m-DEM defect this module exists to prevent.
            raise ValueError(
                f"registration() got {dataset!r}, which is neither 'area' nor "
                "'point'. Pass an open dataset, or the string registration() "
                "returned for one — a path is not an answer, and neither is the "
                "AREA_OR_POINT tag's own spelling."
            )
        return kind
    if getattr(dataset, "closed", False):
        raise ValueError(
            "registration() was asked about a closed raster. Read it inside the "
            "`with` block that opened the input and pass the string on, or the "
            "answer is whatever GDAL returns for a null pointer."
        )
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

    `source` is an open input dataset **or** the string `registration()` already
    returned for it. The second form exists because the output is often written
    after the input's `with` block has closed, and three writers got that wrong
    at once: taking the string while the input is open makes the correct thing
    the easy thing.

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

    sidecar = Path(f"{path}.aux.xml")
    if not sidecar.exists():
        # Checked BEFORE importing rasterio. This function is called at the top
        # of twelve raster operations, and importing an optional dependency here
        # turned a missing extra into a bare ImportError instead of the sentence
        # naming what to install. Nothing overrides the file, so there is
        # nothing to open and nothing to say.
        return {}

    import rasterio

    setting = {
        name: os.environ[name] for name in GEOREF_VARIABLES if os.environ.get(name)
    }
    with rasterio.open(path) as used:
        effective, effective_crs = used.transform, used.crs
    with rasterio.Env(GDAL_GEOREF_SOURCES="INTERNAL"), rasterio.open(path) as own:
        internal, internal_crs = own.transform, own.crs

    # The CRS as well as the transform. A PAM sidecar can override the `<SRS>`
    # and leave the geotransform alone, and comparing only the transform then
    # reported `georeferencing_source: internal` — a field affirmatively saying
    # the numbers came from the file's own tags when the coordinate system that
    # produced them came from the sidecar. An absent field claims nothing; that
    # one claimed something false. It is also the more consequential axis: a
    # wrong CRS changes every area, length and reprojection downstream, which is
    # the family Argleton measures as `projection-distortion`.
    transform_differs = effective != internal
    crs_differs = not _same_crs(effective_crs, internal_crs)
    from_sidecar = transform_differs or crs_differs
    # A plain image plus an `.aux.xml` carrying SRS and GeoTransform is the
    # documented GDAL way to georeference something that has none. There is one
    # georeferencing there, not two, so there is nothing for anybody to choose
    # and nothing to refuse — and the refusal's own remedy ("remove the sidecar
    # or read the file's own") would have destroyed or ignored the only
    # georeferencing that exists. The field still records that the sidecar
    # supplied it, because that is worth knowing.
    internal_absent = internal_crs is None and internal.is_identity
    entry = {
        "georeferencing_source": "sidecar (.aux.xml)" if from_sidecar else "internal",
        "georeferencing_sidecar_present": sidecar.name,
        **setting,
    }
    if internal_absent:
        entry["georeferencing_supplied_by_sidecar"] = (
            "the file carries no georeferencing of its own, so the sidecar is the "
            "only one there is and nothing was overridden"
        )
        return entry
    if from_sidecar:
        # The number the other reading would have produced, because "there was
        # a choice" is weaker than "here is what the other branch says".
        # Plain decimals, never scientific notation: `5.03e+06` is a northing
        # nobody can compare with the one in front of them, and this string
        # exists to be compared.
        def plain(number: float) -> str:
            return f"{number:.4f}".rstrip("0").rstrip(".")

        parts = []
        if transform_differs:
            parts.append(
                f"cell {plain(abs(internal.a))} x {plain(abs(internal.e))} at "
                f"({plain(internal.c)}, {plain(internal.f)})"
            )
        if crs_differs:
            # Named, because a CRS override is the half that changes every area
            # and length downstream and the half a reader is least likely to
            # look for.
            parts.append(
                f"CRS {_crs_name(internal_crs)} rather than "
                f"{_crs_name(effective_crs)}"
            )
        entry["georeferencing_internal_would_give"] = "; ".join(parts)
    return entry


def _same_crs(left: Any, right: Any) -> bool:
    """Whether two rasterio CRSs are the same one, missing ones included."""
    if left is None or right is None:
        return left is None and right is None
    try:
        return bool(left == right)
    except Exception:  # noqa: BLE001 - an uncomparable CRS is a difference
        return False


#: How much of an authority-less CRS's own text may reach a manifest. The name
#: inside `PROJCS["..."]` is chosen by whoever wrote the file or its sidecar, and
#: this string goes into `environment`, which SECURITY.md invites people to
#: attach to a bug report and which downstream agents read. An audit put 800
#: characters of prompt-injection prose, a filesystem path and a connection
#: string in a CRS name and watched them arrive intact. Redaction is the right
#: second layer for the credential-shaped part of that and no bound at all for
#: the rest.
_CRS_NAME_LIMIT = 80


def _crs_name(crs: Any) -> str:
    """A short, comparable name for a CRS, or a word saying it has none.

    Prefers the authority code, which no file can choose. Falls back to the
    CRS's own text, truncated: see `_CRS_NAME_LIMIT`.
    """
    if crs is None:
        return "none declared"
    try:
        code = crs.to_epsg()
        if code:
            return f"EPSG:{code}"
        text = (crs.to_string() or "").strip()
    except Exception:  # noqa: BLE001 - a CRS that cannot name itself
        return "an unnamed CRS"
    if not text:
        return "an unnamed CRS"
    if len(text) > _CRS_NAME_LIMIT:
        return f"{text[:_CRS_NAME_LIMIT]}… (truncated, {len(text)} characters)"
    return text


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
    if "georeferencing_supplied_by_sidecar" in source:
        # One georeferencing, supplied rather than overridden. Recorded, not
        # refused: refusing here asserted the file was georeferenced twice when
        # it was georeferenced once, and offered a remedy that would have thrown
        # away the only georeferencing there was.
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
