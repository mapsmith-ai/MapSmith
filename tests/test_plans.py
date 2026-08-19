"""Typed plan DAG: every error code has a closed-form trigger; execution is exact."""

import json

import geopandas as gpd
import pytest
from pydantic import ValidationError
from shapely.geometry import Point, box

from mapsmith import catalog
from mapsmith.plans import Plan, execute, validate
from mapsmith.plans.registry import BINDINGS


def codes(report):
    return [e.code for e in report.errors]


def make_plan(*steps, goal=""):
    return Plan.model_validate({"goal": goal, "steps": list(steps)})


@pytest.fixture()
def wells(tmp_path):
    gdf = gpd.GeoDataFrame(
        {"name": ["a", "b"]},
        geometry=[Point(9.19, 45.46), Point(9.30, 45.60)],  # ~15 km apart, EPSG:4326
        crs="EPSG:4326",
    )
    path = tmp_path / "wells.gpkg"
    gdf.to_file(path)
    return str(path)


@pytest.fixture()
def zone(tmp_path):
    """Mask polygon that contains only well 'a' (even after a 300 m buffer)."""
    gdf = gpd.GeoDataFrame(
        geometry=[box(9.15, 45.42, 9.23, 45.50)], crs="EPSG:4326"
    )
    path = tmp_path / "zone.parquet"
    gdf.to_parquet(path)
    return str(path)


# --- registry <-> catalog sync ---------------------------------------------------


def test_every_available_operation_is_bound_or_planning():
    available = {
        op["name"] for op in catalog.OPERATIONS
        if op["status"] == "available" and op["category"] != "planning"
    }
    assert available == set(BINDINGS)
    for name in BINDINGS:
        entry = next(op for op in catalog.OPERATIONS if op["name"] == name)
        assert entry["status"] == "available"


# --- schema-level rejections ------------------------------------------------------


def test_schema_rejects_bad_step_id_and_extra_fields():
    with pytest.raises(ValidationError):
        make_plan({"id": "NotValid!", "operation": "buffer_layer", "arguments": {}})
    with pytest.raises(ValidationError):
        make_plan(
            {"id": "ok", "operation": "buffer_layer", "arguments": {}, "invented": 1}
        )
    with pytest.raises(ValidationError):
        Plan.model_validate({"steps": []})  # empty plans are meaningless


# --- one closed-form trigger per error code ---------------------------------------


def test_unknown_operation_suggests_bm25_neighbors():
    report = validate(make_plan({"id": "s1", "operation": "bufffer", "arguments": {}}))
    assert codes(report) == ["UNKNOWN_OPERATION"]
    assert "buffer_layer" in report.errors[0].message


def test_planned_operation_is_rejected_explicitly():
    report = validate(make_plan({"id": "s1", "operation": "isochrone", "arguments": {}}))
    assert codes(report) == ["OPERATION_NOT_AVAILABLE"]


def test_nested_plans_are_rejected():
    report = validate(
        make_plan({"id": "s1", "operation": "execute_plan", "arguments": {}})
    )
    assert codes(report) == ["NESTED_PLAN"]


def test_duplicate_step_ids(wells, tmp_path):
    out = str(tmp_path / "o.parquet")
    step = {
        "id": "same",
        "operation": "buffer_layer",
        "arguments": {"input_path": wells, "distance_meters": 10, "output_path": out},
    }
    step2 = dict(step, arguments=dict(step["arguments"], output_path=str(tmp_path / "p.parquet")))
    report = validate(make_plan(step, step2))
    assert "DUPLICATE_STEP_ID" in codes(report)


def test_missing_unknown_and_mistyped_arguments(wells, tmp_path):
    report = validate(
        make_plan(
            {
                "id": "s1",
                "operation": "buffer_layer",
                "arguments": {
                    "input_path": wells,
                    "distance_meters": "three hundred",  # WRONG_TYPE
                    "surprise": 1,  # UNKNOWN_ARGUMENT
                    # output_path missing -> MISSING_ARGUMENT
                },
            }
        )
    )
    assert sorted(codes(report)) == ["MISSING_ARGUMENT", "UNKNOWN_ARGUMENT", "WRONG_TYPE"]


