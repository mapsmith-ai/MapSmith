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
    assert collections.Counter(len(case["accepted_sequences"][0]) for case in supported) == {1: 45, 2: 24, 3: 12, 4: 2}
    assert bench.validate_cases(cases)["cases"] == 93
    assert bench.validate_cases(cases)["unsupported_cases"] == 10


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


def test_typed_numeric_output_accepts_descriptive_answer():
    case = next(c for c in bench.load_cases() if c["id"] == "complete_classification-028")
    steps = [{
        "id": "describe_rivers",
        "operation": "describe_dataset",
        "arguments": {"path": "rivers"},
        "output": {"kind": "description", "cardinality": "one"},
    }]
    assert bench.output_matches(case, ["describe_dataset"], steps)


def test_more_output_cardinality_allows_multiple_final_branches():
    case = next(c for c in bench.load_cases() if c["id"] == "data_databases-028")
    steps = [
        {
            "id": name,
            "operation": "run_sql",
            "arguments": {"query": "SELECT 1", "output_path": f"{name}.parquet"},
            "output": {"kind": "table", "cardinality": cardinality},
        }
        for name, cardinality in (("towns", "fewer"), ("countries", "fewer"), ("people", "more"))
    ]
    assert bench.output_matches(case, ["run_sql"] * 3, steps)


def test_more_output_cardinality_includes_intermediate_deliverables():
    case = next(c for c in bench.load_cases() if c["id"] == "data_databases-028")
    steps = [
        {
            "id": name,
            "operation": "run_sql",
            "arguments": {"query": "SELECT 1", "output_path": f"{name}.parquet"},
            "output": {"kind": "table", "cardinality": cardinality},
        }
        for name, cardinality in (("towns", "fewer"), ("countries", "fewer"), ("people_normalized", "same_as_input"))
    ]
    steps[-1]["arguments"]["input_paths"] = ["input_1", "$towns", "$countries"]
    assert bench.output_matches(case, ["run_sql"] * 3, steps)


def test_point_buffer_area_accepts_two_sql_steps():
    case = next(c for c in bench.load_cases() if c["id"] == "data_databases-049")
    assert ["run_sql", "run_sql"] in case["accepted_sequences"]


def test_one_output_cardinality_accepts_operation_guarantee():
    case = next(c for c in bench.load_cases() if c["id"] == "vector_analysis-002")
    steps = [{
        "id": "reproject_oceans",
        "operation": "reproject_layer",
        "arguments": {"input_path": "input_1", "target_crs": "+proj=vandg", "output_path": "oceans.parquet"},
        "output": {"kind": "vector", "geometry": "polygon", "cardinality": "same_as_input"},
    }]
    assert bench.output_matches(case, ["reproject_layer"], steps)


def test_one_output_cardinality_requires_one_final_branch():
    case = next(c for c in bench.load_cases() if c["id"] == "data_databases-028")
    case = {**case, "output": {**case["output"], "cardinality": "one"}}
    steps = [
        {
            "id": name,
            "operation": "run_sql",
            "arguments": {"query": "SELECT 1", "output_path": f"{name}.parquet"},
            "output": {"kind": "table", "cardinality": "one"},
        }
        for name in ("towns", "countries")
    ]
    assert not bench.output_matches(case, ["run_sql"] * 2, steps)


def test_table_output_accepts_vector_dataset():
    case = next(c for c in bench.load_cases() if c["id"] == "complete_classification-029")
    steps = [{
        "id": "select_towns",
        "operation": "select_features",
        "arguments": {"input_path": "input_1", "output_path": "towns.parquet", "by": "field_equals", "field": "place", "value": "town"},
        "output": {"kind": "vector", "geometry": "point", "cardinality": "fewer"},
    }]
    assert bench.output_matches(case, ["select_features"], steps)


def test_table_output_accepts_numeric_answer():
    case = next(c for c in bench.load_cases() if c["id"] == "processing-016")
    steps = [{
        "id": "stats",
        "operation": "summarize_field",
        "arguments": {"input_path": "input_1", "field": "POP2000", "statistics": ["mean"]},
        "output": {"kind": "number", "cardinality": "one"},
    }]
    assert bench.output_matches(case, ["summarize_field"], steps)


def test_buffer_area_can_finish_with_measure_area():
    case = next(c for c in bench.load_cases() if c["id"] == "data_databases-049")
    assert ["run_sql", "run_sql", "buffer_layer", "measure_area"] in case["accepted_sequences"]
    assert ["run_sql", "buffer_layer", "measure_area"] in case["accepted_sequences"]
    assert ["run_sql"] * 4 in case["accepted_sequences"]


def test_raster_reprojection_case_supplies_target_crs():
    case = next(c for c in bench.load_cases() if c["id"] == "rasters_grass-002")
    assert "EPSG:32633" in case["goal"]


def test_multi_origin_service_area_truth_uses_one_operation():
    case = next(c for c in bench.load_cases() if c["id"] == "vector_analysis-017")
    assert case["accepted_sequences"] == [["service_area"]]
    parameters = {item["name"] for item in bench.available_operations()["service_area"]["parameters"]}
    assert "start_points_path" in parameters


