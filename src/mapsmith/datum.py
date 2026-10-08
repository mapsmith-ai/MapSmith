"""Which coordinate operation PROJ uses between two CRSs, and whether it shifts anything.

Argleton trap 021 is the reason this module exists. When no datum transformation
is available for a pair, PROJ does not fail: it falls back to a *ballpark*
operation, which carries the coordinates across as if the two datums coincided.
The geometry is plausible, the output CRS is genuinely the one that was asked
for, and every downstream check passes -- while the numbers are tens of metres
out. In Italy the NAD27/WGS84 style of mistake is worth about 74 m.

`engines/vector.py` has answered that for `reproject_layer` since 0.4.0. It was
the only answer anywhere: on 2026-09-02 a sweep found `transformation` in one
manifest out of fifty-eight, and `reproject_raster` -- which is the head example
of section 3.7 of the manifest specification -- recorded the two CRS labels and
said nothing about the operation between them. The trap we measure in other
people's software survived intact on our own raster side.

**The distinction this module exists to keep**, and getting it wrong would be
worse than saying nothing:

- `best_operation` is for callers that *choose*. It hands back a transformer and
  will pick a stated-accuracy operation over a ballpark one when both exist. The
  vector path uses it, so the record describes what actually happened.
- `default_operation` is for callers that *do not choose* -- rasterio's warp,
  GDAL, anything that takes the two CRSs and reaches for PROJ itself. It reports
  the operation PROJ picks on its own. Recording a better operation's accuracy
  beside a raster that was warped with the default would be a false manifest,
  which is a worse failure than an incomplete one.

Both are plain pyproj. Nothing here needs a provenance format, which is the
point: a caller can check this without adopting anything of ours.
"""

from __future__ import annotations

import math
from typing import Any

#: Inside `transformation`, when part of the data got a datum shift and part
#: got none: a layer straddling the edge of a grid. `is_ballpark` is true, which
#: is the conservative reading, and this says how much of it.
BALLPARK_SHARE = "x-mapsmith:ballpark_share"
#: Inside `transformation`, when some of the data lies outside the area of use
#: of every operation published for the pair, installed or not. Independent of
#: `is_ballpark`: outside every area PROJ falls back to the ballpark for some
#: pairs and extends a published operation for others, and either way the
#: stated accuracy was not established for those coordinates.
OUTSIDE_AREA = "x-mapsmith:outside_area_of_use"

# A ballpark operation reports accuracy -1 (or nothing at all). Anything >= 0 is
# a real, published operation with a stated accuracy in metres.
#
# That reading is true of the EPSG database, which is where the number comes
# from for a published operation, and it is NOT enough on its own: see
# `shares_a_datum`, whose comment records the day the same value started
# meaning two different things.
_STATED = 0.0


def shares_a_datum(source_crs: Any, target_crs: Any) -> bool:
    """Do these two CRSs sit on the same datum, so there is no shift to apply?

    Asked of the datums rather than inferred from an accuracy, and that is the
    whole point of this function existing.

    PROJ used to report a stated accuracy of 0.0 for a projection change within
    one datum, so `accuracy >= 0` happened to mean "not a ballpark". PROJ 9.8
    reports **-1.0** for the same pair — the value it also uses for a ballpark —
    so the discriminator stopped discriminating, and MapSmith on a current
    pyproj wrote `is_ballpark: true` into the manifest of a plain UTM
    projection. That is not a cosmetic slip: a record saying a datum shift was
    skipped, on an operation where none was ever needed, accuses the engine of
    something it did not do, and this module exists to prevent exactly that
    sentence in the other direction.

    Measured 2026-09-06 on EPSG:4326 -> EPSG:32633, the same call on two
    builds::

        PROJ 9.5.1   get_last_used_operation().accuracy ==  0.0
        PROJ 9.8.1   get_last_used_operation().accuracy == -1.0

    while `CRS.datum == CRS.datum` answers True on both. It was found by CI
    going red on Python 3.14, which resolves pyproj to 3.8 — the weekly
    "latest versions" job this repository has on its list precisely to tell
    "broken by us" from "broken by them" apart, arriving here by accident
    before it was built.

    Returns False whenever the question cannot be answered — a CRS with no
    datum, a comparison that raises — because the caller then falls back to
    asking PROJ, which is the behaviour that was there before.
    """
    from contextlib import suppress

    with suppress(Exception):
        source_datum = getattr(_as_crs(source_crs), "datum", None)
        target_datum = getattr(_as_crs(target_crs), "datum", None)
        if source_datum is not None and target_datum is not None:
            return bool(source_datum == target_datum)
    return False


