"""Arithmetic and population checks for the validated-plan token measurement."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from mapsmith.plans.brief import execute_plan_requests


def total(arm):
    return arm["requests"]["tokens"] + arm["results"]["tokens"]


def test_plan_token_savings_report_reconciles_all_sixteen_cases():
    artifact = json.loads((ROOT / "benchmarks" / "plan_token_savings.json").read_text(encoding="utf-8"))
    assert artifact["model"] == "claude-sonnet-5"
    assert "serialized" in artifact["measurement"]
    assert "Anthropic" in artifact["measurement"]
    cases = artifact["cases"]
    assert [case["index"] for case in cases] == [index for index, _ in execute_plan_requests()]
    assert len(cases) == 16

    for case in cases:
        assert case["operations"] > 0
        assert case["plan"]["executed"] is True
        assert case["single"]["executed"] is True
        assert case["plan"]["calls"] == 2
        assert case["single"]["calls"] == case["operations"]
        for arm_name in ("plan", "single"):
            arm = case[arm_name]
            for kind in ("requests", "results"):
                assert arm[kind]["characters"] > 0
                assert arm[kind]["tokens"] > 0
                assert len(arm[kind]["sha256"]) == 64

    summary = artifact["summary"]
    assert summary["cases"] == len(cases)
    assert summary["operations"] == sum(case["operations"] for case in cases)
    for arm_name in ("plan", "single"):
        for kind in ("requests", "results"):
            assert summary[arm_name][kind]["characters"] == sum(
                case[arm_name][kind]["characters"] for case in cases
            )
            assert summary[arm_name][kind]["tokens"] == sum(
                case[arm_name][kind]["tokens"] for case in cases
            )
    plan_tokens = sum(total(case["plan"]) for case in cases)
    single_tokens = sum(total(case["single"]) for case in cases)
    assert summary["tokens_saved"] == single_tokens - plan_tokens
    assert summary["savings_fraction"] == pytest.approx((single_tokens - plan_tokens) / single_tokens)


def test_tool_calls_use_the_same_envelopes_as_mcp():
    sys.path.insert(0, str(ROOT / "benchmarks"))
    from plan_token_savings import mcp_tool_call

    operation_name, arguments = mcp_tool_call("buffer_layer", {"input_path": "in.parquet"})
    assert operation_name == "run_operation"
    assert arguments == {
        "operation": "buffer_layer",
        "arguments": {"input_path": "in.parquet"},
    }

    plan = {"version": 1, "steps": []}
    operation_name, arguments = mcp_tool_call("validate_plan", plan)
    assert operation_name == "validate_plan"
    assert arguments == {"plan": plan}


def test_run_pins_a_relative_artifact_before_cases_change_the_cwd(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "benchmarks"))
    from plan_token_savings import run

    start = tmp_path / "start"
    elsewhere = tmp_path / "elsewhere"
    start.mkdir()
    elsewhere.mkdir()
    monkeypatch.chdir(start)
    monkeypatch.setattr("plan_token_savings.execute_plan_requests", lambda: [(7, None)])

    def measure_case(index, workdir):
        monkeypatch.chdir(elsewhere)
        arm = {
            "executed": True,
            "calls": 1,
            "requests": {"characters": 1, "tokens": 1, "sha256": "0" * 64},
            "results": {"characters": 1, "tokens": 1, "sha256": "1" * 64},
        }
        return {"index": index, "operations": 1, "plan": arm, "single": arm}

    monkeypatch.setattr("plan_token_savings.measure_case", measure_case)
    run(Path("savings.json"))

    artifact = json.loads((start / "savings.json").read_text(encoding="utf-8"))
    assert [case["index"] for case in artifact["cases"]] == [7]
    assert not (elsewhere / "savings.json").exists()
