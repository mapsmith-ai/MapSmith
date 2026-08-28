"""The worked example on the README is a recording, and this is what keeps it one.

A diagram is the easiest thing in a repository to lie with. It costs nothing to
draw a step that does not happen, an argument nobody passes or a check nobody
runs, and no reader can tell. So the section between the markers is written by
`benchmarks/worked_example.py` from an actual execution — fixtures built, plan
validated, operations run, manifests read — and this test rebuilds it and
compares. If the catalogue changes, if an operation stops recording a CRS
decision, if the validator's message moves, the README goes stale and the build
says so instead of a reader finding out.

Slow, because it is the real pipeline: buffer, clip, zonal statistics, area and a
SQL filter over fixtures written to disk.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
sys.path.insert(0, str(ROOT / "benchmarks"))

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def section() -> str:
    """Regenerate the README section from a real run."""
    import json
    import shutil
    import tempfile

    import worked_example as example

    from mapsmith.plans import executor, models, validator

    workdir = Path(tempfile.mkdtemp(prefix="mapsmith-worked-test-"))
    try:
        paths = example.build_fixtures(workdir)
        plan = example.build_plan(paths, workdir)
        rejected = validator.validate(
            models.Plan.model_validate(example.wrong_plan_first(plan))
        )
        result = executor.execute(models.Plan.model_validate(plan))

        steps = []
        for step in result.get("steps", []):
            record = {"id": step.get("id"), "operation": step.get("operation")}
            manifest = Path(f"{step.get('output_path') or step.get('output')}.provenance.json")
            if manifest.exists():
                data = json.loads(manifest.read_text(encoding="utf-8"))
                record["crs_decisions"] = data.get("crs_decisions", {})
                checks = data.get("verification", [])
                checks = checks if isinstance(checks, list) else checks.get("checks", [])
                record["checks_passed"] = sum(1 for c in checks if c.get("passed"))
                record["checks_total"] = len(checks)
            record["shown_arguments"] = ", ".join(
                f"`{k}={v}`"
                for k, v in next(
                    s["arguments"] for s in plan["steps"] if s["id"] == record["id"]
                ).items()
                if k not in ("output_path",)
                and not (isinstance(v, str) and v.startswith("/"))
                and not (isinstance(v, str) and ":" in v[:3])
            )
            steps.append(record)

        from mapsmith.engines import duckdb_engine

        measured = workdir / "answer.parquet"
        filtered = workdir / "below_limit.parquet"
        duckdb_engine.run_sql(
            f"SELECT * FROM read_parquet('{measured.as_posix()}') "
            f"WHERE mean < {example.ELEVATION_LIMIT_M}",
            output_path=str(filtered),
        )

        import geopandas as gpd

        before = gpd.read_parquet(measured)
        after = gpd.read_parquet(filtered)
        trace = {
            "goal": (
                "Parcels within 1.5 km of the river whose ground sits below 120 m, "
                "with the elevation and the ground area of each"
            ),
            "discovery": example.trace_discovery(),
            "rejected_plan": {
                "valid": rejected.valid,
                "errors": [
                    {"code": e.code, "step_id": e.step_id, "message": e.message}
                    for e in rejected.errors
                ],
            },
            "accepted_plan": {"valid": True, "steps": len(plan["steps"])},
            "execution": steps,
            "outside_the_plan": {
                "operation": "run_sql",
                "reason": (
                    "`$step` references resolve only in arguments declared as dataset "
                    "inputs; run_sql takes its inputs inside a SQL string, so it cannot "
                    "join the plan's dataflow. Deliberate: substituting into arbitrary "
                    "strings would let a planner assemble a path out of text."
                ),
                "rows_in": len(before),
                "rows_out": len(after),
            },
            "answer": [
                {k: (round(v, 2) if isinstance(v, float) else v) for k, v in row.items()}
                for row in after.drop(columns="geometry").to_dict("records")
            ],
        }
        return example.markdown(trace)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def test_the_readme_worked_example_is_what_a_real_run_produces(section):
    """Byte-for-byte, because every difference here is a page describing a run
    that did not happen. Regenerate with:

        python benchmarks/worked_example.py --write-readme
    """
    import worked_example as example

    prose = README.read_text(encoding="utf-8")
    assert example.START in prose and example.END in prose, (
        "the worked-example markers are gone from the README, so nothing is "
        "keeping that section attached to a run"
    )
    published = example.START + prose.split(example.START, 1)[1].split(example.END, 1)[0]
    published += example.END
    assert published.strip() == section.strip(), (
        "the worked example on the README is not what the script produces now. "
        "Run `python benchmarks/worked_example.py --write-readme` and read the diff "
        "before committing it: what changed in the product is in there."
    )


def test_the_example_answer_matches_what_the_fixture_makes_it(section):
    """The fixture is built so the answer can be stated before MapSmith runs.

    Squares of 0.0015° at 46.2°N are about 115.6 m by 166.7 m, so 19,270 m²
    against a geodesic measurement; elevation is a linear west-to-east ramp, so
    which parcels fall under the threshold is arithmetic. If a change ever makes
    the run disagree with that arithmetic, the run is wrong, not the arithmetic.
    """
    import worked_example as example

    span = example.DEM_EAST_LON - example.DEM_WEST_LON
    rise = example.DEM_EAST_M - example.DEM_WEST_M
    expected = []
    for name, offset in example.PARCELS:
        centre = example.RIVER_LON + offset + example.PARCEL_SIDE / 2
        elevation = example.DEM_WEST_M + (centre - example.DEM_WEST_LON) / span * rise
        if elevation < example.ELEVATION_LIMIT_M:
            expected.append((name, elevation))

    answer_table = section.split("| name |", 1)[1] if "| name |" in section else section
    for name, elevation in expected:
        assert name in answer_table, (
            f"{name} sits at {elevation:.0f} m, under the {example.ELEVATION_LIMIT_M} m "
            "threshold, and is not in the published answer"
        )
    # The exclusions are the half that matters: a filter that drops nothing looks
    # exactly like a filter that works.
    kept = {name for name, _ in expected}
    for name, offset in example.PARCELS:
        if name in kept:
            continue
        centre = example.RIVER_LON + offset + example.PARCEL_SIDE / 2
        elevation = example.DEM_WEST_M + (centre - example.DEM_WEST_LON) / span * rise
        assert name not in answer_table, (
            f"{name} sits at {elevation:.0f} m, above the {example.ELEVATION_LIMIT_M} m "
            "threshold, and is in the published answer anyway"
        )
    # ~19,270 m² by hand; the published figure is geodesic, so allow the ellipsoid
    # its say but not a different order of magnitude.
    assert "1930" in section or "1927" in section, (
        "the published areas are no longer near the 19,270 m² the fixture geometry "
        "implies for a 0.0015° square at this latitude"
    )