def _within_one_datum(pipeline: str | None) -> dict[str, Any]:
    """The record for a pair that needs no datum shift at all.

    Accuracy 0.0 rather than None: a projection change within one datum is
    exact to floating point, it is what PROJ itself reported until 9.8, and it
    is what section 3.7 wants — a number, not an absence a reader has to
    interpret.
    """
    return {"pipeline": pipeline, "accuracy_m": 0.0, "is_ballpark": False}


def _as_crs(value: Any) -> Any:
    """Whatever the caller has, as a pyproj CRS that still knows where it is used.

    Not defensive tidying, and the WKT route is not good enough -- measured on
    2026-09-03, on the pair this module exists for:

        CRS.from_user_input("EPSG:4267")        area_of_use present   ->  7.0 m
        CRS.from_user_input(<the same as WKT>)  area_of_use ABSENT    ->  ballpark

    `area_of_use` comes from the EPSG registry, not from the WKT, and rasterio's
    WKT does not carry it. `accuracy_of` probes a point inside that area to see
    which operation PROJ selects; with no area it falls back to (0, 0), the Gulf
    of Guinea, which is outside almost every real transformation's extent. So
    the same pair came back a ballpark or not **depending on how the caller
    happened to hold the CRS** -- and a wrong `is_ballpark: true` is a manifest
    accusing an engine of something it did not do, which is worse than the
    silence this module replaces.

    Rebuilding from the authority code brings the registry's area back. When
    there is no code (a genuinely custom CRS) the probe is weaker and the record
    says so, rather than asserting a ballpark it cannot see.
    """
    from pyproj import CRS

    crs = value if isinstance(value, CRS) else CRS.from_user_input(
        value.to_wkt() if hasattr(value, "to_wkt") else value
    )
    if crs.area_of_use is not None:
        return crs
    code = crs.to_epsg()
    return CRS.from_epsg(code) if code else crs


#: The most points PROJ itself is asked at, one a transform. Coverage is checked
#: at EVERY vertex (vectorised, against the operations' areas of use); these
#: points only learn which operation PROJ picks where one applies, so they are
#: spread over the layer's extent, one per occupied cell of a grid this size.
_ASK_GRID = 8

#: A raster covers its bounds, so its "vertices" are a grid over them this many
#: to a side: corners and centre alone missed a raster whose corners fell
#: outside a grid that covered most of it.
_RASTER_GRID = 11

#: Features per block when their vertices are extracted.
_BLOCK = 5000


def sample_points(data: Any) -> Any:
    """Coordinates where the data actually is, in its own CRS, or None.

    Which operation PROJ selects depends on where the coordinate is, so the
    question has to be asked where the data is. Asked at one point chosen from
    the CRS alone, the answer was wrong in both directions (measured
    2026-10-08, PROJ 9.5.1):

        EPSG:4326 -> EPSG:3035, data at 10E 45N   recorded ballpark, PROJ used 1 m
        EPSG:4326 -> EPSG:27700, data in England  recorded ballpark, PROJ used 2 m
        EPSG:4267 -> EPSG:4326, data in Italy     recorded 7 m,     PROJ used a noop

    The first two accuse the engine of a shift it did not skip; the third is
    Argleton's trap 021 in our own record, hidden by it.

    Returns None only for None; data with no finite coordinate gives an empty
    array, and the record then says that nothing was transformed.

    Every vertex, not a sample: 24 taken evenly through the layer missed five
    Italian vertices among ten thousand Texan ones, and the record said 7 m for
    all of it (review, 2026-10-08). The closing vertex of each ring is dropped,
    since it repeats the first. Accepts a GeoDataFrame or GeoSeries, an array of
    shapely geometries, or raster bounds as ``(left, bottom, right, top)``.
    """
    import numbers

    import numpy as np
    import shapely

    if data is None:
        return None
    if isinstance(data, tuple) and len(data) == 4 and all(isinstance(v, numbers.Real) for v in data):
        left, bottom, right, top = (float(v) for v in data)
        xs = np.linspace(left, right, _RASTER_GRID)
        ys = np.linspace(bottom, top, _RASTER_GRID)
        points = np.array([(x, y) for x in xs for y in ys])
    else:
        geometries = np.asarray(getattr(data, "geometry", data), dtype=object)
        geometries = geometries[~shapely.is_missing(geometries)]
        if not len(geometries):
            return np.empty((0, 2))
        # In blocks of features: parts, rings and the per-vertex ring index are
        # each as large as the layer, and built all at once they peaked at 82
        # bytes a vertex (review: 828 MB on ten million vertices).
        chunks = []
        for start in range(0, len(geometries), _BLOCK):
            parts = shapely.get_parts(geometries[start:start + _BLOCK])
            areal = np.isin(shapely.get_type_id(parts), (shapely.GeometryType.POLYGON,))
            if (~areal).any():
                chunks.append(shapely.get_coordinates(parts[~areal]))
            if areal.any():
                rings = shapely.get_rings(parts[areal])
                xy, ring = shapely.get_coordinates(rings, return_index=True)
                if len(xy):
                    closing = np.r_[ring[1:] != ring[:-1], True]
                    chunks.append(xy[~closing])
        points = np.concatenate(chunks) if chunks else np.empty((0, 2))
    # Data handed over with no finite coordinate is an empty array, not None:
    # None means "the caller had no data", and falls back to a probe that
    # recorded a 7 m shift for coordinates that do not exist (review).
    return points[np.isfinite(points).all(axis=1)]


