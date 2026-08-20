# GABench A/B: does static plan validation help a GIS agent?

This harness measures one idea in isolation: **validating a typed plan against
the live tool registry before execution**. It runs
[GABench](https://github.com/GeoX-Lab/GABench) unmodified — subclassing its
agents rather than editing them — so its deterministic evaluator scores our
logs without changes.

Results and interpretation: [`docs/benchmarks.md`](../../docs/benchmarks.md).

## The two arms

Both arms share everything: model, prompts, solver, tools, data, and a typed
planner that emits `[{"step_id": 1, "tool": ..., "arguments": {...}}, ...]`.

| | Plan goes to the solver | Static validation first |
|---|---|---|
| **Arm A** | yes | no |
| **Arm B** | yes | yes — unknown tool (with did-you-mean), missing/unknown/mistyped arguments, malformed steps; machine-readable errors go back to the planner, ≤2 repair rounds |

The gate is the only variable. Arm A records the same validation audit
*without acting on it*, so you can see whether both runs produced defective
plans on the same tasks.

## Run it

```bash
# 1. GABench itself (its own instructions: uv sync, model endpoint in config.yaml)
git clone https://github.com/GeoX-Lab/GABench
cd GABench && uv sync

# 2. point the harness at it (or keep GABench as a sibling directory)
export GABENCH_ROOT=/path/to/GABench

# 3. run both arms, from the GABench root so its config.yaml/.env resolve
python /path/to/gabench-ab/run_ab.py --arm a --model <key> --all --rep 1
python /path/to/gabench-ab/run_ab.py --arm b --model <key> --all --rep 1

# 4. assemble artifacts and score both arms
python /path/to/gabench-ab/assemble_eval.py <key> a b
```

`--model <key>` is a key from GABench's `config.yaml`. Runs resume: completed
tasks are skipped, so an interrupted run continues where it stopped. Arms must
run **sequentially** — they share GABench's `output/` working directory.

Inspect a finished run:

```bash
python gate_stats.py results/<key>/arm_b/rep1.jsonl        # what the gate caught
python cost_check.py <usage.jsonl> <in-price> <out-price>  # token spend
python test_gate.py                                        # closed-form gate test, no API calls
```

## Notes on method

- **The solver prompt carries compact tool signatures** (name, typed
  arguments, one-line description) instead of raw tool objects — ~26k
  characters instead of ~148k. This is a declared change from upstream,
  applied identically to both arms; without it the input cost per task is
  roughly four times higher for no measured benefit.
- **Each task starts from a clean output directory** and its artifacts are
  archived afterwards. Without this, a task can "succeed" by reusing a
  previous run's files — observed on our first smoke test.
- **Logs keep the upstream history format**, plus a `gate_audit` field, so
  `evaluation/step_by_step.py` runs unchanged.
- GABench ships no license file, so this directory contains only our own
  code and links to their repository; nothing of theirs is redistributed.
