"""Where a dataset keeps its coordinate system, read without GDAL.

GDAL reads the SRS a file declares the moment it opens the file, and PROJ opens
any grid that SRS names while parsing it -- before MapSmith can refuse anything.
Measured on 2026-10-26 with a named pipe standing in for the grid, under a
workspace: a GeoJSON ``crs`` member, a GML ``srsName`` and a FlatGeobuf header
each made GDAL open the "grid" 21 to 32 times through ``probe_crs`` and again
through the reader, and the file was refused only afterwards. A grid named as
``//host/share/x.gsb`` is a connection to that host, with the machine's
credentials (the 2026-10-23 audit, on GeoPackage).

So the declared SRS is read here, from the bytes, for every format MapSmith
recognises by its CONTENT -- GDAL picks its driver from the content, not the
name, and a GeoJSON called ``x.parquet`` is opened as GeoJSON. Each reader
returns two lists:

- **CRS texts**: a whole coordinate system as GDAL will receive it. These go
  through ``crs_policy.parse`` (the text check, pyproj's parser, the check again).
- **Free texts**: text that may *contain* a coordinate system in a form that
  cannot be isolated (a file geodatabase table, a GeoTIFF citation, a text
  format MapSmith does not parse). These get the text check only.

What this module does not recognise is reported as ``unknown``, and the caller
decides: under a workspace it is refused, because a format whose SRS MapSmith
cannot read first is one GDAL would read first.
"""

from __future__ import annotations

import json
import re
import struct
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

SHAPEFILE_FAMILY = (".shp", ".shx", ".dbf", ".prj", ".cpg", ".sbn", ".sbx", ".qix", ".shp.xml")

#: Above this, a text or JSON file is not read whole to look for its SRS: the
#: caller treats it as unchecked. 512 MiB of GeoJSON is already more than the
#: vector tools load comfortably.
MAX_PARSE_BYTES = 512 * 1024 * 1024
#: A zip with more members than this, or members larger in total, is not
#: walked: an archive is an input an attacker sizes.
MAX_ZIP_MEMBERS = 10_000
MAX_ZIP_BYTES = 512 * 1024 * 1024
#: How much of a large file-geodatabase table is scanned: the field
#: definitions, geometry SRS included, sit in the table header.
GDB_TABLE_HEAD = 4 * 1024 * 1024
GDB_TABLE_WHOLE = 16 * 1024 * 1024


class Unreadable(ValueError):
    """A recognised format whose coordinate system could not be read here.

    Separate from a refusal on content: the caller closes this under a
    workspace and lets it pass without one, where file access is unconfined.
    """


@dataclass
class Declared:
    """What a dataset declares, by how it must be checked."""

    kind: str
    crs_texts: list[str] = field(default_factory=list)
    free_texts: list[str] = field(default_factory=list)
    #: Why a recognised source could not be read, when one could not. Kept
    #: beside what WAS read rather than raised: an unreadable dataset must not
    #: hide the sidecar next to it (an `.aux.xml` went unchecked behind a
    #: broken TIFF in the first version, found by the test suite).
    unreadable: str | None = None

    def extend(self, other: Declared) -> None:
        self.crs_texts.extend(other.crs_texts)
        self.free_texts.extend(other.free_texts)
        self.unreadable = self.unreadable or other.unreadable


#: The kinds whose SRS this module reads. Anything else is `unknown`.
RECOGNISED = frozenset(
    {"sqlite", "parquet", "shapefile", "tiff", "image", "json", "xml", "flatgeobuf", "zip", "text", "gdb", "directory"}
)

_TIFF_MAGIC = (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")
_IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"BM")