def _accuracies(transformer: Any, source_crs: Any, target_crs: Any, points: Any) -> list[Any]:
    """The operation PROJ used at each point: (accuracy or None, its PROJ string).

    PROJ reports the operation only after one has been used, so use one.
    `Transformer.accuracy` is -1 until `proj_trans` runs; the honest value comes
    from `get_last_used_operation()` after a transform -- one point at a time,
    because the operation can change from one point to the next.
    """
    if points is None or not len(points):
        points = [_probe_point(source_crs, target_crs)]
    found: list[Any] = []
    for x, y in points:
        try:
            transformer.transform(float(x), float(y))
            used = transformer.get_last_used_operation()
            found.append((used.accuracy, used.to_proj4()))
        except Exception:  # noqa: BLE001 — no operation to inspect is itself the answer
            found.append((None, None))
    return found


def accuracy_of(transformer: Any, source_crs: Any, points: Any = None, target_crs: Any = None) -> float | None:
    """The stated accuracy in metres of the operation this transformer used.

    The worst over the points asked: None (or a negative value, PROJ's ballpark)
    if any point got no published operation, else the largest stated accuracy.
    """
    found = [a for a, _ in _accuracies(transformer, source_crs, target_crs, points)]
    if any(a is None or a < _STATED for a in found):
        return next((a for a in found if a is None or a < _STATED), None)
    return max(found)


def _covers(area: Any, lon: Any, lat: Any) -> Any:
    """Which points an operation's area of use contains (west > east crosses 180)."""
    import numpy as np

    if area is None:
        return np.zeros(len(lon), dtype=bool)
    across = (lon >= area.west) | (lon <= area.east) if area.west > area.east else (
        (lon >= area.west) & (lon <= area.east)
    )
    return across & (lat >= area.south) & (lat <= area.north)


def _spread(lon: Any, lat: Any, indices: Any) -> Any:
    """One of these indices per occupied cell of an even grid over their extent."""
    import numpy as np

    if not len(indices):
        return indices
    cl, ct = lon[indices], lat[indices]
    span_x = max(float(np.ptp(cl)), 1e-12)
    span_y = max(float(np.ptp(ct)), 1e-12)
    cell = (
        np.minimum(((cl - cl.min()) / span_x * _ASK_GRID).astype(int), _ASK_GRID - 1) * _ASK_GRID
        + np.minimum(((ct - ct.min()) / span_y * _ASK_GRID).astype(int), _ASK_GRID - 1)
    )
    _, first = np.unique(cell, return_index=True)
    return indices[first]


