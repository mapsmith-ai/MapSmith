"""Raster zonal statistics on the exactextract engine.

exactextract computes exact fractional pixel coverage (no all-in/all-out pixel
approximation) with bounded memory — 10-100x faster than rasterstats-class
implementations. Optional extra: ``pip install mapsmith[raster]``.

CRS discipline: zones are reprojected to the raster CRS before extraction
(mismatched CRS is the single most common silent error in GIS analysis), and
the decision is recorded in the provenance manifest. Output stays in the
raster CRS.
"""

from __future__ import annotations

import re
from typing import Any

import geopandas as gpd
import pandas as pd

from .. import readers, verify
from ..provenance import InputRecord, ProvenanceRecord

VALID_STATS = {
    "count",
    "sum",
    "mean",
    "median",
    "min",
    "max",
    "stdev",
    "variance",
    "majority",
    "minority",
    "variety",
}


def _require():
    try:
        import exactextract
        import rasterio
    except ImportError as exc:
        raise ImportError(
            "zonal_statistics requires the raster extra: pip install mapsmith[raster]"
        ) from exc
    return exactextract, rasterio


def _engine_info() -> dict[str, str]:
    from importlib.metadata import version

    return {"name": "exactextract", "version": version("exactextract")}


def _require_rasterio():
    try:
        import rasterio
    except ImportError as exc:
        raise ImportError(
            "raster inspection requires the raster extra: pip install mapsmith[raster]"
        ) from exc
    return rasterio


def describe(path: str) -> dict[str, Any]:
    """CRS, grid, bands, nodata and per-band statistics of a raster (read-only).

    Statistics are computed on the masked read, so nodata cells are excluded
    from min/max/mean and counted separately — most silent raster errors start
    with metadata nobody looked at, and nodata treated as elevation is the
    canonical one.
    """
    rasterio = _require_rasterio()
    with rasterio.open(path) as ds:
        bands = []
        for index in range(1, ds.count + 1):
            data = ds.read(index, masked=True)
            valid = int(data.count())
            bands.append({
                "band": index,
                "dtype": ds.dtypes[index - 1],
                "nodata": ds.nodatavals[index - 1],
                "valid_cells": valid,
                "nodata_cells": int(data.size - valid),
                "min": float(data.min()) if valid else None,
                "max": float(data.max()) if valid else None,
                "mean": float(data.mean()) if valid else None,
            })
        left, bottom, right, top = ds.bounds
        return {
            "path": str(path),
            "kind": "raster",
            "crs": str(ds.crs) if ds.crs else None,
            "width": ds.width,
            "height": ds.height,
            "band_count": ds.count,
            "resolution": {"x": abs(float(ds.res[0])), "y": abs(float(ds.res[1]))},
            "extent": {
                "minx": float(left),
                "miny": float(bottom),
                "maxx": float(right),
                "maxy": float(top),
            },
            "bands": bands,
        }