def sniff_bytes(head: bytes, name: str) -> str:
    """The kind of a file from its first bytes, with the name only where the bytes cannot tell."""
    if head.startswith(b"SQLite format 3\x00"):
        return "sqlite"
    if head.startswith(b"PAR1"):
        return "parquet"
    if head.startswith(_TIFF_MAGIC):
        return "tiff"
    if head[:3] == b"fgb" and head[4:7] == b"fgb":
        return "flatgeobuf"
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        return "zip"
    if head.startswith(b"\x00\x00\x27\x0a"):
        return "shapefile"  # the .shp magic number, whatever the file is called
    if head.startswith(_IMAGE_MAGIC):
        # PNG, JPEG, GIF, BMP: GDAL takes their georeferencing from sidecars
        # (.aux.xml, .prj, world files), which are read, and from nothing inside.
        return "image"
    # Content before name: a JSON or an XML document is opened by its own
    # driver whatever it is called, `x.dbf` included.
    text = head.lstrip(b"\xef\xbb\xbf").lstrip()
    if text.startswith((b"{", b"[", b"\x1e")):
        return "json"
    if text.startswith(b"<"):
        return "xml"
    if name.lower().endswith(SHAPEFILE_FAMILY):
        return "shapefile"
    if b"\x00" not in head and head:
        try:
            head.decode("utf-8")
        except UnicodeDecodeError:
            # A multi-byte character cut at the end of the sample is still text.
            try:
                head[:-3].decode("utf-8")
            except UnicodeDecodeError:
                return "unknown"
        return "text"
    return "unknown"


def sniff(path: Path) -> str:
    if path.is_dir():
        return "gdb" if path.name.lower().endswith(".gdb") else "directory"
    with path.open("rb") as handle:
        head = handle.read(4096)
    return sniff_bytes(head, path.name)


# --- JSON: GeoJSON, GeoJSON sequences, JSON-FG, Esri JSON -------------------------------

#: The members GDAL reads a coordinate system from, at any depth except inside
#: a feature's `properties`, which is attribute data and never an SRS.
_JSON_SRS_KEYS = ("crs", "coordRefSys", "spatialReference")
_SAFE_CURIE = re.compile(r"^\[([A-Za-z][\w.-]*:[\w.-]+)\]$")


def _json_documents(raw: bytes) -> list:
    # JSON's structure is ASCII. A Latin-1 byte in an attribute is not UTF-8,
    # GDAL reads it anyway, and refusing the file for it was a false positive
    # (audit, 2026-10-26): Latin-1 maps every byte to one character, so the
    # keys and any coordinate system text survive it unchanged.
    try:
        text = raw.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    try:
        return [json.loads(text)]
    except ValueError:
        pass
    # GeoJSON text sequences: one document per record, records separated by RS
    # or by newlines. A file that is neither is not a JSON MapSmith can read.
    documents = []
    for record in re.split(r"[\x1e\n]+", text):
        if record.strip():
            documents.append(json.loads(record))
    return documents


def _json_srs(node, out: Declared) -> None:
    if isinstance(node, list):
        for item in node:
            _json_srs(item, out)
        return
    if not isinstance(node, dict):
        return
    for key, value in node.items():
        if key == "properties":
            continue
        if key in _JSON_SRS_KEYS:
            _json_srs_member(key, value, out)
        else:
            _json_srs(value, out)


def _json_srs_member(key: str, value, out: Declared) -> None:
    """Every string GDAL may hand to PROJ from one SRS member, as a CRS text."""
    if isinstance(value, str):
        curie = _SAFE_CURIE.match(value.strip())
        out.crs_texts.append(curie.group(1) if curie else value)
        return
    if isinstance(value, list):
        for item in value:
            _json_srs_member(key, item, out)
        return
    if not isinstance(value, dict):
        return  # a number is an EPSG/wkid code, and a code names no file
    for inner_key, inner in value.items():
        if inner_key == "type":
            continue  # "name", "link", "EPSG", "Reference": a tag, not an SRS
        _json_srs_member(key, inner, out)


def read_json(raw: bytes) -> Declared:
    out = Declared("json")
    for document in _json_documents(raw):
        _json_srs(document, out)
    return out


# --- XML: GML (srsName), its .gfs schema, KML and the rest --------------------------------

_EPSG_XML = re.compile(r"epsg\.xml#(\d+)$", re.IGNORECASE)


def _refuse_doctype(raw_head: bytes, where: str) -> None:
    upper = raw_head.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ValueError(f"{where} carries a DOCTYPE, which MapSmith does not read.")


def _srs_name(value: str) -> str:
    # GML's own spelling for an EPSG code, which PROJ does not parse as such.
    match = _EPSG_XML.search(value.strip())
    return f"EPSG:{match.group(1)}" if match else value