def test_method_choice_truth_excludes_execution_steps():
    for case in bench.load_cases():
        for sequence in case["accepted_sequences"]:
            assert "execute_plan" not in sequence, (case["id"], sequence)


def test_prompt_assigns_literal_initial_input_placeholders(case):
    case["inputs"] = [
        {"kind": "vector", "geometry": "point", "role": "sample points"},
        {"kind": "vector", "geometry": "polygon", "role": "study area"},
    ]
    text = bench.prompt_for(case)
    assert "input_1" in text
    assert "input_2" in text
    assert text.index("input_1") < text.index("input_2")
    assert "$input_1" not in text
    assert "$input_2" not in text
    assert "literal" in text.lower()
    assert "earlier step" in text.lower()


def test_only_executable_operations_are_offered_to_the_planner():
    assert {"preview_map", "validate_plan", "execute_plan"}.isdisjoint(bench.available_operations())


def test_prompt_separates_operation_output_from_requested_output(case):
    text = bench.prompt_for(case).lower()
    assert "operation actually produces" in text
    assert "declared output guarantees" in text
    assert "not the requested output" in text


def test_prompt_rejects_partial_approximations(case):
    text = bench.prompt_for(case).lower()
    assert "any required part" in text
    assert "warning does not make" in text
    assert "empty plan" in text


def test_prompt_names_forbidden_approximation_patterns(case):
    text = " ".join(bench.prompt_for(case).lower().split())
    for example in (
        "filtered copy for a delete",
        "appended rows for an insert",
        "separate files for one required container",
        "combined output for per-feature outputs",
    ):
        assert example in text


def test_prompt_preserves_selections_and_explicit_constraints(case):
    text = " ".join(bench.prompt_for(case).lower().split())
    assert "every requested selection" in text
    assert "without an intermediate layer" in text


def test_prompt_requires_declared_sql_input_paths(case):
    text = " ".join(bench.prompt_for(case).lower().split())
    assert "input_paths" in text
    assert "every dataset used by the query" in text


def test_prompt_shows_literal_inputs_in_sql_and_limits_reference_syntax(case):
    text = bench.prompt_for(case)
    assert "read_parquet('input_1')" in text
    assert "field selection" in text


def test_prompt_requires_output_paths_for_dataset_writers(case):
    assert "every dataset-writing step needs output_path" in bench.prompt_for(case).lower()


def test_prompt_requires_selection_and_simplify_output_paths(case):
    assert "select_features and simplify_layer require output_path" in bench.prompt_for(case).lower()


def test_prompt_does_not_recompute_operation_results(case):
    text = bench.prompt_for(case).lower()
    assert "already returns" in text
    assert "do not add another operation" in text


def test_prompt_names_common_cardinality_guarantees(case):
    assert "buffer_layer and centroid_layer must keep same_as_input" in bench.prompt_for(case).lower()


def test_prompt_refuses_simulated_original_data_writes(case):
    text = bench.prompt_for(case).lower()
    assert "union all" in text
    assert "insert, delete or update" in text
    assert "original data" in text


def test_prompt_requires_selection_for_one_of(case):
    text = bench.prompt_for(case).lower()
    assert '"one of"' in text
    assert "select_features" in text


def test_prompt_prefers_dedicated_crs_and_centroid_operations(case):
    text = bench.prompt_for(case).lower()
    assert "describe_crs reads no data" in text
    assert "for geometric centres, use centroid_layer" in text
    assert "spatial_ref_sys" in text


def test_dedicated_alternatives_and_multi_answer_cardinality():
    cases = {case["id"]: case for case in bench.load_cases()}
    assert ["run_sql", "run_sql", "select_features"] in cases["data_databases-036"]["accepted_sequences"]
    assert ["run_sql", "run_sql", "buffer_layer"] in cases["data_databases-049"]["accepted_sequences"]
    assert cases["forestry-017"]["output"]["cardinality"] == "more"


def test_border_selection_sql_is_a_defensible_alternative():
    case = next(c for c in bench.load_cases() if c["id"] == "data_databases-065")
    assert ["select_features", "run_sql"] in case["accepted_sequences"]


def test_border_without_intermediate_layer_alternative():
    case = next(c for c in bench.load_cases() if c["id"] == "data_databases-067")
    assert ["select_features", "run_sql"] in case["accepted_sequences"]


def test_difference_and_filter_alternatives():
    cases = {case["id"]: case for case in bench.load_cases()}
    assert ["select_features", "simplify_layer", "run_sql", "merge_layers"] in cases["data_databases-008"]["accepted_sequences"]
    assert ["select_features", "run_sql", "run_sql", "run_sql"] in cases["data_databases-041"]["accepted_sequences"]


def test_prompt_requires_service_area_output_and_final_comparison(case):
    text = " ".join(bench.prompt_for(case).lower().split())
    assert '"operation": "service_area"' in text
    assert '"output_path": "reachable.parquet"' in text
    assert "final comparison step" in text


