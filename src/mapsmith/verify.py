"""Deterministic output verification — MapSmith's core promise.

Every operation's output is checked against explicit pre/postconditions
(CRS discipline, geometry validity, count and extent invariants). Results are
recorded in the provenance manifest; critical failures raise instead of
silently returning wrong data. External deterministic signals beat LLM
self-critique — so none of this involves a model.
"""

from __future__ import annotations

import itertools
import os
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import geopandas as gpd


class VerificationError(RuntimeError):
    """An output failed a critical deterministic check. The manifest still records it."""


UNKNOWN_CRS = "unknown"


def probe_crs(path: str) -> str:
    """CRS of an existing dataset from metadata only (no data scan, never raises).

    GeoParquet via the pyarrow ``geo`` schema metadata (missing/null crs means
    OGC:CRS84 per spec), rasters via rasterio when installed, everything else
    via pyogrio. Returns ``"unknown"`` whenever the CRS cannot be determined.
    """
    lower = str(path).lower()
    try:
        if lower.endswith(".parquet"):
            return _probe_geoparquet_crs(str(path))
        if lower.endswith((".tif", ".tiff")):
            try:
                import rasterio
            except ImportError:
                return UNKNOWN_CRS
            with rasterio.open(path) as ds:
                return str(ds.crs) if ds.crs else UNKNOWN_CRS
        import pyogrio

        crs = pyogrio.read_info(str(path)).get("crs")
        return str(crs) if crs else UNKNOWN_CRS
    except Exception:  # noqa: BLE001 — probing must never break the caller
        return UNKNOWN_CRS


def crs_label(crs: Any) -> str:
    """Short, stable label for a CRS: 'EPSG:32632' rather than 2.5 KB of PROJJSON.

    `str(crs)` returns the EPSG string for a CRS read from a GeoPackage but the
    full PROJJSON document for one read from GeoParquet — the canonical format
    — so manifests and check details grew a JSON blob per field depending on
    the input format alone.
    """
    if crs is None:
        return UNKNOWN_CRS
    try:
        epsg = crs.to_epsg()
    except AttributeError:
        return str(crs)
    return f"EPSG:{epsg}" if epsg else (getattr(crs, "name", None) or str(crs))


NATIVE_GEO_TYPES = ("Geometry", "Geography")


def native_geometry_column(path: str) -> tuple[str, str] | None:
    """Column name and CRS declaration of a Parquet file that stores geometry in
    Parquet's own ``GEOMETRY``/``GEOGRAPHY`` logical types, or None.

    That storage is what GeoParquet 2.0 requires, and such a file may carry no
    ``geo`` metadata key at all — the key is optional in 2.0. Files like this are
    already produced by DuckDB (``geoparquet_version 'NONE'`` or ``'BOTH'``), so
    "a Parquet file with geometry in it" no longer implies a ``geo`` key, and
    reading one as CRS-less would refuse valid work for a wrong reason (#23).
    """
    import json

    import pyarrow.parquet as pq

    try:
        schema = pq.ParquetFile(path).schema
    except Exception:  # noqa: BLE001 — probing must never break the caller
        return None
    for index in range(len(schema)):
        column = schema.column(index)
        try:
            described = json.loads(column.logical_type.to_json())
        except Exception:  # noqa: BLE001, S112 — a type with no JSON form is not geometry
            continue
        if described.get("Type") in NATIVE_GEO_TYPES:
            return column.name, described.get("crs") or ""
    return None


