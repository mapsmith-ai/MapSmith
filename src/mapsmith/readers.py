"""The one place MapSmith opens a vector dataset.

Why it has to be one place. GeoParquet 2.0 moved geometry into Parquet's own
``GEOMETRY``/``GEOGRAPHY`` logical types and demoted the ``geo`` metadata key to
optional, so "open a vector file" grew a second branch. In 0.2.2 that branch was
added to two of the six read paths and not to the other four (#28): the same
file that ``describe_layer`` opened, ``preview_map`` refused, in the same
session. A capability that lives in one function cannot be half-applied; one
that is copied into six call sites cannot stay applied.

The other half of #28 was mixing up two different questions. *Resolving* a CRS
declaration answers "what coordinate system is this", and its failure must be
honest — ``None`` plus a reason. *Labelling* a CRS answers "what do we write in
the manifest", and it may compress. Feeding a label back in as if it were a CRS
made a file that states its CRS read as CRS-less, whenever the CRS had no EPSG
code and pyproj happened to name it ``unknown``. So resolution lives here and
returns pyproj objects; presentation lives in :func:`mapsmith.verify.crs_label`
and returns strings, and neither calls the other.
"""

from __future__ import annotations

import json
import os
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

import geopandas as gpd

from . import crs_policy, limits

if TYPE_CHECKING:
    from pyproj import CRS

# Where an unresolvable CRS declaration leaves its explanation, so the CRS
# precondition can say *why* a file with a visible `crs` field reads as CRS-less
# instead of just reporting "no CRS" and leaving the agent to guess.
CRS_REASON = "mapsmith_crs_reason"

NATIVE_GEO_TYPES = ("Geometry", "Geography")


def native_geometry_columns(path: str) -> list[tuple[str, str]]:
    """Every ``GEOMETRY``/``GEOGRAPHY`` column of a Parquet file, with its raw
    CRS declaration, in schema order. Empty when there are none."""
    import pyarrow.parquet as pq

    try:
        schema = pq.ParquetFile(path).schema
    except Exception:  # noqa: BLE001 — probing must never break the caller
        return []
    found = []
    for index in range(len(schema)):
        column = schema.column(index)
        try:
            described = json.loads(column.logical_type.to_json())
        except Exception:  # noqa: BLE001, S112 — a type with no JSON form is not geometry
            continue
        if described.get("Type") in NATIVE_GEO_TYPES:
            found.append((column.name, described.get("crs") or ""))
    return found


def native_geometry_column(path: str) -> tuple[str, str] | None:
    """Column name and raw CRS declaration of a Parquet file that stores geometry
    in Parquet's own ``GEOMETRY``/``GEOGRAPHY`` logical types, or None.

    That storage is what GeoParquet 2.0 requires, and such a file may carry no
    ``geo`` metadata key at all — the key is optional in 2.0. Files like this are
    already produced by DuckDB (``geoparquet_version 'NONE'`` or ``'BOTH'``) and
    by recent GDAL, so "a Parquet file with geometry in it" no longer implies a
    ``geo`` key, and reading one as CRS-less would refuse valid work for a wrong
    reason (#23).
    """
    found = native_geometry_columns(path)
    return found[0] if found else None


def _refuse_declared(path: str) -> None:
    """Refuse a file whose declared coordinate system names a file or a host, before opening it."""
    crs_policy.refuse_declared_in_file(path)


def _checked(frame: Any, path: str) -> Any:
    """The frame a reader returns, refused when the CRS it carries names a file or a host.

    GeoParquet's CRS reaches the frame through geopandas, never through
    `native_crs`; GDAL's drivers carry the file's WKT the same way. One gate
    for every read path (`crs_policy`, 2026-10-23).
    """
    crs = getattr(frame, "crs", None)
    if crs is not None:
        why = crs_policy.reason_to_refuse(crs)
        if why:
            raise ValueError(f"{path}: the file's coordinate system is refused: {why}.")
    return frame


def _admitted(crs: Any) -> tuple[Any, str | None]:
    """A parsed CRS, or the reason the policy refuses it: a grid or init file it names.

    The CRS a file declares is data the file's author wrote, and PROJ reads
    grid files by the path it finds there (`crs_policy`, 2026-10-23).
    """
    why = crs_policy.reason_to_refuse(crs)
    if why:
        return None, f"the file's coordinate system is refused: {why}."
    return crs, None