def test_sql_mean_centre_and_sql_comparison_alternative():
    case = next(c for c in bench.load_cases() if c["id"] == "vector_analysis-023")
    assert ["run_sql", "centroid_layer", "run_sql"] in case["accepted_sequences"]


def test_single_query_alternatives():
    cases = {case["id"]: case for case in bench.load_cases()}
    assert ["run_sql"] in cases["data_databases-008"]["accepted_sequences"]
    assert ["run_sql"] in cases["vector_analysis-023"]["accepted_sequences"]
    assert ["run_sql"] in cases["complete_classification-025"]["accepted_sequences"]


def test_prompt_requires_description_kind_and_all_outputs(case):
    text = " ".join(bench.prompt_for(case).lower().split())
    assert "describe_dataset produces description" in text
    assert "include every requested output" in text


def test_prompt_rejects_answer_output_kind(case):
    text = " ".join(bench.prompt_for(case).lower().split())
    assert "kind=answer" in text
    assert "kind=table" in text
    assert "summarize_field" in text


def test_prompt_shows_simplify_cardinality_example(case):
    text = " ".join(bench.prompt_for(case).lower().split())
    assert '"operation": "simplify_layer"' in text
    assert '"cardinality": "same_as_input"' in text


def test_prompt_shows_centroid_cardinality_example(case):
    text = " ".join(bench.prompt_for(case).lower().split())
    assert '"operation": "centroid_layer"' in text
    assert '"cardinality": "same_as_input"' in text


def test_prompt_refuses_saved_view_ddl(case):
    text = " ".join(bench.prompt_for(case).lower().split())
    assert "create or replace view" in text
    assert "saved view" in text
    assert "empty plan" in text


def test_crs_join_and_multi_engine_truth():
    cases = {case["id"]: case for case in bench.load_cases()}
    assert cases["data_databases-050"]["accepted_sequences"] == [["run_sql"]]
    assert ["join_table", "run_sql"] in cases["data_databases-056"]["accepted_sequences"]
    supported = cases["processing-036"]
    assert supported["supported"] is True
    assert supported["accepted_sequences"] == [["dissolve_layer", "run_sql", "compare_layers"]]


def test_saved_view_rewrite_is_unsupported():
    case = next(c for c in bench.load_cases() if c["id"] == "data_databases-047")
    assert case["supported"] is False
    assert case["accepted_sequences"] == []
    assert "DDL" in case["unsupported_reason"]


def test_virtual_view_is_unsupported():
    case = next(c for c in bench.load_cases() if c["id"] == "data_databases-046")
    assert case["supported"] is False
    assert case["accepted_sequences"] == []
    assert "virtual" in case["unsupported_reason"]


def test_additional_defensible_method_alternatives():
    cases = {case["id"]: case for case in bench.load_cases()}
    assert ["run_sql"] * 4 in cases["data_databases-028"]["accepted_sequences"]
    assert ["run_sql", "run_sql"] in cases["data_databases-044"]["accepted_sequences"]
    assert ["dissolve_layer", "centroid_layer", "centroid_layer", "nearest_join"] in cases["vector_analysis-023"]["accepted_sequences"]
    assert ["run_sql", "buffer_layer", "measure_area", "run_sql"] in cases["data_databases-049"]["accepted_sequences"]


def test_sql_mean_centre_and_merge_alternatives():
    case = next(c for c in bench.load_cases() if c["id"] == "vector_analysis-023")
    assert ["run_sql", "centroid_layer", "merge_layers"] in case["accepted_sequences"]
    assert ["run_sql", "centroid_layer", "nearest_join"] in case["accepted_sequences"]


def test_prompt_recognizes_existing_multi_engine_path(case):
    text = " ".join(bench.prompt_for(case).lower().split())
    assert "dissolve_layer and run_sql already use different engines" in text
    assert "multi-engine comparison is supported" in text


def test_full_layer_simplification_and_smoothing_alternatives():
    cases = {case["id"]: case for case in bench.load_cases()}
    assert ["select_features", "simplify_layer", "overlay_layers", "merge_layers"] in cases["data_databases-008"]["accepted_sequences"]
    assert ["buffer_layer", "dissolve_layer", "simplify_layer"] in cases["vector_analysis-006"]["accepted_sequences"]


def test_centre_comparison_accepts_sql_and_rejects_no_comparison():
    case = next(c for c in bench.load_cases() if c["id"] == "vector_analysis-023")
    assert ["run_sql", "run_sql"] in case["accepted_sequences"]
    assert ["dissolve_layer", "centroid_layer", "centroid_layer"] not in case["accepted_sequences"]


def test_multi_output_truth_uses_defensible_sequences():
    cases = {case["id"]: case for case in bench.load_cases()}
    assert cases["data_databases-044"]["output"]["cardinality"] == "more"
    assert cases["data_databases-049"]["output"] == {
        "kind": "table", "geometry": None, "cardinality": "more",
    }
    assert ["centroid_layer", "centroid_layer", "merge_layers"] in cases["data_databases-070"]["accepted_sequences"]
    alternatives = cases["vector_analysis-023"]["accepted_sequences"]
    assert ["dissolve_layer", "centroid_layer", "centroid_layer", "run_sql"] in alternatives
    assert ["dissolve_layer", "centroid_layer", "dissolve_layer", "centroid_layer", "run_sql"] in alternatives
    assert ["dissolve_layer", "centroid_layer", "centroid_layer", "merge_layers"] in alternatives
    assert ["run_sql", "run_sql", "buffer_layer", "measure_area", "run_sql"] in cases["data_databases-049"]["accepted_sequences"]