class _Survey:
    """Where the data is, against what PROJ has for this pair, and what PROJ did.

    Two facts, kept apart because they part company. **Coverage**: whether any
    installed operation's area of use contains a vertex -- a count over every
    vertex, taken on cells of a hundredth of a degree (an area of use is a box
    drawn to that precision, and five million vertices fit in a few thousand
    cells). **What PROJ did**: asked at one point per occupied cell of the
    covered vertices and one per cell of the uncovered ones. Outside every
    area PROJ falls back to the ballpark for some pairs (NAD27) and extends a
    published operation for others (DHDN, NTF, CH1903+): inferring the
    ballpark from coverage alone recorded "no shift" for coordinates PROJ had
    moved -- DHDN in Italy, Milan into the Swiss grid -- which is the worst
    direction to be wrong in (review, 2026-10-08, round two). So `is_ballpark`
    comes from PROJ's answers, and coverage says how much of the data each
    answer stands for.

    The group is built WITHOUT `area_of_interest`, and that is measured rather
    than assumed: on EPSG:4806 with the extent of the data the group came back
    holding only the ballpark -- the 44 m operation disappears (PROJ 9.5.1,
    2026-08-27). Areas are compared here instead.
    """

    def __init__(self, source_crs: Any, target_crs: Any, points: Any, chooser: Any) -> None:
        from contextlib import suppress

        import numpy as np
        from pyproj.transformer import TransformerGroup

        self.from_data = points is not None and len(points) > 0
        if not self.from_data:
            points = np.array([_probe_point(source_crs, target_crs)], dtype=float)
        # Longitude from GREENWICH, which is what every area of use is stated
        # in: the source's own geodetic CRS can count from Rome or Paris
        # (EPSG:4806 does), and its degrees then sat 12 degrees off every area
        # they were compared with. A geographic source already counted from
        # Greenwich is read as it is: the hop would only cost a transform.
        group = TransformerGroup(source_crs, target_crs, always_xy=True)
        self.stated = [
            t for t in group.transformers if t.accuracy is not None and t.accuracy >= _STATED
        ]
        self.unavailable = [
            op for op in group.unavailable_operations
            if op.accuracy is not None and op.accuracy >= _STATED
        ]
        operations = [*self.stated, *self.unavailable]
        # Each operation's area, in the frame PROJ compares a point with. For a
        # geographic source that is longitude and latitude from Greenwich; for
        # a PROJECTED one it is the area's envelope reprojected into the source
        # CRS, 21 points a side -- not the box in degrees: between the two lies
        # a band where PROJ shifts and the degrees said "outside", and an
        # EPSG:3035 layer past 38E hid two unshifted vertices among fifty
        # (review, round four).
        if source_crs.is_geographic:
            u, v = _greenwich_lonlat(source_crs, points)
            step = 0.01
            boxes = {
                id(op): (a.west, a.south, a.east, a.north, a.west > a.east)
                for op in operations if (a := op.area_of_use) is not None
            }
        else:
            from pyproj import CRS, Transformer

            u, v = np.asarray(points[:, 0], dtype=float), np.asarray(points[:, 1], dtype=float)
            step = max(float(np.ptp(u)), float(np.ptp(v)), 1e-9) / 2000
            into = Transformer.from_crs(CRS.from_epsg(4326), source_crs, always_xy=True)
            boxes = {}
            for op in operations:
                a = op.area_of_use
                if a is None:
                    continue
                with suppress(Exception):
                    left, bottom, right, top = into.transform_bounds(
                        a.west, a.south, a.east, a.north, densify_pts=21
                    )
                    if all(math.isfinite(x) for x in (left, bottom, right, top)):
                        boxes[id(op)] = (left, bottom, right, top, False)

        def inside(op: Any, uu: Any, vv: Any) -> Any:
            box = boxes.get(id(op))
            if box is None:
                return np.zeros(len(uu), dtype=bool)
            west, south, east, north, wraps = box
            across = (uu >= west) | (uu <= east) if wraps else (uu >= west) & (uu <= east)
            return across & (vv >= south) & (vv <= north)

        # Coverage is EXACT for every vertex. On cells judged by one vertex
        # each, a cell an area's edge runs through hid the vertices on its
        # other side -- NAD27 at -43.996 recorded as a 20 m shift beside
        # -44.004 (review, round three). So the vertices of the cells an edge
        # can cross are judged one by one, and every other cell, wholly inside
        # or wholly outside each area, by one of its vertices.
        ku = np.round(u / step).astype(np.int64)
        kv = np.round(v / step).astype(np.int64)
        edge_u = sorted({round(box[i] / step) + d for box in boxes.values() for i in (0, 2) for d in (-1, 0, 1)})
        edge_v = sorted({round(box[i] / step) + d for box in boxes.values() for i in (1, 3) for d in (-1, 0, 1)})
        on_edge = np.isin(ku, edge_u) | np.isin(kv, edge_v)
        off = np.flatnonzero(~on_edge)
        # Only the off-edge vertices are grouped by cell: a sentinel key for
        # the edge vertices collided with a real cell (review, round four).
        _, first, rest_counts = np.unique(
            (ku[off] - ku.min()) * (int(kv.max() - kv.min()) + 1) + (kv[off] - kv.min()),
            return_index=True, return_counts=True,
        )
        rest_first = off[first]
        exact = np.flatnonzero(on_edge)
        del ku, kv, on_edge, off
        # Each group: one vertex per off-edge cell weighted by the cell's
        # count, and every edge vertex weighted one.
        index = np.concatenate([rest_first, exact])
        weight = np.concatenate([rest_counts, np.ones(len(exact), dtype=rest_counts.dtype)])
        gu, gv = u[index], v[index]
        covered = np.zeros(len(index), dtype=bool)
        #: Installed operations whose area contains every vertex, best first.
        self.whole = []
        for operation in self.stated:
            mask = inside(operation, gu, gv)
            covered |= mask
            if mask.all():
                self.whole.append(operation)
        self.whole.sort(key=lambda t: t.accuracy)
        published = covered.copy()
        #: Missing-grid operations whose area contains every vertex that no
        #: installed operation covers -- the only kind a download would help.
        self.better: list[float] = []
        for operation in self.unavailable:
            mask = inside(operation, gu, gv)
            published |= mask
            if mask[~covered].all() if (~covered).any() else mask.all():
                self.better.append(float(operation.accuracy))
        glon, glat = gu, gv
        self.lon, self.lat, self.weight = glon, glat, weight
        self.points = points[index]
        self.n = int(weight.sum())
        self.covered = covered
        self.uncovered = int(weight[~covered].sum())
        #: Vertices outside the area of use of EVERY operation published for
        #: the pair, installed or not: a fact about where the data is,
        #: whatever the engine then did with them.
        self.outside = int(weight[~published].sum())
        inside_at = _spread(self.lon, self.lat, np.flatnonzero(self.covered))
        outside_at = _spread(self.lon, self.lat, np.flatnonzero(~self.covered))
        self.asked_inside = (
            _accuracies(chooser, source_crs, target_crs, self.points[inside_at]) if len(inside_at) else []
        )
        self.asked_outside = (
            _accuracies(chooser, source_crs, target_crs, self.points[outside_at]) if len(outside_at) else []
        )

    @staticmethod
    def _ballpark(found: list[Any]) -> bool:
        return any(a is None or a < _STATED for a, _ in found)

    @property
    def unshifted(self) -> int:
        """Vertices carried across with no datum shift, as far as PROJ's answers show.

        The uncovered vertices count when PROJ, asked among them, left any
        unshifted. A covered vertex PROJ left unshifted -- never seen in 17
        pairs over 14,400 points -- makes the whole layer count.
        """
        if self._ballpark(self.asked_inside):
            return self.n
        return self.uncovered if self._ballpark(self.asked_outside) else 0

    @property
    def is_ballpark(self) -> bool:
        return self.unshifted > 0

    @property
    def accuracy(self) -> float | None:
        if self.is_ballpark:
            return None
        stated = [a for a, _ in self.asked_inside + self.asked_outside if a is not None and a >= _STATED]
        return float(max(stated)) if stated else None

    @property
    def pipeline(self) -> str | None:
        """The operation string, only when PROJ used one operation everywhere it was asked.

        A layer half shifted and half not names no single operation: recording
        the shifted half's would tell a reader to move the other half too.
        """
        strings = {s for _, s in self.asked_inside + self.asked_outside}
        return strings.pop() if len(strings) == 1 and None not in strings else None

    def share(self) -> dict[str, int] | None:
        """How many of the data's vertices got no shift, when it is some and not all."""
        if self.from_data and 0 < self.unshifted < self.n:
            return {"ballpark": self.unshifted, "checked": self.n}
        return None

    def outside_area(self) -> dict[str, int] | None:
        """How many vertices no published operation's area of use contains."""
        if self.from_data and self.outside:
            return {"vertices": self.outside, "checked": self.n}
        return None

    def better_available(self) -> float | None:
        """A published operation this machine lacks that would cover what got no shift.

        Only operations whose area of use contains every vertex that went
        without one: an operation for Canada is not a better answer for NAD27
        coordinates in Italy, and "install its grid" sent a reader to a
        download that would change nothing (review, 2026-10-08). Only the
        UNAVAILABLE ones: an installed operation that covers the data would
        have been used.
        """
        return min(self.better) if self.better else None

    def covering_all(self) -> list[Any]:
        """Installed operations whose area of use contains every vertex, best first."""
        return list(self.whole)


