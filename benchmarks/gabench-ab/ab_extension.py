"""MapSmith x GABench A/B extension: typed plans, optional validation gate.

Both arms share EVERYTHING except the gate:
- arm A: TypedPlanAgent emits a typed plan -> straight to the solver
- arm B: TypedPlanAgent emits a typed plan -> static validation against the
  live tool registry (unknown tool / missing / unknown / mistyped arguments,
  malformed steps) -> on errors the planner receives the machine-readable
  issues and repairs its plan (max 2 rounds) -> solver

No GABench source file is modified: we subclass and re-wire. Logs keep the
upstream history format so GABench's deterministic evaluator
(evaluation/step_by_step.py) runs unchanged on our .jsonl output.

Run from the GABench repo root (config.yaml/.env resolution).
"""

from __future__ import annotations

import difflib
import json
import re
import sys
from pathlib import Path
from typing import Any, AsyncGenerator

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import gabench_root  # noqa: E402

# required=False: the gate's pure functions are importable without the
# checkout (test_gate.py stubs the upstream agent import).
sys.path.insert(0, str(gabench_root(required=False)))

from agents.plan_and_react import PlanAgent  # noqa: E402

TYPED_PLANNER_PROMPT = """
You are a lead geospatial analyst.
Break the user request into a sequence of executable tool calls.

Available tools (name, arguments):
{tools}

Working Protocol:
Use the following format strictly:

Thought: [Analyze the user request and the available tools]
Plan:
```json
[
    {{
        "step_id": 1,
        "tool": "[exact tool name from the list]",
        "arguments": {{"arg_name": "value"}},
        "purpose": "[one line: why this step]"
    }},
    ...
]
```

Constraint Checklist:
1. The plan must be logical and sequential.
2. Every step MUST name one tool from the list and provide its arguments.
3. Use exact argument names as documented. Use file paths produced by
   earlier steps where needed.
4. Output MUST be a strictly valid JSON list wrapped in a markdown code block.

Begin!
"""

REPAIR_MESSAGE = """Your plan failed static validation. Fix ONLY the reported problems and output the full corrected plan in the same JSON format.

Validation errors:
{errors}
"""


def tool_signature_lines(tools: list[Any]) -> str:
    """Compact per-tool signature lines from the MCP inputSchema."""
    lines = []
    for tool in tools:
        name = getattr(tool, "name", None) or tool.get("name")
        schema = getattr(tool, "inputSchema", None) or tool.get("inputSchema") or {}
        props = schema.get("properties", {}) if isinstance(schema, dict) else {}
        required = set(schema.get("required", []) if isinstance(schema, dict) else [])
        args = []
        for arg, spec in props.items():
            kind = spec.get("type", "any") if isinstance(spec, dict) else "any"
            mark = "" if arg in required else "?"
            args.append(f"{arg}{mark}: {kind}")
        desc = (getattr(tool, "description", None) or tool.get("description", "") or "")
        desc = desc.split("Args:")[0].split("Returns:")[0].strip().split("\n")[0][:110]
        lines.append(f"- {name}({', '.join(args)}) — {desc}")
    return "\n".join(lines)


def build_tool_index(tools: list[Any]) -> dict[str, dict[str, Any]]:
    index = {}
    for tool in tools:
        name = getattr(tool, "name", None) or tool.get("name")
        schema = getattr(tool, "inputSchema", None) or tool.get("inputSchema") or {}
        props = schema.get("properties", {}) if isinstance(schema, dict) else {}
        required = list(schema.get("required", []) if isinstance(schema, dict) else [])
        index[name] = {"properties": props, "required": required}
    return index


_JSON_TYPES = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}


def validate_plan_steps(
    steps: list[dict[str, Any]], tool_index: dict[str, dict[str, Any]]
) -> list[dict[str, str]]:
    """Static validation of a typed plan against the live tool registry.

    Mirrors MapSmith's validate_plan philosophy: stable machine-actionable
    error codes the planner can act on, nothing executed.
    """
    issues: list[dict[str, str]] = []

    def issue(code: str, step: Any, message: str) -> None:
        issues.append({"code": code, "step_id": str(step), "message": message})

    if not isinstance(steps, list) or not steps:
        return [{"code": "EMPTY_PLAN", "step_id": "-", "message": "the plan has no steps"}]
    seen_ids = set()
    for raw in steps:
        if not isinstance(raw, dict):
            issue("MALFORMED_STEP", "-", f"step is not an object: {raw!r}")
            continue
        sid = raw.get("step_id", "-")
        if sid in seen_ids:
            issue("DUPLICATE_STEP_ID", sid, f"step_id {sid} is used more than once")
        seen_ids.add(sid)
        tool = raw.get("tool")
        if not tool:
            issue("MISSING_TOOL", sid, "step has no 'tool' field")
            continue
        if tool not in tool_index:
            close = difflib.get_close_matches(tool, list(tool_index), n=3, cutoff=0.6)
            hint = f" Did you mean: {', '.join(close)}?" if close else ""
            issue("UNKNOWN_TOOL", sid, f"tool '{tool}' does not exist.{hint}")
            continue
        spec = tool_index[tool]
        arguments = raw.get("arguments")
        if not isinstance(arguments, dict):
            issue("MALFORMED_ARGUMENTS", sid, "'arguments' must be an object")
            continue
        for req in spec["required"]:
            if req not in arguments:
                issue("MISSING_ARGUMENT", sid,
                      f"tool '{tool}': required argument '{req}' is missing")
        for arg, value in arguments.items():
            if spec["properties"] and arg not in spec["properties"]:
                issue("UNKNOWN_ARGUMENT", sid,
                      f"tool '{tool}': unknown argument '{arg}'; "
                      f"declared: {sorted(spec['properties'])}")
                continue
            prop = spec["properties"].get(arg, {})
            expected = _JSON_TYPES.get(prop.get("type"))
            if expected is None:
                continue
            bad_bool = isinstance(value, bool) and bool not in expected
            if bad_bool or not isinstance(value, expected):
                issue("WRONG_TYPE", sid,
                      f"tool '{tool}': argument '{arg}' should be "
                      f"{prop.get('type')}, got {type(value).__name__}")
    return issues


