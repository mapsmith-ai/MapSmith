"""Measure GIS method choice, independently of single-step execution correctness.

The input is a goal, declared datasets and an output contract. The answer is a
sequence of catalog operations, not code or SQL. Argleton remains the instrument
for single-step silent corruption; this bench compares planning decisions with
teacher-written, defensible alternatives from the QGIS Training Manual.

    python benchmarks/method_choice.py validate
    python benchmarks/method_choice.py plan --split calibration --repeat 2
    python benchmarks/method_choice.py plan --split evaluation --repeat 2
    python benchmarks/method_choice.py score --split evaluation
    python benchmarks/method_choice.py plan --provider anthropic --split evaluation

Planning calls need GEMINI_API_KEY or, with `--provider anthropic`,
ANTHROPIC_API_KEY in the environment. Raw answers and API errors are separate:
an unattempted case or a quota failure is never a wrong plan.
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import functools
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from mapsmith import catalog
from mapsmith.plans import Plan
from mapsmith.plans.registry import BINDINGS
from mapsmith.plans.validator import _check_output_contract

DATA = ROOT / "benchmarks" / "method_choice_cases.json"
OUT = ROOT / ".method_choice"
MODEL = "gemini-3.6-flash"
DEFAULT_MODELS = {"gemini": MODEL, "anthropic": "claude-sonnet-5"}
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"
VERDICTS = ("exact", "reordered", "missing_operation", "extra_or_wrong_tool", "missing_and_wrong")


def load_cases(path: Path = DATA) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


def available_operations() -> dict[str, dict[str, Any]]:
    return {
        op["name"]: op
        for op in catalog.OPERATIONS
        if op.get("status") == "available" and op["name"] in BINDINGS
    }


def validate_cases(cases: list[dict[str, Any]]) -> dict[str, Any]:
    errors: list[str] = []
    names = available_operations()
    ids = [case.get("id") for case in cases]
    if len(ids) != len(set(ids)):
        errors.append("case ids are not unique")
    for case in cases:
        cid = case.get("id", "<missing>")
        if case.get("split") not in ("calibration", "evaluation"):
            errors.append(f"{cid}: invalid split")
        if not isinstance(case.get("inputs"), list):
            errors.append(f"{cid}: inputs must be a list (empty for literal-only cases)")
        if not case.get("goal") or not case.get("output"):
            errors.append(f"{cid}: goal and output are required")
        sequences = case.get("accepted_sequences") or []
        supported = case.get("supported", True)
        if not isinstance(supported, bool):
            errors.append(f"{cid}: supported must be a boolean")
            continue
        if not supported:
            if not case.get("unsupported_reason"):
                errors.append(f"{cid}: unsupported cases need an unsupported_reason")
            if sequences:
                errors.append(f"{cid}: unsupported cases must not have accepted sequences")
        else:
            if not sequences:
                errors.append(f"{cid}: no accepted sequence")
            for sequence in sequences:
                if not sequence:
                    errors.append(f"{cid}: empty accepted sequence")
                if len(sequence) != len(set(sequence)) and not case.get("allow_repeated_operations"):
                    errors.append(f"{cid}: repeated operations need arguments this bench does not model")
                unknown = set(sequence) - names.keys()
                if unknown:
                    errors.append(f"{cid}: unavailable operations {sorted(unknown)}")
        if not isinstance(case.get("required_assumption_keys"), list):
            errors.append(f"{cid}: required_assumption_keys must be a list")
    if errors:
        raise ValueError("\n".join(errors))
    supported_cases = [case for case in cases if case.get("supported", True)]
    return {
        "cases": len(cases),
        "unsupported_cases": len(cases) - len(supported_cases),
        "splits": dict(collections.Counter(case["split"] for case in cases)),
        "steps": dict(collections.Counter(str(len(case["accepted_sequences"][0])) for case in supported_cases)),
        "cases_with_required_assumptions": sum(bool(case["required_assumption_keys"]) for case in supported_cases),
    }


def parameter_rules(op: dict[str, Any]) -> str:
    rules = []
    for param in op.get("parameters", []):
        name = param["name"]
        if "choices" in param:
            rules.append(f"{name} in {param['choices']}")
        for key, operator in (
            ("minimum", ">="), ("maximum", "<="),
            ("exclusive_minimum", ">"), ("exclusive_maximum", "<"),
        ):
            if key in param:
                rules.append(f"{name} {operator} {param[key]}")
        if param.get("odd"):
            rules.append(f"{name} odd")
        if "min_items" in param:
            rules.append(f"{name} at least {param['min_items']} items")
        if "extensions" in param:
            rules.append(f"{name} ends with {param['extensions']}")
    step_rules = catalog.STEP_CONTRACTS.get(op["name"], [])
    if step_rules:
        rules.append(f"step rules: {json.dumps(step_rules, sort_keys=True)}")
    return "; ".join(rules)


def catalogue_text() -> str:
    lines = []
    for op in available_operations().values():
        applicability = op.get("applicability", {})
        rules = parameter_rules(op)
        geometry = applicability.get("geometry") or []
        constraints = f"; geometry: {', '.join(geometry)}" if geometry else ""
        parameters = ", ".join(
            f"{param['name']}:{param['type']}{' required' if param.get('required') else ' optional'}"
            for param in op.get("parameters", [])
        )
        guarantees = ", ".join(
            f"{key}={op[key]}" for key in ("output_geometry", "output_cardinality") if key in op
        )
        lines.append(
            f"- {op['name']}: {op['summary'].strip().rstrip('.')}. "
            f"[takes: {', '.join(applicability.get('inputs', []))}{constraints}; produces: {op.get('produces')}]"
            f" [arguments: {parameters or 'none'}]"
            + (f" [output guarantees: {guarantees}]" if guarantees else "")
            + (f" [rules: {rules}]" if rules else "")
        )
    return "\n".join(lines)


def describe_inputs(inputs: list[dict[str, Any]]) -> str:
    if not inputs:
        return "no files; all values are literal in the request"
    return "; ".join(
        f"input_{index}=a {item.get('kind')} layer"
        + (f" of {item['geometry']}s" if item.get("geometry") else "")
        + (f" ({item['role']})" if item.get("role") else "")
        for index, item in enumerate(inputs, start=1)
    )


def prompt_for(case: dict[str, Any]) -> str:
    output = case["output"]
    shape = (
        f"a {output.get('kind')}"
        + (f" of {output['geometry']}s" if output.get("geometry") else "")
        + f", cardinality={output.get('cardinality', 'unknown')}"
    )
    repetition = (
        "repeating an operation only for distinct arguments; independent branches are allowed "
        "when the task requests multiple results"
        if case.get("allow_repeated_operations") else "each at most once"
    )
    return f"""You are planning a GIS analysis with a fixed set of operations. You cannot write code or SQL
