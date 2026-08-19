"""Workspace jail: uniform path containment at the MCP tool boundary.

Tool arguments arrive from an LLM agent, i.e. from whatever ended up in its
context window — treat every path as untrusted input. Two tiers:

- UNC/device paths and NTFS alternate data streams are rejected ALWAYS,
  workspace or not: on Windows even a ``Path.exists()`` on a UNC path opens
  an SMB/WebDAV connection to an attacker-chosen host (NTLM hash leak), with
  no legitimate agent-side use (mount the share as a drive instead).
- Remote/virtual forms (GDAL /vsi*, URI schemes) and containment are enforced
  only when ``MAPSMITH_WORKSPACE`` is set: uncontained mode deliberately
  admits cloud-native data (COGs over https, /vsicurl) on the user's own
  responsibility, while a workspace means "this directory and nothing else".

Known tradeoff of the ADS check: a colon anywhere past the drive letter is
refused, so POSIX filenames like 'data:v2.parquet' are rejected too — and a
two-character basename like 'a:stream' slips past it, harmlessly, because
containment on the resolved path still applies.

Validated plans stay stricter by design: the plan validator rejects every
non-local form regardless of workspace (stable error codes), because a plan
is a contract checked end-to-end before execution.

The jail assumes a single trusted writer of the workspace filesystem: paths
are resolved at check time, so an adversary able to swap a directory for a
symlink between check and use could escape it. That is out of the threat
model for a local stdio server; revisit for authenticated multi-tenant HTTP.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

_URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")


def root() -> Path | None:
    """The workspace root, or None when unset (uncontained mode)."""
    ws = os.environ.get("MAPSMITH_WORKSPACE", "").strip()
    return Path(ws).resolve() if ws else None


def hard_refusal_reason(path: str) -> str | None:
    """Why a path is refused in EVERY mode, or None. Checked before any
    filesystem call — see the module docstring for why."""
    p = path.strip()
    if p.startswith(("\\\\", "//")):
        return "UNC/device paths are not allowed"
    if (
        not _URI_SCHEME.match(p)
        and not p.lower().startswith("/vsi")
        and ":" in p[2:]  # a colon is only legitimate as the drive separator (C:...)
    ):
        return "NTFS alternate data streams are not allowed"
    return None


def remote_reason(path: str) -> str | None:
    """Why a path is refused under a workspace (remote/virtual form), or None."""
    p = path.strip()
    if p.lower().startswith("/vsi"):
        return "GDAL /vsi* virtual paths are not allowed inside a workspace"
    if _URI_SCHEME.match(p):
        return "URI schemes are not allowed inside a workspace"
    return None


def nonlocal_reason(path: str) -> str | None:
    """Any non-plain-local form (the strict rule used by plan validation)."""
    return hard_refusal_reason(path) or remote_reason(path)


def is_outside(path: str, workspace: Path) -> bool:
    """True when the resolved path falls outside the workspace (or is
    unresolvable). The root itself counts as outside: a dataset path is never
    legitimately the workspace directory, and derived sidecars
    ('<output>.provenance.json') of the root would land beside it, out of jail.
    """
    try:
        child = os.path.normcase(str(Path(path).resolve()))
    except (OSError, ValueError):
        return True
    ws = os.path.normcase(str(workspace))
    return not child.startswith(ws.rstrip("\\/") + os.sep)


def guard(path: str, arg: str) -> str:
    """Validate one tool path argument; returns it unchanged (engines record
    the caller's exact strings in provenance)."""
    reason = hard_refusal_reason(path)
    if reason:
        raise ValueError(f"'{arg}': {reason}: {path}")
    ws = root()
    if ws is not None:
        reason = remote_reason(path)
        if reason:
            raise ValueError(f"'{arg}': {reason}: {path}")
        if is_outside(path, ws):
            raise ValueError(
                f"'{arg}' resolves outside MAPSMITH_WORKSPACE ({ws}): {path}. "
                "Use a path inside the workspace (relative paths resolve against "
                "the server's working directory, so prefer absolute ones)."
            )
    return path
