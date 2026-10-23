"""Nothing inside a coordinate reference system may name a file or a host.

A CRS definition is text an agent can pass as a tool argument and a file can
carry in its own metadata (GeoParquet's PROJJSON, a GeoPackage's WKT), and
PROJ treats parts of it as places to read from: ``+nadgrids=`` and
``+geoidgrids=`` name grid files by path, ``+init=`` names an init file to
parse, and with networking enabled a grid name can be a URL. Measured on
2026-10-23 (security audit before 0.10.0, reproduced on 0.9.0): under a
workspace, ``reproject_layer`` with ``+nadgrids=<a path outside it>`` read
that file and its content moved the output by a degree; the same from a
GeoParquet's PROJJSON through ``clip_layer``; a ``//host/share/x.gsb`` grid
opened over SMB on Windows, the NTLM-leak surface path arguments already
refuse; and with ``PROJ_NETWORK=ON`` an ``http://`` grid made two requests to
any host. ``SECURITY.md`` promised none of that could happen, and the path
jail never saw these strings because they are not path arguments.

The rule, applied wherever a CRS enters (tool arguments, the CRS a file
declares, the datum module): a grid or init name may only be a bare name
resolved from PROJ's own data directories, or ``@null``. No separators, no
drive letters, no ``//`` or ``\\\\``, no URL scheme, no ``.`` segments.
Networking is switched off under a workspace at startup, whatever ``proj.ini``
says, for pyproj's PROJ and -- through ``PROJ_NETWORK=OFF`` in the environment
``gdal_policy`` installs -- for the PROJ inside GDAL and rasterio, which are
separate libraries with separate contexts.

The check reads TEXT and never asks PROJ whether a grid exists: the second
audit pass (2026-10-23) measured that ``coordinate_operation.grids`` opens the
file to answer ``available``, so a policy that looked there opened the UNC
share it was about to refuse. The same holds for the readers: the SRS a file
declares is read as text from the file's own catalogue (GeoPackage's
``gpkg_spatial_ref_sys``, a shapefile's ``.prj``, GeoParquet's ``geo``
metadata) before GDAL or pyproj parse it.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path
from typing import Any

#: PROJ keys whose value is a place to read from. ``+init`` is parsed BEFORE a
#: CRS object exists, so it is checked on the text as well.
_FILE_KEYS = ("nadgrids", "geoidgrids", "xy_grids", "z_grids", "grids", "init", "catalog", "file", "model")
#: A CRS whose transformation is written as a PROJ string in WKT2 can carry
#: any step with any file (`tinshift +file=`, `defmodel +model=`): refused as
#: a whole rather than enumerated (third audit pass, 2026-10-23).
_PROJ_BASED = re.compile(r"PROJ-based operation method", re.IGNORECASE)
#: `+key=value`, but also `key=value`, spaces around `=`, and a quoted value:
#: pyproj accepts all of them (second audit pass).
_KEY_VALUE = re.compile(r"\+?\s*(" + "|".join(_FILE_KEYS) + r")\s*=\s*\"?([^\s\"]+)\"?", re.IGNORECASE)
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_AUTHORITY_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*:[A-Za-z0-9_.-]+$")
#: The WKT and PROJJSON spellings of the same thing: a parameter whose value
#: is a file ("Latitude and longitude difference file", "Geoid (height
#: correction) model file", ...), and the PROJ4 extension of WKT1.
#: A WKT string escapes a quote as `""`, so a quoted value is read up to the
#: first quote that is not doubled (third audit pass: `"@x"",C:/evil/x.gsb"`
#: was read as `@x`).
_Q = r'"((?:[^"]|"")*)"'
_WKT_FILE = re.compile(
    r'(?:PARAMETERFILE\s*\[\s*' + _Q + r'\s*,\s*' + _Q + r'|"parameter_file"\s*:\s*' + _Q + r'|'
    r'EXTENSION\s*\[\s*"PROJ4_GRIDS"\s*,\s*' + _Q + r'|'
    r'"name"\s*:\s*"[^"]*[Ff]ile[^"]*"\s*,\s*"value"\s*:\s*' + _Q + r')',
    re.IGNORECASE,
)


def _unsafe_name(name: str) -> str | None:
    """Why this grid or init name is not a bare name, or None if it is one."""
    bare = name.strip().strip("\"'").removeprefix("@")  # '@' marks an optional grid
    if not bare or bare == "null":
        return None
    if _SCHEME.match(bare):
        return f"'{name}' is a URL"
    if bare.startswith(("//", "\\\\")):
        return f"'{name}' is a UNC path"
    if "/" in bare or "\\" in bare or "|" in bare:
        return f"'{name}' is a path"
    if ":" in bare:
        return f"'{name}' has a drive letter or a scheme"
    if bare.startswith("."):
        return f"'{name}' starts with a dot"
    if '"' in bare or "'" in bare:
        return f"'{name}' carries a quote"
    return None


def reason_to_refuse_text(text: str) -> str | None:
    """A reason to refuse this CRS text before PROJ parses it, or None."""
    if not isinstance(text, str):
        return None
    if _PROJ_BASED.search(text):
        return "the CRS carries a transformation written as a PROJ string, which may name any file"
    for match in _KEY_VALUE.finditer(text):
        key, value = match.group(1).lower(), match.group(2)
        if key == "init":
            if _AUTHORITY_CODE.match(value):
                continue
            return f"+init={value}: only an authority:code is accepted, not a file"
        for name in value.split(","):
            why = _unsafe_name(name.strip())
            if why:
                return f"+{key}: {why}; a grid must be a bare name from PROJ's data directories"
    for match in _WKT_FILE.finditer(text):
        groups = [v for v in match.groups() if v is not None]
        value = groups[-1]  # PARAMETERFILE captures the parameter name first, then the file
        for name in value.split(","):
            why = _unsafe_name(name.strip())
            if why:
                return f"a parameter file in the CRS: {why}"
    return None


def reason_to_refuse(crs: Any) -> str | None:
    """A reason to refuse this parsed CRS, or None: its grids must be bare names.

    Read from the CRS's PROJJSON and WKT as TEXT, never from
    ``coordinate_operation.grids``: pyproj answers that by asking PROJ whether
    the grid is available, which opens the file -- the UNC share the policy
    was about to refuse (second audit pass, 2026-10-23).
    """
    try:
        for text in (crs.to_json(), crs.to_wkt()):
            why = reason_to_refuse_text(text)
            if why:
                return why
    except Exception:  # noqa: BLE001 - a CRS that cannot be serialised is refused elsewhere
        return None
    return None


def parse(value: Any) -> Any:
    """``pyproj.CRS`` for a tool argument or a file's declaration, refused if it names a file or a host.

    Text first, then pyproj's parser (which opens no grid, measured), then the
    text pyproj writes back: a WKT escape (``""``) that a hand-written regex
    misses is normalised by the parser and caught on the second pass (third
    audit pass, 2026-10-23).
    """
    from pyproj import CRS

    if isinstance(value, str):
        why = reason_to_refuse_text(value)
        if why:
            raise ValueError(f"the coordinate system is refused: {why}.")
    crs = value if isinstance(value, CRS) else CRS.from_user_input(value)
    why = reason_to_refuse(crs)
    if why:
        raise ValueError(f"the coordinate system is refused: {why}.")
    return crs


def declared_srs_texts(path: str) -> list[str]:
    """The coordinate systems a file declares, as the text its own catalogue holds.

    Read without GDAL and without pyproj, because both open a grid file named
    in the SRS while parsing it (second audit pass, 2026-10-23: 20 opens from
    `pyogrio.read_info` on one GeoPackage). A GeoPackage is recognised by its
    first bytes, not its extension (`gpkg_spatial_ref_sys` through sqlite, and
    an SQLite file this cannot read is refused rather than passed on); a
    shapefile by the ``.prj`` beside it, in either case; GeoParquet by the
    ``geo`` metadata. Any other format is opened by GDAL first, and
    ``SECURITY.md`` says so.
    """
    texts: list[str] = []
    p = Path(path)
    try:
        if not p.is_file():
            return texts
        with p.open("rb") as handle:
            head = handle.read(16)
    except OSError:
        return texts
    lower = p.name.lower()
    if head.startswith(b"SQLite format 3\x00"):
        import sqlite3
        from contextlib import closing

        # `closing`, not the connection's own context manager, which commits
        # and does NOT close: an open handle kept a GeoPackage locked on
        # Windows and a later write to it failed (found by the test suite).
        try:
            with closing(sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                # The catalogue must be a plain table: a VIEW can answer one
                # thing to this connection and another to GDAL's, which
                # registers ST_* functions (fourth audit pass, 2026-10-23).
                kinds = {
                    name: (kind, sql or "")
                    for name, kind, sql in db.execute(
                        "SELECT lower(name), type, sql FROM sqlite_master "
                        "WHERE lower(name) IN ('gpkg_spatial_ref_sys', 'spatial_ref_sys')"
                    )
                }
                for name, (kind, sql) in kinds.items():
                    if kind != "table" or "VIRTUAL" in sql.upper():
                        raise ValueError(f"{name} is a {kind}, not a table")
                if "gpkg_spatial_ref_sys" in kinds:
                    columns = {row[1] for row in db.execute("PRAGMA table_info(gpkg_spatial_ref_sys)")}
                    wanted = [c for c in ("definition", "definition_12_063") if c in columns]
                    if wanted:
                        # Every row: GDAL treats only 0 and -1 as undefined, and a
                        # layer bound to srs_id -2 was read by GDAL and skipped by
                        # a `WHERE srs_id > 0` (fifth audit pass, 2026-10-23). The
                        # placeholders are skipped by their text, "undefined".
                        for row in db.execute(f"SELECT {', '.join(wanted)} FROM gpkg_spatial_ref_sys"):
                            texts.extend(str(v) for v in row if v and str(v).strip().lower() != "undefined")
                if "spatial_ref_sys" in kinds:
                    # OGR's own SQLite format, and SpatiaLite: the same catalogue under another name.
                    columns = {row[1] for row in db.execute("PRAGMA table_info(spatial_ref_sys)")}
                    wanted = [c for c in ("srtext", "proj4text") if c in columns]
                    if wanted:
                        for row in db.execute(f"SELECT {', '.join(wanted)} FROM spatial_ref_sys"):
                            texts.extend(str(v) for v in row if v and str(v).strip().lower() not in ("undefined", ""))
        except Exception as exc:  # an SQLite file whose catalogue cannot be read is refused, not passed on
            raise ValueError(f"{path}: its spatial reference catalogue could not be read ({exc}).") from exc
    elif lower.endswith(_SHAPEFILE_FAMILY):
        # Any member of the family names the layer: GDAL opens `.dbf` or
        # `.shx` as the shapefile too (fourth audit pass). Two stats, not a
        # glob: a directory of 20,000 files made the glob cost 73 ms a file.
        for candidate in (p.with_suffix(".prj"), p.with_suffix(".PRJ")):
            if candidate.is_file():
                texts.append(candidate.read_text(encoding="utf-8", errors="replace"))
                break
    elif lower.endswith(".parquet") or head.startswith(b"PAR1"):
        import pyarrow.parquet as pq

        try:
            meta = pq.ParquetFile(path).metadata.metadata or {}
        except Exception:  # noqa: BLE001 - not a Parquet file after all: GDAL reads it, and `_checked` follows
            return texts
        import json

        for key, value in meta.items():
            raw = value.decode("utf-8", "replace")
            if key == b"geo":
                # The `geo` document is not a CRS; each column's `crs` is (a
                # PROJJSON object, or a string), and that is what is checked.
                try:
                    columns = json.loads(raw).get("columns", {}) or {}
                except ValueError:
                    texts.append(raw)  # unreadable: the text check still runs on it
                    continue
                for column in columns.values():
                    crs = (column or {}).get("crs")
                    if isinstance(crs, dict):
                        texts.append(json.dumps(crs))
                    elif isinstance(crs, str) and crs:
                        texts.append(crs)
            elif key.startswith(b"projjson"):
                texts.append(raw)
    # GDAL's persistent auxiliary metadata beside a raster carries an SRS of
    # its own, read on open (`reproject_raster`, `describe_dataset`: 31 opens).
    aux = Path(str(path) + ".aux.xml")
    if aux.is_file():
        texts.extend(_aux_srs_texts(aux, path))
    return texts


def _aux_srs_texts(aux: Path, path: str) -> list[str]:
    """The `<SRS>` texts of a raster's `.aux.xml`, decoded as GDAL decodes them.

    Through an XML parser, not a regex on the raw bytes: a backslash written
    as `&#92;` read as a bare name to the regex and as `\\host\\share` to GDAL
    (fifth audit pass, 2026-10-23). A file the parser cannot read, or one
    with a DOCTYPE, is refused: what MapSmith cannot read it does not pass on.
    """
    from xml.etree import ElementTree

    raw = aux.read_bytes()
    if len(raw) > _MAX_SRS_BYTES:
        raise ValueError(f"{path}: its .aux.xml is {len(raw)} bytes, more than a sidecar with an SRS needs.")
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError(f"{path}: its .aux.xml carries a DOCTYPE, which MapSmith does not read.")
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise ValueError(f"{path}: its .aux.xml could not be read ({exc}).") from None
    return [(node.text or "") for node in root.iter("SRS") if (node.text or "").strip()]


_SHAPEFILE_FAMILY = (".shp", ".shx", ".dbf", ".prj", ".cpg", ".sbn", ".sbx", ".qix", ".shp.xml")
#: A declared SRS longer than this is not a coordinate system anybody wrote:
#: the check scales with the text (2.5 s per MiB measured), and a cache of
#: texts this size is a memory cost an input should not be able to set.
_MAX_SRS_BYTES = 64 * 1024


def refuse_declared_in_file(path: str) -> None:
    """Refuse an input file whose declared coordinate system names a file or a host, before anything opens it.

    Each declared text goes through `parse`: the text check, pyproj's parser
    (no grid opened), and the check again on what pyproj writes back.
    """
    for text in declared_srs_texts(path):
        if len(text) > _MAX_SRS_BYTES:
            raise ValueError(
                f"{path}: the file's coordinate system is refused: {len(text)} characters is more than a "
                "coordinate system definition needs."
            )
        why = _verdict(text)
        if why:
            raise ValueError(f"{path}: the file's coordinate system is refused: {why}")


@functools.lru_cache(maxsize=4096)
def _verdict(text: str) -> str | None:
    """Why this declared SRS text is refused, or None. Cached by text: fifty
    shapefiles with the same ESRI `.prj` cost one parse, not fifty."""
    try:
        parse(text)
    except ValueError as exc:
        return str(exc)
    except Exception as exc:  # noqa: BLE001 - whatever pyproj raises: GDAL may still read what PROJ refused
        # Closed, not open: a declared SRS pyproj cannot parse but GDAL can
        # (a WKT with an escape this module did not foresee) would reach
        # GDAL unchecked, and GDAL opens the grid it names.
        return (
            f"PROJ could not read it ({exc}), and a coordinate system MapSmith cannot check is not opened."
        )
    return None


def network_off_under_workspace() -> bool:
    """Switch PROJ networking off when a workspace is set and remote reads are not allowed.

    Returns whether it was switched off. pyproj's default is off, but
    ``PROJ_NETWORK=ON`` in the environment or ``proj.ini`` turns it on
    underneath, and a grid named in a CRS then becomes a request to any host.
    """
    from . import workspace

    if workspace.root() is None:
        return False
    try:
        from pyproj.network import set_network_enabled

        set_network_enabled(False)
    except Exception:  # noqa: BLE001 - an old pyproj without the switch keeps its default, which is off
        return False
    return True