def test_prompt_places_comment_outside_arguments(case):
    text = " ".join(bench.prompt_for(case).lower().split())
    assert "comment belongs at step level" in text
    assert "never inside arguments" in text


def test_prompt_covers_mappable_artifacts_and_declared_keys(case):
    text = " ".join(bench.prompt_for(case).lower().split())
    assert "reprojected vector can be a mappable artifact" in text
    assert "declared key" in text
    assert "summarize_points_in_polygons requires output_path" in text


def test_prompt_refuses_ddl_and_multi_layer_container_substitutes(case):
    text = " ".join(bench.prompt_for(case).lower().split())
    assert "create table" in text
    assert "one geopackage with multiple layers" in text
    assert "empty plan" in text


def test_prompt_names_remaining_operation_contracts(case):
    text = " ".join(bench.prompt_for(case).lower().split())
    assert "network_shortest_path returns total_cost" in text
    assert "service_area and reproject_raster require output_path" in text
    assert "select_features produces vector" in text
    assert "same output_path" in text
    assert "single container" in text
    assert "must keep same_as_input" in text


def test_network_route_case_supplies_required_coordinates():
    case = next(c for c in bench.load_cases() if c["id"] == "vector_analysis-013")
    for declaration in ("from_x=0", "from_y=0", "to_x=1000", "to_y=0"):
        assert declaration in case["goal"]


def test_planner_sees_the_declared_argument_rules():
    text = bench.catalogue_text()
    assert "resampling in [" in text
    assert "window >= 3" in text
    assert "window odd" in text
    assert "output_path ends with ['.parquet']" in text


def test_parser_accepts_typed_dag_with_output_contracts():
    answer = bench.parse_response(json.dumps({
        "steps": [
            {"id": "names", "operation": "run_sql", "arguments": {}, "output": {"kind": "table"}},
            {"id": "main", "operation": "run_sql", "arguments": {}, "output": {"kind": "table"}},
        ],
        "warnings": [],
    }))
    assert answer["plan"] == ["run_sql", "run_sql"]
    assert [step["output"]["kind"] for step in answer["steps"]] == ["table", "table"]


def test_typed_dag_output_contract_does_not_require_a_linear_final_step(case):
    case["output"] = {"kind": "table", "geometry": None, "cardinality": "unknown"}
    answer = response(
        ["run_sql", "run_sql", "run_sql"],
        warnings=["three independent results"],
    )
    answer["steps"] = [
        {"id": name, "operation": "run_sql", "arguments": {
            "query": f"SELECT * FROM {name}", "output_path": f"{name}.parquet",
        }, "output": {"kind": "table"}}
        for name in ("names", "all", "main")
    ]
    row = bench.score_response(case, answer)
    assert row["output_contract"] is True


def test_typed_dag_rejects_an_incompatible_output_kind(case):
    answer = response(["run_sql"])
    answer["steps"] = [
        {"id": "wrong", "operation": "run_sql", "arguments": {}, "output": {"kind": "raster"}},
    ]
    assert bench.score_response(case, answer)["output_contract"] is False


def test_typed_dag_requires_declared_geometry_when_the_output_demands_it(case):
    answer = response(["run_sql"])
    answer["steps"] = [
        {"id": "missing_geometry", "operation": "run_sql", "arguments": {}, "output": {"kind": "vector"}},
    ]
    assert bench.score_response(case, answer)["output_contract"] is False


RESIDUAL_BRANCHES = {
    "data_databases-028": [("run_sql", "table", None)] * 3,
    "vector_analysis-013": [("network_shortest_path", "vector", "line")],
    "data_databases-070": [("centroid_layer", "vector", "point")] * 2,
    "vector_analysis-002": [("reproject_layer", "map", "polygon")],
    "data_databases-046": [("run_sql", "table", None)],
    "data_databases-050": [("run_sql", "table", None)],
    "data_databases-060": [("run_sql", "vector", "point")],
    "data_databases-067": [("run_sql", "vector", "polygon")],
    "data_databases-008": [("select_features", "vector", "polygon"), ("simplify_layer", "vector", "polygon")],
    "data_databases-036": [("run_sql", "table", None)] * 3,
    "vector_analysis-017": [("service_area", "vector", "line")],
    "vector_analysis-023": [
        ("dissolve_layer", "vector", "point"),
        ("centroid_layer", "vector", "point"),
        ("centroid_layer", "vector", "point"),
    ],
    "data_databases-049": [("run_sql", "vector", "polygon")],
}


