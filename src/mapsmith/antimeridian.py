"""What a bounding box means when the data crosses the 180th meridian.

A survey zone two degrees wide in Fijian waters has a bounding box of
`(-180, -17.5, 180, -16.5)` — a band right around the planet — and every part of
that is arithmetically correct. The minimum longitude in the file really is
-180 and the maximum really is 180.

It is correct and it is the wrong answer to the question anybody asked, which is
the shape of failure this whole project exists to name. So an extent is reported
with the sentence that makes it readable rather than left to be misread.

## Why the two halves of the standard do not compose

RFC 7946 §3.1.9 says a geometry crossing the antimeridian **should** be split
into two parts at it, with every coordinate inside [-180, 180]. §5.2 then defines
a bounding box whose western value exceeds its eastern one as the one that
crosses.

A geometry split correctly per the first has parts that reach -180 and +180, so
the bounds *computed* from its coordinates come out as the ordinary
(west < east) form spanning the planet — never the §5.2 form. Nothing in a
planar geometry library computes anything else: shapely, GEOS and PostGIS all
work in a plane where longitude is an ordinary number, and the inverted-bbox
convention has no representation there.

Both halves are right and they do not meet. This module is the bridge: it
notices the case and reports the §5.2 form alongside the plain one.

## The rule

Two spans are computed from the per-feature bounds:

* **plain** — `max(x) - min(x)`, what every library reports.
* **wrapped** — the same over `x mod 360`, which puts the two sides of the
  antimeridian next to each other.

A layer crosses when three things hold together:

1. the wrapped span is **smaller** than the plain one — the data reads as
   narrower when the seam is treated as continuous;
2. the wrapped span is **under 180°** — the data occupies less than half the
   world going the short way round. This is what separates a zone at the seam
   from data that is simply spread everywhere: points at -179, -90, 0, 90 and
   179 also wrap to a narrower span, and calling that a crossing would be
   noise on a global dataset;
3. **the geometry does not reach into the gap.** One rectangle covering the
   world has exactly two longitudes in it, -180 and 180, which wrap onto the
   same value — so its wrapped span is 0 and by arithmetic alone it is the most
   convincing crossing there is. The difference is not in the coordinates, it is
   in what lies between them, so a meridian is drawn through the middle of the
   apparent empty band and the layer is asked whether anything is there.

There is deliberately **no threshold on the plain span**. An earlier version
required it to exceed 350° before looking any further, which sounds like a cheap
guard and is a blindfold: it only ever fires on data split exactly at ±180, the
well-formed case. Buoys at 170°E and 140°W span 310° and were reported in
silence. The negative cases are held by the three rules above and need no help
from a threshold — verified by measurement, not by argument.
"""

from __future__ import annotations

from typing import Any

#: A crossing layer occupies less than half the world going the short way. At
#: exactly 180° the two readings are the same width and neither is truer, so the
#: plain one wins — it is the one every other tool will also report.
HALF_THE_WORLD = 180.0


def _spans(values: Any) -> tuple[float, float, float, float]:
    """(plain span, wrapped minimum, wrapped maximum, wrapped span).

    Vectorised on purpose: this runs inside `describe_dataset`, which is meant
    to be the cheap call somebody makes before deciding anything.
    """
    import numpy as np

    xs = np.asarray(values, dtype="float64")
    wrapped = np.mod(xs, 360.0)
    wrapped_min, wrapped_max = float(wrapped.min()), float(wrapped.max())
    return (
        float(xs.max()) - float(xs.min()),
        wrapped_min,
        wrapped_max,
        wrapped_max - wrapped_min,
    )


#: The axis directions of the two horizontal axes. A third axis — height — is
#: `up` or `down`, and its unit says nothing about how longitude is measured.
_HORIZONTAL = {"east", "west", "north", "south"}


