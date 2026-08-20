"""Assemble per-task artifacts into the layout GABench's evaluator expects,
then run its deterministic step-by-step evaluation.

The evaluator infers the physical-output directory from the result path:
.../results/{model}/{agent}/file.jsonl -> .../output_results/{model}/{agent}/output
Our per-task archives (repN_artifacts/task_*/**) are merged back into that one
directory, preserving each task's relative layout — they are snapshots of
GABench's output/ dir, so merging reconstructs it faithfully, and PEA's
file-existence checks see what each run actually produced.

Usage: python assemble_eval.py <model-key> [a] [b]
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _paths import HARNESS_DIR, gabench_root, results_root  # noqa: E402

GABENCH = gabench_root()
PYTHON = sys.executable


def assemble(model: str, arm: str, rep: int = 1) -> None:
    src_dir = results_root() / model / f"arm_{arm}" / f"rep{rep}_artifacts"
    out = HARNESS_DIR / "output_results" / model / f"arm_{arm}" / "output"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    n = 0
    for task_dir in sorted(src_dir.glob("task_*")):
        for f in task_dir.rglob("*"):
            if f.is_file():
                dest = out / f.relative_to(task_dir)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dest)
                n += 1
    print(f"{model} arm {arm}: {n} artifact files -> {out}", flush=True)


def evaluate(model: str, arm: str, rep: int = 1) -> None:
    result = results_root() / model / f"arm_{arm}" / f"rep{rep}.jsonl"
    print(f"\n=== EVALUATION {model} arm {arm.upper()} ({result.name}) ===", flush=True)
    subprocess.run(
        [PYTHON, str(GABENCH / "evaluation" / "step_by_step.py"),
         "--benchmark", str(GABENCH / "benchmark" / "benchmark.csv"),
         "--result", str(result)],
        cwd=str(GABENCH),
        check=False,
    )


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: python assemble_eval.py <model-key> [a] [b]")
    model_key = sys.argv[1]
    for arm_key in sys.argv[2:] or ["a", "b"]:
        assemble(model_key, arm_key)
        evaluate(model_key, arm_key)