@pytest.mark.parametrize(("case_id", "branches"), RESIDUAL_BRANCHES.items())
def test_residual_cases_have_explicit_branch_contracts(case_id, branches):
    steps = []
    for index, (operation, kind, geometry) in enumerate(branches):
        output = {"kind": kind}
        if geometry:
            output["geometry"] = geometry
        steps.append({
            "id": f"branch_{index}",
            "operation": operation,
            "arguments": {},
            "output": output,
        })
    typed = bench.Plan.model_validate({"goal": case_id, "steps": steps})
    for step in typed.steps:
        errors = []
        entry = bench.available_operations()[step.operation]
        bench._check_output_contract(step, entry, errors)
        assert not errors, (case_id, step.operation, errors)
        if step.operation in {"network_shortest_path", "service_area", "centroid_layer"}:
            assert entry["output_geometry"] == step.output.geometry


def test_parser_normalizes_the_optional_geometry_placeholder():
    answer = bench.parse_response(json.dumps({
        "steps": [{"id": "r", "operation": "reclassify_raster", "arguments": {},
                   "output": {"kind": "raster", "geometry": "optional"}}],
    }))
    assert "geometry" not in answer["steps"][0]["output"]


def test_parser_normalizes_empty_geometry_placeholder():
    answer = bench.parse_response(json.dumps({
        "steps": [{"id": "stats", "operation": "summarize_field", "arguments": {},
                   "output": {"kind": "table", "geometry": ""}}],
    }))
    assert "geometry" not in answer["steps"][0]["output"]


def test_parser_accepts_a_fenced_answer_and_rejects_a_non_plan():
    answer = bench.parse_response('```json\n{"plan": [{"operation": "clip_layer"}]}\n```')
    assert answer["plan"] == ["clip_layer"]
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


def test_anthropic_temperature_zero_is_an_explicit_experimental_arm(monkeypatch):
    seen = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self):
            return json.dumps({"content": [{"type": "text", "text": "zero"}]}).encode()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_test")
    monkeypatch.setattr(bench.urllib.request, "urlopen", lambda request, timeout: seen.__setitem__("request", request) or FakeResponse())
    assert bench.ask_anthropic("plan this", "claude-sonnet-5", temperature=0) == "zero"
    assert json.loads(seen["request"].data)["temperature"] == 0


def test_anthropic_provider_runs_without_calling_gemini(case, tmp_path, monkeypatch):
    monkeypatch.setattr(bench, "ask_gemini", lambda *_: (_ for _ in ()).throw(AssertionError("Gemini called")))
    monkeypatch.setattr(bench, "ask_anthropic", lambda *_: json.dumps({
        "plan": [{"operation": "buffer_layer"}, {"operation": "spatial_join"}],
        "assumptions": ["crs_units=metres"], "warnings": [],
    }))
    bench.plan_cases([case], tmp_path, "claude-sonnet-5", repeat=1, limit=None, provider="anthropic")
    path = bench.plan_path(tmp_path, "claude-sonnet-5", "anthropic")
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["records"]["synthetic"]["responses"][0]["model"] == "claude-sonnet-5"


def test_structurally_invalid_typed_dag_cannot_score_exact(case):
    case["accepted_sequences"] = [["run_sql"]]
    case["output"] = {"kind": "table", "geometry": None}
    case["required_assumption_keys"] = []
    answer = response(["run_sql"])
    answer["steps"] = [
        {"id": "missing", "operation": "run_sql", "arguments": {}, "output": {"kind": "table"}},
    ]
    row = bench.score_response(case, answer)
    assert row["verdict"] == "invalid_plan"
    assert row["first_step_right"] is False
    assert row["every_required_step_present"] is False
    assert row["output_contract"] is False
    assert row["silent_planning_error"] is True
    assert "MISSING_ARGUMENT" in row["structural_errors"]


def test_scorer_rejects_an_initial_input_placeholder_that_does_not_exist(case):
    case["accepted_sequences"] = [["run_sql"]]
    case["output"] = {"kind": "table", "geometry": None}
    case["required_assumption_keys"] = []
    answer = response(["run_sql"])
    answer["steps"] = [{
        "id": "query",
        "operation": "run_sql",
        "arguments": {
            "query": "SELECT * FROM read_parquet('input_999')",
            "input_paths": ["input_999"],
            "output_path": "result.parquet",
        },
        "output": {"kind": "table"},
    }]
    row = bench.score_response(case, answer)
    assert row["verdict"] == "invalid_plan"
    assert "UNKNOWN_INPUT_PLACEHOLDER" in row["structural_errors"]


def test_every_leaf_needs_its_own_output_contract(case):
    case["output"] = {"kind": "table", "geometry": None, "cardinality": "unknown"}
    answer = response(["run_sql", "run_sql"])
    answer["steps"] = [
        {"id": "first", "operation": "run_sql", "arguments": {
            "query": "SELECT 1", "output_path": "first.parquet",
        }, "output": {"kind": "table"}},
        {"id": "last", "operation": "run_sql", "arguments": {
            "query": "SELECT 2", "output_path": "last.parquet",
        }},
    ]
    assert bench.output_matches(case, answer["plan"], answer["steps"]) is False