def test_reference_errors(wells, tmp_path):
    out1 = str(tmp_path / "a.parquet")
    out2 = str(tmp_path / "b.parquet")
    report = validate(
        make_plan(
            {
                "id": "first",
                "operation": "clip_layer",
                "arguments": {
                    "input_path": "$second",  # FORWARD_REFERENCE
                    "mask_path": "$ghost",  # UNKNOWN_REFERENCE
                    "output_path": out1,
                },
            },
            {
                "id": "second",
                "operation": "buffer_layer",
                "arguments": {
                    "input_path": wells,
                    "distance_meters": 10,
                    "output_path": out2,
                },
            },
        )
    )
    assert sorted(codes(report)) == ["FORWARD_REFERENCE", "UNKNOWN_REFERENCE"]


def test_reference_to_step_without_output(wells, tmp_path):
    report = validate(
        make_plan(
            {"id": "look", "operation": "describe_dataset", "arguments": {"path": wells}},
            {
                "id": "buf",
                "operation": "buffer_layer",
                "arguments": {
                    "input_path": "$look",
                    "distance_meters": 10,
                    "output_path": str(tmp_path / "o.parquet"),
                },
            },
        )
    )
    assert codes(report) == ["REF_TO_NO_OUTPUT"]


def test_input_not_found(tmp_path):
    report = validate(
        make_plan(
            {
                "id": "s1",
                "operation": "buffer_layer",
                "arguments": {
                    "input_path": str(tmp_path / "nope.gpkg"),
                    "distance_meters": 10,
                    "output_path": str(tmp_path / "o.parquet"),
                },
            }
        )
    )
    assert codes(report) == ["INPUT_NOT_FOUND"]


def test_output_collision_and_overwrite(wells, tmp_path):
    out = str(tmp_path / "same.parquet")
    report = validate(
        make_plan(
            {
                "id": "a",
                "operation": "buffer_layer",
                "arguments": {"input_path": wells, "distance_meters": 1, "output_path": out},
            },
            {
                "id": "b",
                "operation": "buffer_layer",
                "arguments": {"input_path": wells, "distance_meters": 2, "output_path": out},
            },
            {
                "id": "c",
                "operation": "buffer_layer",
                "arguments": {"input_path": wells, "distance_meters": 3, "output_path": wells},
            },
        )
    )
    assert "OUTPUT_COLLISION" in codes(report)
    assert "OUTPUT_OVERWRITES_INPUT" in codes(report)


def test_output_dir_missing_and_extension_warning(wells, tmp_path):
    report = validate(
        make_plan(
            {
                "id": "s1",
                "operation": "buffer_layer",
                "arguments": {
                    "input_path": wells,
                    "distance_meters": 10,
                    "output_path": str(tmp_path / "no_such_dir" / "o.xyz"),
                },
            }
        )
    )
    assert "OUTPUT_DIR_MISSING" in codes(report)
    assert any(w.code == "SUSPICIOUS_OUTPUT_EXTENSION" for w in report.warnings)


def test_invalid_reprojection_crs(wells, tmp_path):
    report = validate(
        make_plan(
            {
                "id": "s1",
                "operation": "reproject_layer",
                "arguments": {
                    "input_path": wells,
                    "target_crs": "EPSG:notacode",
                    "output_path": str(tmp_path / "o.parquet"),
                },
            }
        )
    )
    assert codes(report) == ["INVALID_CRS"]


def test_workspace_jail(wells, tmp_path, monkeypatch):
    monkeypatch.setenv("MAPSMITH_WORKSPACE", str(tmp_path / "jail"))
    (tmp_path / "jail").mkdir()
    report = validate(
        make_plan(
            {
                "id": "s1",
                "operation": "buffer_layer",
                "arguments": {
                    "input_path": wells,  # outside the jail
                    "distance_meters": 10,
                    "output_path": str(tmp_path / "jail" / "o.parquet"),
                },
            }
        )
    )
    assert codes(report) == ["PATH_OUTSIDE_WORKSPACE"]


def test_sql_preview_warning():
    report = validate(
        make_plan({"id": "q", "operation": "run_sql", "arguments": {"query": "SELECT 1"}})
    )
    assert report.valid
    assert any(w.code == "SQL_PREVIEW_ONLY" for w in report.warnings)