def native_crs_declaration(path: str, declaration: str) -> str:
    """Resolve a native Parquet CRS declaration to a label, or ``unknown``.

    The Parquet geospatial spec allows several forms, and MapSmith has met four
    of them in the wild: absent (the spec's default, ``OGC:CRS84``), an authority
    string (``EPSG:3857``), ``projjson:<key>`` pointing into the file's key-value
    metadata, and — what DuckDB writes — a whole PROJJSON document inline.

    ``srid:<n>`` is deliberately NOT resolved. The spec defines it as a numeric
    spatial reference identifier and names no authority (its own example is
    ``srid:0``), so reading it as EPSG:<n> would be MapSmith inventing a
    coordinate system and recording it as fact — the exact bug fixed in 0.2.1
    when ``crs: null`` was being read as CRS84. The file is therefore reported
    as having no CRS and refused by the CRS precondition.

    What is missing, and is not claimed anywhere: the refusal does not carry
    the ``srid:`` declaration back to the caller, so the agent is told the file
    has no CRS while the file visibly has a ``crs`` field. Surfacing the reason
    is the honest finish to this.
    """
    from pyproj import CRS

    if not declaration:
        return crs_label(CRS.from_user_input("OGC:CRS84"))
    if declaration.lstrip().startswith("{"):
        return crs_label(CRS.from_json(declaration))
    if declaration.lower().startswith("projjson:"):
        import pyarrow.parquet as pq

        key = declaration.split(":", 1)[1].encode()
        document = (pq.ParquetFile(path).metadata.metadata or {}).get(key)
        return crs_label(CRS.from_json(document.decode())) if document else UNKNOWN_CRS
    if declaration.lower().startswith("srid:"):
        return UNKNOWN_CRS
    return crs_label(CRS.from_user_input(declaration))


def _probe_geoparquet_crs(path: str) -> str:
    import json

    import pyarrow.parquet as pq

    metadata = pq.read_schema(path).metadata or {}
    geo = metadata.get(b"geo")
    if not geo:
        # No `geo` key does not mean no geometry: GeoParquet 2.0 stores it in
        # Parquet's own logical types and makes the key optional.
        native = native_geometry_column(path)
        return native_crs_declaration(path, native[1]) if native else UNKNOWN_CRS
    geo_meta = json.loads(geo)
    column = geo_meta.get("primary_column", "geometry")
    spec = geo_meta.get("columns", {}).get(column, {})
    # The GeoParquet spec distinguishes two cases that must NOT be conflated:
    # the "crs" field being ABSENT means OGC:CRS84, while an explicit null
    # means "undefined or unknown". GeoPandas writes null for crs=None and
    # reads it back as None, so treating null as CRS84 would have MapSmith
    # invent a coordinate system and record it in the manifest as fact — the
    # worst possible failure for a provenance product, and it defeated the
    # CRS precondition in the canonical format.
    if "crs" not in spec:
        crs = "OGC:CRS84"
    elif spec["crs"] is None:
        return UNKNOWN_CRS
    else:
        crs = spec["crs"]
    from pyproj import CRS

    parsed = CRS.from_user_input(crs)
    epsg = parsed.to_epsg()
    return f"EPSG:{epsg}" if epsg else parsed.name


@dataclass
class Check:
    """One deterministic verification result.

    ``name`` is a stable code an agent can branch on; ``hint`` says what to do
    about a failure. Benchmarks put the bulk of GIS-agent failures at execution
    time (wrong parameters, outputs that exist but are meaningless), so a check
    that only reports *that* something is wrong wastes the diagnosis it already
    has.
    """

    name: str
    passed: bool
    detail: str
    critical: bool = True
    hint: str | None = None
    # which argument the check is about: without it two inputs produce two
    # identically named entries that a consumer indexing by name would collapse
    argument: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


MAX_REPAIR_ROUNDS = 2


def _gpkg_layers(path: str) -> list[str] | None:
    """The layer names of a container, or None when they cannot be listed.

    None and [] must stay distinct: an empty list means "listed, nothing in
    there", while None means "we do not know what is in there" — and the only
    safe thing to do with a container of unknown contents is refuse to rewrite
    it. Collapsing the failure into [] made the caller's fail-closed branch
    unreachable, so an unlistable GeoPackage looked exactly like a
    single-layer one and the repair went ahead.
    """
    with suppress(Exception):
        from pyogrio import list_layers

        return [str(row[0]) for row in list_layers(path)]
    return None