def test_reproducibility_includes_typed_arguments_and_output_contracts():
    first = response(["run_sql"])
    first["steps"] = [{
        "id": "query", "operation": "run_sql",
        "arguments": {"query": "SELECT 1", "output_path": "one.parquet"},
        "output": {"kind": "table"},
    }]
    second = response(["run_sql"])
    second["steps"] = [{
        "id": "query", "operation": "run_sql",
        "arguments": {"query": "SELECT 2", "output_path": "two.parquet"},
        "output": {"kind": "number"},
    }]
    assert bench.response_signature(first) != bench.response_signature(second)


def test_temperature_and_provider_are_part_of_response_identity(case):
    import copy

    default = stored(case, response(["buffer_layer"]), "shared-model")
    default.update({"provider": "anthropic", "temperature": None})
    zero = copy.deepcopy(default)
    zero["temperature"] = 0
    record = {"split": "evaluation", "responses": [default, zero]}
    assert bench.current_responses(record, case, "shared-model", "anthropic") == [default]
    assert bench.current_responses(record, case, "shared-model", "anthropic", 0) == [zero]
    assert bench.current_responses(record, case, "shared-model") == []
    assert bench.plan_path(Path("out"), "m", "anthropic").name == "plans-m-anthropic-default.json"
    assert bench.plan_path(Path("out"), "m", "anthropic", 0).name == "plans-m-anthropic-temperature-0.json"


def test_gemini_cannot_relabel_its_fixed_zero_temperature(case, tmp_path):
    with pytest.raises(ValueError, match="fixed at temperature zero"):
        bench.plan_cases([case], tmp_path, "m", 1, None, temperature=0.5)
    with pytest.raises(ValueError, match="fixed at temperature zero"):
        bench.score_cases([case], tmp_path, "m", temperature=0)


def test_list_references_mark_producers_as_nonterminal(case):
    case["accepted_sequences"] = [["run_sql", "run_sql", "merge_layers"]]
    case["output"] = {"kind": "vector", "geometry": "polygon", "cardinality": "unknown"}
    answer = response(["run_sql", "run_sql", "merge_layers"])
    answer["steps"] = [
        {"id": "a", "operation": "run_sql", "arguments": {
            "query": "SELECT 1", "output_path": "a.parquet",
        }, "output": {"kind": "vector", "geometry": "polygon"}},
        {"id": "b", "operation": "run_sql", "arguments": {
            "query": "SELECT 2", "output_path": "b.parquet",
        }, "output": {"kind": "vector", "geometry": "polygon"}},
        {"id": "merged", "operation": "merge_layers", "arguments": {
            "input_paths": ["$a", "$b"], "output_path": "merged.parquet",
        }, "output": {"kind": "vector", "geometry": "polygon"}},
    ]
    assert bench.output_matches(case, answer["plan"], answer["steps"]) is True


def test_score_artifacts_are_arm_qualified():
    assert bench.score_path(Path("out"), "m").name == "scores-m.json"
    assert bench.score_path(Path("out"), "m", "anthropic").name == "scores-m-anthropic-default.json"
    assert bench.score_path(Path("out"), "m", "anthropic", 0).name == "scores-m-anthropic-temperature-0.json"


def test_http_error_body_and_arm_are_retained(case, tmp_path, monkeypatch):
    import io
    import urllib.error

    failure = urllib.error.HTTPError(
        "url", 400, "bad request", hdrs=None,
        fp=io.BytesIO(b'{"error":{"message":"temperature: Extra inputs are not permitted"}}'),
    )
    monkeypatch.setattr(bench, "ask_anthropic", lambda *_, **__: (_ for _ in ()).throw(failure))
    bench.plan_cases([case], tmp_path, "m", 1, None, provider="anthropic", temperature=0)
    path = bench.plan_path(tmp_path, "m", "anthropic", 0)
    record = json.loads(path.read_text(encoding="utf-8"))["records"]["synthetic"]
    event = record["api_errors"][0]
    assert event["http_status"] == 400
    assert event["provider"] == "anthropic"
    assert event["temperature"] == 0
    assert "Extra inputs are not permitted" in event["error"]


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


