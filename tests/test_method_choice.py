"""Method-choice bench: public cases, honest denominators and planning metrics."""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "benchmarks"))

import method_choice as bench


@pytest.fixture
def case():
    return {
        "id": "synthetic",
        "split": "evaluation",
        "goal": "Keep buildings near restaurants",
        "inputs": [{"kind": "vector", "geometry": "polygon"}],
        "output": {"kind": "vector", "geometry": "polygon"},
        "accepted_sequences": [["buffer_layer", "dissolve_layer"], ["buffer_layer", "spatial_join"]],
        "required_assumption_keys": ["crs_units"],
    }


def response(plan, assumptions=None, warnings=None):
    return {"plan": plan, "assumptions": assumptions or [], "warnings": warnings or []}


def stored(case, answer, model="test-model"):
    import hashlib

    return {
        **answer,
        "split": case["split"],
        "model": model,
        "prompt_sha256": hashlib.sha256(bench.prompt_for(case).encode()).hexdigest(),
    }


def test_public_dataset_is_balanced_and_covers_multi_step_methods():
    cases = bench.load_cases()
    supported = [case for case in cases if case.get("supported", True)]
    assert len(cases) == 93
    assert collections.Counter(case["split"] for case in cases) == {"calibration": 46, "evaluation": 47}
    assert collections.Counter(len(case["accepted_sequences"][0]) for case in supported) == {1: 47, 2: 24, 3: 12, 4: 1}
    assert bench.validate_cases(cases)["cases"] == 93
    assert bench.validate_cases(cases)["unsupported_cases"] == 9


def test_reviewed_cases_keep_their_defensible_exact_truth():
    expected = {
        "complete_classification-004": [["spatial_join"]],
        "complete_classification-005": [["overlay_layers"]],
        "vector_analysis-009": [["buffer_layer", "spatial_join"]],
        "complete_classification-006": [["buffer_layer", "spatial_join"]],
        "rasters_grass-011": [["reclassify_raster"]],
        "rasters_grass-015": [["reclassify_raster"]],
    }
    cases = {case["id"]: case for case in bench.load_cases()}
    for cid, sequences in expected.items():
        assert cases[cid]["accepted_sequences"] == sequences
    assert cases["vector_analysis-007"]["required_assumption_keys"] == []
    assert cases["vector_analysis-010"]["required_assumption_keys"] == ["crs_units"]
    assert cases["data_databases-066"]["required_assumption_keys"] == []
    assert cases["processing-022"]["required_assumption_keys"] == []


def test_every_accepted_sequence_has_a_matching_output_contract():
    for case in bench.load_cases():
        for sequence in case["accepted_sequences"]:
            assert bench.output_matches(case, sequence), (case["id"], sequence)


def test_dataset_validation_rejects_unknown_and_repeated_operations(case):
    case["accepted_sequences"] = [["not_an_operation"]]
    with pytest.raises(ValueError, match="unavailable"):
        bench.validate_cases([case])
    case["accepted_sequences"] = [["run_sql", "run_sql"]]
    with pytest.raises(ValueError, match="repeated"):
        bench.validate_cases([case])


def test_alternative_defensible_sequences_and_declared_assumptions_pass(case):
    row = bench.score_response(case, response(["buffer_layer", "spatial_join"], ["crs_units=metres"]))
    assert row["verdict"] == "exact"
    assert row["output_contract"] is True
    assert row["assumptions_declared"] is True
    assert row["silent_planning_error"] is False


@pytest.mark.parametrize(
    ("plan", "verdict"),
    [
        (["dissolve_layer", "buffer_layer"], "reordered"),
        (["buffer_layer"], "missing_operation"),
        (["buffer_layer", "dissolve_layer", "select_features"], "extra_or_wrong_tool"),
        (["buffer_layer", "dissolve_layer", "dissolve_layer"], "extra_or_wrong_tool"),
        (["buffer_layer", "select_features"], "missing_and_wrong"),
        ([], "no_plan"),
    ],
)
def test_sequence_verdicts(case, plan, verdict):
    assert bench.score_response(case, response(plan))["verdict"] == verdict