def native_crs(path: str, declaration: str) -> tuple[CRS | None, str | None]:
    """Resolve a native Parquet CRS declaration: ``(CRS, None)`` or ``(None, why)``.

    The Parquet geospatial spec allows several forms, and MapSmith has met four
    of them in the wild: absent (the spec's default, ``OGC:CRS84``), an authority
    string (``EPSG:3857``), ``projjson:<key>`` pointing into the file's key-value
    metadata, and — what DuckDB writes — a whole PROJJSON document inline.

    ``srid:<n>`` is deliberately NOT resolved. The spec defines it as a numeric
    spatial reference identifier and names no authority (its own example is
    ``srid:0``), so reading it as ``EPSG:<n>`` would be MapSmith inventing a
    coordinate system and recording it as fact — the exact bug fixed in 0.2.1
    when ``crs: null`` was being read as CRS84.

    Every refusal carries its reason. Returning a bare ``None`` told the agent
    "this file has no CRS" about a file whose schema visibly has a ``crs``
    field, which is true of the result and false of the file. A malformed
    declaration is refused the same way rather than allowed to raise: a raw
    ``pyproj.CRSError`` reaching the caller beats MapSmith's own message, which
    is what the precondition ordering exists to prevent.
    """
    from pyproj import CRS
    from pyproj.exceptions import CRSError

    text = declaration.strip()
    if not text:
        # Absent is not unknown: the spec's default is OGC:CRS84.
        return CRS.from_user_input("OGC:CRS84"), None
    if text.startswith("{"):
        try:
            return _admitted(CRS.from_json(text))
        except (CRSError, ValueError) as exc:
            return None, (
                "the file carries an inline PROJJSON coordinate system that PROJ "
                f"cannot read ({exc}). The file needs rewriting with a CRS its own "
                "toolchain can produce; MapSmith will not guess at the intent."
            )
    if text.lower().startswith("projjson:"):
        import pyarrow.parquet as pq

        key = text.split(":", 1)[1]
        document = (pq.ParquetFile(path).metadata.metadata or {}).get(key.encode())
        if document is None:
            return None, (
                f"the file declares its coordinate system as '{text}', but there is "
                f"no '{key}' entry in its key-value metadata to point at. The file "
                "is internally inconsistent and needs rewriting at the source."
            )
        try:
            return _admitted(CRS.from_json(document.decode()))
        except (CRSError, ValueError, UnicodeDecodeError) as exc:
            return None, (
                f"the file's '{key}' metadata entry is not a coordinate system PROJ "
                f"can read ({exc})."
            )
    if text.lower().startswith("srid:"):
        return None, (
            f"the file declares its coordinate system as '{text}'. The Parquet "
            "geospatial spec defines srid as a bare numeric identifier and names no "
            "authority for it (its own example is 'srid:0'), so MapSmith will not "
            f"read it as EPSG:{text.split(':', 1)[1]} — that would be inventing a "
            "coordinate system and recording it as fact. Rewrite the file with an "
            "authority string, or assign the CRS explicitly if you know which one "
            "it means."
        )
    # Before PROJ parses it: `+init=<file>` is read during parsing.
    if why := crs_policy.reason_to_refuse_text(text):
        return None, f"the file's coordinate system is refused: {why}."
    try:
        return _admitted(CRS.from_user_input(text))
    except (CRSError, ValueError) as exc:
        return None, (
            f"the file declares its coordinate system as '{text}', which PROJ does "
            f"not recognise ({exc})."
        )


def gpkg_layers(path: str) -> list[str] | None:
    """The layer names of a container, or None when they cannot be listed.

    None and [] must stay distinct: an empty list means "listed, nothing in
    there", while None means "we do not know what is in there" — and the only
    safe thing to do with a container of unknown contents is refuse to rewrite
    it. Collapsing the failure into [] made the caller's fail-closed branch
    unreachable, so an unlistable GeoPackage looked exactly like a single-layer
    one and the repair went ahead.
    """
    with suppress(Exception):
        from pyogrio import list_layers

        return [str(row[0]) for row in list_layers(path)]
    return None