def _probe_point(source_crs: Any, target_crs: Any = None) -> tuple[float, float]:
    """A coordinate inside the CRS's own area of use, IN THAT CRS'S OWN UNITS.

    `area_of_use` is always in degrees, even for a projected CRS. Feeding its
    midpoint straight to the transformer therefore hands degrees to something
    that expects metres -- measured on 2026-09-03 with EPSG:3003 (Gauss-Boaga,
    Italy zone 1), whose area of use is 5.93..12.0 by 36.53..47.04: the probe
    landed at x=8.965 m, y=41.785 m, nine metres from the false origin and
    nowhere near Italy. No location-restricted operation matches there, so PROJ
    returned the ballpark and the record said the default applied no datum
    shift.

    It does. The default for EPSG:3003 -> EPSG:4326 is "Monte Mario to WGS 84
    (4)", stated accuracy 4 m, and it produces coordinates identical to the ones
    the "better" operation this module would have substituted. So the shipped
    note -- "the transformation this library selects by default for this pair is
    a ballpark one, which applies no datum shift at all" -- was false for every
    projected source, which in this domain is most of them.
    """
    area = getattr(source_crs, "area_of_use", None)
    if area is None:
        return (0.0, 0.0)
    # A bounding box that crosses the antimeridian has west > east, and a plain
    # midpoint of it lands on the far side of the planet -- which is outside
    # every real transformation's extent and therefore always ballpark. This
    # cost an afternoon on 2026-08-26.
    lat = (area.south + area.north) / 2
    lon = (area.west + area.east) / 2
    if area.west > area.east:
        lon = ((area.west + area.east + 360) / 2 + 180) % 360 - 180
    # Only a fallback now, for a caller with no data to hand (`sample_points`
    # is the answer). Where the TARGET's area of use is narrower, the probe goes
    # in the overlap of the two: EPSG:4326 is the whole world, whose middle is
    # the Gulf of Guinea, and EPSG:4326 -> EPSG:3035 was recorded a ballpark
    # because no European operation is valid there.
    other = getattr(_as_crs(target_crs), "area_of_use", None) if target_crs is not None else None
    if other is not None and area.west <= area.east and other.west <= other.east:
        west, east = max(area.west, other.west), min(area.east, other.east)
        south, north = max(area.south, other.south), min(area.north, other.north)
        if west < east and south < north:
            lon, lat = (west + east) / 2, (south + north) / 2
    if getattr(source_crs, "is_geographic", False):
        return (lon, lat)
    from pyproj import CRS, Transformer

    into = Transformer.from_crs(CRS.from_epsg(4326), source_crs, always_xy=True)
    x, y = into.transform(lon, lat)
    if not (math.isfinite(x) and math.isfinite(y)):
        # Outside the projection's valid domain: no honest probe exists, and a
        # silent (0, 0) would answer "ballpark" for a pair nobody asked about.
        return (0.0, 0.0)
    return (x, y)


