"""QGIS Processing through a bounded subprocess and MapSmith-owned outputs."""

from __future__ import annotations

import json
import math
import os
import pathlib
import subprocess
import tempfile
from typing import Any

from .. import readers, stacks, verify, workspace
from ..provenance import InputRecord, ProvenanceRecord

ALGORITHMS = {"native:buffer"}


def process(
    algorithm: str,
    input_path: str,
    output_path: str,
    distance: float,
    segments: int = 5,
    dissolve: bool = False,
) -> dict[str, Any]:
    """Run a whitelisted QGIS algorithm, then write and verify with MapSmith."""
    if algorithm not in ALGORITHMS:
        raise ValueError(f"algorithm must be one of {sorted(ALGORITHMS)}, got {algorithm!r}")
    if not math.isfinite(distance):
        raise ValueError("distance must be finite")
    if segments < 1:
        raise ValueError("segments must be positive")
    qgis_process = stacks._qgis_process()
    if not qgis_process:
        raise RuntimeError(
            "qgis_processing needs QGIS with qgis_process installed and discoverable"
        )
    layer = readers.read_vector(input_path)
    if layer.crs is None:
        raise ValueError(readers.no_crs_message(layer, f"{input_path} has no CRS"))
    if layer.crs.is_geographic:
        raise ValueError(
            "qgis_processing requires a projected CRS: buffer distance is in the "
            "input CRS's units, and degrees are not a defensible buffer geometry"
        )
    pre = verify.verify_loaded_inputs("qgis_processing", input_path=layer)
    record = ProvenanceRecord(
        operation="qgis_processing",
        parameters={
            "algorithm": algorithm,
            "distance": distance,
            "distance_unit": "the input CRS's linear or angular unit, not necessarily metres",
            "segments": segments,
            "end_cap_style": 0,
            "join_style": 0,
            "miter_limit": 2,
            "dissolve": dissolve,
            "separate_disjoint": False,
        },
        inputs=[InputRecord.from_path(
            input_path, crs=verify.crs_label(layer.crs), argument="input_path"
        )],
        engine={"name": "QGIS Processing", "version": "pending subprocess result"},
    )
    record.crs_decisions = {
        "analysis_crs": verify.crs_label(layer.crs),
        "reason": "QGIS buffers in the input CRS without reprojection; distance uses that CRS's units",
    }
    root = workspace.root()
    with tempfile.TemporaryDirectory(
        prefix="mapsmith-qgis-", dir=str(root) if root else None
    ) as workdir:
        area = pathlib.Path(workdir)
        source = area / "input.gpkg"
        target = area / "output.gpkg"
        request = {
            "inputs": {
                "INPUT": str(source),
                "DISTANCE": distance,
                "SEGMENTS": segments,
                "END_CAP_STYLE": 0,
                "JOIN_STYLE": 0,
                "MITER_LIMIT": 2,
                "DISSOLVE": dissolve,
                "SEPARATE_DISJOINT": False,
                "OUTPUT": str(target),
            }
        }
        with verify.audit_on_failure(record, output_path, pre):
            layer.to_file(source, driver="GPKG", layer="data")
            done = subprocess.run(
                [
                    qgis_process, "--json", "--no-python", "--skip-loading-plugins",
                    "run", algorithm, "-",
                ],
                input=json.dumps(request, allow_nan=False),
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=900, check=False, cwd=str(area), shell=False,
            )
            if done.returncode != 0:
                raise RuntimeError(
                    f"qgis_process exited {done.returncode}: "
                    f"{(done.stderr or done.stdout)[-1000:]}"
                )
            try:
                outcome = json.loads(done.stdout)
            except ValueError as exc:
                raise RuntimeError("qgis_process did not return one JSON result") from exc
            if outcome.get("algorithm_details", {}).get("id") != algorithm:
                raise RuntimeError("qgis_process returned a result for another algorithm")
            errors = outcome.get("log", {}).get("errors") or []
            if errors:
                raise RuntimeError(f"qgis_process reported errors: {errors}")
            returned = outcome.get("results", {}).get("OUTPUT")
            if not isinstance(returned, str):
                raise TypeError("qgis_process returned no OUTPUT dataset")
            if reason := workspace.nonlocal_reason(returned):
                raise RuntimeError(f"qgis_process returned an unsafe output path: {reason}")
            if os.path.normcase(os.path.normpath(returned)) != os.path.normcase(
                os.path.normpath(str(target))
            ):
                raise RuntimeError(f"qgis_process wrote an unexpected output path: {returned}")
            result = readers.read_vector(str(target))
            if result.crs is None:
                result = result.set_crs(layer.crs)
                record.notes.append(
                    "QGIS returned no CRS metadata; the input CRS was restored explicitly"
                )
            record.engine = {
                "name": "QGIS Processing",
                "version": outcome.get("qgis_version") or "unknown",
                "x-mapsmith:qgis_code_revision": outcome.get("qgis_code_revision") or "unknown",
            }
            for warning in outcome.get("log", {}).get("warning") or []:
                record.notes.append(f"QGIS warning: {warning}")
            from .vector import _write

            _write(result, output_path)

    added = sorted(set(result.columns) - set(layer.columns))
    dropped = sorted(set(layer.columns) - set(result.columns))
    if added or dropped:
        record.notes.append(f"QGIS schema delta: added {added}, dropped {dropped}")
    expected = 1 if dissolve and len(layer) else len(layer)
    manifest, extras = verify.audited(
        record,
        output_path,
        operation="qgis_processing",
        preconditions=pre,
        checks_fn=lambda: verify.verify_vector_output(
            output_path,
            expect_crs=verify.crs_label(layer.crs),
            expect_count=expected,
            expect_geometry={"Polygon", "MultiPolygon"},
            on_empty="fail" if len(layer) else "warn",
            empty_hint="The input is empty, so an empty buffer is the correct result.",
        ),
    )
    return {
        "output": output_path,
        "feature_count": len(result),
        "crs": verify.crs_label(layer.crs),
        "engine_used": "qgis",
        "provenance": manifest,
        "verified": True,
        **extras,
    }