def ambiguous_layers(path: str) -> list[str] | None:
    """Layer names of a MULTI-layer OGR source, or None when there is nothing
    to choose: single layer, unlistable, or not an OGR path at all.

    Despite its name, :func:`gpkg_layers` lists any OGR source; this wrapper
    only adds the question that matters here — is there more than one?
    """
    if str(path).lower().endswith(".parquet"):
        return None
    layers = gpkg_layers(path)
    return layers if layers and len(layers) > 1 else None


def refuse_ambiguous_container(path: str) -> None:
    """Raise for a multi-layer container nobody chose a layer of (issue #29).

    GDAL's default — the first layer — answers a question the caller never
    asked, and the manifest could not honestly record which data produced the
    numbers. Refusing is the only answer every component can give
    consistently; the message tells the agent how to choose instead.
    """
    layers = ambiguous_layers(path)
    if not layers:
        return
    shown = ", ".join(layers[:8]) + (", ..." if len(layers) > 8 else "")
    raise ValueError(
        f"{path} holds {len(layers)} layers ({shown}) and no layer was chosen. "
        "MapSmith will not pick one for you: the format's default is simply the "
        "first layer, which may not be the one you mean, and the provenance "
        "manifest could not honestly say which data produced the numbers. "
        "Inspect the container with describe_dataset, which lists the layers, "
        "then extract the one you mean into its own dataset: "
        "run_operation(operation='extract_layer', arguments={'input_path': "
        f"'{path}', 'layer': '<name>', 'output_path': ...}}). It has no tool of "
        "its own, like most of the catalogue. For a condition on the rows as "
        "well, run_sql can do both at once."
    )


def _native_columns_when_geo_crs_is_absent(path: str) -> tuple[list[tuple[str, str]], str]:
    """Native geometry columns whose declaration must win over the CRS84 default, and their primary column."""
    columns = native_geometry_columns(path)
    if not columns:
        return [], ""
    import pyarrow.parquet as pq

    raw_geo = (pq.read_schema(path).metadata or {}).get(b"geo")
    primary = columns[0][0]
    if raw_geo:
        geo = json.loads(raw_geo)
        primary = geo.get("primary_column", "geometry")
        spec = geo.get("columns", {}).get(primary, {})
        if "crs" in spec:
            return [], primary  # an explicit value, including null, keeps the 1.x meaning
    declaration = next((value for name, value in columns if name == primary), "")
    return (columns, primary) if declaration.strip() else ([], primary)


def _read_geoparquet(path: str, *, allow_no_geometry: bool) -> gpd.GeoDataFrame:
    """GeoParquet 1.x through GeoPandas, 2.0-native through pyarrow.

    GeoPandas 1.1 raises ``Missing geo metadata`` on a 2.0-native file, which
    reached the agent as the tool's error — not our message, no hint, and
    misleading, since the file does carry geospatial metadata. The geometry is
    WKB either way, so reading it is a matter of looking in the other place.
    """
    import pyarrow.parquet as pq

    try:
        frame = gpd.read_parquet(path)
    except ValueError as exc:
        if "geo metadata" not in str(exc).lower():
            raise
        original = exc
        columns = native_geometry_columns(path)
        primary = columns[0][0] if columns else ""
    else:
        columns, primary = _native_columns_when_geo_crs_is_absent(path)
        if not columns:
            return frame
    if not columns:
        if not allow_no_geometry:
            raise original  # genuinely no geometry anywhere: keep the original error
        import pandas as pd

        table = pd.read_parquet(path)
        if len(table):
            # The tolerated case is DuckDB's zero-row COPY output, and only that.
            # A file with rows and no geometry read as an empty geometry column
            # produces "N/N invalid geometries", which is critical, which calls
            # the repair that rewrites the file — the shape of the bug #28 closed.
            raise original
        return gpd.GeoDataFrame(table, geometry=gpd.GeoSeries([], dtype="geometry"))

    raw_geo = (pq.read_schema(path).metadata or {}).get(b"geo")
    geo_columns = json.loads(raw_geo).get("columns", {}) if raw_geo else {}
    frame = pq.read_table(path).to_pandas()
    reason = None
    for name, declaration in columns:
        spec = geo_columns.get(name, {})
        if "crs" in spec and spec["crs"] is None:
            crs, why = None, "the file declares its coordinate system as null (undefined or unknown)."
        elif "crs" in spec:
            explicit = spec["crs"] if isinstance(spec["crs"], str) else json.dumps(spec["crs"])
            crs, why = native_crs(path, explicit)
        else:
            crs, why = native_crs(path, declaration)
        # `.rename(name)` is load-bearing: `from_wkb` drops the Series name, so
        # the geometry silently became "geometry" — and the mechanical repair
        # then rewrites the user's file under that name, defeating the care
        # `verify._repair_invalid_geometry` takes never to assume it.
        frame[name] = gpd.GeoSeries.from_wkb(frame[name], crs=crs).rename(name)
        if name == primary:
            reason = why
    # Every native geometry column is read as geometry, not only the active one:
    # one left as raw bytes survives a round-trip as an opaque binary field and
    # loses both its logical type and its CRS.
    gdf = gpd.GeoDataFrame(frame, geometry=primary)
    if reason:
        gdf.attrs[CRS_REASON] = reason
    return gdf