def zonal_statistics(
    raster_path: str,
    zones_path: str,
    output_path: str,
    stats: list[str] | None = None,
) -> dict[str, Any]:
    """Statistics of a single-band raster within each vector zone."""
    exactextract, rasterio = _require()
    ops = stats or ["count", "mean", "min", "max"]
    unknown = [s for s in ops if s not in VALID_STATS]
    if unknown:
        raise ValueError(
            f"Unknown statistics {unknown}. Valid: {sorted(VALID_STATS)} "
            "(note: 'stdev', not 'std')"
        )

    zones = readers.read_vector(zones_path)
    if zones.crs is None:
        raise ValueError(readers.no_crs_message(
            zones, f"{zones_path} has no CRS — cannot align zones to the raster."
        ))

    with rasterio.open(raster_path) as ds:
        raster_crs = ds.crs
        record = ProvenanceRecord(
            operation="zonal_statistics",
            parameters={"stats": ops, "bands": ds.count},
            inputs=[
                InputRecord.from_path(raster_path, crs=verify.crs_label(raster_crs)),
                InputRecord.from_path(zones_path, crs=verify.crs_label(zones.crs)),
            ],
            engine=_engine_info(),
        )
        if raster_crs is not None and not verify.same_crs(zones.crs, raster_crs):
            zones = zones.to_crs(raster_crs)
            record.crs_decisions = {
                "analysis_crs": verify.crs_label(raster_crs),
                "reason": "zones reprojected to the raster CRS for exact pixel "
                "alignment; output kept in the raster CRS",
            }
        else:
            record.crs_decisions = {
                "analysis_crs": str(raster_crs),
                "reason": "zones and raster share the same CRS",
            }
        stats_df = exactextract.exact_extract(ds, zones, ops, output="pandas")

    out = gpd.GeoDataFrame(
        pd.concat(
            [zones.reset_index(drop=True), stats_df.reset_index(drop=True)], axis=1
        ),
        geometry=zones.geometry.name,
        crs=zones.crs,
    )
    if str(output_path).endswith(".parquet"):
        out.to_parquet(output_path)
    else:
        out.to_file(output_path)

    # the zone geometries are carried through verbatim, so an invalid input
    # yields an invalid output: mechanical repair applies here
    manifest, extras = verify.audited(
        record,
        output_path,
        operation="zonal_statistics",
        preconditions=verify.verify_loaded_inputs("zonal_statistics", zones_path=zones),
        checks_fn=lambda: verify.verify_vector_output(
            output_path,
            expect_crs=zones.crs,
            expect_count=len(zones),
        ),
    )
    return {
        "output": str(output_path),
        "feature_count": len(out),
        "statistics": ops,
        "provenance": manifest,
        "verified": True,
        **extras,
    }


# Resampling methods that AVERAGE their neighbours. On a categorical raster
# these invent class codes that were never in the data, which is the whole
# reason this operation refuses to have a default.
# Measured against the installed rasterio, not assumed: read() accepts nine of
# the fifteen Resampling members and raises ResamplingAlgorithmError for
# min/max/med/q1/q3/sum, which are warp-only. rasterio has three different
# valid sets (read, warp, overviews) and the intersection is what matters here.
INTERPOLATING_RESAMPLING = {
    "bilinear", "cubic", "cubic_spline", "lanczos", "average", "rms", "gauss",
}
# These pick an existing value instead of deriving one, so a class code
# survives them. On the read path that is exactly two methods.
CATEGORICAL_RESAMPLING = {"nearest", "mode"}
WARP_ONLY_RESAMPLING = {"min", "max", "med", "q1", "q3", "sum"}
# Beyond this many distinct values a raster is treated as continuous and the
# new-code check is skipped: the point is to catch class codes, not elevations.
_CATEGORICAL_MAX_CLASSES = 64