def _is_in_degrees(crs: Any) -> bool:
    """Geographic is not enough: EPSG:4807 is geographic in **grads**.

    Wrapping at 360 and talking about the 180th meridian would both be wrong
    there, and it is the same class of mistake `measure_area` exists to avoid —
    reading the unit from the CRS instead of assuming it.

    Only the **horizontal** axes are asked. Requiring every axis to be in
    degrees switched the whole module off on EPSG:4979 — WGS 84 3D, which is
    what a great deal of GNSS and LiDAR data declares — because its third axis
    is ellipsoidal height in metres. The result was the silent false negative
    this module exists to prevent: a Fijian survey zone came back with the plain
    world-spanning bounding box, no `crosses_antimeridian`, no note, nothing
    anywhere to say a question had been skipped.
    """
    try:
        if not crs.is_geographic:
            return False
        horizontal = [
            axis
            for axis in crs.axis_info
            if str(axis.direction).lower() in _HORIZONTAL
        ]
        if not horizontal:
            return False
        return all(axis.unit_name == "degree" for axis in horizontal)
    except AttributeError:  # pragma: no cover — a CRS without axis metadata
        return False


def describe_extent(gdf: Any) -> dict[str, Any]:
    """The extent of a layer, and what it means if it crosses the antimeridian.

    Always returns `minx/miny/maxx/maxy` — the plain bounding box, unchanged,
    because that is what the coordinates say and something downstream may be
    relying on it. When the data crosses, three more keys appear:
    `crosses_antimeridian`, `true_extent` in RFC 7946 §5.2 form (west greater
    than east), and `note`.

    Only degree-based geographic coordinate systems are considered. A projected
    CRS has no antimeridian in it — the seam is outside the projection's
    domain — and testing for one would produce a note on any dataset that
    happens to span 360 units of easting.
    """
    bounds = gdf.total_bounds
    extent = {
        "minx": float(bounds[0]),
        "miny": float(bounds[1]),
        "maxx": float(bounds[2]),
        "maxy": float(bounds[3]),
    }
    if gdf.crs is None or not len(gdf) or not _is_in_degrees(gdf.crs):
        return extent

    # Per-feature bounds rather than every vertex: for a geometry split at the
    # seam as §3.1.9 prescribes, the halves' own bounds already carry the
    # extremes, and this is two numbers per feature instead of two per vertex.
    # It cannot see a single unsplit geometry that itself spans the seam — that
    # one has coordinates outside [-180, 180], its plain bounds are already the
    # narrow truth, and nothing about them misleads.
    import numpy as np

    per_feature = gdf.bounds
    xs = per_feature[["minx", "maxx"]].to_numpy().ravel()
    # A null geometry has NaN bounds, and NaN poisons min/max silently.
    xs = xs[np.isfinite(xs)]
    if not len(xs):
        return extent

    plain_span, wrapped_min, wrapped_max, wrapped_span = _spans(xs)
    if wrapped_span >= plain_span or wrapped_span >= HALF_THE_WORLD:
        # Either the seam buys nothing, or the data is spread over more than
        # half the world and neither reading is the narrow one.
        return extent

    # The probe: a meridian through the middle of the apparent empty band. Data
    # that really crosses the antimeridian does not reach it; a rectangle
    # covering the world does. This is the case arithmetic cannot decide.
    import shapely

    middle = ((wrapped_max + wrapped_min + 360.0) / 2.0) % 360.0
    probe_lon = middle if middle <= 180.0 else middle - 360.0
    probe = shapely.LineString(
        [(probe_lon, extent["miny"]), (probe_lon, extent["maxy"])]
    )
    if len(gdf.sindex.query(probe, predicate="intersects")):
        return extent

    west = wrapped_min if wrapped_min <= 180.0 else wrapped_min - 360.0
    east = wrapped_max if wrapped_max <= 180.0 else wrapped_max - 360.0
    extent.update(
        crosses_antimeridian=True,
        true_extent={
            "minx": round(west, 9),
            "miny": extent["miny"],
            "maxx": round(east, 9),
            "maxy": extent["maxy"],
            "width_degrees": round(wrapped_span, 9),
        },
        note=(
            f"the extent above spans {plain_span:.6g} degrees of longitude and the "
            f"data spans {wrapped_span:.6g}. Both are correct: the coordinates really "
            "do reach across the antimeridian, because the geometry is split there "
            "as RFC 7946 3.1.9 prescribes. `true_extent` is the same envelope in the "
            "form RFC 7946 5.2 defines for this case, with the western value GREATER "
            "than the eastern one. Filtering by the plain box — a coordinate slice, a "
            "tile request, a WHERE on min/max columns — selects everything at these "
            "latitudes anywhere on Earth."
        ),
    )
    return extent