def read_vector(path: str) -> gpd.GeoDataFrame:
    """Open a vector dataset. Every operational read path goes through here.

    GeoParquet is read natively rather than through GDAL: GDAL's Parquet driver
    is not bundled in the wheels, so routing ``.parquet`` through ``read_file``
    breaks on a default install.

    A multi-layer container with no chosen layer is REFUSED (issue #29, closed
    the day Argleton's trap 006 measured the old behaviour). GDAL's default —
    the first layer — answered a question the caller never asked, silently:
    quieter even than the bare pyogrio call, whose stderr warning this reader
    used to swallow. Refusal is the one answer every component can give
    consistently; ``verify.probe_crs`` returns ``unknown`` for the same case so
    the dispatcher and the plan validator never inspect a layer no operation
    will read. The single deliberate exception stays in
    :func:`read_vector_or_table`: verification prefers the stem-named layer of
    outputs MapSmith wrote and named itself.
    """
    _refuse_declared(path)
    if str(path).lower().endswith(".parquet"):
        _refuse_more_rows_than_the_file_pays_for(path)
        return _checked(_read_geoparquet(path, allow_no_geometry=False), path)
    refuse_ambiguous_container(path)
    return _checked(gpd.read_file(path), path)


def _refuse_more_rows_than_the_file_pays_for(path: str) -> None:
    """The one input format whose size on disk does not bound what reading it costs.

    Parquet stores a repeated value once, so a file of rows that are all alike
    is kilobytes for millions of them, and every one becomes a geometry object
    in memory. Here and not in :func:`_read_geoparquet`: verification reads
    back outputs MapSmith wrote itself through :func:`read_vector_or_table`,
    and refusing to read an output back would lose its checks. That read-back
    is therefore not limited, and an output can be larger than its inputs (a
    join's fan-out): `SECURITY.md` declares it.
    """
    refuse_costly_parquet(path)


def _bytes_on_disk(file: Path) -> int:
    """What the file occupies, not what it says it is long.

    A sparse file reports its logical length and holds almost nothing: a 3 GiB
    hole between the data and the footer of a 770 kB Parquet bought fifty
    million rows of entitlement (audit). Where the filesystem does not say what
    is allocated — some network and FUSE mounts report nothing for every file —
    the length is all there is to go by, and `SECURITY.md` says so.
    """
    length = file.stat().st_size
    allocated = 0
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.GetCompressedFileSizeW.restype = ctypes.c_ulong
        kernel32.GetCompressedFileSizeW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
        high = ctypes.c_ulong(0)
        low = kernel32.GetCompressedFileSizeW(str(file), ctypes.byref(high))
        if low != 0xFFFFFFFF:
            allocated = (high.value << 32) + low
    else:
        allocated = getattr(file.stat(), "st_blocks", 0) * 512
    return min(length, allocated) if allocated else length


#: Bytes a value of each fixed-width Parquet physical type decodes to. A
#: FIXED_LEN_BYTE_ARRAY carries its own length in the schema.
_FIXED_WIDTHS = {"BOOLEAN": 1, "INT32": 4, "FLOAT": 4, "INT64": 8, "DOUBLE": 8, "INT96": 12}

