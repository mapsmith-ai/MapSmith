"""A small, reproducible agentic pilot: plan, brief, execute, verify."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import geopandas as gpd
import shapely

from mapsmith import server, stacks
from mapsmith.plans import Plan


def vector(path: Path, geometries, fields: dict[str, list]) -> str:
    gpd.GeoDataFrame(fields, geometry=geometries, crs="EPSG:32632").to_file(path)
    return str(path)


def build_plan(workdir: Path) -> Plan:
    road = vector(workdir / "road.gpkg", [shapely.LineString([(0, 0), (100, 0)])], {"id": [1]})
    candidates = vector(
        workdir / "candidates.gpkg",
        [shapely.box(10, -10, 60, 10), shapely.box(70, -10, 90, 10)],
        {"id": [1, 2]},
    )
    overhead = vector(workdir / "overhead.gpkg", [shapely.box(50, -5, 60, 5)], {"id": [1]})
    return Plan.model_validate({
        "goal": "Two candidate sites within 20 m of the road and clear of overhead lines",
        "steps": [
            {"id": "road_buffer", "operation": "qgis_processing", "arguments": {
                "algorithm": "native:buffer", "input_path": road, "distance": 20,
                "output_path": str(workdir / "road_buffer.parquet"),
            }},
            {"id": "intersection", "operation": "overlay_layers", "arguments": {
                "input_path": candidates, "overlay_path": "$road_buffer",
                "output_path": str(workdir / "intersection.parquet"),
            }},
            {"id": "clear", "operation": "overlay_layers", "arguments": {
                "input_path": "$intersection", "overlay_path": overhead,
                "how": "difference", "output_path": str(workdir / "clear.parquet"),
            }},
            {"id": "area", "operation": "measure_area", "arguments": {
                "input_path": "$clear", "method": "planar",
                "output_path": str(workdir / "answer.parquet"),
            }},
        ],
    })


def run(workdir: Path | None = None) -> dict:
    if not stacks.installed()["opensource"]["qgis"]:
        raise RuntimeError("The pilot needs QGIS with qgis_process installed")
    root = workdir or Path(tempfile.mkdtemp(prefix="mapsmith-pilot-"))
    root.mkdir(parents=True, exist_ok=True)
    plan = build_plan(root)
    brief_response = server.brief_plan(plan)
    if not brief_response["valid"]:
        raise ValueError(brief_response["validation"])
    response = server.execute_plan(plan)
    if not response["executed"]:
        raise RuntimeError(response)
    answer = gpd.read_parquet(root / "answer.parquet")
    areas = sorted(round(float(value), 3) for value in answer.area)
    return {
        "goal": plan.goal,
        "brief": brief_response["brief"],
        "feature_count": len(answer),
        "areas_m2": areas,
        "total_area_m2": round(sum(areas), 3),
        "first_engine": response["steps"][0]["engine_used"],
        "plan_manifest": response["plan_manifest"],
    }


def main() -> None:
    result = run()
    summary = {key: value for key, value in result.items() if key != "brief"}
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