def test_non_local_paths_rejected_before_any_io(tmp_path):
    """UNC/vsi/URI/ADS paths must be refused syntactically (NTLM-leak surface)."""
    for bad in (
        r"\\attacker.example\share\x.gpkg",
        "/vsicurl/https://example.com/x.gpkg",
        "https://example.com/x.gpkg",
        "data.parquet:hidden_stream",
    ):
        report = validate(
            make_plan(
                {
                    "id": "s1",
                    "operation": "buffer_layer",
                    "arguments": {
                        "input_path": bad,
                        "distance_meters": 10,
                        "output_path": str(tmp_path / "o.parquet"),
                    },
                }
            )
        )
        assert codes(report) == ["NON_LOCAL_PATH"], f"{bad}: {codes(report)}"
    # outputs are covered too
    report = validate(
        make_plan(
            {
                "id": "q",
                "operation": "run_sql",
                "arguments": {"query": "SELECT 1", "output_path": "s3://bucket/x.parquet"},
            }
        )
    )
    assert codes(report) == ["NON_LOCAL_PATH"]


def test_reference_not_allowed_outside_dataset_inputs(wells, tmp_path):
    report = validate(
        make_plan(
            {
                "id": "buf",
                "operation": "buffer_layer",
                "arguments": {
                    "input_path": wells,
                    "distance_meters": 10,
                    "output_path": str(tmp_path / "a.parquet"),
                },
            },
            {
                "id": "join",
                "operation": "spatial_join",
                "arguments": {
                    "left_path": "$buf",
                    "right_path": wells,
                    "output_path": str(tmp_path / "b.parquet"),
                    "predicate": "$buf",  # not a dataset input
                },
            },
        )
    )
    assert codes(report) == ["REFERENCE_NOT_ALLOWED"]


def test_explicit_missing_engine_rejected_statically(wells, tmp_path):
    report = validate(
        make_plan(
            {
                "id": "join",
                "operation": "spatial_join",
                "arguments": {
                    "left_path": wells,
                    "right_path": wells,
                    "output_path": str(tmp_path / "j.parquet"),
                    "engine": "sedonadb",  # optional extra, not installed in CI/test env
                },
            }
        )
    )
    import importlib.util

    if importlib.util.find_spec("sedona") is None:
        assert "ENGINE_NOT_AVAILABLE" in codes(report)
    else:  # environment with sedona installed: nothing to reject
        assert "ENGINE_NOT_AVAILABLE" not in codes(report)


def test_sql_not_sandboxed_warning_under_workspace(tmp_path, monkeypatch):
    ws = tmp_path / "jail"
    ws.mkdir()
    monkeypatch.setenv("MAPSMITH_WORKSPACE", str(ws))
    report = validate(
        make_plan({"id": "q", "operation": "run_sql", "arguments": {"query": "SELECT 1"}})
    )
    assert any(w.code == "SQL_NOT_SANDBOXED" for w in report.warnings)


def test_output_collision_is_case_and_separator_insensitive(wells, tmp_path):
    """'OUT.PARQUET' and 'out.parquet' are the same file on Windows."""
    out = tmp_path / "out.parquet"
    report = validate(
        make_plan(
            {
                "id": "a",
                "operation": "buffer_layer",
                "arguments": {"input_path": wells, "distance_meters": 1,
                              "output_path": str(out)},
            },
            {
                "id": "b",
                "operation": "buffer_layer",
                "arguments": {"input_path": wells, "distance_meters": 2,
                              "output_path": str(out).upper().replace("\\", "/")},
            },
        )
    )
    assert "OUTPUT_COLLISION" in codes(report)


# --- CRS simulation ----------------------------------------------------------------


def test_crs_simulation_through_the_chain(wells, zone, tmp_path):
    """4326 wells: buffer keeps 4326 (with UTM note), reproject flips to 32632."""
    buf = str(tmp_path / "buf.parquet")
    rep = str(tmp_path / "rep.parquet")
    report = validate(
        make_plan(
            {
                "id": "buf",
                "operation": "buffer_layer",
                "arguments": {"input_path": wells, "distance_meters": 300, "output_path": buf},
            },
            {
                "id": "rep",
                "operation": "reproject_layer",
                "arguments": {"input_path": "$buf", "target_crs": "EPSG:32632",
                              "output_path": rep},
            },
        )
    )
    assert report.valid
    assert report.simulated_outputs["buf"].crs == "EPSG:4326"
    assert report.simulated_outputs["rep"].crs == "EPSG:32632"
    assert any(n.code == "CRS_NOTE" for n in report.notes)  # geographic buffer note


