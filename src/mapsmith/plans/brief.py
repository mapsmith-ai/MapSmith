"""Deterministic project briefs rendered only from validated plans.

Every statement carries its source. No planner or model writes prose here:
operation declarations, argument contracts, validation findings and engine
availability are the only inputs.
"""

from __future__ import annotations

from typing import Any

from .. import catalog
from .models import Plan
from .registry import BINDINGS
from .validator import validate as validate_plan

SOURCE_KINDS = {"operation", "parameter", "validation", "engine", "install"}


def statement(text: str, kind: str, ref: str) -> dict[str, Any]:
    if kind not in SOURCE_KINDS or not text or not ref:
        raise ValueError("a brief statement needs text and a valid source")
    return {"text": text, "source": {"kind": kind, "ref": ref}}


def operation(name: str) -> dict[str, Any]:
    return next(op for op in catalog.OPERATIONS if op["name"] == name)


def render(plan: Plan) -> dict[str, Any]:
    """Validate now, then render; a caller cannot supply a stale valid report."""
    import json

    checked = validate_plan(plan)
    if not checked.valid:
        raise ValueError("an invalid plan has no project brief")
    steps = []
    decisions = []
    for step in plan.steps:
        entry = operation(step.operation)
        binding = BINDINGS[step.operation]
        item = statement(f"{step.id}: {entry['summary']}", "operation", step.operation)
        item["arguments"] = [
            {
                "name": name,
                "value": value,
                "source": {"kind": "parameter", "ref": f"{step.operation}.{name}"},
            }
            for name, value in step.arguments.items()
        ]
        steps.append(item)
        applicability = entry.get("applicability", {})
        if applicability.get("requires_projected_crs"):
            decisions.append(statement(
                f"{step.operation} requires a projected CRS; geographic inputs are refused.",
                "operation", step.operation,
            ))
        path_arguments = {*binding.input_args, *binding.list_input_args, binding.output_arg}
        params = {param["name"]: param for param in entry.get("parameters", [])}
        for name, value in step.arguments.items():
            if name in path_arguments:
                continue
            rendered = json.dumps(value, ensure_ascii=False, sort_keys=True)
            decisions.append(statement(
                f"{step.id}: {step.operation}.{name} = {rendered}. "
                f"{params[name]['description']}",
                "parameter", f"{step.operation}.{name}",
            ))
    findings = []
    for issue in [*checked.warnings, *checked.notes]:
        findings.append(statement(issue.message, "validation", issue.code))
    return {
        "goal": plan.goal,
        "plan_sha256": plan.sha256(),
        "steps": steps,
        "decisions": decisions,
        "validation": findings,
        "engines": engine_requirements(plan),
        "install": install_requirements(plan),
    }


def install_requirements(plan: Plan) -> list[dict[str, Any]]:
    return [
        statement(f"pip install mapsmith[{item['extra']}]", "install", item["engine"])
        for item in engine_requirements(plan)
        if not item["available"] and item["extra"]
    ]


def engine_requirements(plan: Plan) -> list[dict[str, Any]]:
    from ..engines import dispatch
    from .validator import EXTRA_FOR_FLAG

    available = dispatch.available_engines()
    required = []
    seen = set()
    for step in plan.steps:
        binding = BINDINGS.get(step.operation)
        if binding is None:
            continue
        requested = step.arguments.get("engine") if step.operation == "spatial_join" else None
        flag = requested if requested and requested != "auto" else binding.engine_flag or "core"
        if flag in seen:
            continue
        seen.add(flag)
        extra = None if flag in {"core", "duckdb", "geopandas", "qgis"} else (
            "sedona" if flag == "sedonadb" else EXTRA_FOR_FLAG.get(flag, flag)
        )
        required.append({
            "engine": flag,
            "available": True if flag == "core" else bool(available.get(flag)),
            "extra": extra,
            "source": {"kind": "engine", "ref": flag},
        })
    return required


def assert_traced(brief: dict[str, Any], plan: Plan | None = None) -> None:
    """Check sources and, when given the plan, the complete canonical rendering."""
    if plan is not None and brief != render(plan):
        raise ValueError("brief does not match the validated plan and its declared sources")

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            if "text" in value:
                source = value.get("source") or {}
                kind, ref = source.get("kind"), source.get("ref")
                if kind not in SOURCE_KINDS or not ref:
                    raise ValueError(f"untracked brief statement at {path}")
                if kind == "operation":
                    operation(ref)
                elif kind == "parameter":
                    operation_name, parameter_name = ref.split(".", 1)
                    entry = operation(operation_name)
                    if not any(param["name"] == parameter_name for param in entry.get("parameters", [])):
                        raise ValueError(f"unknown parameter reference {ref}")
            for key, item in value.items():
                walk(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    walk(brief, "brief")


def execute_plan_requests() -> list[tuple[int, dict[str, Any]]]:
    """The D-063 population: execute_plan as primary or defensible secondary."""
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "tests" / "data" / "discovery_queries.json"
    queries = json.loads(path.read_text(encoding="utf-8"))["queries"]
    found = []
    for index, query in enumerate(queries):
        answers = {
            query.get("label_fable"), query.get("label_human"),
            *(query.get("label_fable_also") or []), *(query.get("label_human_also") or []),
        }
        if "execute_plan" in answers:
            found.append((index, query))
    return found