def test_unknown_output_operation_is_an_error_not_a_crash(case):
    row = bench.score_response(case, response(["invented_tool"]))
    assert row["unknown_names"] == ["invented_tool"]
    assert row["output_contract"] is False


def test_assumptions_require_machine_readable_keys(case):
    row = bench.score_response(case, response(
        ["buffer_layer", "dissolve_layer"], ["the units are metres"],
    ))
    assert row["missing_assumption_keys"] == ["crs_units"]
    assert row["silent_planning_error"] is True


def test_relevant_warnings_prevent_a_wrong_plan_from_being_called_silent(case):
    row = bench.score_response(case, response(
        ["buffer_layer"], warnings=["output result: missing operation; assumption crs_units unresolved"],
    ))
    assert row["silent_planning_error"] is False


def test_empty_or_unrelated_warnings_do_not_hide_an_error(case):
    for warning in ("", "the weather is nice"):
        assert bench.score_response(case, response(["buffer_layer"], warnings=[warning]))[
            "silent_planning_error"
        ] is True


def test_an_empty_assumption_value_is_not_a_declaration(case):
    row = bench.score_response(case, response(["buffer_layer", "dissolve_layer"], ["crs_units="]))
    assert row["missing_assumption_keys"] == ["crs_units"]


def test_an_empty_plan_is_not_a_silent_wrong_answer(case):
    row = bench.score_response(case, response([]))
    assert row["verdict"] == "no_plan"
    assert row["silent_planning_error"] is False


def test_numeric_output_contract_uses_answer_operations(case):
    case["accepted_sequences"] = [["nearest_neighbour_index"]]
    case["output"] = {"kind": "number", "geometry": None}
    case["required_assumption_keys"] = []
    assert bench.score_response(case, response(["nearest_neighbour_index"]))["output_contract"] is True


def test_planner_sees_the_declared_argument_rules():
    text = bench.catalogue_text()
    assert "resampling in [" in text
    assert "window >= 3" in text
    assert "window odd" in text
    assert "output_path ends with ['.parquet']" in text


def test_parser_accepts_a_fenced_answer_and_rejects_a_non_plan():
    answer = bench.parse_response('```json\n{"plan": [{"operation": "clip_layer"}]}\n```')
    assert answer["plan"] == [{"operation": "clip_layer"}]
    assert answer["warnings"] == []
    with pytest.raises(ValueError, match="no JSON"):
        bench.parse_response("not json")
    with pytest.raises(TypeError, match="no plan"):
        bench.parse_response('{"answer": 4}')


def test_api_errors_are_bounded_and_saved(case, tmp_path, monkeypatch):
    import urllib.error

    calls = []
    monkeypatch.setattr(bench.time, "sleep", lambda _: None)

    def unavailable(*_):
        calls.append(1)
        raise urllib.error.HTTPError("url", 429, "quota exhausted", hdrs=None, fp=None)

    monkeypatch.setattr(bench, "ask_gemini", unavailable)
    bench.plan_cases([case], tmp_path, "test-model", repeat=1, limit=None)
    document = json.loads((tmp_path / "plans-test-model.json").read_text(encoding="utf-8"))
    assert len(calls) == 3
    assert len(document["records"]["synthetic"]["api_errors"]) == 3
    assert document["records"]["synthetic"]["responses"] == []


def test_configuration_errors_fail_fast(case, tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "ask_gemini", lambda *_: (_ for _ in ()).throw(RuntimeError("no key")))
    with pytest.raises(RuntimeError, match="no key"):
        bench.plan_cases([case], tmp_path, "test-model", repeat=1, limit=None)