def test_crs_alignment_note_on_mixed_inputs(wells, tmp_path):
    utm = gpd.read_file(wells).to_crs("EPSG:32632")
    utm_path = tmp_path / "wells_utm.parquet"
    utm.to_parquet(utm_path)
    report = validate(
        make_plan(
            {
                "id": "join",
                "operation": "spatial_join",
                "arguments": {
                    "left_path": wells,
                    "right_path": str(utm_path),
                    "output_path": str(tmp_path / "j.parquet"),
                },
            }
        )
    )
    assert report.valid
    assert any(n.code == "CRS_ALIGNMENT" for n in report.notes)


# --- execution ---------------------------------------------------------------------


def test_execute_refuses_invalid_plan(tmp_path):
    result = execute(
        make_plan({"id": "s1", "operation": "bufffer", "arguments": {}})
    )
    assert result["executed"] is False
    assert result["validation"]["errors"][0]["code"] == "UNKNOWN_OPERATION"


def test_execute_two_step_pipeline_closed_form(wells, zone, tmp_path):
    """Buffer 300 m then clip: only well 'a' survives — exactly 1 feature."""
    buf = str(tmp_path / "buf.parquet")
    final = str(tmp_path / "at_risk.parquet")
    plan = make_plan(
        {
            "id": "buf",
            "operation": "buffer_layer",
            "arguments": {"input_path": wells, "distance_meters": 300, "output_path": buf},
        },
        {
            "id": "cut",
            "operation": "clip_layer",
            "arguments": {"input_path": "$buf", "mask_path": zone, "output_path": final},
        },
        goal="wells at risk",
    )
    result = execute(plan)
    assert result["executed"] is True
    assert [s["status"] for s in result["steps"]] == ["ok", "ok"]
    assert result["steps"][1]["feature_count"] == 1
    out = gpd.read_parquet(final)
    assert len(out) == 1

    # plan manifest: written next to the last output, fingerprint matches
    manifest = json.loads((tmp_path / "at_risk.parquet.plan.json").read_text(encoding="utf-8"))
    assert manifest["plan_sha256"] == plan.sha256() == result["plan_sha256"]
    assert manifest["goal"] == "wells at risk"
    assert [s["id"] for s in manifest["steps"]] == ["buf", "cut"]
    # per-step provenance manifests exist on disk
    assert (tmp_path / "buf.parquet.provenance.json").exists()
    assert (tmp_path / "at_risk.parquet.provenance.json").exists()


def test_execute_stops_at_first_failure_keeps_earlier_outputs(wells, tmp_path):
    """Step 2 fails at runtime (bad SQL): step 1 output + manifest must survive."""
    buf = str(tmp_path / "buf.parquet")
    plan = make_plan(
        {
            "id": "buf",
            "operation": "buffer_layer",
            "arguments": {"input_path": wells, "distance_meters": 50, "output_path": buf},
        },
        {
            "id": "boom",
            "operation": "run_sql",
            "arguments": {
                "query": f"SELECT nonexistent_col FROM read_parquet('{buf}')",
                "output_path": str(tmp_path / "never.parquet"),
            },
        },
    )
    result = execute(plan)
    assert result["executed"] is False
    assert result["failed_step"]["step_id"] == "boom"
    assert result["steps"][0]["status"] == "ok"
    assert (tmp_path / "buf.parquet").exists()
    assert (tmp_path / "buf.parquet.provenance.json").exists()
    # the plan manifest still records the partial run
    manifest = json.loads((tmp_path / "buf.parquet.plan.json").read_text(encoding="utf-8"))
    assert [s["status"] for s in manifest["steps"]] == ["ok", "failed"]


def test_plan_sha256_is_deterministic(wells, tmp_path):
    args = {"input_path": wells, "distance_meters": 10,
            "output_path": str(tmp_path / "o.parquet")}
    p1 = make_plan({"id": "s1", "operation": "buffer_layer", "arguments": args})
    p2 = make_plan({"id": "s1", "operation": "buffer_layer", "arguments": dict(args)})
    assert p1.sha256() == p2.sha256()
    # semantically equal numbers hash identically (300 vs 300.0)
    p3 = make_plan(
        {"id": "s1", "operation": "buffer_layer",
         "arguments": dict(args, distance_meters=10.0)}
    )
    assert p1.sha256() == p3.sha256()
