"""Relabel every discovery request against today's catalogue, with resumable raw evidence.

    python benchmarks/relabel_discovery.py

Raw responses are saved after every provider block. The dated labels are written
only after both providers have complete, valid answers for all 155 requests.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import sys
import time
import urllib.error
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
QUERIES = ROOT / "tests" / "data" / "discovery_queries.json"
ARTIFACT = ROOT / "benchmarks" / "discovery_labels_2026-11-04.json"
LABEL_FIELDS = {
    "claude": "label_claude_2026_11",
    "haiku": "label_haiku_2026_11",
}

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "benchmarks"))

import discovery_report
import make_labelling_pack as pack
import method_choice as api
from mapsmith import catalog


def call_with_retries(call, attempts: int = 4):
    delays = (5, 15, 45)
    for attempt in range(attempts):
        try:
            return call()
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt + 1 == attempts or not api.retryable(exc):
                raise
            delay = delays[attempt]
            print(f"provider attempt {attempt + 1} failed: {api.error_detail(exc)}; retrying in {delay}s", flush=True)
            time.sleep(delay)


def parse_answers(raw: str, expected_ids: set[str], allowed: set[str]) -> dict[str, str]:
    stripped = raw.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1].rsplit("```", 1)[0]
    start, end = stripped.find("["), stripped.rfind("]")
    if start < 0 or end < start:
        raise ValueError("provider returned no JSON array")
    rows = json.loads(stripped[start:end + 1])
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("provider response is not an array of objects")
    ids = [row.get("id") for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate question ids")
    unknown_ids = set(ids) - expected_ids
    if unknown_ids:
        raise ValueError(f"unknown question ids: {sorted(unknown_ids)}")
    missing = expected_ids - set(ids)
    if missing:
        raise ValueError(f"missing question ids: {sorted(missing)}")
    answers = {row["id"]: row.get("label") for row in rows}
    unknown_labels = set(answers.values()) - allowed
    if unknown_labels:
        raise ValueError(f"unknown labels: {sorted(unknown_labels)}")
    return answers


def apply_labels(
    queries: list[dict[str, Any]], answers: dict[str, dict[str, str]], ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    expected = ids or [pack.question_id(query["query"]) for query in queries]
    if set(answers) != set(LABEL_FIELDS) or any(set(given) != set(expected) for given in answers.values()):
        raise ValueError("both providers need complete answers before ingestion")
    updated = copy.deepcopy(queries)
    for labeller, field in LABEL_FIELDS.items():
        for query, question_id in zip(updated, expected, strict=True):
            query[field] = answers[labeller][question_id]
    return updated


def prompt_for(block: list[dict[str, Any]]) -> str:
    requests = []
    for query in block:
        scenario = f"Scenario: {query['scenario']}\n" if query.get("scenario") else ""
        requests.append(f"### `{pack.question_id(query['query'])}`\n\n{scenario}{query['query']}")
    return (
        f"{pack.INSTRUCTIONS}\n\n## The catalogue\n\n{pack.catalogue_table()}\n\n"
        f"## The requests\n\n" + "\n\n".join(requests)
    )


def dataset_hash(queries: list[dict[str, Any]]) -> str:
    canonical = json.dumps({"queries": queries}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def block_matches(block: dict[str, Any], expected: set[str], model: str, prompt: str) -> bool:
    return (
        block.get("model") == model
        and set(block.get("expected_ids", [])) == expected
        and block.get("prompt_sha256") == hashlib.sha256(prompt.encode()).hexdigest()
    )


def block_metadata(block: list[dict[str, Any]], model: str, prompt: str, raw: str) -> dict[str, Any]:
    return {
        "model": model,
        "block_size": len(block),
        "expected_ids": sorted(pack.question_id(query["query"]) for query in block),
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "raw": raw,
    }


def migrate_raw_blocks(document: dict[str, Any], queries: list[dict[str, Any]]) -> bool:
    changed = False
    by_id = {pack.question_id(query["query"]): query for query in queries}
    allowed = {op["name"] for op in catalog.OPERATIONS} | set(discovery_report.NOT_AN_OPERATION)
    for state in document.get("providers", {}).values():
        for key, candidate in list(state.get("raw_blocks", {}).items()):
            if not isinstance(candidate, str):
                continue
            try:
                answers = parse_answers(candidate, set(), allowed)
            except (TypeError, ValueError):
                ids = set()
                raw = candidate.strip()
                if raw.startswith("```"):
                    raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
                try:
                    rows = json.loads(raw[raw.find("["):raw.rfind("]") + 1])
                    ids = {row["id"] for row in rows}
                    answers = parse_answers(candidate, ids, allowed)
                except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                    continue
            subset = [by_id[question_id] for question_id in answers if question_id in by_id]
            prompt = prompt_for(subset)
            state["raw_blocks"][key] = block_metadata(subset, state["model"], prompt, candidate)
            changed = True
    return changed


def run(out: Path = ARTIFACT, block_size: int = 25) -> dict[str, Any]:
    queries = discovery_report.load()
    query_bytes = QUERIES.read_bytes()
    catalogue = pack.catalogue_table()
    catalogue_hash = hashlib.sha256(catalogue.encode()).hexdigest()
    document = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {
        "schema_version": 1,
        "measured_at": dt.datetime.now(dt.UTC).date().isoformat(),
        "dataset_sha256": dataset_hash(queries),
        "catalogue_sha256": catalogue_hash,
        "complete": False,
        "providers": {},
    }
    if migrate_raw_blocks(document, queries):
        api.write_json(out, document)
    if document.get("complete"):
        answers = {labeller: document["providers"][labeller]["answers"] for labeller in LABEL_FIELDS}
        if apply_labels(queries, answers) == queries:
            return document
        raise ValueError("completed artifact and current labels differ; use a new relabelling artifact")
    if document["dataset_sha256"] not in {hashlib.sha256(query_bytes).hexdigest(), dataset_hash(queries)}:
        raise ValueError("the request file changed; use a new relabelling artifact")
    if document["catalogue_sha256"] != catalogue_hash:
        raise ValueError("the catalogue changed; use a new relabelling artifact")
    allowed = {op["name"] for op in catalog.OPERATIONS} | set(discovery_report.NOT_AN_OPERATION)
    for labeller in ("claude", "haiku"):
        provider_block_size = block_size
        blocks = [queries[index:index + provider_block_size] for index in range(0, len(queries), provider_block_size)]
        model = api.DEFAULT_MODELS["anthropic"] if labeller == "claude" else "claude-haiku-4-5"
        state = document["providers"].setdefault(labeller, {
            "model": model, "raw_blocks": {}, "invalid_blocks": {}, "answers": {},
        })
        state["temperature"] = None
        state["used_for_current_labels"] = True
        state["block_size"] = provider_block_size
        for number, block in enumerate(blocks, start=1):
            key = f"{number:02d}"
            expected = {pack.question_id(query["query"]) for query in block}
            prompt = prompt_for(block)
            candidate = state["raw_blocks"].get(key)
            if expected <= set(state["answers"]):
                if isinstance(candidate, dict) and not block_matches(candidate, expected, model, prompt):
                    state["raw_blocks"][key] = block_metadata(block, model, prompt, candidate["raw"])
                continue
            raw = None
            if isinstance(candidate, dict) and block_matches(candidate, expected, model, prompt):
                raw = candidate["raw"]
            elif isinstance(candidate, str):
                try:
                    parse_answers(candidate, expected, allowed)
                    raw = candidate
                    state["raw_blocks"][key] = block_metadata(block, model, prompt, raw)
                except (TypeError, ValueError):
                    state["raw_blocks"].pop(key)
            if raw is None:
                raw = call_with_retries(lambda: api.ask_anthropic(prompt, model, timeout=300))
                state["raw_blocks"][key] = block_metadata(block, model, prompt, raw)
                api.write_json(out, document)
            try:
                answers = parse_answers(raw, expected, allowed)
            except (TypeError, ValueError):
                state["invalid_blocks"][key] = state["raw_blocks"].pop(key)
                api.write_json(out, document)
                raise
            state["answers"].update(answers)
            api.write_json(out, document)
            print(f"{labeller} block {key}: {len(state['answers'])}/{len(queries)}", flush=True)
        state["complete"] = len(state["answers"]) == len(queries)
        api.write_json(out, document)

    answers = {labeller: document["providers"][labeller]["answers"] for labeller in LABEL_FIELDS}
    updated = apply_labels(queries, answers)
    api.write_json(QUERIES, {"queries": updated})
    document["labelled_dataset_sha256"] = dataset_hash(updated)
    document["complete"] = True
    api.write_json(out, document)
    return document


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=ARTIFACT)
    parser.add_argument("--block", type=int, default=25)
    args = parser.parse_args()
    if args.block < 1:
        parser.error("--block must be at least 1")
    document = run(args.out, args.block)
    print(f"complete: {document['complete']}; labels written to {QUERIES.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
