"""Issue #26: structured failures and three manifest-linked agent attempts."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import Point

from mapsmith import verify
from mapsmith.plans import Plan, RepairRequest, execute
from mapsmith.plans.registry import BINDINGS


def make_plan(tmp_path, name="out.parquet"):
    source = tmp_path / "source.gpkg"
    if not source.exists():
        gpd.GeoDataFrame(
            {"id": [1]}, geometry=[Point(500000, 5000000)], crs="EPSG:32632"
        ).to_file(source)
    return Plan.model_validate({"goal": "bounded repair", "steps": [{
        "id": "buffer", "operation": "buffer_layer", "arguments": {
            "input_path": str(source), "distance_meters": 10,
            "output_path": str(tmp_path / name),
        },
    }]})


def fail_verification(**_):
    verify.enforce([
        verify.Check(
            "feature_count_exact", False, "expected 1, got 0",
            argument="input_path", hint="choose the non-empty input",
            observed=0, expected=1,
        )
    ], "buffer_layer")


def fail_runtime(**_):
    raise RuntimeError("engine failed")


def succeed(**kwargs):
    output = kwargs["output_path"]
    gpd.GeoDataFrame(
        {"id": [1]}, geometry=[Point(500000, 5000000)], crs="EPSG:32632"
    ).to_parquet(output)
    return {"output": output, "feature_count": 1}


def binding(monkeypatch, function):
    monkeypatch.setitem(
        BINDINGS, "buffer_layer", replace(BINDINGS["buffer_layer"], loader=lambda: function)
    )


def test_verification_failure_is_structured_and_recorded(tmp_path, monkeypatch):
    binding(monkeypatch, fail_verification)
    result = execute(make_plan(tmp_path))

    assert result["executed"] is False
    failure = result["failed_step"]
    assert failure["kind"] == "verification"
    assert failure["checks"] == [{
        "name": "feature_count_exact", "passed": False,
        "detail": "expected 1, got 0", "critical": True,
        "hint": "choose the non-empty input", "argument": "input_path",
        "observed": 0, "expected": 1,
    }]
    assert result["repair"]["attempt"] == 1
    assert result["repair"]["budget"] == 3
    assert result["repair"]["attempts_remaining"] == 2
    assert result["repair"]["can_retry"] is True

    manifest = json.loads((tmp_path / "out.parquet.plan.json").read_text(encoding="utf-8"))
    assert manifest["executed"] is False
    assert manifest["failure"]["checks"] == failure["checks"]
    assert manifest["repair"]["attempt"] == 1
    assert manifest["steps"][0]["failure"]["kind"] == "verification"


def test_agent_retry_succeeds_with_a_linked_second_manifest(tmp_path, monkeypatch):
    binding(monkeypatch, fail_verification)
    first = execute(make_plan(tmp_path))
    binding(monkeypatch, succeed)

    second = execute(
        make_plan(tmp_path, "repaired.parquet"),
        RepairRequest(previous_manifest=first["plan_manifest"]),
    )

    assert second["executed"] is True
    assert second["repair"]["attempt"] == 2
    assert second["repair"]["attempts_remaining"] == 1
    assert second["repair"]["can_retry"] is False
    assert second["repair"]["previous_manifest"] == first["plan_manifest"]
    assert second["repair"]["history"][0]["plan_sha256"] == first["plan_sha256"]
    manifest = json.loads((tmp_path / "repaired.parquet.plan.json").read_text(encoding="utf-8"))
    assert manifest["repair"]["attempt"] == 2
    assert manifest["repair"]["history"][0]["manifest"] == first["plan_manifest"]


def test_fourth_attempt_is_refused_before_the_engine_runs(tmp_path, monkeypatch):
    binding(monkeypatch, fail_verification)
    previous = None
    for index in range(3):
        result = execute(make_plan(tmp_path, f"attempt-{index}.parquet"), previous)
        assert result["repair"]["attempt"] == index + 1
        previous = RepairRequest(previous_manifest=result["plan_manifest"])
    assert result["repair"]["can_retry"] is False

    binding(monkeypatch, succeed)
    refused = execute(make_plan(tmp_path, "fourth.parquet"), previous)
    assert refused["executed"] is False
    assert refused["error"]["code"] == "RETRY_BUDGET_EXHAUSTED"
    assert refused["repair"]["attempts_remaining"] == 0
    assert "steps" not in refused
    assert not (tmp_path / "fourth.parquet").exists()


def test_retry_cannot_overwrite_an_earlier_attempt(tmp_path, monkeypatch):
    binding(monkeypatch, fail_verification)
    current = make_plan(tmp_path)
    first = execute(current)
    binding(monkeypatch, succeed)
    result = execute(
        current, RepairRequest(previous_manifest=first["plan_manifest"])
    )
    assert result["error"]["code"] == "REPAIR_OUTPUT_COLLISION"
    manifest = json.loads((tmp_path / "out.parquet.plan.json").read_text(encoding="utf-8"))
    assert manifest["repair"]["attempt"] == 1


def test_inconsistent_linked_history_is_refused(tmp_path, monkeypatch):
    binding(monkeypatch, fail_verification)
    first = execute(make_plan(tmp_path))
    second = execute(
        make_plan(tmp_path, "second.parquet"),
        RepairRequest(previous_manifest=first["plan_manifest"]),
    )
    path = tmp_path / "second.parquet.plan.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["repair"]["attempt"] = 1
    manifest["repair"]["history"] = []
    path.write_text(json.dumps(manifest), encoding="utf-8")
    result = execute(
        make_plan(tmp_path, "third.parquet"),
        RepairRequest(previous_manifest=second["plan_manifest"]),
    )
    assert result["error"]["code"] == "INVALID_REPAIR_CHAIN"


def test_outside_workspace_manifest_is_a_structured_refusal(tmp_path, monkeypatch):
    monkeypatch.setenv("MAPSMITH_WORKSPACE", str(tmp_path))
    binding(monkeypatch, succeed)
    outside = tmp_path.parent / "outside.plan.json"
    result = execute(
        make_plan(tmp_path), RepairRequest(previous_manifest=str(outside))
    )
    assert result["error"]["code"] == "INVALID_REPAIR_CHAIN"


@pytest.mark.parametrize("bad_path", [
    "//attacker/share/output.parquet", "../outside.parquet", 7,
])
def test_unsafe_previous_output_is_rejected_before_canonicalization(
    bad_path, tmp_path, monkeypatch
):
    monkeypatch.setenv("MAPSMITH_WORKSPACE", str(tmp_path))
    binding(monkeypatch, fail_verification)
    first = execute(make_plan(tmp_path))
    manifest_path = Path(first["plan_manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["steps"][0]["attempted_output"] = bad_path
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    binding(monkeypatch, succeed)
    result = execute(
        make_plan(tmp_path, "repaired.parquet"),
        RepairRequest(previous_manifest=first["plan_manifest"]),
    )
    assert result["error"]["code"] == "INVALID_REPAIR_CHAIN"


def test_invalid_or_successful_previous_manifest_is_not_a_retry(tmp_path, monkeypatch):
    invalid = tmp_path / "invalid.plan.json"
    invalid.write_text("{}", encoding="utf-8")
    binding(monkeypatch, succeed)
    result = execute(
        make_plan(tmp_path), RepairRequest(previous_manifest=str(invalid))
    )
    assert result["error"]["code"] == "INVALID_REPAIR_CHAIN"

    first = execute(make_plan(tmp_path))
    second = execute(
        make_plan(tmp_path, "second.parquet"),
        RepairRequest(previous_manifest=first["plan_manifest"]),
    )
    assert second["error"]["code"] == "INVALID_REPAIR_CHAIN"


def test_runtime_failure_does_not_open_agent_repair(tmp_path, monkeypatch):
    binding(monkeypatch, fail_runtime)
    result = execute(make_plan(tmp_path))
    assert result["failed_step"]["kind"] == "runtime"
    assert result["repair"]["can_retry"] is False


def test_successful_first_attempt_still_declares_the_budget(tmp_path, monkeypatch):
    binding(monkeypatch, succeed)
    result = execute(make_plan(tmp_path))
    assert result["executed"] is True
    assert result["repair"] == {
        "budget": 3, "attempt": 1, "attempts_remaining": 2,
        "previous_manifest": None, "history": [], "can_retry": False,
    }


def test_check_observed_and_expected_do_not_change_manifest_schema():
    check = verify.Check("x", False, "detail", observed=1, expected=2)
    assert check.as_dict() == {
        "name": "x", "passed": False, "detail": "detail",
        "critical": True, "hint": None, "argument": None,
    }
    with pytest.raises(verify.VerificationError) as caught:
        verify.enforce([check], "buffer_layer")
    assert caught.value.failures == (check,)
    assert caught.value.operation == "buffer_layer"


def test_output_checks_carry_actionable_fields(tmp_path):
    output = tmp_path / "points.parquet"
    gpd.GeoDataFrame(
        {"id": [1]}, geometry=[Point(0, 0)], crs="EPSG:4326"
    ).to_parquet(output)
    checks = {
        check.name: check
        for check in verify.verify_vector_output(
            str(output), expect_crs="EPSG:32632", expect_count=2,
            expect_geometry={"Polygon"},
        )
    }
    for name in ("crs_matches", "feature_count_exact", "geometry_types"):
        check = checks[name]
        assert check.passed is False
        assert check.argument == "output_path"
        assert check.observed is not None
        assert check.expected is not None
        assert check.hint


def test_real_missing_crs_failure_returns_actionable_checks(tmp_path):
    source = tmp_path / "source.gpkg"
    mask = tmp_path / "no-crs.gpkg"
    gpd.GeoDataFrame({"id": [1]}, geometry=[Point(0, 0)], crs="EPSG:32632").to_file(source)
    gpd.GeoDataFrame({"id": [1]}, geometry=[Point(0, 0)]).to_file(mask)
    current = Plan.model_validate({"steps": [{
        "id": "clip", "operation": "clip_layer", "arguments": {
            "input_path": str(source), "mask_path": str(mask),
            "output_path": str(tmp_path / "out.parquet"),
        },
    }]})
    result = execute(current)
    assert result["executed"] is False
    assert result["failed_step"]["kind"] == "verification"
    check = result["failed_step"]["checks"][0]
    assert check["name"] == "input_crs_present"
    assert check["argument"] == "mask_path"
    assert check["observed"] is None
    assert check["expected"] == "a declared CRS"
    assert result["repair"]["can_retry"] is True
    assert result["plan_manifest"]
    manifest = json.loads((tmp_path / "out.parquet.plan.json").read_text(encoding="utf-8"))
    assert manifest["failure"]["kind"] == "verification"


def test_plan_manifest_write_does_not_follow_an_external_link(tmp_path, monkeypatch):
    monkeypatch.setenv("MAPSMITH_WORKSPACE", str(tmp_path))
    binding(monkeypatch, succeed)
    outside = tmp_path.parent / "outside-plan.json"
    outside.write_text("original", encoding="utf-8")
    sidecar = tmp_path / "out.parquet.plan.json"
    try:
        sidecar.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")

    with pytest.raises(ValueError, match="sidecar|workspace"):
        execute(make_plan(tmp_path))
    assert outside.read_text(encoding="utf-8") == "original"


def test_retry_cannot_overwrite_the_previous_plan_manifest(tmp_path, monkeypatch):
    binding(monkeypatch, fail_verification)
    first = execute(make_plan(tmp_path))
    previous = Path(first["plan_manifest"]).read_bytes()
    binding(monkeypatch, succeed)

    result = execute(
        make_plan(tmp_path, "out.parquet.plan.json"),
        RepairRequest(previous_manifest=first["plan_manifest"]),
    )

    assert result["executed"] is False
    assert result["error"]["code"] == "REPAIR_OUTPUT_COLLISION"
    assert Path(first["plan_manifest"]).read_bytes() == previous


@pytest.mark.parametrize("section,value", [
    ("failure", []),
    ("steps", {}),
    ("attempted_output", []),
])
def test_malformed_previous_manifest_sections_are_a_structured_refusal(
    section, value, tmp_path, monkeypatch
):
    binding(monkeypatch, fail_verification)
    first = execute(make_plan(tmp_path))
    manifest_path = Path(first["plan_manifest"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if section in {"failure", "steps"}:
        manifest[section] = value
    else:
        manifest["steps"][0][section] = value
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    binding(monkeypatch, succeed)

    result = execute(
        make_plan(tmp_path, "repaired.parquet"),
        RepairRequest(previous_manifest=first["plan_manifest"]),
    )

    assert result["executed"] is False
    assert result["error"]["code"] == "INVALID_REPAIR_CHAIN"