def test_multistep_artifact_separates_truth_audit_from_planner_change():
    artifact = json.loads((ROOT / "benchmarks" / "method_choice_multistep.json").read_text(encoding="utf-8"))
    historical = json.loads((ROOT / "benchmarks" / "method_choice_typed_dag.json").read_text(encoding="utf-8"))
    assert artifact["original_20_not_rerun"] is True
    assert artifact["responses_per_case"] == 2
    assert artifact["dataset_sha256"] == historical["dataset_sha256"]
    baseline = artifact["before"]["original_truth"]["reports"]["all"]
    audited = artifact["before"]["audited_truth"]["reports"]["all"]
    assert baseline["answered"] == baseline["cases"] == 73
    assert baseline["exact_rate"] == 0.5205
    assert audited["scored_cases"] == 65
    assert audited["unsupported_cases"] == 8
    assert audited["exact_rate"] == 0.6308
    after = artifact["after"]["reports"]["all"]
    assert after["complete"] is True
    assert after["answered"] == 73
    assert after["api_errors"] == after["invalid_response_cases"] == 0
    assert after["scored_cases"] == 65
    assert after["exact_rate"] == 0.8
    comparison = artifact["comparison"]
    assert len(comparison["improved_ids"]) == 11
    assert comparison["regressed_ids"] == []
    assert comparison["after_no_plan_ids"] == []
    page = (ROOT / "docs" / "benchmarks.md").read_text(encoding="utf-8")
    for text in ("41/65 (63.1%)", "52/65 (80.0%)", "4 refusals before versus 3 after"):
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
        "data_databases-065": ["select_features", "spatial_join", "run_sql"],
        "data_databases-070": ["centroid_layer", "centroid_layer"],
        "data_databases-044": ["run_sql"],
        "data_databases-028": ["run_sql", "run_sql", "run_sql"],
        "vector_analysis-020": ["run_sql"],
    }
    cases = {case["id"]: case for case in bench.load_cases()}
    for cid, minimal in expected.items():
        assert minimal in cases[cid]["accepted_sequences"], cid
    assert cases["data_databases-070"]["output"]["cardinality"] == "more"
    assert cases["data_databases-028"]["allow_repeated_operations"] is True
    assert cases["vector_analysis-020"]["accepted_sequences"] == [["run_sql"]]
    assert ["select_features", "spatial_join"] not in cases["data_databases-065"]["accepted_sequences"]
    assert cases["processing-036"]["output"]["kind"] == "table"