def _read_vector(path: str):
    """Read a dataset for verification.

    GeoPackages can hold many layers, and GDAL hands back the *first* one by
    default — which would verify a layer the operation never wrote and mark an
    unchecked output as verified. MapSmith's writers name the layer after the
    file stem, so prefer that one when it is there.

    A Parquet file with no ``geo`` metadata is read as a plain table rather
    than refused: DuckDB writes exactly that for a zero-row result, and
    raising here would kill the writer *before* it could record why the output
    is empty — losing the manifest for the very case the checks exist to
    explain.
    """
    lower = str(path).lower()
    if lower.endswith(".parquet"):
        try:
            return gpd.read_parquet(path)
        except ValueError as exc:
            if "geo metadata" not in str(exc).lower():
                raise
            import pandas as pd

            table = pd.read_parquet(path)
            return gpd.GeoDataFrame(table, geometry=gpd.GeoSeries([], dtype="geometry"))
    if lower.endswith(".gpkg"):
        stem = Path(path).stem
        if stem in (_gpkg_layers(path) or []):
            return gpd.read_file(path, layer=stem)
    return gpd.read_file(path)


def verify_loaded_inputs(operation: str, **frames: Any) -> list[Check]:
    """Per-input preconditions on frames the engine has ALREADY read.

    A missing CRS is refused here: metric maths on unknown units produces
    numbers that look fine and mean nothing. An empty input is reported but
    allowed — it is legitimate, just rarely intended.

    Pairwise checks live in :func:`verify_input_pairs` because callers need
    these *before* aligning coordinate systems and those *after*.
    """
    checks: list[Check] = []
    for arg, gdf in frames.items():
        if gdf is None:
            continue
        has_crs = gdf.crs is not None
        checks.append(
            Check(
                "input_crs_present",
                has_crs,
                f"'{arg}': {gdf.crs if has_crs else 'no CRS'}",
                argument=arg,
                hint=None if has_crs else
                f"'{arg}' has no coordinate reference system, so {operation} cannot "
                "know what its numbers mean. Assign the correct CRS to the source "
                "dataset (reproject_layer cannot invent one).",
            )
        )
        empty = len(gdf) == 0
        checks.append(
            Check(
                "input_not_empty",
                not empty,
                f"'{arg}': {len(gdf)} features",
                critical=False,
                argument=arg,
                hint=(
                    f"'{arg}' has no features, so {operation} can only produce an "
                    "empty result. Check the dataset, or the step that produced it."
                ) if empty else None,
            )
        )
    return checks


def verify_input_pairs(operation: str, **frames: Any) -> list[Check]:
    """Pairwise preconditions: can these inputs possibly interact at all?

    Every pair is checked, not just the first two: a check that silently
    ignored a third input would claim more than it verified.
    """
    checks: list[Check] = []
    usable = {
        arg: gdf
        for arg, gdf in frames.items()
        if gdf is not None and gdf.crs is not None and len(gdf)
    }
    for (a_arg, a_gdf), (b_arg, b_gdf) in itertools.combinations(usable.items(), 2):
        pair = f"{a_arg}+{b_arg}"
        if not a_gdf.crs.equals(b_gdf.crs):
            # comparing extents across coordinate systems is meaningless; say so
            # instead of returning a check that verified nothing
            checks.append(
                Check(
                    "inputs_comparable",
                    False,
                    f"'{a_arg}' is {a_gdf.crs} and '{b_arg}' is {b_gdf.crs}",
                    critical=False,
                    argument=pair,
                    hint=f"'{a_arg}' and '{b_arg}' are in different coordinate "
                    f"systems, so their extents cannot be compared before "
                    f"{operation} aligns them.",
                )
            )
            continue
        a = tuple(float(v) for v in a_gdf.total_bounds)
        b = tuple(float(v) for v in b_gdf.total_bounds)
        disjoint = a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1]
        checks.append(
            Check(
                "inputs_may_intersect",
                not disjoint,
                f"'{a_arg}' bounds {[round(v, 4) for v in a]} vs "
                f"'{b_arg}' bounds {[round(v, 4) for v in b]}",
                critical=False,
                argument=pair,
                hint=(
                    f"The extents of '{a_arg}' and '{b_arg}' do not overlap, so "
                    f"{operation} will produce an empty result. They probably "
                    "cover different areas, or one of them is mis-georeferenced."
                ) if disjoint else None,
            )
        )
    return checks