def read_xml(source, where: str) -> Declared:
    """`srsName` attributes anywhere in the document, and `SRSName` elements (a `.gfs`).

    Straight on expat, streaming, with a handler that refuses a DOCTYPE wherever
    it appears: the first version looked for one in the first 4 KiB only, and a
    long comment ahead of it walked past (audit, 2026-10-26).
    """
    from xml.parsers import expat

    out = Declared("xml")
    in_srs_element: list[bool] = [False]
    text: list[str] = []

    def refuse_doctype(*_args):
        raise ValueError(f"{where} carries a DOCTYPE, which MapSmith does not read.")

    def start(tag, attrs):
        for name, value in attrs.items():
            if name.rsplit("}", 1)[-1].lower() == "srsname" and value.strip():
                out.crs_texts.append(_srs_name(value))
        if tag.rsplit("}", 1)[-1].lower() == "srsname":
            in_srs_element[0] = True
            text.clear()

    def end(tag):
        if in_srs_element[0] and tag.rsplit("}", 1)[-1].lower() == "srsname":
            in_srs_element[0] = False
            if "".join(text).strip():
                out.crs_texts.append(_srs_name("".join(text)))

    def chars(data):
        if in_srs_element[0]:
            text.append(data)

    parser = expat.ParserCreate(namespace_separator="}")
    parser.StartDoctypeDeclHandler = refuse_doctype
    parser.EntityDeclHandler = refuse_doctype
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = chars
    try:
        if isinstance(source, str):
            with open(source, "rb") as handle:
                parser.ParseFile(handle)
        else:
            parser.ParseFile(source)
    except expat.ExpatError as exc:
        raise ValueError(f"{where} could not be read as XML ({exc}).") from None
    return out


# --- FlatGeobuf: the Crs table of the header ----------------------------------------------


class _Buffer:
    """Just enough of the FlatBuffers wire format to read one table, bounds-checked."""

    def __init__(self, data: bytes) -> None:
        self.data = data

    def _u(self, fmt: str, at: int) -> int:
        size = struct.calcsize(fmt)
        if at < 0 or at + size > len(self.data):
            raise ValueError("FlatGeobuf header points outside itself")
        return struct.unpack_from(fmt, self.data, at)[0]

    def root(self) -> int:
        return self._u("<I", 0)

    def field(self, table: int, index: int) -> int | None:
        vtable = table - self._u("<i", table)
        vtable_size = self._u("<H", vtable)
        slot = 4 + 2 * index
        if slot + 2 > vtable_size:
            return None
        offset = self._u("<H", vtable + slot)
        return table + offset if offset else None

    def table(self, table: int, index: int) -> int | None:
        at = self.field(table, index)
        return None if at is None else at + self._u("<I", at)

    def string(self, table: int, index: int) -> str | None:
        at = self.field(table, index)
        if at is None:
            return None
        start = at + self._u("<I", at)
        length = self._u("<I", start)
        if start + 4 + length > len(self.data):
            raise ValueError("FlatGeobuf string runs past the header")
        return self.data[start + 4 : start + 4 + length].decode("utf-8", errors="strict")

    def int32(self, table: int, index: int) -> int | None:
        at = self.field(table, index)
        return None if at is None else self._u("<i", at)


def read_flatgeobuf(handle) -> Declared:
    """The header's `crs` (org, code, name, wkt, code_string). Header table index 10."""
    out = Declared("flatgeobuf")
    magic = handle.read(8)
    if len(magic) < 8:
        raise ValueError("FlatGeobuf file too short")
    size_raw = handle.read(4)
    if len(size_raw) < 4:
        raise ValueError("FlatGeobuf file too short")
    size = struct.unpack("<I", size_raw)[0]
    if size > 64 * 1024 * 1024:
        raise ValueError(f"FlatGeobuf header of {size} bytes")
    buf = _Buffer(handle.read(size))
    header = buf.root()
    crs = buf.table(header, 10)
    if crs is None:
        return out
    org, code = buf.string(crs, 0), buf.int32(crs, 1)
    for text in (buf.string(crs, 4), buf.string(crs, 5)):  # wkt, code_string
        if text and text.strip():
            out.crs_texts.append(text)
    if org and org.strip():
        out.crs_texts.append(f"{org}:{code}" if code else org)
    name = buf.string(crs, 2)
    if name and name.strip():
        out.free_texts.append(name)  # a label GDAL does not parse; checked as text anyway
    return out


