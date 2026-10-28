"""The agentic pilot produces its declared answer and a durable brief/manifest."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from examples import agentic_pilot

from mapsmith import stacks
from mapsmith.plans.brief import assert_traced


@pytest.mark.skipif(
    not stacks.installed()["opensource"]["qgis"], reason="QGIS is not installed"
)
@pytest.mark.slow
def test_agentic_pilot_end_to_end(tmp_path):
    result = agentic_pilot.run(tmp_path)
    assert result["feature_count"] == 2
    assert result["areas_m2"] == [400.0, 900.0]
    assert result["total_area_m2"] == 1300.0
    assert result["first_engine"] == "qgis"
    assert_traced(result["brief"])
    manifest_path = Path(result["plan_manifest"])
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["executed"] is True
    assert [step["operation"] for step in manifest["steps"]] == [
        "qgis_processing", "overlay_layers", "overlay_layers", "measure_area"
    ]
    assert all(step["status"] == "ok" for step in manifest["steps"])