def resample(
    input_path: str,
    output_path: str,
    resolution: float,
    resampling: str,
) -> dict[str, Any]:
    """Resample a raster to a target cell size. The method is REQUIRED, by design.

    Every raster library defaults to nearest neighbour, and the caller who
    wanted a smooth surface silently gets a blocky one; the caller who reaches
    for bilinear on land-cover codes silently gets classes that do not exist.
    Neither failure raises anything, so the choice is the caller's to state.
    """
    rasterio = _require_rasterio()
    from rasterio.enums import Resampling

    if resolution <= 0:
        raise ValueError(f"resolution must be positive, got {resolution}")
    valid = INTERPOLATING_RESAMPLING | CATEGORICAL_RESAMPLING
    if resampling not in valid:
        warp_only = (
            f" '{resampling}' exists in rasterio's Resampling enum but is valid only "
            "for warping, not for the read path this operation uses."
            if resampling in WARP_ONLY_RESAMPLING
            else ""
        )
        raise ValueError(
            f"resampling must be one of {sorted(valid)}, got {resampling!r}.{warp_only} "
            "There is no default on purpose: interpolating methods (bilinear, cubic, "
            "average) derive values between the ones present, which is right for a "
            "continuous surface and wrong for class codes — use nearest or mode there."
        )
    method = getattr(Resampling, resampling)

    with rasterio.open(input_path) as src:
        if src.crs is None:
            raise ValueError(
                f"{input_path} declares no CRS, so a resolution in its units cannot be "
                "interpreted. Assign a CRS first."
            )
        left, bottom, right, top = src.bounds
        # Closed form: the new grid covers the same extent, so its shape follows
        # from the extent and the target cell size. Computed BEFORE the engine
        # runs, then verified against what landed on disk.
        width = max(1, round((right - left) / resolution))
        height = max(1, round((top - bottom) / resolution))
        source_values, categorical = _distinct_values(src)
        record = ProvenanceRecord(
            operation="resample_raster",
            parameters={
                "resolution": resolution,
                "resampling": resampling,
                "target_shape": [height, width],
            },
            inputs=[InputRecord.from_path(input_path, crs=verify.crs_label(src.crs))],
            engine={"name": "rasterio", "version": rasterio.__version__},
        )
        record.crs_decisions = {
            "analysis_crs": verify.crs_label(src.crs),
            "reason": "resampling changes the grid, not the coordinate system; "
            "the target resolution is read in the raster's own CRS units",
        }
        data = src.read(out_shape=(src.count, height, width), resampling=method)
        profile = src.profile.copy()
        profile.update(
            width=width,
            height=height,
            transform=src.transform * src.transform.scale(
                src.width / width, src.height / height
            ),
        )
        # The source's tiling describes the source's grid: carried onto a
        # smaller output GDAL complains and drops it. Let the driver choose.
        for key in ("blockxsize", "blockysize", "tiled"):
            profile.pop(key, None)

    with rasterio.open(output_path, "w", **profile) as dst:
        dst.write(data)

    checks: list[verify.Check] = []
    with rasterio.open(output_path) as out:
        checks.append(
            verify.Check(
                "x-mapsmith:shape_matches_resolution",
                (out.height, out.width) == (height, width),
                f"expected {height}x{width}, got {out.height}x{out.width}",
            )
        )
        checks.append(
            verify.Check(
                "crs_matches",
                verify.same_crs(out.crs, record.inputs[0].crs),
                f"{verify.crs_label(out.crs)}",
            )
        )
        result_values, _ = _distinct_values(out)

    # The check that looks at the VALUES, not at whether the run finished: an
    # interpolating method on a categorical raster produces codes that were
    # never in the input, and nothing else in the stack will say so.
    invented: list[float] = []
    if categorical and resampling in INTERPOLATING_RESAMPLING and result_values is not None:
        invented = sorted(result_values - source_values)
        checks.append(
            verify.Check(
                "x-mapsmith:no_invented_class_codes",
                not invented,
                f"{resampling} introduced codes absent from the input: {invented}"
                if invented
                else "output codes are a subset of the input codes",
                critical=False,
                hint=(
                    "This raster looks categorical (integer, few distinct values) and was "
                    f"resampled with '{resampling}', which averages neighbours. The codes "
                    f"{invented} exist in the result and not in the source: if they mean "
                    "something in your legend, every downstream count and area for those "
                    "classes is fabricated. Use nearest or mode for class codes."
                )
                if invented
                else None,
            )
        )

    manifest = record.add_verification(checks).finish().write_for(output_path)
    verify.enforce(checks, "resample_raster")
    result = {
        "output": str(output_path),
        "resolution": resolution,
        "resampling": resampling,
        "shape": [height, width],
        "provenance": str(manifest),
        "verified": True,
    }
    hinted = verify.advisories(checks)
    if hinted:
        result["warnings"] = hinted
    if invented:
        result["invented_values"] = invented
    return result


