"""Workspace jail: uniform path containment at the MCP tool boundary.

Tool arguments arrive from an LLM agent, i.e. from whatever ended up in its
context window — treat every path as untrusted input. Two layers:

- Non-local path forms (UNC, GDAL /vsi*, URI schemes, NTFS alternate data
  streams) are rejected ALWAYS, workspace or not: on Windows even a
  ``Path.exists()`` on a UNC path opens an SMB/WebDAV connection to an
  attacker-chosen host, and /vsi*/URI reach the network inside the GDAL
  drivers.
- When ``MAPSMITH_WORKSPACE`` is set, every path must resolve inside it.
  Unset means uncontained (explicitly documented in the README) — fine for
  a local stdio server on your own files, mandatory before exposing HTTP.

The plan validator enforces the same rules at plan-validation time (stable
error codes); this module is the runtime enforcement for direct tool calls.
"""

from __future__ import annotations

import os
from pathlib import Path


def root() -> Path | None:
    """The workspace root, or None when unset (uncontained mode)."""
    ws = os.environ.get("MAPSMITH_WORKSPACE", "").strip()
    return Path(ws).resolve() if ws else None


def nonlocal_reason(path: str) -> str | None:
    """Why a path string is not a plain local path, or None if it is.

    Checked BEFORE any filesystem call — see the module docstring for why.
    """
    p = path.strip()
    if p.startswith(("\\\\", "//")):
        return "UNC/device paths are not allowed"
    if p.lower().startswith("/vsi"):
        return "GDAL /vsi* virtual paths are not allowed"
    if ":" in p[2:]:  # a colon is only legitimate as the drive separator (C:...)
        return "URI schemes and NTFS alternate data streams are not allowed"
    return None


def is_outside(path: str, workspace: Path) -> bool:
    """True when the resolved path falls outside the workspace (or is unresolvable)."""
    try:
        child = os.path.normcase(str(Path(path).resolve()))
    except (OSError, ValueError):
        return True
    ws = os.path.normcase(str(workspace))
    return child != ws and not child.startswith(ws.rstrip("\\/") + os.sep)


def guard(path: str, arg: str) -> str:
    """Validate one tool path argument; returns it unchanged (engines record
    the caller's exact strings in provenance)."""
    reason = nonlocal_reason(path)
    if reason:
        raise ValueError(f"'{arg}': {reason}: {path}")
    ws = root()
    if ws is not None and is_outside(path, ws):
        raise ValueError(
            f"'{arg}' resolves outside MAPSMITH_WORKSPACE ({ws}): {path}. "
            "Use a path inside the workspace (relative paths resolve against "
            "the server's working directory, so prefer absolute ones)."
        )
    return path
