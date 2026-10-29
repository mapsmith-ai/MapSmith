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
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from mapsmith import catalog

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
    return {op["name"]: op for op in catalog.OPERATIONS if op.get("status") == "available"}


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
    return {
        "cases": len(cases),
        "splits": dict(collections.Counter(case["split"] for case in cases)),
        "steps": dict(collections.Counter(str(len(case["accepted_sequences"][0])) for case in cases)),
        "cases_with_required_assumptions": sum(bool(case["required_assumption_keys"]) for case in cases),
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
        lines.append(
            f"- {op['name']}: {op['summary'].strip().rstrip('.')}. "
            f"[takes: {', '.join(applicability.get('inputs', []))}; produces: {op.get('produces')}]"
            + (f" [rules: {rules}]" if rules else "")
        )
    return "\n".join(lines)


def describe_inputs(inputs: list[dict[str, Any]]) -> str:
    if not inputs:
        return "no files; all values are literal in the request"
    return "; ".join(
        f"a {item.get('kind')} layer"
        + (f" of {item['geometry']}s" if item.get("geometry") else "")
        + (f" ({item['role']})" if item.get("role") else "")
        for item in inputs
    )


def prompt_for(case: dict[str, Any]) -> str:
    output = case["output"]
    shape = f"a {output.get('kind')}" + (f" of {output['geometry']}s" if output.get("geometry") else "")
    repetition = (
        "repeating an operation only when the task requires distinct arguments"
        if case.get("allow_repeated_operations") else "each at most once"
    )
    return f"""You are planning a GIS analysis with a fixed set of operations. You cannot write code or SQL;
you can only chain the operations listed below, {repetition}, output feeding the next.

THE REQUEST:
{case['goal']}

WHAT THEY HAVE: {describe_inputs(case['inputs'])}
WHAT THEY WANT BACK: {shape}

THE OPERATIONS AVAILABLE (name: what it does [what it takes; what it produces]):
{catalogue_text()}

Write the shortest plan that answers the request. Return ONLY a JSON object, no markdown fence:
{{"plan": [{{"operation": "<name from the list>", "why": "<one short clause>"}}, ...],
 "assumptions": ["<key>=<value>", ...], "warnings": ["<material uncertainty", ...]}}
Use only names from the list. Declare CRS, units, selection scope, boundary rules and other
material assumptions as short key=value strings when the request does not settle them.
Do not add reading or saving steps. Do not invent a result when no listed operation applies;
return an empty plan with a warning instead."""


def parse_response(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1].rsplit("```", 1)[0]
    start, end = stripped.find("{"), stripped.rfind("}")
    if start < 0 or end < start:
        raise ValueError("planner returned no JSON object")
    answer = json.loads(stripped[start:end + 1])
    if not isinstance(answer.get("plan"), list):
        raise TypeError("planner JSON has no plan list")
    if not all(isinstance(step, dict) and isinstance(step.get("operation"), str) for step in answer["plan"]):
        raise TypeError("every plan step needs an operation string")
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


def ask_anthropic(prompt: str, model: str, timeout: int = 120) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set in the environment")
    body = json.dumps({
        "model": model,
        "max_tokens": 16384,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
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


def current_responses(record: dict[str, Any], case: dict[str, Any], model: str) -> list[dict[str, Any]]:
    if record.get("split") != case["split"]:
        return []
    prompt_hash = hashlib.sha256(prompt_for(case).encode()).hexdigest()
    return [
        response for response in record.get("responses", [])
        if response.get("model") == model and response.get("prompt_sha256") == prompt_hash
    ]


def current_events(record: dict[str, Any], case: dict[str, Any], model: str, key: str) -> list[dict[str, Any]]:
    if record.get("split") != case["split"]:
        return []
    prompt_hash = hashlib.sha256(prompt_for(case).encode()).hexdigest()
    return [
        event for event in record.get(key, [])
        if event.get("split") == case["split"]
        and event.get("model") == model
        and event.get("prompt_sha256") == prompt_hash
    ]


def now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


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


def plan_cases(
    cases: list[dict[str, Any]], out: Path, model: str, repeat: int, limit: int | None,
    provider: str = "gemini",
) -> None:
    path = out / f"plans-{model}.json"
    document = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"records": {}}
    records = document.setdefault("records", {})
    prompt_hash = hashlib.sha256(catalogue_text().encode()).hexdigest()
    for case in cases[:limit]:
        record = records.setdefault(case["id"], {"split": case["split"], "responses": [], "api_errors": []})
        valid = current_responses(record, case, model)
        if len(valid) != len(record["responses"]):
            record.setdefault("stale_responses", []).extend(
                response for response in record["responses"] if response not in valid
            )
        record["responses"] = valid
        record.setdefault("invalid_responses", [])
        for key in ("api_errors", "invalid_responses"):
            events = current_events(record, case, model, key)
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
                    raw = ask_gemini(prompt, model) if provider == "gemini" else ask_anthropic(prompt, model)
                except (urllib.error.URLError, TimeoutError) as exc:
                    record["api_errors"].append({
                        "attempt": attempt, "retry": retry + 1, "time": now(), "error": str(exc)[:500],
                        "split": case["split"], "model": model,
                        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                    })
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
                        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                    })
                    write_json(path, document)
                    break
                answered = True
                break
            if not answered:
                break
            record["responses"].append({
                "attempt": attempt,
                "time": now(),
                "model": model,
                "catalogue_sha256": prompt_hash,
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "raw": raw,
                "plan": [step["operation"] for step in answer["plan"]],
                "assumptions": answer["assumptions"],
                "warnings": answer["warnings"],
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


def output_matches(case: dict[str, Any], got: list[str]) -> bool:
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


def score_response(case: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    got = response.get("plan") or []
    accepted = case["accepted_sequences"]
    unknown = sorted(set(got) - available_operations().keys())
    verdict = verdict_for(got, accepted)
    output_ok = output_matches(case, got)
    missing_keys = sorted(set(case["required_assumption_keys"]) - declared_keys(response))
    disclosed = relevant_warnings(response, unknown, output_ok, missing_keys, verdict)
    return {
        "id": case["id"],
        "split": case["split"],
        "plan": got,
        "verdict": verdict,
        "unknown_names": unknown,
        "first_step_right": bool(got) and any(got[0] == sequence[0] for sequence in accepted),
        "every_required_step_present": bool(got) and any(
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
    return (
        response.get("plan") or [],
        sorted(item.strip().lower() for item in response.get("assumptions", []) if item.strip()),
        sorted(item.strip().lower() for item in response.get("warnings", []) if item.strip()),
    )


def score_cases(cases: list[dict[str, Any]], out: Path, model: str) -> dict[str, Any]:
    path = out / f"plans-{model}.json"
    records = json.loads(path.read_text(encoding="utf-8"))["records"] if path.exists() else {}
    rows = []
    api_errors = invalid_cases = not_attempted = 0
    repeated = reproducible = 0
    for case in cases:
        record = records.get(case["id"], {})
        responses = current_responses(record, case, model)
        if not responses:
            if current_events(record, case, model, "api_errors"):
                api_errors += 1
            elif current_events(record, case, model, "invalid_responses"):
                invalid_cases += 1
            else:
                not_attempted += 1
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
    answered = len(rows)
    answered_ids = {row["id"] for row in rows}
    applicable = sum(bool(case["required_assumption_keys"]) for case in cases if case["id"] in answered_ids)
    assumptions_declared = sum(
        row["assumptions_declared"] for row in rows
        if next(case for case in cases if case["id"] == row["id"])["required_assumption_keys"]
    )
    summary = {
        "model": model,
        "split": cases[0]["split"] if cases and all(case["split"] == cases[0]["split"] for case in cases) else "all",
        "cases": len(cases),
        "attempted": answered + api_errors + invalid_cases,
        "answered": answered,
        "api_errors": api_errors,
        "invalid_response_cases": invalid_cases,
        "not_attempted": not_attempted,
        "api_error_events": sum(
            len(current_events(records.get(case["id"], {}), case, model, "api_errors")) for case in cases
        ),
        "invalid_response_events": sum(
            len(current_events(records.get(case["id"], {}), case, model, "invalid_responses")) for case in cases
        ),
        "complete": answered == len(cases),
        "verdicts": dict(verdicts),
        "exact_rate": round(verdicts["exact"] / answered, 4) if answered else None,
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
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")
    model = args.model or DEFAULT_MODELS[args.provider]
    cases = selected(args)
    out = Path(args.out)
    if args.command == "validate":
        print(json.dumps(validate_cases(cases), indent=2))
    elif args.command == "plan":
        plan_cases(cases, out, model, args.repeat, args.limit, provider=args.provider)
    else:
        report = score_cases(cases, out, model)
        write_json(out / f"scores-{model}.json", report)
        print(json.dumps(report["summary"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