#: Fixed-width on disk and an object in memory: pandas holds a decimal as a
#: Python `Decimal`, a date as a `date` and a time as a `time`, measured at
#: about 150, 50 and 140 bytes a value (audit: a hundred decimal columns
#: estimated at 73 MB read at 456; a hundred `time32` columns 2.8 times over
#: their estimate). Priced in the bytes the estimate multiplies by eight.
_OBJECT_VALUE_BYTES = {"DECIMAL": 19, "DATE": 6, "TIME": 18}

#: Bytes one counting batch may materialise, and the most rows it may hold.
#: The first is sized from what the footer says a row weighs, and a footer can
#: say anything: with its uncompressed size rewritten to nothing, 6000 rows of
#: 50 kB were decoded in one batch and the check itself peaked at 775 MB
#: (audit). The second does not depend on the footer. A thousand rows at a time
#: costs 0.2 s on a million ordinary points where 65 536 cost 0.1, and the same
#: file then peaks at 195 MB.
_COUNTING_BATCH_BYTES = 16_000_000
_COUNTING_BATCH_ROWS = 1024


def _is_binary_like(kind: Any) -> bool:
    import pyarrow as pa

    return (
        pa.types.is_binary(kind)
        or pa.types.is_large_binary(kind)
        or pa.types.is_string(kind)
        or pa.types.is_large_string(kind)
    )


def _children(array: Any) -> list[Any]:
    """The arrays inside a nested Arrow array, or none."""
    import pyarrow as pa

    kind = array.type
    if pa.types.is_struct(kind):
        return [array.field(i) for i in range(kind.num_fields)]
    if pa.types.is_map(kind):
        return [array.keys, array.items]
    if pa.types.is_list(kind) or pa.types.is_large_list(kind) or pa.types.is_fixed_size_list(kind):
        return [array.flatten()]
    return []


def _variable_bytes(array: Any) -> int:
    """Bytes the binary and string values inside an Arrow array decode to.

    Inside, because they are not always at the top: a list of binaries, a
    struct with a string in it and a column typed as a dictionary of binaries
    all cost what their values add up to, and a count that looked only at
    top-level binary columns took them for nothing (audit: 249 MB to open
    where the estimate said 1). Fixed-width values are not counted here; the
    footer counts those.
    """
    import pyarrow as pa
    import pyarrow.compute as pc

    array = getattr(array, "storage", array)
    kind = array.type
    if pa.types.is_dictionary(kind):
        if not _is_binary_like(array.dictionary.type) or not len(array.dictionary):
            return 0
        lengths = pc.binary_length(array.dictionary)
        return pc.sum(pc.take(lengths, array.indices)).as_py() or 0
    if _is_binary_like(kind):
        return pc.sum(pc.binary_length(array)).as_py() or 0
    return sum(_variable_bytes(child) for child in _children(array))


def _longest_dictionary_value(array: Any) -> int:
    """Bytes of the longest value a dictionary inside this array holds.

    Asked of one row read with the dictionaries kept: where a column is
    dictionary encoded that hands back the whole dictionary and one index. A
    value 100 kB long and stored once is what makes a few thousand rows cost
    hundreds of megabytes, and this is where it shows — at the top level or
    inside a struct, where the first version of this did not look and the
    check itself ran to 820 MB on a file of 130 kB (audit).
    """
    import pyarrow as pa
    import pyarrow.compute as pc

    array = getattr(array, "storage", array)
    if pa.types.is_dictionary(array.type):
        if not _is_binary_like(array.dictionary.type) or not len(array.dictionary):
            return 0
        return pc.max(pc.binary_length(array.dictionary)).as_py() or 0
    return max((_longest_dictionary_value(child) for child in _children(array)), default=0)