def pipeline_of(transformer: Any) -> str | None:
    try:
        return transformer.to_proj4() or None
    except Exception:  # noqa: BLE001 — a missing pipeline string is not a failure
        return None


def why_unshifted(shift: dict[str, Any]) -> tuple[str, bool]:
    """Why the coordinates a record calls unshifted got no shift, and whether all lie outside.

    Three cases a reader acts on differently, told apart by the record's own
    counts: every unshifted vertex outside the area of every published
    operation (nothing to install; check the declared CRS), none outside (an
    installed operation was not used, or a missing one would cover them), or
    some of each -- where "installed or not" said of all of them was false of
    the ones a download would fix (review, round four).
    """
    share = shift.get(BALLPARK_SHARE)
    outside = shift.get(OUTSIDE_AREA)
    unshifted = share["ballpark"] if share else (outside["checked"] if outside else None)
    if outside and unshifted is not None and outside["vertices"] >= unshifted:
        return "no published datum transformation applies to them, installed or not", True
    if outside:
        reason = (
            "no published datum transformation applies to some of them, and for the rest "
            "one exists that is not installed here"
        )
        return reason, False
    return "no datum transformation installed here applies to them", False


def _nothing_moved() -> dict[str, Any]:
    """The record for a move of no coordinates at all: nothing to describe."""
    return {"pipeline": None, "accuracy_m": None, "is_ballpark": False}


def _with_outside(survey: Any, record: dict[str, Any]) -> dict[str, Any]:
    """Add how much of the data no published operation covers, when some is."""
    outside = survey.outside_area()
    if outside:
        record[OUTSIDE_AREA] = outside
    return record


