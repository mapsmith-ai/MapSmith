"""All sixteen D-063 plans render, execute and return closed-form results."""

from __future__ import annotations

import geopandas as gpd
import pandas as pd
import pytest

from brief_cases import case
from mapsmith.plans import execute
from mapsmith.plans.brief import assert_traced, execute_plan_requests, render


def assert_closed_form_result(index, workdir):
    output = lambda name: pd.read_parquet(workdir / name)
    if index == 19:
        assert gpd.read_parquet(workdir / "overhead_out.parquet").area.sum() == pytest.approx(900)
    elif index == 21:
        rates = output("rates.parquet").iloc[0]
        assert rates["raw_rate"] == pytest.approx(10)
        assert rates["smoothed_rate"] == pytest.approx(10)
        assert rates["shrinkage"] == 0
    elif index == 24:
        assert output("incidence.parquet")["incidence"].tolist() == pytest.approx([0.025])
    elif index == 43:
        assert output("flagged.parquet")["id"].tolist() == [1]
    elif index == 47:
        assert output("score.parquet")["suitability"].tolist() == pytest.approx([1])
    elif index == 93:
        assert output("area.parquet")["area_m2"].tolist() == pytest.approx([80])
    elif index == 99:
        result = output("heat.parquet")
        assert result["id"].tolist() == [1, 2]
        assert result["weighted_heat"].tolist() == pytest.approx([7 / 3, 18 / 5])
    elif index in (107, 109):
        result = output("grade.parquet").iloc[0]
        assert result["chainage"] == 0
        assert result["grade_percent"] == pytest.approx(100)
    elif index == 111:
        assert output("grade.parquet")["exceeds"].tolist() == [True]
    elif index == 112:
        assert output("compare.parquet")["ascent_difference"].tolist() == pytest.approx([95])
    elif index == 122:
        assert output("old.parquet")["point_count"].tolist() == [2]
        assert output("new.parquet")["point_count"].tolist() == [1, 1]
    elif index == 140:
        assert output("score.parquet")["resistance"].tolist() == pytest.approx([2])
    elif index == 141:
        assert output("score.parquet")["score"].tolist() == pytest.approx([2])
    elif index == 142:
        assert output("fraction.parquet")["mean"].tolist() == pytest.approx([1])
    elif index == 147:
        result = output("best.parquet").iloc[0]
        assert result["id"] == 2
        assert result["visible_population"] == pytest.approx(6)
    else:
        raise AssertionError(f"no closed-form result for {index}")


@pytest.mark.parametrize("index", [index for index, _ in execute_plan_requests()])
def test_every_d063_plan_renders_and_executes(index, tmp_path, monkeypatch):
    monkeypatch.setenv("MAPSMITH_WORKSPACE", str(tmp_path))
    current = case(index, tmp_path)
    brief = render(current)
    assert_traced(brief, current)
    response = execute(current)
    assert response["executed"] is True, response
    assert response["plan_manifest"]
    assert all(step["status"] == "ok" for step in response["steps"])
    assert_closed_form_result(index, tmp_path)