def _counted_from_the_footer(file: Path, plain: Any, path: str) -> tuple[int, int, int, list[tuple[int, float]]]:
    """`(rows, bytes of numbers, bytes of other fixed values, row groups to read)`.

    Refuses a footer that disagrees with itself. The file's row count, each row
    group's and the value count of every column that holds one value a row are
    the same number in anything a writer produces, and they are read by
    different parts of a reader: with only the row counts rewritten to 1, a
    counting pass stopped after one row and the read went on to all of them
    (audit, twice — the second time after the first fix).
    """
    footer, schema = plain.metadata, plain.schema
    leaves = [schema.column(j) for j in range(footer.num_columns)]
    rows = numbers = values = 0
    to_read = []
    for index in range(footer.num_row_groups):
        group = footer.row_group(index)
        variable = 0
        for j, leaf in enumerate(leaves):
            chunk = group.column(j)
            flat = leaf.max_repetition_level == 0
            if flat and chunk.num_values != group.num_rows:
                raise ValueError(
                    f"{path} is not a Parquet file MapSmith will read: row group {index} "
                    f"declares a row count of {group.num_rows:,} and its column "
                    f"{leaf.path!r} holds {chunk.num_values:,} values. A writer produces "
                    "the same number for "
                    "both; a file where they differ cannot be sized before it is read."
                )
            if leaf.physical_type == "BYTE_ARRAY":
                variable += chunk.total_uncompressed_size
                continue
            logical = getattr(leaf.logical_type, "type", "")
            if logical in _OBJECT_VALUE_BYTES:
                values += chunk.num_values * _OBJECT_VALUE_BYTES[logical]
                continue
            width = _FIXED_WIDTHS.get(leaf.physical_type) or max(leaf.length, 1)
            if flat and "." not in leaf.path:
                numbers += chunk.num_values * width
            elif flat:
                # A field of a struct: each row becomes a dictionary, and every
                # field an object in it whatever its width. Two hundred
                # booleans a row were 65 MB by their width and 362 read (audit).
                values += chunk.num_values * max(width, 8)
            else:
                # Numbers in a list are not held as a column of numbers.
                values += chunk.num_values * width
        rows += group.num_rows
        if variable:
            to_read.append((index, variable / max(group.num_rows, 1)))
    if footer.num_rows != rows:
        raise ValueError(
            f"{path} is not a Parquet file MapSmith will read: it declares "
            f"{footer.num_rows:,} rows and its row groups {rows:,}."
        )
    return rows, numbers, values, to_read


def refuse_costly_input(path: str) -> None:
    """For an engine that opens an input itself: the check this reader makes.

    `calculate_field` and the DuckDB and Sedona joins hand the file to another
    engine, which streams it, and then read their own output back in full: two
    million identical rows in 30 kB went through `calculate_field` to
    `verified: true` at 926 MB (audit). Only Parquet is counted, here as in
    :func:`read_vector`.
    """
    if str(path).lower().endswith(".parquet"):
        refuse_costly_parquet(path)