def make_compact_solver(solve_cls):
    """Factory: a solver whose system prompt embeds compact tool signatures
    instead of the raw tool objects (148k chars -> ~26k, the observed cost
    driver at ~$1.70/task). Applied to BOTH arms: identical change, the gate
    stays the only experimental variable. Declared in the write-up.
    """
    class _Compact(solve_cls):
        async def run(self, input):  # noqa: A002 - upstream signature
            if not self.history:
                if not self.tools:
                    await self.load_tools()
                self.sys_prompt = self.sys_prompt.format(
                    tools=tool_signature_lines(self.tools), subtasks=self.subtasks
                )
                self.history.append(
                    {"role": "system",
                     "content": [{"type": "text", "text": self.sys_prompt}]}
                )
            self.history.append(
                {"role": "user", "content": [{"type": "text", "text": input}]}
            )
            async for chunk in super()._run_loop():
                yield chunk

    return _Compact


class TypedPlanAgent(PlanAgent):
    """PlanAgent that emits typed steps (tool + arguments) instead of prose."""

    def __init__(self, mcp_clients=None, init_model_name: str = "gpt-4o"):
        super().__init__(mcp_clients, init_model_name, sys_prompt=TYPED_PLANNER_PROMPT)
        self.tool_index: dict[str, dict[str, Any]] = {}

    async def run(self, input: str) -> AsyncGenerator[str, None]:
        if not self.history:
            await self.load_tools()
            self.tool_index = build_tool_index(self.tools)
            self.sys_prompt = self.sys_prompt.format(
                tools=tool_signature_lines(self.tools)
            )
            self.history.append(
                {"role": "system", "content": [{"type": "text", "text": self.sys_prompt}]}
            )
        self.history.append(
            {"role": "user", "content": [{"type": "text", "text": input}]}
        )
        response_text = ""
        async for chunk in self.llm.async_stream_generate(prompt="", history=self.history):
            response_text += chunk
            yield chunk
        self.history.append(
            {"role": "assistant", "content": [{"type": "text", "text": response_text}]}
        )
        match = re.search(r"```json\s*(\[.*?\])\s*```", response_text, re.DOTALL)
        if not match:
            match = re.search(r"(\[.*\])", response_text, re.DOTALL)
        self.subtasks = match.group(1) if match else ""

    def parsed_steps(self) -> list[dict[str, Any]] | None:
        try:
            steps = json.loads(self.subtasks)
        except (json.JSONDecodeError, TypeError):
            return None
        return steps if isinstance(steps, list) else None


async def plan_with_optional_gate(
    planner: TypedPlanAgent, query: str, gate: bool, max_repairs: int = 2
) -> dict[str, Any]:
    """Run the planner; with gate=True, validate and feed errors back.

    Returns audit info: rounds, issues per round, and the final plan string.
    """
    audit: dict[str, Any] = {"gate": gate, "rounds": []}
    async for chunk in planner.run(query):
        print(chunk, end="", flush=True)
    for round_no in range(max_repairs + 1):
        steps = planner.parsed_steps()
        if steps is None:
            issues = [{"code": "UNPARSABLE_PLAN", "step_id": "-",
                       "message": "the plan is not a valid JSON list"}]
        else:
            issues = validate_plan_steps(steps, planner.tool_index)
        audit["rounds"].append({"round": round_no, "issues": issues})
        if not gate or not issues or round_no == max_repairs:
            break
        print(f"\n--- Validation round {round_no}: {len(issues)} issue(s), repairing ---")
        errors_text = "\n".join(
            f"- [{i['code']}] step {i['step_id']}: {i['message']}" for i in issues
        )
        async for chunk in planner.run(REPAIR_MESSAGE.format(errors=errors_text)):
            print(chunk, end="", flush=True)
    audit["final_plan"] = planner.subtasks
    audit["final_issue_count"] = len(audit["rounds"][-1]["issues"])
    return audit
