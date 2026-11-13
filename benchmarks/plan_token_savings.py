"""Measure serialized tool traffic for validated plans versus single-operation calls.

    python benchmarks/plan_token_savings.py

The measurement covers the sixteen executable D-063 cases. Token counts use
Anthropic's count_tokens endpoint; compact JSON payload texts are hashed, never
published with their random local paths.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
ARTIFACT = ROOT / "benchmarks" / "plan_token_savings.json"
MODEL = "claude-sonnet-5"

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "benchmarks"))

import method_choice as api
from relabel_discovery import call_with_retries

import brief_cases
from mapsmith import server
from mapsmith.plans.brief import execute_plan_requests


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def mcp_tool_call(name: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if name in ("validate_plan", "execute_plan"):
        return name, {"plan": arguments}
    return "run_operation", {"operation": name, "arguments": arguments}


def blob(calls: list[tuple[str, dict[str, Any], dict[str, Any]]]) -> tuple[str, str]:
    requests = "\n".join(compact({"tool": name, "arguments": arguments}) for name, arguments, _ in calls)
    results = "\n".join(compact(result) for _, _, result in calls)
    return requests, results


def count_tokens(text: str) -> int:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set in the environment")
    payload = {"model": MODEL, "messages": [{"role": "user", "content": text}]}
    request = urllib.request.Request(
        "https://api.anthropic.com/v1/messages/count_tokens",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2026-10-21",
            "anthropic-workspace-id": api.anthropic_workspace_id(key),
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)["input_tokens"]


def measure_blob(text: str) -> dict[str, Any]:
    return {
        "characters": len(text),
        "tokens": call_with_retries(lambda: count_tokens(text)),
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
    }


def measure_arm(calls: list[tuple[str, dict[str, Any], dict[str, Any]]], executed: bool) -> dict[str, Any]:
    requests, results = blob(calls)
    return {
        "executed": executed,
        "calls": len(calls),
        "requests": measure_blob(requests),
        "results": measure_blob(results),
    }


def measure_case(index: int, workdir: Path) -> dict[str, Any]:
    plan_dir = workdir / "plan"
    single_dir = workdir / "single"
    plan = brief_cases.case(index, plan_dir)
    plan_payload = plan.model_dump(mode="json")
    with mock.patch.dict(os.environ, {"MAPSMITH_WORKSPACE": str(plan_dir)}):
        validated = server.validate_plan(plan)
        executed = server.execute_plan(plan) if validated.get("valid") else {"executed": False}
    plan_calls = [
        (*mcp_tool_call("validate_plan", plan_payload), validated),
        (*mcp_tool_call("execute_plan", plan_payload), executed),
    ]

    single = brief_cases.case(index, single_dir)
    single_calls = []
    with mock.patch.dict(os.environ, {"MAPSMITH_WORKSPACE": str(single_dir)}):
        for step in single.steps:
            result = server.run_operation(step.operation, step.arguments)
            single_calls.append((*mcp_tool_call(step.operation, step.arguments), result))
    plan_ok = bool(validated.get("valid") and executed.get("executed"))
    single_ok = bool(single_calls) and all(result.get("ran") for _, _, result in single_calls)
    return {
        "index": index,
        "operations": len(single.steps),
        "plan": measure_arm(plan_calls, plan_ok),
        "single": measure_arm(single_calls, single_ok),
    }


def summarize(cases: list[dict[str, Any]]) -> dict[str, Any]:
    included = [case for case in cases if case["plan"]["executed"] and case["single"]["executed"]]
    summary: dict[str, Any] = {
        "cases": len(included),
        "operations": sum(case["operations"] for case in included),
        "excluded_cases": [case["index"] for case in cases if case not in included],
    }
    for arm_name in ("plan", "single"):
        summary[arm_name] = {
            kind: {
                "characters": sum(case[arm_name][kind]["characters"] for case in included),
                "tokens": sum(case[arm_name][kind]["tokens"] for case in included),
            }
            for kind in ("requests", "results")
        }
    plan_tokens = sum(
        case["plan"]["requests"]["tokens"] + case["plan"]["results"]["tokens"] for case in included
    )
    single_tokens = sum(
        case["single"]["requests"]["tokens"] + case["single"]["results"]["tokens"] for case in included
    )
    summary["tokens_saved"] = single_tokens - plan_tokens
    summary["savings_fraction"] = (single_tokens - plan_tokens) / single_tokens if single_tokens else None
    return summary


def run(out: Path = ARTIFACT) -> dict[str, Any]:
    out = out.resolve()
    document: dict[str, Any] = {
        "schema_version": 1,
        "measured_at": dt.datetime.now(dt.UTC).date().isoformat(),
        "model": MODEL,
        "measurement": "serialized compact JSON tool-call and result payloads; Anthropic count_tokens input tokens",
        "population": "the sixteen executable D-063 cases",
        "cases": [],
    }
    if out.exists():
        document = json.loads(out.read_text(encoding="utf-8"))
    completed = {case["index"] for case in document["cases"]}
    with tempfile.TemporaryDirectory(prefix="mapsmith-token-savings-", ignore_cleanup_errors=True) as temporary:
        for index, _ in execute_plan_requests():
            if index in completed:
                continue
            measured = measure_case(index, Path(temporary) / str(index))
            document["cases"].append(measured)
            document["summary"] = summarize(document["cases"])
            api.write_json(out, document)
            print(
                f"case {index}: plan={measured['plan']['executed']}, "
                f"single={measured['single']['executed']}", flush=True,
            )
    document["summary"] = summarize(document["cases"])
    api.write_json(out, document)
    return document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=ARTIFACT)
    args = parser.parse_args()
    document = run(args.out)
    print(json.dumps(document["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