def _distinct_values(dataset: Any) -> tuple[set[float] | None, bool]:
    """The distinct values of band 1, and whether the raster looks categorical.

    Categorical here means integer dtype with few distinct values — a heuristic,
    stated as such: it decides whether to RUN a non-critical check, never
    whether to alter data.
    """
    import numpy as np

    if not np.issubdtype(np.dtype(dataset.dtypes[0]), np.integer):
        return None, False
    band = dataset.read(1, masked=True)
    values = {float(v) for v in np.unique(band.compressed())}
    return (values, True) if len(values) <= _CATEGORICAL_MAX_CLASSES else (values, False)


def clip_raster(
    raster_path: str,
    mask_path: str,
    output_path: str,
    all_touched: bool = False,
) -> dict[str, Any]:
    """Clip a raster to the area of a vector mask, with the CRS handled openly.

    ``rasterio.mask`` never looks at a CRS — its documentation states the
    precondition and the code does not enforce it. Three things then happen,
    and only the first is loud: disjoint bounds raise or warn; bounds that
    overlap *numerically* while the CRS differ (metres against US survey feet,
    UTM 32N against 33N) clip a plausible wrong piece of the raster in total
    silence; degrees against metres usually yields an all-nodata output with a
    warning nobody reads. So the mask is reprojected here, deliberately, and
    the decision is recorded.
    """
    rasterio = _require_rasterio()
    from rasterio.mask import mask as rio_mask

    frame = readers.read_vector(mask_path)
    with rasterio.open(raster_path) as src:
        if src.crs is None:
            raise ValueError(
                f"{raster_path} declares no CRS, so a vector mask cannot be placed "
                "on it. Assign a CRS first."
            )
        record = ProvenanceRecord(
            operation="clip_raster",
            parameters={"all_touched": all_touched},
            inputs=[
                InputRecord.from_path(raster_path, crs=verify.crs_label(src.crs)),
                InputRecord.from_path(mask_path, crs=verify.crs_label(frame.crs)),
            ],
            engine={"name": "rasterio", "version": rasterio.__version__},
        )
        pre = verify.verify_loaded_inputs("clip_raster", mask_path=frame)
        if verify.has_critical_failure(pre):
            record.add_verification(pre).finish().write_for(output_path)
            verify.enforce(pre, "clip_raster")
        if verify.same_crs(frame.crs, src.crs):
            record.crs_decisions = {
                "analysis_crs": verify.crs_label(src.crs),
                "reason": "mask and raster already share a CRS; no reprojection needed",
            }
        else:
            frame = frame.to_crs(src.crs)
            record.crs_decisions = {
                "analysis_crs": verify.crs_label(src.crs),
                "reason": (
                    f"mask reprojected from {verify.crs_label(record.inputs[1].crs)} to "
                    "the raster CRS before clipping; rasterio.mask does not check CRS "
                    "and would have clipped the wrong area without saying so"
                ),
            }
        # nodata: rasterio.mask falls back to 0 when the raster declares none,
        # and 0 is a valid elevation, reflectance and temperature. Refuse to
        # let that be implicit.
        nodata = src.nodata
        if nodata is None:
            record.notes.append(
                "the source raster declares no nodata value, so the area outside the "
                "mask is filled with 0 — a legal value in most bands. Consider "
                "declaring nodata on the source before clipping"
            )
        with verify.audit_on_failure(record, output_path, pre):
            data, transform = rio_mask(
                src, list(frame.geometry), crop=True, all_touched=all_touched
            )
            profile = src.profile.copy()
            profile.update(
                height=data.shape[1], width=data.shape[2], transform=transform
            )
            for key in ("blockxsize", "blockysize", "tiled"):
                profile.pop(key, None)
            with rasterio.open(output_path, "w", **profile) as dst:
                dst.write(data)
        source_shape = (src.height, src.width)

    checks: list[verify.Check] = []
    with rasterio.open(output_path) as out:
        checks.append(
            verify.Check(
                "crs_matches",
                verify.same_crs(out.crs, record.inputs[0].crs),
                verify.crs_label(out.crs),
            )
        )
        # A clip can only shrink the grid. Growing means the mask was placed
        # somewhere the raster is not, which is the CRS failure this operation
        # exists to prevent.
        checks.append(
            verify.Check(
                "x-mapsmith:not_larger_than_source",
                out.height <= source_shape[0] and out.width <= source_shape[1],
                f"{out.height}x{out.width} from {source_shape[0]}x{source_shape[1]}",
            )
        )
        band = out.read(1, masked=True)
        valid = int(band.count())
        checks.append(
            verify.Check(
                "result_not_empty",
                valid > 0,
                f"{valid} cells with data",
                critical=False,
                hint=None
                if valid
                else "The clip produced a raster with no data at all. The mask and the "
                "raster overlap in extent but not where the data is — or the mask "
                "covers only nodata cells. Check the two extents before trusting it.",
            )
        )
        result_shape = [out.height, out.width]

    manifest = record.add_verification(checks).finish().write_for(output_path)
    verify.enforce(checks, "clip_raster")
    result = {
        "output": str(output_path),
        "shape": result_shape,
        "valid_cells": valid,
        "provenance": str(manifest),
        "verified": True,
    }
    advisories = verify.advisories(checks)
    if advisories:
        result["warnings"] = advisories
    return result


