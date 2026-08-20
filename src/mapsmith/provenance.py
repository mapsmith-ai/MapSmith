"""Lineage manifests for every MapSmith output.

Every operation that writes a dataset also writes ``<output>.provenance.json``
next to it. The manifest is the product's core promise: any result can be
audited and re-run the analysis without an LLM in the loop.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_of(path: str | Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class InputRecord:
    path: str
    sha256: str
    crs: str | None = None

    @classmethod
    def from_path(cls, path: str | Path, crs: str | None = None) -> InputRecord:
        return cls(path=str(path), sha256=sha256_of(path), crs=crs)


@dataclass
class ProvenanceRecord:
    operation: str
    parameters: dict[str, Any]
    inputs: list[InputRecord]
    crs_decisions: dict[str, str] = field(default_factory=dict)
    engine: dict[str, str] = field(default_factory=dict)
    verification: list[dict[str, Any]] = field(default_factory=list)
    # deterministic repair attempts (issue #3): empty unless something was fixed
    repairs: list[dict[str, Any]] = field(default_factory=list)
    # disclosures about how the inputs were handled before the engine saw them
    notes: list[str] = field(default_factory=list)
    mapsmith_version: str = __version__
    started_at: str = field(default_factory=_utcnow)
    finished_at: str | None = None

    def add_verification(self, checks: list[Any]) -> ProvenanceRecord:
        """Attach deterministic check results (objects with .as_dict())."""
        self.verification.extend(c.as_dict() for c in checks)
        return self

    def add_repairs(self, attempts: list[dict[str, Any]]) -> ProvenanceRecord:
        """Attach deterministic repair attempts (see verify.repair_and_reverify)."""
        self.repairs.extend(attempts)
        return self

    def finish(self) -> ProvenanceRecord:
        self.finished_at = _utcnow()
        return self

    def write_for(self, output_path: str | Path) -> Path:
        """Write the manifest next to the output it describes."""
        manifest_path = Path(f"{output_path}.provenance.json")
        manifest_path.write_text(
            json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return manifest_path


def read_provenance(output_path: str | Path) -> dict[str, Any]:
    """Read the lineage manifest of a MapSmith output, if present."""
    manifest_path = Path(f"{output_path}.provenance.json")
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"No provenance manifest found for {output_path}. "
            "Either it was not produced by MapSmith or the manifest was moved."
        )
    return json.loads(manifest_path.read_text(encoding="utf-8"))