def verify_vector_output(
    output_path: str,
    *,
    expect_crs: str | None = None,
    expect_count: int | None = None,
    max_count: int | None = None,
    expect_geometry: set[str] | None = None,
    within_bounds: tuple[float, float, float, float] | None = None,
    bounds_margin: float = 1e-6,
    on_empty: str = "ignore",
) -> list[Check]:
    """Run postcondition checks on a vector output. Returns all checks (pass and fail)."""
    if on_empty not in ("ignore", "warn", "fail"):
        raise ValueError(f"on_empty must be ignore/warn/fail, got {on_empty!r}")
    checks: list[Check] = []
    gdf = _read_vector(output_path)

    if on_empty != "ignore":
        checks.append(
            Check(
                "result_not_empty",
                len(gdf) > 0,
                f"{len(gdf)} features",
                # "fail" where emptiness is provably a bug (a buffer of a
                # non-empty layer cannot be empty); "warn" where it is
                # legitimate but suspicious (a clip whose geometries genuinely
                # miss each other) — silently passing it off as success is what
                # we refuse to do either way.
                critical=on_empty == "fail",
                hint=None if len(gdf) else
                "The operation ran but matched nothing. A file exists and is valid, "
                "so downstream steps would silently work on an empty layer: check the "
                "inputs' extents and coordinate systems before trusting this result.",
            )
        )

    checks.append(
        Check(
            "crs_present",
            gdf.crs is not None,
            str(gdf.crs) if gdf.crs else "output has no CRS",
        )
    )
    if expect_crs is not None and gdf.crs is not None:
        same = gdf.crs.equals(expect_crs) if hasattr(gdf.crs, "equals") else str(
            gdf.crs
        ) == str(expect_crs)
        checks.append(
            Check("crs_matches", bool(same), f"expected {expect_crs}, got {gdf.crs}")
        )

    if len(gdf) > 0:
        invalid = int((~gdf.geometry.is_valid).sum())
        checks.append(
            Check(
                "geometry_valid",
                invalid == 0,
                f"{invalid}/{len(gdf)} invalid geometries" if invalid else "all valid",
            )
        )
        empty = int(gdf.geometry.is_empty.sum())
        checks.append(
            Check(
                "geometry_not_empty",
                empty == 0,
                f"{empty}/{len(gdf)} empty geometries" if empty else "none empty",
                critical=False,
                hint=(
                    "Every feature kept its row but lost its geometry — the classic "
                    "sign of a distance with the wrong sign or magnitude (a negative "
                    "buffer larger than the features erodes them away). Check the "
                    "parameter and the CRS units."
                ) if empty == len(gdf) else None,
            )
        )

    if expect_count is not None:
        checks.append(
            Check(
                "feature_count_exact",
                len(gdf) == expect_count,
                f"expected {expect_count}, got {len(gdf)}",
            )
        )
    if max_count is not None:
        checks.append(
            Check(
                "feature_count_bounded",
                len(gdf) <= max_count,
                f"expected <= {max_count}, got {len(gdf)}",
            )
        )

    if expect_geometry is not None and len(gdf) > 0:
        found = set(gdf.geom_type.dropna().unique())
        checks.append(
            Check(
                "geometry_types",
                found.issubset(expect_geometry),
                f"expected subset of {sorted(expect_geometry)}, got {sorted(found)}",
            )
        )

    if within_bounds is not None and len(gdf) > 0:
        minx, miny, maxx, maxy = gdf.total_bounds
        exp = within_bounds
        m = bounds_margin
        inside = (
            minx >= exp[0] - m and miny >= exp[1] - m and maxx <= exp[2] + m and maxy <= exp[3] + m
        )
        checks.append(
            Check(
                "extent_within_expected",
                bool(inside),
                f"output bounds {[round(v, 6) for v in (minx, miny, maxx, maxy)]} "
                f"vs expected {[round(v, 6) for v in exp]} (margin {m})",
                critical=False,
            )
        )

    return checks


