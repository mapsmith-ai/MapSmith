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

## Results (1 repetition per arm, 57 tasks each, 2026-08-19)

| Metric | Arm A (no gate) | Arm B (gate) | Δ |
|---|---|---|---|
| TAO F1 | 0.824 | 0.781 | −0.043 |
| TIO | 0.743 | 0.680 | −0.063 |
| TEM | 0.483 | 0.464 | −0.019 |
| PEA | 0.430 | 0.425 | −0.005 |
| LLM calls / task | 19.1 | 20.0 | +0.9 |
| Cost / task (USD, Sonnet 5 intro pricing) | 0.63 | 0.66 | +0.03 |

**The headline is a null result, and the mechanism behind it is the
interesting part.**

1. **Plan-level interface hallucination is real but rare on a frontier
   model**: the gate fired on 5/57 plans (three wrong-typed arguments, two
   unparsable plans) and repaired every one to zero residual issues in ≤2
   rounds. The failures are *systematic*: arm A's audit shows defective plans
   on the same tasks (6, 33, 42, 56) in its own independent run.
2. **A ReAct solver downstream absorbs plan-level repairs.** On the tasks
   where both arms produced the same defective plan and only arm B repaired
   it, the final trajectories scored **identically** (tasks 6, 33, 42 —
   same TAO/TIO/TEM/PEA to three decimals). The solver re-decides tool calls
   as it goes, so fixing the plan's argument types rarely changes what
   actually executes. Advisory validation upstream of an improvising agent
   gets absorbed by the improvisation.
3. **The aggregate deltas are run-to-run noise, and that is a finding.** On
   the 52 tasks the gate never touched, the two arms are the same system —
   yet single-run aggregates differ by 4–6 points of TAO/TIO. Treat any
   single-repetition agent-benchmark delta below that bar as unproven.
4. **Where the real headroom is**: PEA sits at ~0.43 in both arms — wrong
   parameters and missing outputs at *execution* time dominate, exactly the
   failure class that plan-time advice cannot reach.

## What this says about MapSmith's design

MapSmith does not use its plan validation as advice to an improvising agent
— that is the configuration this experiment measured, and it measured it
doing approximately nothing on a frontier model. In MapSmith,
`validate_plan`'s contract is enforced at the **execution boundary**:
`execute_plan` runs the validated plan exactly as written (re-validating
first, resolving `$step` references, recording provenance per step), so a
repaired plan *is* what executes. The gap this experiment exposes — solvers
that improvise past their plans and fail on parameters at execution time —
is the gap that design closes.

Open question worth money: does the gate lift *smaller* models, whose plans
are dirtier? The harness is ready; a Haiku-class run costs a fraction of
this one.

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
