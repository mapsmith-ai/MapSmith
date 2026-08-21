"""Take GDAL's indirection and network drivers off the table when remote is off.

The path guard and the SQL scan both work on the string they are handed. GDAL
does not: it resolves indirection files itself, in-process. A ``.vrt`` is a plain
local path — no scheme, no ``/vsi`` prefix, nothing for a textual check to catch
— and GDAL then fetches whatever its ``<SrcDataSource>`` names.

Measured before this module existed, with ``MAPSMITH_ALLOW_REMOTE`` unset *and*
``MAPSMITH_WORKSPACE`` set: reading a ``.vrt`` from inside the workspace through
pyogrio sent HEAD and GET to an attacker-named host. That contradicted the one
promise SECURITY.md states as testable — no network egress in sandbox mode — so
the fix has to be at GDAL's level, not at the string's. DuckDB's spatial reader
was already safe there, because it routes GDAL I/O through DuckDB's own
filesystem with external access off; the GeoPandas/pyogrio path had no such gate,
which is the path most tools take.

``GDAL_SKIP`` / ``OGR_SKIP`` deregister drivers at registration time, so this has
to run **before** anything imports pyogrio, rasterio or duckdb's spatial
extension — hence the call in ``mapsmith/__init__``. Unknown names in those lists
are ignored by GDAL, which is what keeps this safe across GDAL versions.

With ``MAPSMITH_ALLOW_REMOTE=1`` nothing is skipped: an operator who has decided
to allow remote data gets VRT, WMS and the rest, because that is the whole point
of the switch.
"""

from __future__ import annotations

import os

# Raster side: VRT is the indirection format; the rest speak HTTP by design.
_RASTER_SKIP = (
    "VRT", "GTI", "WMS", "WMTS", "WCS", "TMS", "OGCAPI", "STACIT", "STACTA",
    "HTTP", "DAAS", "EEDAI", "PLMOSAIC", "Zarr",
)
# Vector side: OGR_VRT plus the network drivers.
_VECTOR_SKIP = (
    "OGR_VRT", "VRT", "WFS", "OAPIF", "CSW", "GMLAS", "AmigoCloud", "Carto",
    "Elasticsearch", "PLSCENES", "NGW", "ADBC",
)

APPLIED_ENV = ("GDAL_SKIP", "OGR_SKIP")


def _merge(existing: str, names: tuple[str, ...]) -> str:
    """Add our names to whatever the operator already set, without duplicates.

    COMMA separated, which is what GDAL parses these variables as. Space
    separation silently produces a single token that matches no driver at all:
    the policy looked installed — the variables were set, the code ran — and
    changed nothing, which is the failure mode this project keeps meeting.
    """
    have = [n for n in existing.replace(" ", ",").split(",") if n]
    return ",".join([*have, *(n for n in names if n not in have)])


SENTINEL = "MAPSMITH_GDAL_POLICY"


def _strip(existing: str, names: tuple[str, ...]) -> str:
    return ",".join(
        n for n in (x for x in existing.replace(" ", ",").split(",") if x) if n not in names
    )


def apply(force: bool = False) -> dict[str, str]:
    """Install — or lift — the driver policy. Returns the variables as they stand.

    Lifting matters as much as installing: these variables are inherited, and a
    container or a pod passes its whole environment down. Without the sentinel,
    an operator who sets ``MAPSMITH_ALLOW_REMOTE=1`` in a process whose parent
    had already installed the policy would get a switch that does nothing —
    found by the test for exactly that, which is why the sentinel exists. It also
    keeps us from undoing a skip list the operator set for their own reasons: we
    remove our names only if a previous MapSmith is the one that added them.

    ``force`` installs regardless of the setting, for tests.
    """
    # imported here, not at module level: this module must stay importable
    # before the geospatial stack exists
    from . import workspace

    if not force and workspace.remote_allowed():
        if os.environ.pop(SENTINEL, None):
            for name, ours in (("GDAL_SKIP", _RASTER_SKIP), ("OGR_SKIP", _VECTOR_SKIP)):
                remaining = _strip(os.environ.get(name, ""), ours)
                if remaining:
                    os.environ[name] = remaining
                else:
                    os.environ.pop(name, None)
        return {name: os.environ.get(name, "") for name in APPLIED_ENV}
    os.environ["GDAL_SKIP"] = _merge(os.environ.get("GDAL_SKIP", ""), _RASTER_SKIP)
    os.environ["OGR_SKIP"] = _merge(os.environ.get("OGR_SKIP", ""), _VECTOR_SKIP)
    os.environ[SENTINEL] = "applied"
    return {name: os.environ[name] for name in APPLIED_ENV}