def _repair_invalid_geometry(output_path: str) -> str:
    """make_valid() the output, replacing the original only once the new file
    is complete.

    Three things this must never do: write through the original (a failed write
    would destroy the user's data — GDAL truncates before it writes), assume
    the active geometry column is called "geometry" (engines and ogr2ogr
    routinely name it "geom"), and rewrite a container holding layers it did
    not read — replacing a multi-layer GeoPackage with a single-layer temp file
    would silently delete the rest of the user's project.
    """
    path = Path(output_path)
    suffix = path.suffix.lower()
    if suffix not in (".parquet", ".gpkg"):
        raise ValueError(
            f"'{suffix or path.name}' is not a single-file format: rewriting it "
            "cannot be made atomic, so the repair was skipped rather than risk "
            "the output. Convert to GeoParquet or fix the geometry upstream."
        )
    layers = _gpkg_layers(output_path) if suffix == ".gpkg" else []
    if layers is None:
        raise ValueError(
            f"the layers of {path.name} could not be listed, so rewriting it "
            "could drop layers this operation never read; repair skipped."
        )
    if len(layers) > 1:
        raise ValueError(
            f"{path.name} holds {len(layers)} layers ({', '.join(layers)}): "
            "repairing it would rewrite the whole container and drop the layers "
            "this operation did not write. Write the output to its own file "
            "(GeoParquet is the canonical format) or fix the geometry upstream."
        )
    gdf = _read_vector(output_path)
    column = gdf.geometry.name
    fixed = gdf.copy()
    fixed[column] = gdf.geometry.make_valid()
    tmp = path.with_name(f"{path.stem}.mapsmith-repair{path.suffix}")
    try:
        if suffix == ".parquet":
            fixed.to_parquet(tmp)
        else:
            fixed.to_file(tmp, layer=layers[0] if layers else path.stem, driver="GPKG")
        os.replace(tmp, output_path)  # the original survives any failure above
    finally:
        if tmp.exists():
            tmp.unlink()
    return f"make_valid() on geometry column '{column}', written atomically"


# Failed check -> deterministic repair. Only mechanical, engine-level fixes
# belong here: anything needing judgement (empty results, wrong CRS choice)
# must reach the agent as a hint instead of being silently "fixed".
_REPAIRS = {"geometry_valid": _repair_invalid_geometry}


def repair_and_reverify(
    output_path: str,
    checks: list[Check],
    *,
    operation: str,
    reverify,
    max_rounds: int = MAX_REPAIR_ROUNDS,
) -> tuple[list[Check], list[dict[str, Any]]]:
    """Apply deterministic repairs to failed checks, re-verifying after each.

    Returns the final checks and an audit of every attempt, which the caller
    records in the manifest — a repaired output must never look like one that
    was right the first time, and an attempt that did NOT work must not claim
    it did. Bounded by max_rounds so a repair that fails to converge stops
    loudly instead of looping.
    """
    attempts: list[dict[str, Any]] = []
    # a repair that raised will raise again on the same input: retrying it would
    # burn the budget without ever changing the outcome
    impossible: set[str] = set()
    for round_no in range(1, max_rounds + 1):
        repairable = [
            c
            for c in checks
            if not c.passed and c.critical and c.name in _REPAIRS
            and c.name not in impossible
        ]
        if not repairable:
            break
        this_round: list[dict[str, Any]] = []
        for check in repairable:
            try:
                action, error = _REPAIRS[check.name](output_path), None
            except Exception as exc:  # noqa: BLE001 — a failed repair is an audit entry
                action, error = None, f"{type(exc).__name__}: {exc}"
                impossible.add(check.name)
            entry = {
                "round": round_no,
                "check": check.name,
                "operation": operation,
                "action": action,
                "error": error,
                "resolved": False,
            }
            attempts.append(entry)
            this_round.append(entry)
        try:
            checks = reverify()
        except Exception as exc:  # noqa: BLE001 — verification must not mask the audit
            for entry in this_round:
                entry["error"] = entry["error"] or (
                    f"re-verification failed: {type(exc).__name__}: {exc}"
                )
            break
        passed = {c.name: c.passed for c in checks}
        for entry in this_round:
            # resolved means "the check we targeted now passes" — not "the whole
            # output is clean", which would mislabel a repair that did work
            entry["resolved"] = bool(passed.get(entry["check"]))
    return checks, attempts


