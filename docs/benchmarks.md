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

  **What that compression costs, found later and worth knowing before reading
  any number on this page**: the compact signature keeps a tool's name, typed
  arguments and the first line of its description, which drops the `Args:`
  section — and that is where the per-argument rules live, such as
  `output_name: Output filename (must end with .tif)`. Measured on the live
  registry: **86 of the 133 tools state an extension rule that the compact form
  throws away** (25,441 characters against 77,937 for the full descriptions).
  So neither planner nor solver was ever told those rules. It applies identically to every arm, so the
  comparisons hold; but part of the absolute failure rate below is ours, and
  more importantly it is *one concrete piece* of the information an improvising
  solver recovers from error messages and an enforced plan cannot. Separating
  "the model cannot plan this" from "we never told it the rules" needs a run
  with the full argument documentation, which has not happened yet.
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

Both open leads this page used to end on — repeat with several repetitions on
the tasks whose plans are actually defective, and test whether an *enforced*
plan moves what advisory validation could not — have since been run. They are
the section below, and one of them did not survive the repetitions.

## Arm C: what happens when the plan is enforced

The configuration MapSmith actually ships — `execute_plan`, no improvisation
between validation and execution — had never been measured. This is that arm,
with the hypotheses
[registered before the run](https://github.com/mapsmith-ai/MapSmith/issues/22)
rather than after it:

- **H1** — on tasks whose plans were defective, enforcing the plan scores at
  least as well as repairing it and letting the solver improvise, because the
  repaired plan *is* the trajectory.
- **H2** — enforcing scores worse on parameter accuracy, because it cannot
  adapt to what it learns at runtime.

**Result: H1 is rejected and H2 holds directionally.** Both are below.

### Setup

| Arm | Static validation | Who executes |
|---|---|---|
| A | no | ReAct solver |
| B | yes, ≤2 repair rounds | ReAct solver |
| C | yes, identical to B | the plan itself, no model in the loop |

`claude-haiku-4-5`, **five repetitions per arm**, on 15 tasks frozen before the
run: the 9 whose plans failed validation in at least one of the four runs above,
plus 6 never-defective control tasks matched on domain mix and reference-chain
length (mean 9.2 against 9.4). `derive_groups.py` re-derives both groups from
the published logs and fails if they drift.

The control group is what turns a delta into a measurement. On those tasks the
arms are the same system, so the spread between repetitions there *is* the noise
floor — measured, not assumed. Every delta below is barred against the noisier
of the two arms being compared; pooling all three arms into one floor would let
an A-versus-B delta clear a bar set by arm C's near-zero variance.

### Results (225 task-runs, 2026-08-20/21)

<!-- generated by rep_analysis.py --markdown; reps per arm: A=5, B=5, C=5 -->

#### Defective tasks (n=9)

| Arm | TAO | TIO | TEM | PEA |
|---|---|---|---|---|
| A | 0.575 +/-0.082 | 0.320 +/-0.035 | 0.159 +/-0.029 | 0.205 +/-0.044 |
| B | 0.601 +/-0.103 | 0.399 +/-0.065 | 0.231 +/-0.090 | 0.178 +/-0.037 |
| C | 0.577 +/-0.023 | 0.177 +/-0.007 | 0.142 +/-0.007 | 0.129 +/-0.020 |

Deltas, each against the intra-arm noise floor of this group (`~` = within the noise floor of the noisier arm in the pair, i.e. not a result):

| Pair | TAO | TIO | TEM | PEA |
|---|---|---|---|---|
| A->B | +0.026~ (noise 0.165) | +0.080~ (noise 0.114) | +0.072~ (noise 0.157) | -0.027~ (noise 0.109) |
| A->C | +0.002~ (noise 0.159) | -0.143 (noise 0.070) | -0.017~ (noise 0.061) | -0.076~ (noise 0.109) |
| B->C | -0.024~ (noise 0.165) | -0.223 (noise 0.114) | -0.088~ (noise 0.157) | -0.049~ (noise 0.085) |

#### Control tasks (n=6)

| Arm | TAO | TIO | TEM | PEA |
|---|---|---|---|---|
| A | 0.583 +/-0.108 | 0.393 +/-0.101 | 0.238 +/-0.045 | 0.158 +/-0.037 |
| B | 0.525 +/-0.122 | 0.417 +/-0.116 | 0.225 +/-0.071 | 0.166 +/-0.098 |
| C | 0.500 +/-0.028 | 0.288 +/-0.011 | 0.229 +/-0.011 | 0.084 +/-0.022 |

Deltas, each against the intra-arm noise floor of this group (`~` = within the noise floor of the noisier arm in the pair, i.e. not a result):

| Pair | TAO | TIO | TEM | PEA |
|---|---|---|---|---|
| A->B | -0.059~ (noise 0.239) | +0.024~ (noise 0.242) | -0.013~ (noise 0.117) | +0.008~ (noise 0.149) |
| A->C | -0.083~ (noise 0.188) | -0.105~ (noise 0.213) | -0.009~ (noise 0.117) | -0.074 (noise 0.073) |
| B->C | -0.025~ (noise 0.239) | -0.129~ (noise 0.242) | +0.004~ (noise 0.101) | -0.082~ (noise 0.149) |

**Three deltas out of twenty-four clear their noise floor, and all three are
costs of enforcement**: TIO −0.143 against arm A and −0.223 against arm B on the
defective tasks, and PEA −0.074 on the control tasks. Every single delta
involving the validation gate — A→B, all four metrics, both groups — is inside
the noise.

### What the enforced arm did

74 of 75 task-runs stopped on a runtime error (the 75th had no parsable plan to
run), at **52% of the planned steps** (294 of 567). The dominant cause is one
thing, and it is not the absence of improvisation: a step declares
`output_name: "x.tif"`, the next step reads `raster_path: "x.tif"`, and the file
is at `output/x.tif`. **The plan's data flow is not wired up.** The improvising
solver recovers because it reads the tool's reply, which carries the real output
path; an enforced plan passes what the plan says.

So the gate validated tool names and argument types, and certified plans that
could not run — because it never checked that step N+1's input is step N's
output.

### The result we were not looking for

The noise floor was supposed to be a nuisance parameter. It turned out to be the
finding. Here is the aggregate score each arm produced on five *identical* runs:

| Arm | TAO F1 per repetition | Std. dev. |
|---|---|---|
| A | 0.549 · 0.462 · 0.625 · 0.595 · 0.662 | 0.077 |
| B | 0.556 · 0.407 · 0.602 · 0.654 · 0.634 | 0.099 |
| **C** | 0.548 · 0.539 · 0.548 · 0.548 · 0.549 | **0.004** |

The enforced arm reproduces its own headline number to within half a point,
five times running: **18× more stable than arm A and 23× more stable than arm
B**. Precision on tool selection is 0.918 ±0.013 against 0.571 ±0.074 and 0.573
±0.093 — when the enforced plan acts, it acts correctly; it just does not reach
the end (recall 0.412 against 0.647 and 0.642).

That cuts both ways, and the uncomfortable half is ours to state: with an
improvising solver on this benchmark, **two runs of the same configuration
differ by up to 0.24 TAO per task**. Any single-run comparison of any
intervention here — including the two-arm tables at the top of this page — is
reporting noise unless the effect is larger than that.

### What we conclude

1. **The validation gate does nothing measurable**, now with five repetitions
   and a control group instead of one run and an inference. This is the same
   null as above, arrived at by a stricter route.
2. **Enforcing a plan trades completeness for precision and reproducibility.**
   It calls the right tools and almost nothing else (0.92 precision), it stops
   halfway (0.41 recall), and it does the same thing every time (±0.004).
3. **H1 is rejected**: the repaired plan being the trajectory did not help, because
   the plan does not survive contact with the filesystem. **H2 holds
   directionally** — PEA is lower in every group — though only the control-group
   delta clears its floor.
4. The honest reading of arm C is therefore not "enforcement is worse". It is
   that a gate checking names and types certifies plans that cannot run, and
   MapSmith's own validator refuses exactly what this replica let through:
   `UNKNOWN_REFERENCE`, `FORWARD_REFERENCE`, `PREFER_REFERENCE`, input-file
   existence, and a simulated CRS flow across steps. Arm C is a demonstration of
   why those error codes exist, run with a gate that lacks them.

The next measurement follows from that, and it is cheap because an enforced arm
makes no solver calls: a **flow-aware gate** — the same static check MapSmith
ships — against the same tasks, model and repetitions. Pre-registered before it
runs, like this one.

## Reproduce it

The harness lives in [`benchmarks/gabench-ab/`](../benchmarks/gabench-ab/) —
it subclasses GABench instead of modifying it, so the upstream evaluator scores
our logs unchanged. Full instructions in its README; the short version:

```bash
git clone https://github.com/GeoX-Lab/GABench && cd GABench && uv sync
export GABENCH_ROOT=$PWD
H=<path>/benchmarks/gabench-ab

# the two-arm experiment, all 57 tasks
python $H/run_ab.py --arm a --model <key> --all --rep 1
python $H/run_ab.py --arm b --model <key> --all --rep 1
python $H/assemble_eval.py <key> a b --rep 1

# the three-arm experiment, frozen task groups, five repetitions
for r in 1 2 3 4 5; do
  for arm in a b c; do
    python $H/run_ab.py --arm $arm --model <key> --group defective+control --rep $r
  done
  python $H/assemble_eval.py <key> a b c --rep $r
done
python $H/rep_analysis.py <key>              # every delta beside its noise floor
python $H/derive_groups.py                   # re-derive the frozen task groups
python $H/test_enforced.py                   # closed-form, no API calls
```

Repetition is the outer loop on purpose: an interrupted run still leaves a
balanced design across all three arms instead of five repetitions of one arm and
none of another.

Run the scripts from the GABench root. PEA validates path arguments by asking
whether the file exists, and its fallback is a relative probe against the
current directory — where the `dataset/` inputs live — so the same logs score
0.320 there and 0.192 elsewhere, with every other metric unchanged. The harness
scripts now move into that directory themselves, which is worth knowing about
any benchmark you reproduce: a deterministic evaluator can still have a silently
wrong answer.

Fine print: task 55 of arm A was re-run once after a local network drop cut
the API connection mid-task (infrastructure error, not an agent failure);
the raw logs keep both timestamps.