def _greenwich_twin(crs: Any) -> Any:
    """The same datum counted from Greenwich, when this CRS counts from elsewhere.

    EPSG registers Monte Mario twice -- EPSG:4265 from Greenwich, EPSG:4806 from
    Rome -- as two datums, and publishes the mainland Italian operations for
    the first only. So for EPSG:4806 PROJ offers a 44 m operation valid in
    Sardinia and the ballpark, and Argleton's trap 021 (a station in Piedmont)
    has no operation that covers it. The twin route -- a prime-meridian change,
    exact, then the published 4 m operation -- lands on the truth to 0.0 m.
    Until 2026-10-08 MapSmith passed that trap by applying the Sardinian
    operation in Piedmont: 6.3 m off, inside the tolerance, by luck.

    Found by name in the registry ("Monte Mario (Rome)" -> "Monte Mario") and
    confirmed by ellipsoid and a Greenwich meridian; None when there is none.
    """
    from contextlib import suppress

    from pyproj import CRS
    from pyproj.database import query_crs_info
    from pyproj.enums import PJType

    with suppress(Exception):
        geodetic = crs.geodetic_crs or crs
        meridian = geodetic.prime_meridian
        if meridian is None or meridian.name == "Greenwich" or " (" not in geodetic.name:
            return None
        base = geodetic.name.rsplit(" (", 1)[0]
        for info in query_crs_info(auth_name="EPSG", pj_types=PJType.GEOGRAPHIC_2D_CRS):
            if info.name != base:
                continue
            twin = CRS.from_epsg(int(info.code))
            if twin.prime_meridian.name == "Greenwich" and twin.ellipsoid == geodetic.ellipsoid:
                return twin
    return None


def _join_pipelines(*transformers: Any) -> Any:
    """One transformer that applies these in turn, built from their PROJ strings."""
    from pyproj import Transformer

    steps = []
    for transformer in transformers:
        definition = transformer.to_proj4()
        if not definition:
            return None
        steps.append(definition.removeprefix("+proj=pipeline").strip()
                     if definition.startswith("+proj=pipeline") else f"+step {definition}")
    return Transformer.from_pipeline("+proj=pipeline " + " ".join(steps))


def _greenwich_lonlat(source_crs: Any, points: Any) -> tuple[Any, Any]:
    """Longitude and latitude counted from Greenwich, which every area of use is stated in.

    A geographic source already counted from Greenwich is read as it is; any
    other is transformed -- the datum shift that hop applies is metres, and an
    area of use is a box drawn to the nearest hundredth of a degree.
    """
    import numpy as np
    from pyproj import CRS, Transformer

    meridian = getattr(source_crs, "prime_meridian", None)
    if source_crs.is_geographic and meridian is not None and meridian.name == "Greenwich":
        lon, lat = points[:, 0], points[:, 1]
    else:
        lon, lat = Transformer.from_crs(source_crs, CRS.from_epsg(4326), always_xy=True).transform(
            points[:, 0], points[:, 1]
        )
    return (np.asarray(lon, dtype=float) + 180) % 360 - 180, np.asarray(lat, dtype=float)


def twin_route(source_crs: Any, target_crs: Any, points: Any) -> tuple[Any, Any, Any] | None:
    """The route through a datum's Greenwich twin, when an operation of the twin covers the data.

    Returns (transformer, the operation, the twin CRS), or None. The covering
    test is exact over every point: a hint naming the twin to a raster in
    Spain, where none of the twin's operations reaches, prescribed a route that
    changes nothing (review, round three).
    """
    import numpy as np
    from pyproj import Transformer
    from pyproj.transformer import TransformerGroup

    source_crs, target_crs = _as_crs(source_crs), _as_crs(target_crs)
    twin = _greenwich_twin(source_crs)
    if twin is None:
        return None
    if points is None or not len(points):
        points = np.array([_probe_point(source_crs, target_crs)], dtype=float)
    lon, lat = _greenwich_lonlat(source_crs, points)
    candidates = sorted(
        (
            t for t in TransformerGroup(twin, target_crs, always_xy=True).transformers
            if t.accuracy is not None and t.accuracy >= _STATED
            and np.all(_covers(t.area_of_use, lon, lat))
        ),
        key=lambda t: t.accuracy,
    )
    if not candidates:
        return None
    route = _join_pipelines(Transformer.from_crs(source_crs, twin, always_xy=True), candidates[0])
    return (route, candidates[0], twin) if route is not None else None