def test_requests_beyond_the_declared_operations_are_not_method_choice_cases():
    expected = {
        "data_databases-029",
        "data_databases-034",
        "data_databases-035",
        "data_databases-038",
        "data_databases-046",
        "data_databases-047",
        "data_databases-048",
        "processing-030",
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
    planned = dict(unsupported, id="unsupported-with-plan")
    empty = dict(unsupported, id="unsupported-empty")
    irrelevant = dict(unsupported, id="unsupported-irrelevant")
    cases = [case, unsupported, planned, empty, irrelevant]
    assert bench.validate_cases(cases)["unsupported_cases"] == 4
    records = {
        "synthetic": {"split": "evaluation", "responses": [
            stored(case, response(["buffer_layer", "dissolve_layer"], ["crs_units=m"])),
        ]},
        "unsupported": {"split": "evaluation", "responses": [
            stored(unsupported, response([], warnings=["no listed operation can change the table"])),
        ]},
        "unsupported-with-plan": {"split": "evaluation", "responses": [
            stored(planned, response(["select_features"])),
        ]},
        "unsupported-empty": {"split": "evaluation", "responses": [stored(empty, response([]))]},
        "unsupported-irrelevant": {"split": "evaluation", "responses": [
            stored(irrelevant, response([], warnings=["weather is nice"])),
        ]},
    }
    bench.write_json(tmp_path / "plans-test-model.json", {"records": records})
    summary = bench.score_cases(cases, tmp_path, "test-model")["summary"]
    assert summary["answered"] == summary["cases"] == 5
    assert summary["scored_cases"] == 1
    assert summary["unsupported_answered"] == 4
    assert summary["unsupported_cases"] == 4
    assert summary["unsupported_refused"] == 3
    assert summary["unsupported_warned"] == 1
    assert summary["unsupported_empty_without_warning"] == 1
    assert summary["unsupported_irrelevant_warning"] == 1
    assert summary["unsupported_with_plan"] == 1
    assert summary["exact_rate"] == 1.0
    assert summary["complete"] is True


def test_an_unsupported_case_requires_a_reason(case):
    case.update({"supported": False, "accepted_sequences": []})
    with pytest.raises(ValueError, match="unsupported_reason"):
        bench.validate_cases([case])


@pytest.mark.parametrize("value", [0, None, "", "false", 1])
def test_supported_flag_must_be_a_boolean(case, value):
    case["supported"] = value
    with pytest.raises(ValueError, match="supported must be a boolean"):
        bench.validate_cases([case])


def test_planner_sees_cardinality_geometry_and_independent_branches(case):
    case["output"]["cardinality"] = "one"
    case["allow_repeated_operations"] = True
    text = bench.prompt_for(case)
    assert "cardinality=one" in text
    assert "independent branches" in text
    assert "run_sql is the allowed operation" in text
    assert "MAPSMITH_WORKSPACE is enabled for every case" in text
    assert "run_sql is SELECT-only" in text
    assert "Prefer dedicated operations over run_sql" in text
    assert "Do not add extract_layer" in text
    assert "declared data rather than external geographic knowledge" in text
    catalogue = bench.catalogue_text()
    assert "geometry: line" in catalogue
    assert "only SELECT under MAPSMITH_WORKSPACE" in catalogue


def test_preview_contract_does_not_preserve_a_custom_projection():
    preview = next(op for op in bench.catalog.OPERATIONS if op["name"] == "preview_map")
    assert "preview_map renders every layer in EPSG:4326" in preview["summary"]
    assert "preview_map" not in bench.available_operations()


def test_unsupported_work_is_not_replaced_by_a_read_only_approximation(case):
    text = bench.prompt_for(case)
    assert "read-only approximation" in text


def test_generalization_artifact_reports_variance_and_no_truth_changes():
    artifact = json.loads((ROOT / "benchmarks" / "method_choice_generalization.json").read_text(encoding="utf-8"))
    historical = json.loads((ROOT / "benchmarks" / "method_choice_typed_dag.json").read_text(encoding="utf-8"))
    assert artifact["original_20_not_rerun"] is True
    assert artifact["truth_unchanged"] is True
    assert artifact["dataset_sha256"] == historical["dataset_sha256"]
    runs = artifact["runs"]
    assert [run["summary"]["verdicts"]["exact"] for run in runs] == [52, 53, 53]
    assert [run["summary"]["exact_rate"] for run in runs] == [0.8, 0.8154, 0.8154]
    assert artifact["final_vs_independent"]["improved_ids"] == ["data_databases-065"]
    assert artifact["final_vs_independent"]["regressed_ids"] == ["processing-036"]
    final = runs[-1]["summary"]
    assert final["unsupported_refused"] == 6
    assert final["unsupported_with_plan"] == 2
    page = (ROOT / "docs" / "benchmarks.md").read_text(encoding="utf-8")
    assert "53/65 (81.5%)" in page
    assert "refusals double from 3/8 to 6/8" in page


def test_literal_inputs_artifact_reconciles_supported_and_unsupported_counts():
    artifact = json.loads((ROOT / "benchmarks" / "method_choice_literal_inputs.json").read_text(encoding="utf-8"))
    summary = artifact["summary"]
    assert summary["exact"] == summary["exact_plus_output"] == artifact["scored_cases"] == 63
    assert summary["exact_rate"] == 1.0
    assert artifact["non_exact_ids"] == artifact["output_failed_ids"] == []
    assert summary["unsupported_refused"] + summary["unsupported_with_plan"] == 10
    assert artifact["plans_with_structural_errors"] == 0
    assert sum(artifact["structural_errors"].values()) == 0
    assert artifact["responses"] == artifact["cases"] * artifact["responses_per_case"] == 146
    assert artifact["previous_raw_rescored"]["exact_plus_output"] == 38
    assert artifact["public_cases_sha256"] == "8483d6bced5a870799a521ec6440f51eaa12cbbe3daaa39da5ce5c824300e940"
    assert artifact["validation"]["focused_tests_passed"] == 220
    assert artifact["validation"]["full_tests_passed"] == 2587
    page = " ".join((ROOT / "docs" / "benchmarks.md").read_text(encoding="utf-8").split())
    assert "63/63 exact and 63/63 exact-plus-output" in page
    assert "10/10 unsupported requests refused" in page


def test_independent_literal_inputs_confirms_the_same_frozen_result():
    first = json.loads((ROOT / "benchmarks" / "method_choice_literal_inputs.json").read_text(encoding="utf-8"))
    independent = json.loads((ROOT / "benchmarks" / "method_choice_literal_inputs_independent.json").read_text(encoding="utf-8"))
    for key in ("dataset_sha256", "public_cases_sha256", "catalogue_sha256", "prompt_sha256"):
        assert independent[key] == first[key]
    assert independent["summary"] == first["summary"]
    assert independent["transitions_exact_plus_output"] == {"improved_ids": [], "regressed_ids": []}
    assert independent["comparison"]["no_intervening_edits"] is True
    assert independent["comparison"]["same_prompt_catalogue_dataset_and_scorer"] is True


def test_literal_inputs_raw_evidence_reproduces_scores():
    import hashlib

    for filename in (
        "method_choice_literal_inputs.json",
        "method_choice_literal_inputs_independent.json",
    ):
        artifact = json.loads((ROOT / "benchmarks" / filename).read_text(encoding="utf-8"))
        evidence = artifact["raw_evidence"]
        for kind in ("dataset", "plans", "scores"):
            path = ROOT / evidence[kind]["path"]
            assert path.is_file()
            bench.assert_public_evidence(path)
            assert hashlib.sha256(path.read_bytes()).hexdigest() == evidence[kind]["sha256"]
        scores_path = ROOT / evidence["scores"]["path"]
        expected = json.loads(scores_path.read_text(encoding="utf-8"))
        recomputed = bench.score_cases(
            bench.load_cases(ROOT / evidence["dataset"]["path"]), scores_path.parent,
            artifact["model"], artifact["provider"], artifact["temperature"], frozen=True,
        )
        assert recomputed == expected
        assert "--provider anthropic" in evidence["scoring_command"]
        assert "--frozen" in evidence["scoring_command"]
        assert f"--data {evidence['dataset']['path']}" in evidence["scoring_command"]
        assert f"--out {scores_path.parent.relative_to(ROOT).as_posix()}" in evidence["scoring_command"]


@pytest.mark.parametrize(
    "marker",
    [
        r"C:\Users\person\private.json",
        "/home/person/private.json",
        ("where" "tech"),
        "api_key=abc123",
        "-----BEGIN PRIVATE KEY-----",
    ],
)
def test_public_evidence_rejects_private_markers(tmp_path, marker):
    path = tmp_path / "raw.json"
    path.write_text(marker, encoding="utf-8")
    with pytest.raises(ValueError, match="private"):
        bench.assert_public_evidence(path)