def reclassify(
    input_path: str,
    output_path: str,
    intervals: list[str],
) -> dict[str, Any]:
    """Reclassify raster values into new codes, with the ranges stated as text.

    Each interval is ``"low:high:new"``, half-open — ``low <= value < high`` —
    so ``["0:100:1", "100:200:2"]`` maps everything under 100 to 1 and
    everything from 100 up to (not including) 200 to 2. Half-open is the only
    convention that tiles the number line without overlap, and the off-by-one
    at the boundary is the classic silent error of this operation: a cell of
    exactly 100 belongs to the second class, and this docstring is the contract.

    Ranges are checked for overlap before anything runs, and cells that fall in
    no interval become nodata and are counted in the manifest — the alternative,
    leaving them at their original value, mixes old codes with new ones in the
    same band and is unreadable afterwards.
    """
    rasterio = _require_rasterio()
    import numpy as np

    parsed: list[tuple[float, float, float]] = []
    for entry in intervals:
        parts = str(entry).split(":")
        if len(parts) != 3:
            raise ValueError(
                f"interval {entry!r} must be 'low:high:new', e.g. '0:100:1' "
                "(low inclusive, high exclusive)"
            )
        try:
            low, high, new = (float(p) for p in parts)
        except ValueError as exc:
            raise ValueError(f"interval {entry!r} has a non-numeric bound") from exc
        if not low < high:
            raise ValueError(f"interval {entry!r}: low must be less than high")
        parsed.append((low, high, new))
    for i, (low_a, high_a, _) in enumerate(parsed):
        for low_b, high_b, _ in parsed[i + 1:]:
            if low_a < high_b and low_b < high_a:
                raise ValueError(
                    f"intervals [{low_a}, {high_a}) and [{low_b}, {high_b}) overlap: "
                    "a value in both would take whichever class was listed first, "
                    "which is a coin toss the caller should not have to know about"
                )

    with rasterio.open(input_path) as src:
        record = ProvenanceRecord(
            operation="reclassify_raster",
            parameters={
                "intervals": [f"{low}:{high}:{new}" for low, high, new in parsed],
                "bounds": "low inclusive, high exclusive",
            },
            inputs=[InputRecord.from_path(input_path, crs=verify.crs_label(src.crs))],
            engine={"name": "rasterio", "version": rasterio.__version__},
        )
        record.crs_decisions = {
            "analysis_crs": verify.crs_label(src.crs),
            "reason": "reclassification changes values, not geometry or CRS",
        }
        band = src.read(1, masked=True)
        nodata_out = -9999.0
        # Plain arrays on purpose: comparisons on a masked array return masked
        # booleans, and indexing with those does not mean what it looks like.
        # The validity mask is carried separately and applied explicitly.
        valid = ~np.ma.getmaskarray(band)
        values = np.ma.getdata(band).astype("float64")
        result_band = np.full(band.shape, nodata_out, dtype="float32")
        assigned = np.zeros(band.shape, dtype=bool)
        for low, high, new in parsed:
            selected = valid & (values >= low) & (values < high)
            result_band[selected] = new
            assigned |= selected
        unmapped = int(np.sum(valid & ~assigned))
        profile = src.profile.copy()
        profile.update(dtype="float32", nodata=nodata_out, count=1)
        for key in ("blockxsize", "blockysize", "tiled"):
            profile.pop(key, None)
        source_shape = (src.height, src.width)

    if unmapped:
        record.notes.append(
            f"{unmapped} cells fell outside every interval and became nodata "
            f"({nodata_out}); they are not left at their original values, which "
            "would mix old codes with new ones in one band"
        )
    with rasterio.open(output_path, "w", **profile) as dst:
        dst.write(result_band, 1)

    checks: list[verify.Check] = []
    with rasterio.open(output_path) as out:
        checks.append(
            verify.Check(
                "shape_preserved",
                (out.height, out.width) == source_shape,
                f"{out.height}x{out.width}",
            )
        )
        checks.append(
            verify.Check(
                "crs_matches",
                verify.same_crs(out.crs, record.inputs[0].crs),
                verify.crs_label(out.crs),
            )
        )
        written = out.read(1, masked=True)
        produced = {float(v) for v in np.unique(written.compressed())}
        declared = {new for _, _, new in parsed}
        # Closed form: every value in the output must be one of the codes the
        # caller asked for. Anything else means the mapping did not do what the
        # intervals say, and a reclassified raster nobody can trust is worse
        # than one that failed.
        checks.append(
            verify.Check(
                "x-mapsmith:values_are_declared_codes",
                produced <= declared,
                f"unexpected codes {sorted(produced - declared)}"
                if produced - declared
                else f"all values in {sorted(declared)}",
            )
        )
        result_shape = [out.height, out.width]

    manifest = record.add_verification(checks).finish().write_for(output_path)
    verify.enforce(checks, "reclassify_raster")
    return {
        "output": str(output_path),
        "shape": result_shape,
        "unmapped_cells": unmapped,
        "codes": sorted(declared),
        "provenance": str(manifest),
        "verified": True,
    }