def best_operation(source_crs: Any, target_crs: Any, points: Any = None) -> tuple[Any, dict[str, Any]]:
    """The transformer to use when the caller gets to choose, and its record.

    Returns the transformer and the `crs_decisions.transformation` object that
    section 3.7 of the manifest specification asks for. ``points`` are the
    data's own coordinates in the source CRS (`sample_points`): the operation
    is asked where the data is.

    It substitutes only an operation whose area of use contains EVERY vertex.
    Substituting the first published one for the pair applied a Canadian
    operation to NAD27 coordinates in Italy: the data moved about 190 m and
    the record said 20 m (review, 2026-10-08). Where no single operation covers
    the data, the default stays -- it picks per point, as `to_crs` does -- and
    the record says what part of the data got no shift.
    """
    from pyproj import Transformer

    source_crs, target_crs = _as_crs(source_crs), _as_crs(target_crs)
    chosen = Transformer.from_crs(source_crs, target_crs, always_xy=True)
    if shares_a_datum(source_crs, target_crs):
        return chosen, _within_one_datum(pipeline_of(chosen))
    if points is not None and not len(points):
        return chosen, _nothing_moved()
    survey = _Survey(source_crs, target_crs, points, chosen)
    if not survey.is_ballpark:
        return chosen, _with_outside(survey, {
            "pipeline": survey.pipeline or pipeline_of(chosen),
            "accuracy_m": survey.accuracy,
            "is_ballpark": False,
        })
    whole = survey.covering_all() if not survey.uncovered else []
    if whole:
        best = whole[0]
        return best, {
            "pipeline": pipeline_of(best),
            "accuracy_m": float(best.accuracy),
            "is_ballpark": False,
            # The caller is owed this: the transformation the library would
            # have picked by itself applied no datum shift, and this one was
            # chosen instead. Without it the record says the right thing and
            # hides that anything happened.
            "x-mapsmith:default_was_ballpark": True,
        }
    # A datum counted from another meridian: its published operations may be
    # registered for its Greenwich twin only.
    found = twin_route(source_crs, target_crs, points) if not whole else None
    if found is not None:
        route, operation, _ = found
        return route, {
            "pipeline": pipeline_of(route),
            "accuracy_m": float(operation.accuracy),
            "is_ballpark": False,
            "x-mapsmith:default_was_ballpark": True,
        }
    # No datum shift for (part of) these coordinates: saying so is the only
    # honest answer. Recording `is_ballpark: true` rather than refusing keeps
    # the operation usable where the caller knows the datums are equivalent.
    record: dict[str, Any] = {
        "pipeline": survey.pipeline or pipeline_of(chosen),
        "accuracy_m": None,
        "is_ballpark": True,
    }
    share = survey.share()
    if share:
        record[BALLPARK_SHARE] = share
    _with_outside(survey, record)
    better = survey.better_available()
    if better is not None:
        record["better_available_m"] = better
    return chosen, record


def default_operation(source_crs: Any, target_crs: Any, points: Any = None) -> dict[str, Any]:
    """What PROJ does on its own, for engines that do not let us choose.

    rasterio's warp, GDAL and DuckDB all take two CRSs and reach for PROJ
    themselves. The record has to describe the operation they will actually get,
    so this never substitutes a better one -- it *reports* that a better one
    exists, under `better_available_m`, which is the fact a caller needs in
    order to go and install the missing grid.

    Returns the `crs_decisions.transformation` object. There is no transformer
    to hand back: the engine builds its own. ``points`` are the data's own
    coordinates in the source CRS (`sample_points`); without them the answer is
    a guess from the two CRSs' areas of use.
    """
    from pyproj import Transformer

    source_crs, target_crs = _as_crs(source_crs), _as_crs(target_crs)
    chosen = Transformer.from_crs(source_crs, target_crs, always_xy=True)
    if shares_a_datum(source_crs, target_crs):
        return _within_one_datum(pipeline_of(chosen))
    if points is not None and not len(points):
        return _nothing_moved()
    survey = _Survey(source_crs, target_crs, points, chosen)
    if not survey.is_ballpark:
        return _with_outside(survey, {
            "pipeline": survey.pipeline or pipeline_of(chosen),
            "accuracy_m": survey.accuracy,
            "is_ballpark": False,
        })
    record: dict[str, Any] = {
        "pipeline": survey.pipeline or pipeline_of(chosen),
        "accuracy_m": None,
        "is_ballpark": True,
        # Deliberately not "chosen_by": the engine chose, and this module is
        # only reporting. Naming us as the chooser is how a manifest starts
        # describing an operation that never ran.
        "x-mapsmith:chosen_by": "the engine, not MapSmith",
    }
    share = survey.share()
    if share:
        record[BALLPARK_SHARE] = share
    _with_outside(survey, record)
    # "There is no datum shift for these coordinates" and "there is one and
    # this machine has not got it" are different problems with different
    # fixes; only the second is a download.
    better = survey.better_available()
    if better is not None:
        record["better_available_m"] = better
    return record