def test_invalid_responses_keep_the_raw_answer_and_are_not_api_errors(case, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(bench, "ask_gemini", lambda *_: calls.append(1) or "not json")
    bench.plan_cases([case], tmp_path, "test-model", repeat=1, limit=None)
    assert len(calls) == 1
    record = json.loads((tmp_path / "plans-test-model.json").read_text(encoding="utf-8"))["records"]["synthetic"]
    assert record["invalid_responses"][0]["raw"] == "not json"
    report = bench.score_cases([case], tmp_path, "test-model")
    assert report["summary"]["invalid_response_cases"] == 1
    assert report["summary"]["api_errors"] == 0


def test_successful_cases_are_not_called_again(case, tmp_path, monkeypatch):
    document = {"records": {"synthetic": {
        "split": "evaluation",
        "responses": [stored(case, response(["buffer_layer", "dissolve_layer"]))],
        "api_errors": [],
    }}}
    bench.write_json(tmp_path / "plans-test-model.json", document)
    monkeypatch.setattr(
        bench,
        "ask_gemini",
        lambda *_: (_ for _ in ()).throw(AssertionError("successful case was called again")),
    )
    bench.plan_cases([case], tmp_path, "test-model", repeat=1, limit=None)


def test_stale_responses_are_archived_and_regenerated(case, tmp_path, monkeypatch):
    stale = response(["buffer_layer", "dissolve_layer"])
    stale["prompt_sha256"] = "old-prompt"
    stale["model"] = "test-model"
    bench.write_json(tmp_path / "plans-test-model.json", {"records": {"synthetic": {
        "split": "evaluation", "responses": [stale], "api_errors": [],
    }}})
    monkeypatch.setattr(bench, "ask_gemini", lambda *_: json.dumps({
        "plan": [{"operation": "buffer_layer"}, {"operation": "spatial_join"}],
        "assumptions": ["crs_units=metres"], "warnings": [],
    }))
    bench.plan_cases([case], tmp_path, "test-model", repeat=1, limit=None)
    document = json.loads((tmp_path / "plans-test-model.json").read_text(encoding="utf-8"))
    assert len(document["records"]["synthetic"]["stale_responses"]) == 1
    assert bench.score_cases([case], tmp_path, "test-model")["summary"]["answered"] == 1


def test_score_partitions_answers_errors_and_unattempted_cases(case, tmp_path):
    cases = []
    for index in range(4):
        item = dict(case)
        item["id"] = f"case-{index}"
        item["required_assumption_keys"] = []
        cases.append(item)
    records = {
        "case-0": {"split": "evaluation", "responses": [stored(case, response(["buffer_layer", "dissolve_layer"]))]},
        "case-1": {"split": "evaluation", "responses": [], "api_errors": [stored(case, {"error": "429"})]},
        "case-2": {"split": "evaluation", "responses": [stored(case, response(["buffer_layer"])), stored(case, response(["buffer_layer"]))]},
    }
    bench.write_json(tmp_path / "plans-test-model.json", {"records": records})
    report = bench.score_cases(cases, tmp_path, "test-model")
    summary = report["summary"]
    assert summary["answered"] == 2
    assert summary["attempted"] == 3
    assert summary["api_errors"] == 1
    assert summary["api_error_events"] == 1
    assert summary["not_attempted"] == 1
    assert summary["complete"] is False
    assert summary["repeated_cases"] == 1
    assert summary["reproducible_cases"] == 1


def test_reproducibility_is_separate_from_correctness(case, tmp_path):
    records = {
        "synthetic": {
            "split": "evaluation",
            "responses": [
                stored(case, response(["buffer_layer"], assumptions=["crs_units=m"])),
                stored(case, response(["dissolve_layer"], assumptions=["crs_units=m"])),
            ],
        }
    }
    bench.write_json(tmp_path / "plans-test-model.json", {"records": records})
    report = bench.score_cases([case], tmp_path, "test-model")
    assert report["summary"]["reproducible_cases"] == 0
    assert report["summary"]["answered"] == 1
    assert report["rows"][0]["reproducible"] is False


def test_reproducibility_includes_assumptions(case, tmp_path):
    records = {
        "synthetic": {
            "split": "evaluation",
            "responses": [
                stored(case, response(["buffer_layer"], assumptions=["crs_units=m"])),
                stored(case, response(["buffer_layer"], assumptions=[])),
            ],
        }
    }
    bench.write_json(tmp_path / "plans-test-model.json", {"records": records})
    report = bench.score_cases([case], tmp_path, "test-model")
    assert report["rows"][0]["reproducible"] is False


def test_errors_from_another_split_or_prompt_do_not_pollute_a_report(case, tmp_path):
    other = dict(case)
    other["id"] = "other"
    other["split"] = "calibration"
    records = {
        "other": {
            "split": "calibration",
            "responses": [],
            "api_errors": [stored(other, {"error": "429"})],
            "invalid_responses": [stored(other, {"error": "bad json"})],
        }
    }
    bench.write_json(tmp_path / "plans-test-model.json", {"records": records})
    summary = bench.score_cases([case], tmp_path, "test-model")["summary"]
    assert summary["not_attempted"] == 1
    assert summary["api_errors"] == 0
    assert summary["api_error_events"] == 0
    assert summary["invalid_response_events"] == 0


def test_anthropic_request_uses_the_versioned_messages_contract(monkeypatch):
    seen = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self):
            return json.dumps({"content": [{"type": "text", "text": "first"}, {"type": "text", "text": "second"}]}).encode()

    def fake_urlopen(request, timeout):
        seen["request"] = request
        seen["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_test")
    monkeypatch.setattr(bench.urllib.request, "urlopen", fake_urlopen)
    answer = bench.ask_anthropic("plan this", "claude-sonnet-5")
    assert answer == "first\nsecond"
    request = seen["request"]
    body = json.loads(request.data)
    assert request.full_url == "https://api.anthropic.com/v1/messages"
    assert request.get_header("Anthropic-version") == "2026-10-21"
    assert request.get_header("Anthropic-workspace-id") == "wrkspc_test"
    assert request.get_header("X-api-key") == "test-key"
    assert body["model"] == "claude-sonnet-5"
    assert body["messages"] == [{"role": "user", "content": "plan this"}]
    assert "temperature" not in body