# --- GeoTIFF: the GeoKey ASCII parameters and GDAL's metadata tag -------------------------

_TIFF_TYPES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8, 11: 4, 12: 8, 16: 8, 17: 8, 18: 8}
_GEO_ASCII = 34737
_GDAL_METADATA = 42112


def read_tiff(handle) -> Declared:
    """Free text from the first IFD: GeoAsciiParamsTag (citations, ESRI PE strings) and GDAL_METADATA."""
    out = Declared("tiff")
    head = handle.read(16)
    order = "<" if head[:2] == b"II" else ">"
    big = struct.unpack_from(order + "H", head, 2)[0] == 43
    # Classic TIFF: a 2-byte entry count, 12-byte entries (tag, type, 4-byte
    # count, 4-byte value or offset). BigTIFF: 8-byte count, 20-byte entries
    # with 8-byte count and value. The directory's count and an entry's count
    # are different fields of different widths.
    if big:
        ifd = struct.unpack_from(order + "Q", head, 8)[0]
        entries_fmt, field_fmt, entry_size = "Q", "Q", 20
    else:
        ifd = struct.unpack_from(order + "I", head, 4)[0]
        entries_fmt, field_fmt, entry_size = "H", "I", 12
    inline = struct.calcsize(field_fmt)
    handle.seek(ifd)
    count_raw = handle.read(struct.calcsize(entries_fmt))
    count = struct.unpack(order + entries_fmt, count_raw)[0]
    if count > 4096:
        raise ValueError(f"TIFF directory with {count} entries")
    entries = handle.read(count * entry_size)
    for i in range(count):
        entry = entries[i * entry_size : (i + 1) * entry_size]
        if len(entry) < entry_size:
            raise ValueError("TIFF directory truncated")
        tag, typ = struct.unpack_from(order + "HH", entry, 0)
        if tag not in (_GEO_ASCII, _GDAL_METADATA):
            continue
        n = struct.unpack_from(order + field_fmt, entry, 4)[0]
        length = n * _TIFF_TYPES.get(typ, 1)
        if length > 16 * 1024 * 1024:
            raise ValueError(f"TIFF tag {tag} of {length} bytes")
        if length <= inline:
            value = entry[4 + inline : 4 + inline + length]
        else:
            offset = struct.unpack_from(order + field_fmt, entry, 4 + inline)[0]
            here = handle.tell()
            handle.seek(offset)
            value = handle.read(length)
            handle.seek(here)
        out.free_texts.append(value.decode("latin-1"))
    return out


# --- file geodatabase: the SRS WKT in the tables, stored as UTF-16 -------------------------


def read_gdb(folder: Path) -> Declared:
    out = Declared("gdb")
    for table in sorted(folder.glob("*.gdbtable")):
        size = table.stat().st_size
        with table.open("rb") as handle:
            raw = handle.read(size if size <= GDB_TABLE_WHOLE else GDB_TABLE_HEAD)
        out.free_texts.extend(_decodings(raw))
    return out


def _decodings(raw: bytes) -> list[str]:
    """The same bytes read as Latin-1 and as UTF-16 at both alignments: a WKT in
    a binary table is in one of them, and the text check runs on all three."""
    texts = [raw.decode("latin-1")]
    for start in (0, 1):
        chunk = raw[start:]
        texts.append(chunk[: len(chunk) - len(chunk) % 2].decode("utf-16-le", errors="replace"))
    return texts


# --- the dispatcher ------------------------------------------------------------------------


def _sidecars(path: Path) -> Declared:
    """What GDAL reads beside a dataset whatever its format: a `.prj` and an `.aux.xml`."""
    out = Declared("sidecar")
    # A `.prj` passed as the input is its own sidecar: it is checked like any other.
    for prj in {path.with_suffix(".prj"), path.with_suffix(".PRJ"), Path(str(path) + ".prj")}:
        if prj.is_file():
            out.crs_texts.append(prj.read_text(encoding="utf-8", errors="replace"))
    gfs = path.with_suffix(".gfs")
    if gfs.is_file():
        with gfs.open("rb") as handle:
            _refuse_doctype(handle.read(4096), str(gfs))
        out.extend(read_xml(str(gfs), str(gfs)))
    return out


