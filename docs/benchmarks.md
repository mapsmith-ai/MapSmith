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
- **Models**: `claude-sonnet-5` and `claude-haiku-4-5` — planner and solver
  are the same model within a run. The second model tests the obvious
  hypothesis: a smaller model writes dirtier plans, so validation should
  help it more.
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

## Results (1 repetition per arm per model, 57 tasks each, 2026-08-19/20)

### Sonnet 5

| Metric | Arm A (no gate) | Arm B (gate) | Δ |
|---|---|---|---|
| TAO F1 | 0.824 | 0.781 | −0.043 |
| TIO | 0.743 | 0.680 | −0.063 |
| TEM | 0.483 | 0.464 | −0.019 |
| PEA | 0.430 | 0.425 | −0.005 |
| LLM calls / task | 19.1 | 20.0 | +0.9 |
| Cost / task (USD, Sonnet 5 intro pricing) | 0.63 | 0.66 | +0.03 |

### Haiku 4.5

| Metric | Arm A (no gate) | Arm B (gate) | Δ |
|---|---|---|---|
| TAO F1 | 0.660 | 0.714 | **+0.054** |
| TIO | 0.596 | 0.644 | **+0.048** |
| TEM | 0.424 | 0.447 | **+0.023** |
| PEA | 0.320 | 0.366 | **+0.046** |
| LLM calls / task | 16.9 | 16.5 | −0.4 |
| Cost / task (USD) | 0.44 | 0.42 | −0.02 |

Every metric improves, the gate arm is slightly *cheaper*, and the smaller
model is worse than Sonnet across the board — a tidy story. **It is also
wrong, and the way it is wrong is the most useful thing we measured.**

## Why the Haiku "win" is not a win

The gate can only affect a task whose plan it actually repaired. On Haiku that
was **4 of 57** tasks. So we split the per-task deltas into the tasks the gate
touched and the 53 it never saw — where the two arms are the *same system*,
differing only by sampling:

| Group | n | ΔTAO | ΔTIO | ΔTEM | ΔPEA |
|---|---|---|---|---|---|
| Plans repaired by the gate | 4 | **+0.188** | −0.028 | 0.000 | +0.111 |
| Never touched by the gate | 53 | +0.043 | +0.054 | +0.024 | +0.042 |

(Reproduce with `benchmarks/gabench-ab/split_analysis.py haiku`; the two rows
weight back to the aggregate deltas above.)

The untouched tasks move by almost exactly the aggregate delta. **The
headline improvement is run-to-run variance**, and that second row is a direct
measurement of it: on a single repetition, two identical configurations differ
by 2–5 points per metric on this benchmark. Any single-run agent-benchmark
result reporting a delta of that size — ours included — is reporting noise.

What survives the split is narrower and more interesting: on the four tasks
where the gate actually repaired a plan, tool selection improved by **+0.19
TAO**, four times the noise level. n=4 is not a result, it is a lead — and it
comes with a mechanism. The *kind* of defect differed by model: Haiku invented
tool names that do not exist (`UNKNOWN_TOOL`), while Sonnet only ever mistyped
arguments of real tools. Validation against a live registry catches invented
names cold, which is exactly why it should matter more as models get smaller.

## What we conclude

1. **Advisory validation upstream of an improvising solver does approximately
   nothing at aggregate level** — on a frontier model *and* on a small one.
   The ReAct solver re-decides its calls as it goes: on tasks where both arms
   produced the same defective plan and only one repaired it, the final
   trajectories scored identically (Sonnet tasks 6, 33, 42 — same values to
   three decimals).
2. **The failure mass is at execution time**: PEA is 0.43 (Sonnet) and 0.37
   (Haiku) — wrong parameters and missing outputs, the class of failure that
   plan-time advice cannot reach.
3. **Measure your noise floor before believing your effect.** Running the
   experiment where the gate is inert on 90% of tasks is what turned a
   publishable "+8% on a small model" into an honest null.

## What this says about MapSmith's design

MapSmith does not use its plan validation as advice to an improvising agent
— that is the configuration this experiment measured, and it measured it doing
approximately nothing, on both models. In MapSmith,
`validate_plan`'s contract is enforced at the **execution boundary**:
`execute_plan` runs the validated plan exactly as written (re-validating
first, resolving `$step` references, recording provenance per step), so a
repaired plan *is* what executes. The gap this experiment exposes — solvers
that improvise past their plans and fail on parameters at execution time —
is the gap that design closes.

It also redirected the roadmap: the next piece of work is not more plan-time
validation but runtime verification — preconditions that refuse an analysis
whose inputs cannot produce a meaningful answer, and results that come back
with named warnings instead of a silent "success"
([shipped](../README.md#verification-in-and-out)).

Open leads, in order of value: repeat with 3+ repetitions on the ~10% of tasks
where plans are actually defective (where the +0.19 TAO signal lives, and
where repetitions are affordable); and test whether an *enforced* plan — the
`execute_plan` contract, no improvisation between validation and execution —
moves what advisory validation could not.

## Reproduce it

The harness lives in [`benchmarks/gabench-ab/`](../benchmarks/gabench-ab/) —
it subclasses GABench instead of modifying it, so the upstream evaluator scores
our logs unchanged. Full instructions in its README; the short version:

```bash
git clone https://github.com/GeoX-Lab/GABench && cd GABench && uv sync
export GABENCH_ROOT=$PWD
python <path>/benchmarks/gabench-ab/run_ab.py --arm a --model <key> --all --rep 1
python <path>/benchmarks/gabench-ab/run_ab.py --arm b --model <key> --all --rep 1
python <path>/benchmarks/gabench-ab/assemble_eval.py <key> a b
```

Fine print: task 55 of arm A was re-run once after a local network drop cut
the API connection mid-task (infrastructure error, not an agent failure);
the raw logs keep both timestamps.