def test_anthropic_provider_runs_without_calling_gemini(case, tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "ask_gemini", lambda *_: (_ for _ in ()).throw(AssertionError("Gemini called")))
    monkeypatch.setattr(bench, "ask_anthropic", lambda *_: json.dumps({
        "plan": [{"operation": "buffer_layer"}, {"operation": "spatial_join"}],
        "assumptions": ["crs_units=metres"], "warnings": [],
    }))
    bench.plan_cases([case], tmp_path, "claude-sonnet-5", repeat=1, limit=None, provider="anthropic")
    document = json.loads((tmp_path / "plans-claude-sonnet-5.json").read_text(encoding="utf-8"))
    assert document["records"]["synthetic"]["responses"][0]["model"] == "claude-sonnet-5"


def test_workspace_resolution_prefers_default_and_caches(monkeypatch):
    calls = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self):
            return json.dumps({"data": [{"name": "Minuta", "id": "wrkspc_other"}, {"name": "Default", "id": "wrkspc_default"}]}).encode()

    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)
    monkeypatch.setattr(bench.urllib.request, "urlopen", lambda *_, **__: calls.append(1) or FakeResponse())
    bench.anthropic_workspace_id.cache_clear()
    assert bench.anthropic_workspace_id("another-test-key") == "wrkspc_default"
    assert bench.anthropic_workspace_id("another-test-key") == "wrkspc_default"
    assert len(calls) == 1