def refuse_costly_parquet(path: str) -> None:
    """Refuse a Parquet input that would take more memory to read than it may.

    Every path that opens a caller's Parquet comes here first: this reader,
    and the engines that hand the file to DuckDB or Sedona and then read their
    own output back in full. `limits.refuse_too_costly_a_read` has the
    arithmetic and the measurements; this is where its numbers come from.

    Rows and fixed-width values are counted from the footer, which costs
    nothing and is what a table of two hundred numbers a row is made of.
    Binary and string values, wherever they sit, are read and their lengths
    added up: from the dictionary and its indices where the longest value a
    row group's dictionaries hold is far longer than the group spends per row,
    which is what repetition looks like, and plainly otherwise, which is ten
    times faster on values that do not repeat. A plain batch holds no more
    rows than fit `_COUNTING_BATCH_BYTES` at that longest value, and the pass
    as a whole is bounded by the uncompressed size the footer declares, which
    is checked first.
    """
    import pyarrow.dataset as ds
    import pyarrow.parquet as pq

    # A directory of Parquet files is one dataset to GeoPandas, and was read as
    # one before this check existed. Counted from the files the dataset reader
    # itself will open, whatever they are called: a glob for `*.parquet` let a
    # directory through with its rows in a file named `big.pq` (audit).
    files = (
        [Path(file) for file in ds.dataset(path, format="parquet").files]
        if Path(path).is_dir()
        else [Path(path)]
    )
    on_disk = sum(_bytes_on_disk(file) for file in files)
    allowance = limits.input_memory_allowance(on_disk)

    rows = numbers = values = declared = 0
    plans = []
    for file in files:
        # One reader for the footer and the plain pass, whatever the number of
        # row groups: opening the file again for each made a thousand groups
        # take 9 s to count (audit).
        plain = pq.ParquetFile(file)
        in_file, file_numbers, file_values, to_read = _counted_from_the_footer(file, plain, path)
        rows += in_file
        numbers += file_numbers
        values += file_values
        for index, average in to_read:
            declared += round(average * plain.metadata.row_group(index).num_rows)
            plans.append((file, plain, index, average))

    def refuse_if_over(counted: int, *, complete: bool) -> None:
        limits.refuse_too_costly_a_read(
            rows, counted, on_disk, path, number_bytes=numbers, complete=complete
        )

    # The rows and the fixed-width values, then what the footers say has to be
    # decompressed: all before any page is touched.
    refuse_if_over(values, complete=False)
    refuse_if_over(values + declared, complete=False)

    # Per file: the top-level columns that hold binary or string values
    # somewhere, and a second reader that keeps the dictionaries of every such
    # leaf, nested ones by their path.
    columns: dict[Path, tuple[list[str], Any]] = {}
    read = 0
    for file, plain, index, average in plans:
        if file not in columns:
            leaves = [plain.schema.column(j) for j in range(plain.metadata.num_columns)]
            paths = [leaf.path for leaf in leaves if leaf.physical_type == "BYTE_ARRAY"]
            names = sorted({leaf_path.split(".")[0] for leaf_path in paths})
            columns[file] = (names, pq.ParquetFile(file, read_dictionary=paths))
        names, keeping = columns[file]
        longest = 0
        for batch in keeping.iter_batches(batch_size=1, row_groups=[index], columns=names):
            longest = max(_longest_dictionary_value(column) for column in batch.columns)
            break
        widest = max(average, longest, 1)
        batch_rows = int(min(_COUNTING_BATCH_ROWS, max(1, _COUNTING_BATCH_BYTES // widest)))
        reader = keeping if longest > 8 * average else plain
        for batch in reader.iter_batches(batch_size=batch_rows, row_groups=[index], columns=names):
            read += sum(_variable_bytes(column) for column in batch.columns)
            if limits.estimated_read_memory(rows, values + read, numbers) > allowance:
                refuse_if_over(values + read, complete=False)
    refuse_if_over(values + max(read, declared), complete=True)


def read_named_layer(path: str, layer: str) -> gpd.GeoDataFrame:
    """One named layer of a multi-layer container.

    The counterpart of :func:`refuse_ambiguous_container`: that function exists
    because nobody chose, and this one because somebody did. The ambiguity the
    refusal protects against is gone the moment a name is given, so this is the
    one read that is allowed past it — and it lives here, with every other
    "open a vector dataset" decision, rather than at the call site. #28 was that
    decision in six copies with four of them missing a branch.

    An unknown name is refused with the real list rather than answered with an
    empty frame: a typo and an empty layer are different problems, and only one
    of them is the caller's data.
    """
    # Before asking OGR anything: a GeoParquet has no layers to choose between,
    # and whether GDAL can even list it depends on whether the build has the
    # Arrow driver. Deciding by installed driver is how the same call reads one
    # file two ways on two machines — and `read_vector` routes .parquet away
    # from OGR on purpose, for the 2.0 types and the CRS-declaration branch.
    if str(path).lower().endswith(".parquet"):
        raise ValueError(
            f"{path} is a GeoParquet: a single dataset, with no layers to choose "
            "between. Read it directly — there is nothing to extract."
        )
    available = gpkg_layers(path)
    if not available:
        raise ValueError(
            f"{path} holds no listable layers. It is either a single-dataset format "
            "— a GeoJSON, a shapefile — which needs no extraction and can be read "
            "directly, or it is not an OGR source at all."
        )
    if layer not in available:
        raise ValueError(
            f"{path} has no layer {layer!r}. It holds: {sorted(available)}. Layer "
            "names are case-sensitive; describe_dataset lists them with their "
            "feature counts and geometry types."
        )
    _refuse_declared(path)
    return _checked(gpd.read_file(path, layer=layer), path)


def read_vector_or_table(path: str) -> gpd.GeoDataFrame:
    """As :func:`read_vector`, but a Parquet file with no geometry *at all*
    becomes a frame with an empty geometry column instead of an error.

    Only verification wants this, and it wants one more thing: for a
    GeoPackage, the layer named after the file stem. Verification runs on
    outputs, which MapSmith wrote and named itself, and GDAL handing back the
    first layer would verify a layer the operation never wrote and mark an
    unchecked output as verified. On *inputs* that same preference would fork
    from every other component (see :func:`read_vector`), so it stays here.
    """
    lower = str(path).lower()
    if lower.endswith(".parquet"):
        _refuse_declared(path)
        return _checked(_read_geoparquet(path, allow_no_geometry=True), path)
    if lower.endswith(".gpkg"):
        stem = Path(path).stem
        layers = gpkg_layers(path) or []
        if stem in layers:
            _refuse_declared(path)
            return _checked(gpd.read_file(path, layer=stem), path)
        if len(layers) > 1:
            # Verification-only tolerance, chosen on purpose rather than
            # inherited from GDAL: a foreign multi-layer container must still
            # be INSPECTABLE for its defects so the checks land in a manifest,
            # and the repair separately refuses to rewrite containers.
            # Operational reads refuse this same case outright (#29).
            _refuse_declared(path)
            return _checked(gpd.read_file(path, layer=layers[0]), path)
    return read_vector(path)


def read_vector_capped(path: str, cap: int) -> tuple[gpd.GeoDataFrame, int, list[float] | None]:
    """``(at most cap rows, total feature count, full native bounds)``.

    OGR formats read only ``cap`` features and take count and bounds from the
    layer metadata, with no full scan. GeoParquet currently reads fully and
    subsets in memory — a pushdown read is a known follow-up. The count and the
    bounds always describe the WHOLE dataset, so a truncated preview never lies
    about the extent it stands for.
    """
    if str(path).lower().endswith(".parquet"):
        gdf = read_vector(path)
        total = len(gdf)
        bounds = [float(v) for v in gdf.total_bounds] if total else None
        return gdf.head(cap), total, bounds

    import pyogrio

    refuse_ambiguous_container(path)
    info = pyogrio.read_info(path)
    total = int(info.get("features") or -1)
    meta_bounds = info.get("total_bounds")
    if total < 0 or meta_bounds is None:  # driver without cheap metadata
        gdf = read_vector(path)
        total = len(gdf)
        bounds = [float(v) for v in gdf.total_bounds] if total else None
        return gdf.head(cap), total, bounds
    _refuse_declared(path)
    return _checked(gpd.read_file(path, max_features=cap), path), total, [float(v) for v in meta_bounds]


def crs_reason(frame: Any) -> str | None:
    """Why this frame has no CRS, when we know — see :data:`CRS_REASON`."""
    attrs = getattr(frame, "attrs", None) or {}
    return attrs.get(CRS_REASON)


def no_crs_message(frame: Any, base: str) -> str:
    """``base``, plus the reader's explanation when it has one.

    Several operations refuse a CRS-less input themselves, before
    ``verify_loaded_inputs`` can run, and each one used to throw the reason
    away — so the same file got an honest message from ``clip`` and a blind one
    from ``buffer_layer``. The reason belongs to the read, not to the caller
    that happens to notice first.
    """
    reason = crs_reason(frame)
    return f"{base} {reason[0].upper() + reason[1:]}" if reason else base


def refuse_missing_geometry(frame: Any, path: str, consequence: str) -> None:
    """Refuse features with no geometry, or an empty one, saying what they would do.

    An operation that has nowhere to put a feature without a position either
    crashed on it -- `thin_points` with an AttributeError, the sampler turning
    a NaN coordinate into a cell index, the line operations sorting a set
    holding None -- or would have to drop it without saying so. Refused, with
    the count and the consequence, like `cluster_points_by_distance` does
    (found by review, 2026-09-29).
    """
    import shapely

    geoms = frame.geometry.to_numpy()
    missing = int((shapely.is_missing(geoms) | shapely.is_empty(geoms)).sum())
    if missing:
        raise ValueError(
            f"{path} has {missing} feature(s) with no or an empty geometry. "
            f"{consequence} Remove those features first."
        )