def _check_size(path: Path, kind: str) -> None:
    if path.stat().st_size > MAX_PARSE_BYTES:
        raise ValueError(f"{path}: a {kind} file of {path.stat().st_size} bytes is larger than MapSmith reads to check its coordinate system.")


def read_zip(path: Path) -> Declared:
    out = Declared("zip")
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise ValueError(f"{path}: not a zip MapSmith can read ({exc}).") from None
    with archive:
        members = [m for m in archive.infolist() if not m.is_dir()]
        if len(members) > MAX_ZIP_MEMBERS or sum(m.file_size for m in members) > MAX_ZIP_BYTES:
            raise ValueError(f"{path}: a zip of {len(members)} members is larger than MapSmith walks to check it.")
        for member in members:
            data = archive.read(member)
            kind = sniff_bytes(data[:4096], member.filename)
            where = f"{path}!{member.filename}"
            if kind == "shapefile":
                if member.filename.lower().endswith(".prj"):
                    out.crs_texts.append(data.decode("utf-8", errors="replace"))
            elif kind == "json":
                out.extend(read_json(data))
            elif kind == "xml":
                _refuse_doctype(data[:4096], where)
                import io

                out.extend(read_xml(io.BytesIO(data), where))
            elif kind == "flatgeobuf":
                import io

                out.extend(read_flatgeobuf(io.BytesIO(data)))
            elif kind == "tiff":
                import io

                out.extend(read_tiff(io.BytesIO(data)))
            elif kind == "text":
                out.free_texts.append(data.decode("utf-8", errors="replace"))
            else:
                # A GeoPackage, a Parquet file or a zip inside a zip, or an
                # unknown member: reading it would mean extracting it first.
                raise ValueError(
                    f"{where}: a {kind} member inside a zip is not one MapSmith checks; unzip it and pass the file itself."
                )
    return out


def read(path: Path) -> Declared:
    """Everything `path` declares that GDAL would hand to PROJ, by kind.

    sqlite and parquet stay with `crs_policy`, which reads their catalogues;
    for them, and for a shapefile, this returns the kind and the sidecars.
    Raises `Unreadable` when a recognised format cannot be read.
    """
    try:
        return _read(path)
    except Unreadable:
        raise
    except RecursionError:
        # 60,000 nested JSON arrays made the gate crash instead of answering
        # (audit, 2026-10-26): a document this deep is not one anyone checks.
        raise Unreadable(f"{path}: nested too deeply to check its coordinate system") from None
    except (ValueError, OSError, struct.error) as exc:
        message = str(exc) if str(exc).startswith(str(path)) else f"{path}: {exc}"
        raise Unreadable(f"{message} (its coordinate system could not be read)") from None


def _read(path: Path) -> Declared:
    kind = sniff(path)
    out = Declared(kind)
    if kind == "gdb":
        out.extend(read_gdb(path))
        return out
    if kind == "directory":
        entries = [p for p in path.iterdir() if p.is_file()]
        if len(entries) > MAX_ZIP_MEMBERS:
            raise ValueError(f"{path}: a folder of {len(entries)} files is larger than MapSmith walks to check it.")
        for entry in entries:
            sub = _read(entry)
            if sub.kind == "unknown":
                continue  # GDAL opens a folder through the shapefile driver; the caller decides on the members it names
            out.extend(sub)
        return out
    out.extend(_sidecars(path))
    if kind == "json":
        _check_size(path, "JSON")
        out.extend(read_json(path.read_bytes()))
    elif kind == "xml":
        with path.open("rb") as handle:
            _refuse_doctype(handle.read(4096), str(path))
        out.extend(read_xml(str(path), str(path)))
    elif kind == "flatgeobuf":
        with path.open("rb") as handle:
            out.extend(read_flatgeobuf(handle))
    elif kind == "tiff":
        with path.open("rb") as handle:
            out.extend(read_tiff(handle))
    elif kind == "zip":
        out.extend(read_zip(path))
    elif kind == "text":
        _check_size(path, "text")
        out.free_texts.append(path.read_text(encoding="utf-8", errors="replace"))
    return out