# Only these names, and only these operators, reach the evaluator. Band
# references are b1..bN; everything else is rejected before anything is read.
_BAND_REFERENCE = re.compile(r"\bb([1-9][0-9]?)\b")
_ALLOWED_EXPRESSION = re.compile(r"^[b0-9+\-*/(). ]+$")


def band_math(input_path: str, output_path: str, expression: str) -> dict[str, Any]:
    """Evaluate an arithmetic expression over a raster's bands (NDVI and friends).

    Bands are referenced as ``b1``, ``b2``, … and the expression may use
    ``+ - * / ** ( )`` and numbers, nothing else — it is matched against a
    regular expression before anything is read, then evaluated over numpy
    arrays with no builtins in scope. ``**`` is allowed on purpose (an index
    that squares a band is ordinary); names, calls and attribute access are
    not.

    Three things are done that a hand-rolled version usually is not, each of
    which is a silent wrong answer waiting:

    * **Declared scale and offset are applied**, and the manifest says so. GDAL
      states that applying them is the caller's job and that ``RasterIO`` will
      not; an index computed on stored digital numbers is a plausible number
      that is not the one asked for.
    * **Arithmetic happens in float64.** Subtracting two ``uint16`` bands wraps
      around at zero — ``red - nir`` where red is larger comes back near 65535,
      silently — and the result of an index built on that is well formed and
      meaningless.
    * **The output is written as float32 with a declared nodata**, rather than
      inheriting the input's integer profile, which would round an index in
      [-1, 1] to zeros and ones on the way to disk.
    """
    rasterio = _require_rasterio()
    import numpy as np

    if not _ALLOWED_EXPRESSION.match(expression):
        raise ValueError(
            f"expression {expression!r} may only contain band references (b1, b2, …), "
            "numbers, the operators + - * / ** and parentheses. Names, function "
            "calls and attribute access are rejected before the file is opened."
        )
    referenced = sorted({int(m) for m in _BAND_REFERENCE.findall(expression)})
    if not referenced:
        raise ValueError(
            f"expression {expression!r} references no band; write them as b1, b2, …"
        )

    with rasterio.open(input_path) as src:
        missing = [b for b in referenced if b > src.count]
        if missing:
            raise ValueError(
                f"expression references band(s) {missing} but {input_path} has "
                f"{src.count} band(s)"
            )
        record = ProvenanceRecord(
            operation="band_math",
            parameters={"expression": expression, "bands_used": referenced},
            inputs=[InputRecord.from_path(input_path, crs=verify.crs_label(src.crs))],
            engine={"name": "rasterio", "version": rasterio.__version__},
        )
        record.crs_decisions = {
            "analysis_crs": verify.crs_label(src.crs),
            "reason": "band arithmetic is per-cell; geometry and CRS are unchanged",
        }
        # float64 before any arithmetic: integer bands wrap around on subtraction.
        namespace: dict[str, Any] = {}
        applied: list[str] = []
        for band_index in referenced:
            data = src.read(band_index, masked=True).astype("float64")
            scale = src.scales[band_index - 1]
            offset = src.offsets[band_index - 1]
            if scale != 1.0 or offset != 0.0:
                data = data * scale + offset
                applied.append(f"b{band_index}: value * {scale} + {offset}")
            namespace[f"b{band_index}"] = data
        if applied:
            record.notes.append(
                "declared scale and offset applied before the expression — "
                + "; ".join(applied)
                + ". GDAL leaves this to the caller, so an index computed on the "
                "stored numbers would have been a plausible wrong answer"
            )
        else:
            record.notes.append(
                "no band declares a scale or offset: the stored values are the "
                "physical ones"
            )
        source_shape = (src.height, src.width)
        profile = src.profile.copy()

    with np.errstate(divide="ignore", invalid="ignore"):
        computed = eval(
            expression, {"__builtins__": {}}, namespace
        )
    computed = np.ma.masked_invalid(np.ma.asarray(computed))
    nodata_out = -9999.0
    profile.update(count=1, dtype="float32", nodata=nodata_out)
    for key in ("blockxsize", "blockysize", "tiled"):
        profile.pop(key, None)
    with rasterio.open(output_path, "w", **profile) as dst:
        dst.write(computed.filled(nodata_out).astype("float32"), 1)

    checks: list[verify.Check] = []
    with rasterio.open(output_path) as out:
        checks.append(
            verify.Check(
                "shape_preserved",
                (out.height, out.width) == source_shape,
                f"{out.height}x{out.width}",
            )
        )
        checks.append(
            verify.Check(
                "x-mapsmith:written_as_float",
                out.dtypes[0].startswith("float"),
                out.dtypes[0],
            )
        )
        band = out.read(1, masked=True)
        valid = int(band.count())
        invalid = int(band.size - valid)
        checks.append(
            verify.Check(
                "result_not_empty",
                valid > 0,
                f"{valid} of {band.size} cells carry a value",
                critical=False,
                hint=None
                if valid
                else "Every cell is nodata: the expression divided by zero or "
                "operated on nodata everywhere. Check the bands' nodata values.",
            )
        )
        stats = (
            {"min": float(band.min()), "max": float(band.max()), "mean": float(band.mean())}
            if valid
            else {}
        )

    manifest = record.add_verification(checks).finish().write_for(output_path)
    verify.enforce(checks, "band_math")
    result = {
        "output": str(output_path),
        "expression": expression,
        "bands_used": referenced,
        "nodata_cells": invalid,
        "provenance": str(manifest),
        "verified": True,
        **stats,
    }
    if applied:
        result["scale_offset_applied"] = applied
    advisories = verify.advisories(checks)
    if advisories:
        result["warnings"] = advisories
    return result