def has_critical_failure(checks: list[Check]) -> bool:
    """True when at least one critical check failed (i.e. enforce would raise)."""
    return any(not c.passed and c.critical for c in checks)


def result_extras(
    checks: list[Check], repairs: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Optional keys a writer adds to its result.

    Repairs are reported as loudly as warnings: MapSmith rewriting the user's
    geometry is exactly the kind of thing that must not live only in a file the
    agent has to go and read.
    """
    extras: dict[str, Any] = {}
    warnings = advisories(checks)
    if warnings:
        extras["warnings"] = warnings
    if repairs:
        extras["repairs"] = [
            {k: r[k] for k in ("check", "action", "resolved") if k in r} for r in repairs
        ]
    return extras


def advisories(checks: list[Check]) -> list[dict[str, Any]]:
    """Non-critical failed checks, for the tool result.

    A warning that only lands in the manifest is a warning the agent has to go
    looking for; returning it inline is what makes it actionable.
    """
    return [
        {"check": c.name, "detail": c.detail, "hint": c.hint}
        for c in checks
        if not c.passed and not c.critical
    ]


@contextmanager
def audit_on_failure(record: Any, output_path: str, preconditions: list[Check]):
    """Persist the preconditions if the operation itself raises.

    The diagnosis this module exists to produce ("these extents cannot
    overlap") is most valuable exactly when the engine blows up, so it must not
    be lost with the exception.
    """
    try:
        yield
    except Exception:
        with suppress(Exception):
            record.add_verification(preconditions).finish().write_for(output_path)
        raise


def audited(
    record: Any,
    output_path: str,
    *,
    operation: str,
    preconditions: list[Check] | None = None,
    checks_fn,
    repair: bool = True,
) -> tuple[str, dict[str, Any]]:
    """Verify, repair what is mechanically repairable, persist, then enforce.

    Returns the manifest path and the extra keys for the writer's result. The
    order — manifest first, enforce second — is the invariant this helper
    exists to make unmissable.
    """
    with audit_on_failure(record, output_path, preconditions or []):
        checks = checks_fn()
    repairs: list[dict[str, Any]] = []
    if repair:
        checks, repairs = repair_and_reverify(
            output_path, checks, operation=operation, reverify=checks_fn
        )
    all_checks = (preconditions or []) + checks
    record.add_verification(all_checks)
    if repairs:
        record.add_repairs(repairs)
    manifest = record.finish().write_for(output_path)
    enforce(all_checks, operation, repairs)
    return str(manifest), result_extras(all_checks, repairs)


def enforce(
    checks: list[Check], operation: str, repairs: list[dict[str, Any]] | None = None
) -> None:
    """Raise VerificationError if any critical check failed.

    When repairs were attempted, the message says so: an error about an output
    MapSmith itself rewrote must not read like an error about the original.
    """
    failed = [c for c in checks if not c.passed and c.critical]
    if failed:
        details = "; ".join(f"{c.name}: {c.detail}" for c in failed)
        hints = " ".join(c.hint for c in failed if c.hint)
        repaired = ""
        if repairs:
            attempted = ", ".join(sorted({r["check"] for r in repairs}))
            repaired = (
                f"MapSmith already attempted deterministic repair of [{attempted}] "
                "on this output; this is what remained. "
            )
        subject = (
            "input preconditions"
            if all(c.name.startswith("input") for c in failed)
            else "output"
        )
        raise VerificationError(
            f"{operation} {'failed' if subject == 'input preconditions' else 'output failed'}"
            f" deterministic verification ({subject}) — {details}. "
            + (f"{hints} " if hints else "")
            + repaired
            + "The provenance manifest records the full check list."
        )
