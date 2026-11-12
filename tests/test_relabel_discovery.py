"""Validation and all-or-nothing ingestion for current discovery labels."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "benchmarks"))

import relabel_discovery as bench


def test_parse_answers_accepts_a_complete_fenced_array():
    raw = '```json\n[{"id":"q1","label":"buffer_layer"},{"id":"q2","label":"none"}]\n```'
    assert bench.parse_answers(raw, {"q1", "q2"}, {"buffer_layer", "none"}) == {
        "q1": "buffer_layer",
        "q2": "none",
    }


@pytest.mark.parametrize(
    "raw, expected_ids, allowed, message",
    [
        ('[{"id":"q1","label":"buffer_layer"}]', {"q1", "q2"}, {"buffer_layer"}, "missing"),
        ('[{"id":"q1","label":"invented"}]', {"q1"}, {"buffer_layer"}, "unknown"),
        ('[{"id":"q1","label":"buffer_layer"},{"id":"q1","label":"none"}]', {"q1"}, {"buffer_layer", "none"}, "duplicate"),
    ],
)
def test_parse_answers_rejects_incomplete_unknown_and_duplicate_rows(raw, expected_ids, allowed, message):
    with pytest.raises(ValueError, match=message):
        bench.parse_answers(raw, expected_ids, allowed)


def test_call_with_retries_retries_a_transient_provider_failure(monkeypatch):
    import urllib.error

    attempts = 0

    def flaky():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise urllib.error.URLError("temporary provider failure")
        return "ok"

    monkeypatch.setattr(bench.time, "sleep", lambda _: None)
    assert bench.call_with_retries(flaky) == "ok"
    assert attempts == 2


def test_apply_labels_is_all_or_nothing_and_preserves_historical_fields():
    queries = [
        {"query": "first", "label_claude": "old_c", "label_gemini": "old_g"},
        {"query": "second", "label_claude": "old_c", "label_gemini": "old_g"},
    ]
    with pytest.raises(ValueError, match="complete"):
        bench.apply_labels(queries, {"claude": {"q1": "buffer_layer", "q2": "clip_layer"}})
    assert queries[0]["label_claude"] == "old_c"
    assert "label_claude_2026_11" not in queries[0]

    updated = bench.apply_labels(
        queries,
        {
            "claude": {"q1": "buffer_layer", "q2": "clip_layer"},
            "haiku": {"q1": "spatial_join", "q2": "none"},
        },
        ids=["q1", "q2"],
    )
    assert updated[0]["label_claude_2026_11"] == "buffer_layer"
    assert updated[1]["label_haiku_2026_11"] == "none"
    assert updated[0]["label_claude"] == "old_c"
    assert updated[1]["label_gemini"] == "old_g"


def test_completed_relabel_run_is_a_noop(monkeypatch):
    monkeypatch.setattr(bench.api, "ask_anthropic", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("API called")))
    writes = []
    monkeypatch.setattr(bench.api, "write_json", lambda *args: writes.append(args))
    document = bench.run(block_size=10)
    assert document["complete"] is True
    assert writes == []


def test_resume_uses_question_ids_and_finalizes_atomically(monkeypatch, tmp_path):
    import hashlib
    import json
    import re

    queries = [{"query": "first"}, {"query": "second"}, {"query": "third"}]
    ids = [bench.pack.question_id(query["query"]) for query in queries]
    queries_path = tmp_path / "queries.json"
    original = json.dumps({"queries": queries}, ensure_ascii=False).encode()
    queries_path.write_bytes(original)
    out = tmp_path / "artifact.json"
    monkeypatch.setattr(bench, "QUERIES", queries_path)
    monkeypatch.setattr(bench.discovery_report, "load", lambda: json.loads(queries_path.read_text(encoding="utf-8"))["queries"])
    monkeypatch.setattr(bench.pack, "catalogue_table", lambda: "catalogue")
    document = {
        "schema_version": 1,
        "dataset_sha256": hashlib.sha256(original).hexdigest(),
        "catalogue_sha256": hashlib.sha256(b"catalogue").hexdigest(),
        "complete": False,
        "providers": {
            "claude": {
                "model": "claude-sonnet-5",
                "raw_blocks": {
                    "01": {
                        "model": "claude-sonnet-5",
                        "block_size": 2,
                        "expected_ids": ids[:2],
                        "prompt_sha256": "stale",
                        "raw": json.dumps([{"id": question_id, "label": "buffer_layer"} for question_id in ids[:2]]),
                    }
                },
                "invalid_blocks": {},
                "answers": dict(zip(ids[:2], ["buffer_layer", "buffer_layer"], strict=True)),
            }
        },
    }
    out.write_text(json.dumps(document), encoding="utf-8")
    calls = []

    def answer(prompt, model, timeout):
        calls.append(model)
        selected = re.findall(r"### `([^`]+)`", prompt)
        return json.dumps([{"id": question_id, "label": "buffer_layer"} for question_id in selected])

    monkeypatch.setattr(bench.api, "ask_anthropic", answer)
    completed = bench.run(out=out, block_size=1)
    assert completed["complete"] is True
    assert calls == ["claude-sonnet-5", *["claude-haiku-4-5"] * 3]
    for provider in ("claude", "haiku"):
        for block in completed["providers"][provider]["raw_blocks"].values():
            assert len(block["prompt_sha256"]) == 64
            assert block["expected_ids"]
            assert block["block_size"] == 1
    labelled = json.loads(queries_path.read_text(encoding="utf-8"))["queries"]
    assert all(query["label_claude_2026_11"] == "buffer_layer" for query in labelled)
    assert all(query["label_haiku_2026_11"] == "buffer_layer" for query in labelled)
    assert completed["labelled_dataset_sha256"] == bench.dataset_hash(labelled)

    bench.run(out=out, block_size=2)
    assert calls == ["claude-sonnet-5", *["claude-haiku-4-5"] * 3]


def test_dashboard_disagreements_use_current_pair_without_proposing_it():
    import dashboard

    current = {
        "query": "a current disagreement",
        "label_claude": "buffer_layer",
        "label_gemini": "buffer_layer",
        "label_claude_2026_11": "spatial_join",
        "label_haiku_2026_11": "clip_layer",
    }
    rows = dashboard.disagreements([current])
    assert len(rows) == 1
    assert rows[0]["claude"] == "spatial_join"
    assert rows[0]["gemini"] == "clip_layer"
    assert dashboard._proposer(current) is None
    assert dashboard._proposer({**current, "label_fable": "measure_area"}) == "fable"


def test_labelling_pack_disagreements_use_current_pair(monkeypatch, tmp_path):
    import make_labelling_pack as pack

    queries_path = tmp_path / "queries.json"
    queries_path.write_text(
        '{"queries": [{"query": "current", "label_claude": "buffer_layer", "label_gemini": "buffer_layer", '
        '"label_claude_2026_11": "spatial_join", "label_haiku_2026_11": "clip_layer"}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(pack, "QUERIES", queries_path)
    pack.main(["--out", str(tmp_path / "packs"), "--only-disagreements"])
    files = list((tmp_path / "packs").glob("labelling-pack-*.md"))
    assert len(files) == 1
    assert "current" in files[0].read_text(encoding="utf-8")


def test_legacy_raw_blocks_are_migrated_once():
    queries = [{"query": "first"}, {"query": "second"}]
    ids = [bench.pack.question_id(query["query"]) for query in queries]
    raw = '[{"id":"' + ids[0] + '","label":"buffer_layer"}]'
    document = {
        "providers": {
            "claude": {
                "model": "claude-sonnet-5",
                "raw_blocks": {"01": raw},
            }
        }
    }
    assert bench.migrate_raw_blocks(document, queries) is True
    block = document["providers"]["claude"]["raw_blocks"]["01"]
    assert block["model"] == "claude-sonnet-5"
    assert block["block_size"] == 1
    assert block["expected_ids"] == ids[:1]
    assert len(block["prompt_sha256"]) == 64
    assert block["raw"] == raw
    assert bench.migrate_raw_blocks(document, queries) is False