directly; run_sql is the allowed operation when a request needs SQL and no dedicated operation applies.
MAPSMITH_WORKSPACE is enabled for every case, so run_sql is SELECT-only.
You can only chain the operations listed below, {repetition}. Connect an output to a later step
only when the later step actually depends on it.

THE REQUEST:
{case['goal']}

WHAT THEY HAVE: {describe_inputs(case['inputs'])}
WHAT THEY WANT BACK: {shape}

THE OPERATIONS AVAILABLE (name: what it does [what it takes; what it produces]):
{catalogue_text()}

Write the shortest plan that answers the request. Return ONLY a JSON object, no markdown fence:
{{"steps": [{{"id": "<short unique name>", "operation": "<name from the list>",
   "arguments": {{"<declared argument>": "<literal value or $step_id>"}},
   "output": {{"kind": "<vector|raster|table|number|map|description>",
               "geometry": "<point|line|polygon|optional>",
               "cardinality": "<one|fewer|more|same_as_input|unknown>"}}}}}}],
 "assumptions": ["<key>=<value>", ...], "warnings": ["<material uncertainty", ...]}}
Every final branch must declare its output kind, geometry and cardinality. Arguments must name
inputs and parameters explicitly. The input_1, input_2, ... placeholders in WHAT THEY HAVE are
literal values for the original datasets: use them without a $ prefix. Use $step_id only when
a step consumes an earlier step output. References have no field selection: $step.field is invalid.
In run_sql, list every dataset used by the query in input_paths, using literal initial-input names
or $step_id references. Read an initial dataset with read_parquet('input_1') and an earlier output
with read_parquet('$step_id'). Each step's output describes what the operation actually produces under
its declared output guarantees, not the requested output: never relabel a vector as a table or map,
and never replace an operation's cardinality guarantee with the desired result's cardinality.
Every dataset-writing step needs output_path. If an operation already returns a requested value,
do not add another operation to recompute it: network_shortest_path returns total_cost, so do not
add measure_length for it. buffer_layer and centroid_layer must keep same_as_input even when the
request asks for one result. For example, {{"operation": "centroid_layer", "output":
{{"kind": "vector", "geometry": "point", "cardinality": "same_as_input"}}}} is correct even for one point.
Likewise, {{"operation": "simplify_layer", "output":
{{"kind": "vector", "geometry": "polygon", "cardinality": "same_as_input"}}}} is correct for one area.
service_area and reproject_raster require output_path. For example,
{{"operation": "service_area", "arguments": {{"output_path": "reachable.parquet"}}}} has a required output path.
select_features and simplify_layer require output_path. select_features produces vector, never table.
describe_dataset produces description, never table. Include every requested output, not just every
operation: if the request names multiple results, each needs its own output branch.
Operations returning an answer object, including compare_layers and summarize_field, must declare
kind=table for that report, never kind=answer.
summarize_points_in_polygons requires output_path.
Use only names from the list. Prefer dedicated operations over run_sql whenever they answer the
whole request. describe_crs reads no data: use it for a CRS already identified by code or text;
for a lookup inside a spatial_ref_sys table, use run_sql. For geometric centres, use centroid_layer.
A reprojected vector can be a mappable artifact when no rendering operation is available; do not
refuse it merely because it is not an image. When a declared key joins two datasets, use join_table
or run_sql, not nearest_join as a substitute for the declared relationship.
dissolve_layer and run_sql already use different engines, so a multi-engine comparison is supported
when the operations themselves differ. When the request asks to compare two results, include a final
comparison step such as run_sql, nearest_join or merge_layers.
For a repeated operation, use comment to name its input and distinguishing arguments.
comment belongs at step level, never inside arguments.
Preserve every requested selection and explicit constraint, including a request without an intermediate
layer. When the request says "one of" a layer's features, use select_features with an explicit
criterion instead of processing every feature. The shortest plan has no redundant steps but never
drops required selections or transformations.
Use the declared data rather than external geographic knowledge. Declare CRS, units, selection
scope, boundary rules and other material assumptions as short key=value strings when the request
does not settle them.
Do not add reading, saving, plan-validation or plan-execution steps. Do not add extract_layer merely
to read an input; it is only for choosing a named layer in a multi-layer container. Do not substitute
a read-only approximation for a write, iteration or multi-engine request you cannot fulfill as asked.
If any required part cannot be fulfilled as asked, return an empty plan with a warning instead.
A warning does not make an approximation or a partially fulfilled request acceptable. Invalid
approximations include a filtered copy for a delete, appended rows for an insert, separate files
for one required container, and one combined output for per-feature outputs. INSERT, DELETE or UPDATE
of original data must be refused rather than simulated with UNION ALL or rewritten files. Repeated
steps sharing the same output_path do not create a single container with independent layers. CREATE TABLE
and other DDL must be refused. CREATE OR REPLACE VIEW and modifying a saved view are DDL:
return an empty plan, not a sorted copy. If one GeoPackage with multiple layers is required, separate files
are not a valid answer: return an empty plan."""


def parse_response(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1].rsplit("```", 1)[0]
    start, end = stripped.find("{"), stripped.rfind("}")
    if start < 0 or end < start:
        raise ValueError("planner returned no JSON object")
    answer = json.loads(stripped[start:end + 1])
    if "steps" in answer:
        if not isinstance(answer["steps"], list):
            raise TypeError("planner JSON has no steps list")
        for step in answer["steps"]:
            if isinstance(step, dict) and isinstance(step.get("output"), dict):
                geometry = step["output"].get("geometry")
                if isinstance(geometry, str) and geometry.lower() in {"optional", "none", ""}:
                    step["output"].pop("geometry")
        if answer["steps"]:
            typed = Plan.model_validate({"goal": answer.get("goal", ""), "steps": answer["steps"]})
            answer["plan"] = [step.operation for step in typed.steps]
        else:
            answer["plan"] = []
    else:
        if not isinstance(answer.get("plan"), list):
            raise TypeError("planner JSON has no plan list")
        if not all(isinstance(step, dict) and isinstance(step.get("operation"), str) for step in answer["plan"]):
            raise TypeError("every plan step needs an operation string")
        answer["plan"] = [step["operation"] for step in answer["plan"]]
    for key in ("assumptions", "warnings"):
        value = answer.setdefault(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise TypeError(f"{key} must be a list of strings")
    return answer


def ask_gemini(prompt: str, model: str, timeout: int = 120) -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set in the environment")
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }).encode()
    request = urllib.request.Request(
        GEMINI_ENDPOINT.format(model=model), data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.load(response)
    return data["candidates"][0]["content"]["parts"][0]["text"]


@functools.lru_cache(maxsize=8)
def anthropic_workspace_id(key: str) -> str:
    configured = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
    if configured:
        return configured
    request = urllib.request.Request(
        "https://api.anthropic.com/v1/organizations/workspaces?include_default=true&include_archived=false",
        headers={"x-api-key": key, "anthropic-version": "2026-10-21"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        workspaces = json.load(response).get("data", [])
    default = [workspace for workspace in workspaces if workspace.get("name") == "Default"]
    if len(default) == 1:
        return default[0]["id"]
    if len(workspaces) == 1:
        return workspaces[0]["id"]
    raise RuntimeError("set ANTHROPIC_WORKSPACE_ID to choose the workspace for this baseline")


def ask_anthropic(prompt: str, model: str, timeout: int = 120, temperature: float | None = None) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set in the environment")
    payload = {
        "model": model,
        "max_tokens": 16384,
        "messages": [{"role": "user", "content": prompt}],
    }
    if temperature is not None:
        payload["temperature"] = temperature
    body = json.dumps(payload).encode()
    request = urllib.request.Request(
        ANTHROPIC_ENDPOINT, data=body,
        headers={
            "Content-Type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2026-10-21",
            "anthropic-workspace-id": anthropic_workspace_id(key),
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.load(response)
    return "\n".join(block["text"] for block in data["content"] if block.get("type") == "text")


def retryable(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or exc.code >= 500
    return isinstance(exc, (urllib.error.URLError, TimeoutError))


def validate_arm(provider: str, temperature: float | None) -> None:
    if provider == "gemini" and temperature is not None:
        raise ValueError("Gemini's benchmark request is fixed at temperature zero; do not relabel it")


def plan_path(out: Path, model: str, provider: str = "gemini", temperature: float | None = None) -> Path:
    if provider == "gemini" and temperature is None:
        return out / f"plans-{model}.json"
    arm = "default" if temperature is None else f"temperature-{temperature:g}"
    return out / f"plans-{model}-{provider}-{arm}.json"


def score_path(out: Path, model: str, provider: str = "gemini", temperature: float | None = None) -> Path:
    if provider == "gemini" and temperature is None:
        return out / f"scores-{model}.json"
    arm = "default" if temperature is None else f"temperature-{temperature:g}"
    return out / f"scores-{model}-{provider}-{arm}.json"


def current_responses(
    record: dict[str, Any], case: dict[str, Any], model: str,
    provider: str = "gemini", temperature: float | None = None, frozen: bool = False,
) -> list[dict[str, Any]]:
    if record.get("split") != case["split"]:
        return []
    prompt_hash = hashlib.sha256(prompt_for(case).encode()).hexdigest()
    return [
        response for response in record.get("responses", [])
        if response.get("model") == model
        and response.get("provider", "gemini") == provider
        and response.get("temperature") == temperature
        and (frozen or response.get("prompt_sha256") == prompt_hash)
    ]


def current_events(
    record: dict[str, Any], case: dict[str, Any], model: str, key: str,
    provider: str = "gemini", temperature: float | None = None, frozen: bool = False,
) -> list[dict[str, Any]]:
    if record.get("split") != case["split"]:
        return []
    prompt_hash = hashlib.sha256(prompt_for(case).encode()).hexdigest()
    return [
        event for event in record.get(key, [])
        if event.get("split") == case["split"]
        and event.get("model") == model
        and event.get("provider", "gemini") == provider
        and event.get("temperature") == temperature
        and (frozen or event.get("prompt_sha256") == prompt_hash)
    ]


def now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


PRIVATE_EVIDENCE_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]"),
    re.compile(r"(?<!\w)/(?:home|Users|tmp|var|etc)/"),
    re.compile(r"(?i)\b(?:lavoro|onedrive|" + "where" "tech" r"|wt" "ca" r"|gc" "apo" r")\b"),
    re.compile(r"(?i)\b(?:api[_-]?key|secret|password|access[_-]?token|refresh[_-]?token)\s*[:=]\s*\S+"),
    re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----"),
)


def assert_public_evidence(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for pattern in PRIVATE_EVIDENCE_PATTERNS:
        match = pattern.search(text)
        if match:
            raise ValueError(f"private content in public evidence {path}: {match.group(0)[:80]}")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for attempt in range(5):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05)


def error_detail(exc: BaseException) -> str:
    detail = str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = exc.read()
            if body:
                detail += "; " + body.decode("utf-8", errors="replace")
        except (OSError, AttributeError):
            pass
    return detail[:500]


def plan_cases(
    cases: list[dict[str, Any]], out: Path, model: str, repeat: int, limit: int | None,
    provider: str = "gemini", temperature: float | None = None,
) -> None:
    validate_arm(provider, temperature)
    path = plan_path(out, model, provider, temperature)
    document = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"records": {}}
    records = document.setdefault("records", {})
    prompt_hash = hashlib.sha256(catalogue_text().encode()).hexdigest()
    for case in cases[:limit]:
        record = records.setdefault(case["id"], {"split": case["split"], "responses": [], "api_errors": []})
        valid = current_responses(record, case, model, provider, temperature)
        if len(valid) != len(record["responses"]):
            record.setdefault("stale_responses", []).extend(
                response for response in record["responses"] if response not in valid
            )
        record["responses"] = valid
        record.setdefault("invalid_responses", [])
        for key in ("api_errors", "invalid_responses"):
            events = current_events(record, case, model, key, provider, temperature)
            record.setdefault(f"stale_{key}", []).extend(
                event for event in record[key] if event not in events
            )
            record[key] = events
        record["split"] = case["split"]
        while len(record["responses"]) < repeat:
            attempt = len(record["responses"]) + 1
            prompt = prompt_for(case)
            answered = False
            for retry in range(3):
                try:
                    if provider == "gemini":
                        raw = ask_gemini(prompt, model)
                    elif temperature is None:
                        raw = ask_anthropic(prompt, model)
                    else:
                        raw = ask_anthropic(prompt, model, temperature=temperature)
                except (urllib.error.URLError, TimeoutError) as exc:
                    event = {
                        "attempt": attempt, "retry": retry + 1, "time": now(),
                        "error": error_detail(exc), "split": case["split"], "model": model,
                        "provider": provider, "temperature": temperature,
                        "catalogue_sha256": prompt_hash,
                        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                    }
                    if isinstance(exc, urllib.error.HTTPError):
                        event["http_status"] = exc.code
                    record["api_errors"].append(event)
                    write_json(path, document)
                    if not retryable(exc):
                        break
                    time.sleep(2)
                    continue
                try:
                    answer = parse_response(raw)
                except (TypeError, ValueError) as exc:
                    record["invalid_responses"].append({
                        "attempt": attempt, "time": now(), "raw": raw, "error": str(exc)[:500],
                        "split": case["split"], "model": model,
                        "provider": provider, "temperature": temperature,
                        "catalogue_sha256": prompt_hash,
                        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                    })
                    write_json(path, document)
                    break
                answered = True
                break
            if not answered:
                break
            record["responses"].append({
                "attempt": attempt, "time": now(), "model": model, "provider": provider,
                "catalogue_sha256": prompt_hash,
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "raw": raw, "temperature": temperature,
                "plan": answer["plan"], "steps": answer.get("steps", []),
                "assumptions": answer["assumptions"], "warnings": answer["warnings"],
            })
            write_json(path, document)
            time.sleep(1)
        record["api_errors"] = record["api_errors"][-20:]
        record["invalid_responses"] = record["invalid_responses"][-20:]
        write_json(path, document)
        print(f"{case['id']}: {len(record['responses'])}/{repeat} answers", flush=True)


def verdict_for(got: list[str], accepted: list[list[str]]) -> str:
    if not got:
        return "no_plan"
    if got in accepted:
        return "exact"
    candidates = []
    got_counts = collections.Counter(got)
    for sequence in accepted:
        counts = collections.Counter(sequence)
        missing = counts - got_counts
        extra = got_counts - counts
        if not missing and not extra:
            verdict = "reordered"
        elif missing and not extra:
            verdict = "missing_operation"
        elif extra and not missing:
            verdict = "extra_or_wrong_tool"
        else:
            verdict = "missing_and_wrong"
        candidates.append(verdict)
    return min(candidates, key=VERDICTS.index)


def output_matches(case: dict[str, Any], got: list[str], steps: list[dict[str, Any]] | None = None) -> bool:
    if steps:
        typed = Plan.model_validate({"goal": case["goal"], "steps": steps})
        operations = available_operations()
        for step in typed.steps:
            if step.operation not in operations:
                return False
            errors = []
            _check_output_contract(step, operations[step.operation], errors)
            if errors:
                return False
        from mapsmith.plans.models import REFERENCE

        referenced = set()
        for step in typed.steps:
            for value in step.arguments.values():
                for item in value if isinstance(value, list) else [value]:
                    if isinstance(item, str) and REFERENCE.fullmatch(item):
                        referenced.add(item[1:])
        leaves = [step for step in typed.steps if step.id not in referenced]
        if not leaves or any(step.output is None for step in leaves):
            return False
        expected = case["output"]
        compatible = {
            "vector": {"vector"}, "raster": {"raster"}, "map": {"map", "vector"},
            "table": {"table", "description", "vector", "number"},
            "number": {"number", "table", "description"},
            "description": {"description"},
        }
        expected_cardinality = expected.get("cardinality", "unknown")
        output_steps = [step for step in typed.steps if step.output is not None]
        if expected_cardinality == "one" and len(leaves) != 1:
            return False
        for step in leaves:
            contract = step.output
            if contract.kind not in compatible.get(expected["kind"], set()):
                return False
            if expected.get("geometry") and contract.geometry != expected["geometry"]:
                return False
            if expected_cardinality == "more":
                if len(output_steps) == 1 and contract.cardinality != "more":
                    return False
            elif expected_cardinality not in {"unknown", "one"} and contract.cardinality != expected_cardinality:
                return False
        return True
    if not got or got[-1] not in available_operations():
        return False
    if got[-1] not in {sequence[-1] for sequence in case["accepted_sequences"]}:
        return False
    produced = available_operations()[got[-1]].get("produces")
    expected = {
        "vector": {"dataset:vector", "plan_result"},
        "raster": {"dataset:raster", "plan_result"},
        "map": {"dataset:vector", "plan_result"},
        "table": {"answer", "dataset:vector", "description", "plan_result"},
        "number": {"answer", "dataset:vector", "description", "plan_result"},
    }
    return produced in expected.get(case["output"]["kind"], set())


def declared_keys(response: dict[str, Any]) -> set[str]:
    return {
        item.split("=", 1)[0].strip().lower()
        for item in response.get("assumptions", [])
        if "=" in item and item.split("=", 1)[0].strip() and item.split("=", 1)[1].strip()
    }


def relevant_warnings(
    response: dict[str, Any], unknown: list[str], output_ok: bool, missing_keys: list[str], verdict: str,
) -> bool:
    text = " ".join(item.strip().lower() for item in response.get("warnings", []) if item.strip())
    if not text:
        return False
    required = []
    if unknown:
        required.append(unknown)
    if not output_ok:
        required.append(["output", "result", "contract"])
    if missing_keys:
        required.append([*missing_keys, "assumption"])
    if verdict != "exact":
        required.append(["plan", "operation", "step", "missing", "wrong", "order"])
    return all(any(token in text for token in alternatives) for alternatives in required)


def _argument_strings(value: Any):
    if isinstance(value, dict):
        for item in value.values():
            yield from _argument_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _argument_strings(item)
    elif isinstance(value, str):
        yield value


def structural_error_codes(
    steps: list[dict[str, Any]] | None, input_count: int | None = None
) -> list[str]:
    if not steps:
        return []
    from mapsmith.plans.validator import validate_structure

    try:
        typed = Plan.model_validate({"goal": "", "steps": steps})
    except (TypeError, ValueError):
        return ["INVALID_WIRE_PLAN"]
    errors = [issue.code for issue in validate_structure(typed).errors]
    if input_count is not None:
        for step in typed.steps:
            for text in _argument_strings(step.arguments):
                for match in re.finditer(r"(?<!\w)input_(\d+)\b", text):
                    if not 1 <= int(match.group(1)) <= input_count:
                        errors.append("UNKNOWN_INPUT_PLACEHOLDER")
    return sorted(set(errors))


def score_response(case: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    got = response.get("plan") or []
    accepted = case["accepted_sequences"]
    unknown = sorted(set(got) - available_operations().keys())
    structural_errors = structural_error_codes(response.get("steps"), len(case["inputs"]))
    verdict = "invalid_plan" if structural_errors else verdict_for(got, accepted)
    output_ok = not structural_errors and output_matches(case, got, response.get("steps"))
    missing_keys = sorted(set(case["required_assumption_keys"]) - declared_keys(response))
    disclosed = relevant_warnings(response, unknown, output_ok, missing_keys, verdict)
    return {
        "id": case["id"],
        "split": case["split"],
        "plan": got,
        "verdict": verdict,
        "structural_errors": structural_errors,
        "unknown_names": unknown,
        "first_step_right": not structural_errors and bool(got) and any(
            got[0] == sequence[0] for sequence in accepted
        ),
        "every_required_step_present": not structural_errors and bool(got) and any(
            not (collections.Counter(sequence) - collections.Counter(got)) for sequence in accepted
        ),
        "output_contract": output_ok,
        "missing_assumption_keys": missing_keys,
        "assumptions_declared": not missing_keys,
        "silent_planning_error": bool(got) and not disclosed and (
            verdict != "exact" or not output_ok or bool(missing_keys) or bool(unknown)
        ),
    }


def response_signature(response: dict[str, Any]) -> tuple[Any, ...]:
    steps = response.get("steps") or []
    plan_fingerprint = (
        Plan.model_validate({"goal": "", "steps": steps}).sha256()
        if steps else json.dumps(response.get("plan") or [], separators=(",", ":"))
    )
    return (
        plan_fingerprint,
        sorted(item.strip().lower() for item in response.get("assumptions", []) if item.strip()),
        sorted(item.strip().lower() for item in response.get("warnings", []) if item.strip()),
    )


def score_cases(
    cases: list[dict[str, Any]], out: Path, model: str,
    provider: str = "gemini", temperature: float | None = None, frozen: bool = False,
) -> dict[str, Any]:
    validate_arm(provider, temperature)
    path = plan_path(out, model, provider, temperature)
    records = json.loads(path.read_text(encoding="utf-8"))["records"] if path.exists() else {}
    rows = []
    api_errors = invalid_cases = not_attempted = 0
    repeated = reproducible = 0
    answered = unsupported_answered = 0
    unsupported_refused = unsupported_warned = unsupported_with_plan = 0
    unsupported_empty = unsupported_irrelevant = 0
    for case in cases:
        record = records.get(case["id"], {})
        responses = current_responses(record, case, model, provider, temperature, frozen)
        if not responses:
            if current_events(record, case, model, "api_errors", provider, temperature, frozen):
                api_errors += 1
            elif current_events(record, case, model, "invalid_responses", provider, temperature, frozen):
                invalid_cases += 1
            else:
                not_attempted += 1
            continue
        answered += 1
        if not case.get("supported", True):
            unsupported_answered += 1
            if responses[0].get("plan"):
                unsupported_with_plan += 1
                continue
            unsupported_refused += 1
            warnings = [item.strip() for item in responses[0].get("warnings", []) if item.strip()]
            if not warnings:
                unsupported_empty += 1
                continue
            text = " ".join(warnings).lower()
            terms = (
                "no operation", "no available", "no listed", "cannot", "can't",
                "not support", "read-only", "outside the declared", "not executable", "unable",
            )
            if any(term in text for term in terms):
                unsupported_warned += 1
            else:
                unsupported_irrelevant += 1
            continue
        row = score_response(case, responses[0])
        if len(responses) > 1:
            repeated += 1
            signature = response_signature(responses[0])
            same = all(response_signature(response) == signature for response in responses[1:])
            row["reproducible"] = same
            reproducible += same
        rows.append(row)
    verdicts = collections.Counter(row["verdict"] for row in rows)
    answered_ids = {row["id"] for row in rows}
    applicable = sum(bool(case["required_assumption_keys"]) for case in cases if case["id"] in answered_ids)
    assumptions_declared = sum(
        row["assumptions_declared"] for row in rows
        if next(case for case in cases if case["id"] == row["id"])["required_assumption_keys"]
    )
    summary = {
        "model": model,
        "provider": provider,
        "temperature": temperature,
        "split": cases[0]["split"] if cases and all(case["split"] == cases[0]["split"] for case in cases) else "all",
        "cases": len(cases),
        "scored_cases": len(rows),
        "unsupported_cases": sum(not case.get("supported", True) for case in cases),
        "unsupported_answered": unsupported_answered,
        "unsupported_refused": unsupported_refused,
        "unsupported_warned": unsupported_warned,
        "unsupported_empty_without_warning": unsupported_empty,
        "unsupported_irrelevant_warning": unsupported_irrelevant,
        "unsupported_with_plan": unsupported_with_plan,
        "attempted": answered + api_errors + invalid_cases,
        "answered": answered,
        "api_errors": api_errors,
        "invalid_response_cases": invalid_cases,
        "not_attempted": not_attempted,
        "api_error_events": sum(
            len(current_events(records.get(case["id"], {}), case, model, "api_errors", provider, temperature, frozen))
            for case in cases
        ),
        "invalid_response_events": sum(
            len(current_events(records.get(case["id"], {}), case, model, "invalid_responses", provider, temperature, frozen))
            for case in cases
        ),
        "complete": answered == len(cases),
        "verdicts": dict(verdicts),
        "exact_rate": round(verdicts["exact"] / len(rows), 4) if rows else None,
        "first_step_right": sum(row["first_step_right"] for row in rows),
        "every_required_step_present": sum(row["every_required_step_present"] for row in rows),
        "output_contract": sum(row["output_contract"] for row in rows),
        "assumptions_applicable": applicable,
        "assumptions_declared": assumptions_declared,
        "silent_planning_errors": sum(row["silent_planning_error"] for row in rows),
        "repeated_cases": repeated,
        "reproducible_cases": reproducible,
    }
    return {"summary": summary, "rows": rows}


def selected(args: argparse.Namespace) -> list[dict[str, Any]]:
    cases = load_cases(Path(args.data))
    validate_cases(cases)
    if args.split != "all":
        cases = [case for case in cases if case["split"] == args.split]
    return cases


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "plan", "score"))
    parser.add_argument("--data", default=str(DATA))
    parser.add_argument("--out", default=str(OUT))
    parser.add_argument("--provider", choices=("gemini", "anthropic"), default="gemini")
    parser.add_argument("--model")
    parser.add_argument("--split", choices=("calibration", "evaluation", "all"), default="all")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--temperature", type=float)
    parser.add_argument(
        "--frozen", action="store_true",
        help="score archived responses without requiring today's prompt hash",
    )
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    model = args.model or DEFAULT_MODELS[args.provider]
    cases = selected(args)
    out = Path(args.out)
    if args.command == "validate":
        print(json.dumps(validate_cases(cases), indent=2))
    elif args.command == "plan":
        plan_cases(cases, out, model, args.repeat, args.limit, provider=args.provider, temperature=args.temperature)
    else:
        report = score_cases(cases, out, model, args.provider, args.temperature, frozen=args.frozen)
        write_json(score_path(out, model, args.provider, args.temperature), report)
        print(json.dumps(report["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
