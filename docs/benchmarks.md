# Does plan validation help GIS agents? An A/B on GABench

MapSmith's core bet is that agents fail at geoprocessing less because they
"don't know GIS" and more because they hallucinate interfaces: tools that do
not exist, arguments that were never declared, values of the wrong type.
`validate_plan` exists to catch all of that *before* anything runs. This page
measures that idea on a third-party benchmark, with a deterministic evaluator
— no LLM judging an LLM.

## Setup

- **Benchmark**: [GABench](https://github.com/GeoX-Lab/GABench) — 57 executable
  GIS analysis tasks (urban heat, flood risk, burn scars, transit equity…)
  over a 133-tool MCP server, with expert reference toolchains. GABench is
  used unmodified; its evaluator runs unchanged on our logs. (GABench
  currently ships no license file, so we publish our *results* and our own
  harness code, and link to their repository instead of redistributing it.)
- **Agent**: GABench's own plan-and-react architecture, with one change kept
  identical across both arms: the planner emits a **typed plan** — a JSON
  list of `{step_id, tool, arguments}` — instead of prose, and the solver's
  system prompt embeds compact tool signatures instead of raw tool objects
  (~26k chars instead of ~148k; declared methodology change, applied to both
  arms).
- **Model**: `claude-sonnet-5` for planner and solver in both arms.
- **The single experimental variable**:
  - **Arm A** — the typed plan goes straight to the solver.
  - **Arm B** — the typed plan is first validated statically against the live
    tool registry (unknown tool with did-you-mean suggestions, missing /
    unknown / mistyped arguments, malformed steps). Machine-readable errors
    go back to the planner, which repairs the plan — at most 2 rounds — and
    only then the solver runs. This mirrors MapSmith's `validate_plan`
    error-code design.
- **Hygiene**: the tool output directory is wiped before every task (no
  cross-task artifact reuse); every task's artifacts are archived for the
  physical-output checks; results use GABench's history format so
  `evaluation/step_by_step.py` runs on them unchanged.

## Metrics (GABench's deterministic evaluator)

| Metric | Question it answers |
|---|---|
| TAO (Tools-Any-Order) | Did the agent use the right tools at all? (F1) |
| TIO (Tools-In-Order) | Are the right tools in the right relative order? |
| TEM (Tool-Exact-Match) | Is the trajectory exactly the reference one? |
| PEA (Parameter Execution Accuracy) | Right parameters — and do the declared output files actually exist? |

Same logs in, same numbers out, every time: the evaluator compares
trajectories against reference toolchains with no model in the loop.

## Results

> **Pending** — one repetition per arm (57 tasks each) completed on
> 2026-08-20; evaluation in progress. Numbers, deltas and cost per arm land
> here.

| Metric | Arm A (no gate) | Arm B (gate) | Δ |
|---|---|---|---|
| TAO F1 | — | — | — |
| TIO | — | — | — |
| TEM | — | — | — |
| PEA | — | — | — |
| LLM calls / task | — | — | — |
| Cost / task (USD) | — | — | — |

## Reproduce it

The A/B harness (planner subclass, static gate, runner) subclasses GABench
without modifying it. Reproduction steps:

1. Clone GABench and follow its `uv sync` setup; add your model endpoint to
   its `config.yaml`.
2. Fetch our harness (published alongside the results) into a sibling
   directory.
3. `python run_ab.py --arm a --model <key> --all --rep 1`, then the same with
   `--arm b`.
4. Assemble each arm's artifacts into the layout GABench's evaluator expects
   and run `evaluation/step_by_step.py`.

Fine print: task 55 of arm A was re-run once after a local network drop cut
the API connection mid-task (infrastructure error, not an agent failure);
the raw logs keep both timestamps.