def test_baseline_artifact_is_complete_and_the_public_page_agrees():
    import hashlib

    artifact = json.loads((ROOT / "benchmarks" / "method_choice_baseline.json").read_text(encoding="utf-8"))
    assert artifact["dataset_sha256"] == hashlib.sha256(
        (ROOT / "benchmarks" / "method_choice_cases_20.json").read_bytes().replace(b"\r\n", b"\n")
    ).hexdigest()
    assert artifact["responses_per_case"] == 2
    for split in ("calibration", "evaluation"):
        report = artifact["reports"][split]
        assert report["complete"] is True
        assert report["cases"] == report["attempted"] == report["answered"] == 10
        assert report["api_errors"] == report["invalid_response_cases"] == 0
    assert artifact["reports"]["evaluation"]["api_error_events"] == 1
    assert artifact["reports"]["evaluation"]["invalid_response_events"] == 1
    page = (ROOT / "docs" / "benchmarks.md").read_text(encoding="utf-8")
    for text in ("7/10 | 3/10", "7/10 | 4/10", "0/6 | 0/4", "0/10 | 0/10", "One initial HTTP 400"):
        assert text in page


def test_contracts_artifact_is_complete_and_the_comparison_agrees():
    artifact = json.loads((ROOT / "benchmarks" / "method_choice_contracts.json").read_text(encoding="utf-8"))
    assert artifact["dataset_sha256"] == "b6aba9c8277721671a02016592c80e60ee0512fa9380691824cf6415663d9d34"
    assert artifact["dataset_revision"] == "38675d9"
    assert artifact["original_20_not_rerun"] is True
    for split, cases, rate in (("calibration", 46, 0.5217), ("evaluation", 47, 0.5319)):
        report = artifact["reports"][split]
        assert report["complete"] is True
        assert report["cases"] == report["attempted"] == report["answered"] == cases
        assert report["api_errors"] == report["invalid_response_cases"] == 0
        assert report["exact_rate"] == rate
    assert artifact["new_73"]["reports"]["calibration"]["exact_rate"] == 0.5
    assert artifact["new_73"]["reports"]["evaluation"]["exact_rate"] == 0.5135
    analysis = artifact["failure_analysis"]
    assert analysis["new_73_by_steps"]["3"]["exact"] == 2
    assert analysis["new_73_by_steps"]["4"]["exact"] == 0
    assert analysis["new_73_repeated_operations"] == {"cases": 21, "exact": 8, "exact_rate": 0.381}
    page = (ROOT / "docs" / "benchmarks.md").read_text(encoding="utf-8")
    for text in ("24/46", "25/47", "37/73 (50.7%)", "8/21 (38.1%)", "2/10 (20.0%)"):
        assert text in page


def test_repeated_operations_are_an_explicit_case_scoped_opt_in(case):
    original_prompt = bench.prompt_for(case)
    case["accepted_sequences"] = [["run_sql", "run_sql"]]
    with pytest.raises(ValueError, match="repeated"):
        bench.validate_cases([case])

    case["allow_repeated_operations"] = True
    assert bench.validate_cases([case])["cases"] == 1
    repeated_prompt = bench.prompt_for(case)
    assert "each at most once" not in repeated_prompt
    assert "distinct arguments" in repeated_prompt
    assert original_prompt != repeated_prompt
    assert "each at most once" in original_prompt


def test_literal_only_cases_do_not_require_an_input_dataset(case):
    case["inputs"] = []
    case["accepted_sequences"] = [["run_sql"]]

    assert bench.validate_cases([case])["cases"] == 1
    assert "no files; all values are literal in the request" in bench.prompt_for(case)


def test_every_required_step_present_counts_multiplicity(case):
    case["accepted_sequences"] = [["run_sql", "run_sql"]]
    case["allow_repeated_operations"] = True

    assert bench.score_response(case, response(["run_sql"]))["every_required_step_present"] is False
    assert bench.score_response(case, response(["run_sql", "run_sql"]))["every_required_step_present"] is True


def test_inputs_must_be_a_present_list(case):
    del case["inputs"]
    with pytest.raises(ValueError, match="inputs"):
        bench.validate_cases([case])
    case["inputs"] = {}
    with pytest.raises(ValueError, match="inputs"):
        bench.validate_cases([case])


