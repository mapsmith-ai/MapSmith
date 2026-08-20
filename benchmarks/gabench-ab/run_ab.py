"""A/B runner: GABench Plan-and-React with typed plans, gate on/off.

Usage (run with GABench's venv, from its repo root so config.yaml/.env
resolve; GABENCH_ROOT may point the harness at the checkout):
    python run_ab.py --arm a --model claude --ids 1,2,3 --rep 1
    python run_ab.py --arm b --model claude --all --rep 1

Output: results/{model}/arm_{arm}/rep{rep}.jsonl in the upstream history
format (GABench's evaluation/step_by_step.py runs on it unchanged) plus a
`gate_audit` field recording validation rounds.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

AB_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(AB_DIR))

from _paths import gabench_root, results_root  # noqa: E402

GABENCH_ROOT = gabench_root()
sys.path.insert(0, str(GABENCH_ROOT))

from core.mcp_client import get_mcp_clients  # noqa: E402
from agents.plan_and_react import SolveReactAgent  # noqa: E402
from runners.run_benchmark import load_tasks_from_csv, get_completed_tasks  # noqa: E402

from ab_extension import (  # noqa: E402
    TypedPlanAgent,
    make_compact_solver,
    plan_with_optional_gate,
)

CompactSolver = make_compact_solver(SolveReactAgent)

CSV_PATH = GABENCH_ROOT / "benchmark" / "benchmark.csv"
TOOL_OUTPUT_DIR = GABENCH_ROOT / "output"


def reset_tool_output() -> None:
    """Each task starts from a CLEAN output dir: artifacts from a previous run
    would let the agent 'succeed' by reusing them (observed on the first smoke)."""
    if TOOL_OUTPUT_DIR.exists():
        shutil.rmtree(TOOL_OUTPUT_DIR)
    TOOL_OUTPUT_DIR.mkdir()


def archive_tool_output(dest: Path) -> None:
    """Keep every task's artifacts for the end-to-end evaluation and replay."""
    if TOOL_OUTPUT_DIR.exists():
        if dest.exists():
            shutil.rmtree(dest)
        shutil.move(str(TOOL_OUTPUT_DIR), str(dest))


async def run_task(task: dict, model: str, gate: bool) -> dict:
    mcp_clients = get_mcp_clients()
    start = datetime.now()
    status, error, history, audit = "success", None, [], None
    try:
        print("\n--- Planning Phase (typed) ---")
        planner = TypedPlanAgent(mcp_clients=mcp_clients, init_model_name=model)
        async with planner:
            audit = await plan_with_optional_gate(planner, task["query"], gate=gate)
        history.extend(planner.history)

        print("\n--- Solving Phase ---")
        solver = CompactSolver(mcp_clients=mcp_clients, init_model_name=model)
        solver.set_subtasks(planner.subtasks)
        async with solver:
            async for chunk in solver.run(task["query"]):
                print(chunk, end="", flush=True)
        history.extend(solver.history)
    except Exception as exc:  # noqa: BLE001 — the record must survive any failure
        status, error = "error", f"{type(exc).__name__}: {exc}"
        print(f"\n!!! {error}")
    return {
        "task_id": str(task["id"]),
        "agent": "plan_and_react_typed",
        "model": model,
        "arm": "B" if gate else "A",
        "gate_audit": audit,
        "status": status,
        "error": error,
        "duration_seconds": (datetime.now() - start).total_seconds(),
        "start_time": start.isoformat(),
        "end_time": datetime.now().isoformat(),
        "query": task["query"],
        "history": history,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="MapSmith x GABench A/B runner")
    parser.add_argument("--arm", choices=["a", "b"], required=True,
                        help="a = typed plan only, b = typed plan + validation gate")
    parser.add_argument("--model", required=True, help="model key from config.yaml")
    parser.add_argument("--ids", help="comma-separated task IDs (default: all)")
    parser.add_argument("--all", action="store_true", help="run every task in the CSV")
    parser.add_argument("--rep", type=int, default=1, help="repetition index (1..3)")
    args = parser.parse_args()

    tasks = load_tasks_from_csv(CSV_PATH)
    if args.ids:
        wanted = {t.strip() for t in args.ids.split(",")}
        tasks = [t for t in tasks if str(t["id"]) in wanted]
    elif not getattr(args, "all"):
        parser.error("pass --ids or --all")
    if not tasks:
        print("no matching tasks")
        return

    out = results_root() / args.model / f"arm_{args.arm}"
    out.mkdir(parents=True, exist_ok=True)
    result_file = out / f"rep{args.rep}.jsonl"
    done = get_completed_tasks(result_file)
    print(f"=== arm {args.arm.upper()} | model {args.model} | rep {args.rep} "
          f"| tasks {len(tasks)} | already done {len(done)} ===")
    print(f"=== results -> {result_file} ===")

    for i, task in enumerate(tasks, 1):
        if str(task["id"]) in done:
            print(f"[{i}/{len(tasks)}] task {task['id']} SKIPPED (done)")
            continue
        print(f"\n[{i}/{len(tasks)}] task {task['id']}")
        reset_tool_output()
        record = await run_task(task, args.model, gate=(args.arm == "b"))
        archive_tool_output(out / f"rep{args.rep}_artifacts" / f"task_{task['id']}")
        with open(result_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
        print(f"\n[saved] task {task['id']}: {record['status']} "
              f"({record['duration_seconds']:.0f}s)")


if __name__ == "__main__":
    asyncio.run(main())