def test_repeated_truth_includes_minimal_defensible_alternatives():
    expected = {
        "complete_classification-028": ["describe_dataset"],
        "data_databases-080": ["clip_layer"],
        "vector_analysis-005": ["buffer_layer", "dissolve_layer"],
    }
    cases = {case["id"]: case for case in bench.load_cases()}
    for cid, minimal in expected.items():
        assert minimal in cases[cid]["accepted_sequences"]


def test_additional_repeated_truths_include_minimal_alternatives():
    expected = {
        "data_databases-079": ["run_sql"],
        "data_databases-067": ["run_sql"],
        "data_databases-045": ["run_sql"],
        "data_databases-056": ["run_sql"],
        "data_databases-043": ["run_sql"],
        "data_databases-081": ["run_sql"],
        "data_databases-049": ["run_sql"],
        "data_databases-041": ["run_sql", "run_sql", "run_sql"],
        "rasters_grass-016": ["reclassify_raster"],
        "rasters_grass-007": ["reclassify_raster"],
        "rasters_grass-008": ["reclassify_raster", "reclassify_raster"],
        "complete_classification-020": ["dissolve_layer", "buffer_layer"],
        "data_databases-065": ["select_features", "spatial_join"],
        "data_databases-070": ["centroid_layer", "centroid_layer"],
    }
    cases = {case["id"]: case for case in bench.load_cases()}
    for cid, minimal in expected.items():
        assert minimal in cases[cid]["accepted_sequences"], cid
    assert cases["data_databases-070"]["output"]["cardinality"] == "more"


def test_requests_beyond_the_declared_operations_are_not_method_choice_cases():
    expected = {
        "data_databases-029",
        "data_databases-034",
        "data_databases-035",
        "data_databases-038",
        "data_databases-048",
        "processing-030",
        "processing-036",
        "processing-038",
        "vector_analysis-004",
    }
    cases = {case["id"]: case for case in bench.load_cases()}
    assert {cid for cid, case in cases.items() if not case.get("supported", True)} == expected
    for cid in expected:
        assert cases[cid]["unsupported_reason"]
        assert cases[cid]["accepted_sequences"] == []


def test_unsupported_cases_have_a_separate_denominator(case, tmp_path):
    unsupported = dict(case)
    unsupported.update({
        "id": "unsupported",
        "supported": False,
        "unsupported_reason": "The operation is read-only in the workspace",
        "accepted_sequences": [],
    })
    assert bench.validate_cases([case, unsupported])["unsupported_cases"] == 1
    records = {
        "synthetic": {"split": "evaluation", "responses": [
            stored(case, response(["buffer_layer", "dissolve_layer"], ["crs_units=m"])),
        ]},
        "unsupported": {"split": "evaluation", "responses": [
            stored(unsupported, response([], warnings=["no listed operation can change the table"])),
        ]},
    }
    bench.write_json(tmp_path / "plans-test-model.json", {"records": records})
    summary = bench.score_cases([case, unsupported], tmp_path, "test-model")["summary"]
    assert summary["answered"] == summary["cases"] == 2
    assert summary["scored_cases"] == 1
    assert summary["unsupported_cases"] == 1
    assert summary["unsupported_refused"] == 1
    assert summary["exact_rate"] == 1.0
    assert summary["complete"] is True


def test_an_unsupported_case_requires_a_reason(case):
    case.update({"supported": False, "accepted_sequences": []})
    with pytest.raises(ValueError, match="unsupported_reason"):
        bench.validate_cases([case])


def test_planner_sees_cardinality_geometry_and_independent_branches(case):
    case["output"]["cardinality"] = "one"
    case["allow_repeated_operations"] = True
    text = bench.prompt_for(case)
    assert "cardinality=one" in text
    assert "independent branches" in text
    assert "run_sql is the allowed operation" in text
    assert "declared data rather than external geographic knowledge" in text
    catalogue = bench.catalogue_text()
    assert "geometry: line" in catalogue
    assert "only SELECT under MAPSMITH_WORKSPACE" in catalogue
