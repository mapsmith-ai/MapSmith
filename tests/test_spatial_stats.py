"""Four statistics, each with an answer worked out on paper before it ran.

Gi* has a closed form small enough to evaluate by hand; empirical Bayes has an
exact fixed point when every rate is equal; the aggregation and the thinning are
both deterministic greedy walks whose outcome can be traced step by step. Every
expected value below was derived that way and then compared, not read off a run
and pasted back in.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import geopandas as gpd
import pytest
from shapely.geometry import Point, box

from mapsmith.engines import spatial_stats


def _manifest(output: Path) -> dict:
    return json.loads(Path(f"{output}.provenance.json").read_text(encoding="utf-8"))


def _named(output: Path) -> dict:
    return {c["name"]: c for c in _manifest(output)["verification"]}


def _squares(counts, tmp_path: Path, name: str, extra: dict | None = None) -> Path:
    """A west-to-east strip of touching 100 m squares."""
    path = tmp_path / f"{name}.gpkg"
    data = {"n": list(counts)}
    data.update(extra or {})
    gpd.GeoDataFrame(
        data,
        geometry=[box(i * 100, 0, (i + 1) * 100, 100) for i in range(len(counts))],
        crs="EPSG:32632",
    ).to_file(path, layer="a", driver="GPKG")
    return path


# ------------------------------------------------------------------- Gi*

def test_gi_star_matches_the_value_worked_out_by_hand(tmp_path):
    """Four isolated features, values 4, 0, 0, 0.

    mean = 1, sum of squares = 16, variance = 16/4 - 1 = 3, S = sqrt(3).
    With a distance band too small to reach anything, each neighbourhood is the
    feature alone: sum of weights = 1, sum of squared weights = 1, so the
    denominator is S * sqrt((4*1 - 1)/3) = sqrt(3) * 1.
    Feature 0: (4 - 1*1) / sqrt(3) = sqrt(3) = 1.7320508.
    Feature 1: (0 - 1) / sqrt(3) = -0.5773503.
    """
    path = tmp_path / "four.gpkg"
    gpd.GeoDataFrame(
        {"v": [4.0, 0.0, 0.0, 0.0]},
        geometry=[Point(0, 0), Point(1000, 0), Point(2000, 0), Point(3000, 0)],
        crs="EPSG:32632",
    ).to_file(path, layer="p", driver="GPKG")

    out = tmp_path / "gi.parquet"
    spatial_stats.hot_spots(
        str(path), str(out), value_field="v", weights="distance_band",
        distance_band=10.0,
    )
    got = gpd.read_parquet(out)
    assert got["gi_z"].tolist() == pytest.approx(
        [math.sqrt(3), -1 / math.sqrt(3), -1 / math.sqrt(3), -1 / math.sqrt(3)]
    )
    assert got["neighbours"].tolist() == [0, 0, 0, 0]


def test_a_neighbourhood_that_is_the_whole_layer_scores_zero(tmp_path):
    """Not a special case — the formula says so, and it is worth pinning.

    When every feature is a neighbour of every other, the sum of weights is n
    and the sum of squared weights is n, so `n*n - n*n` is zero: there is
    nothing to compare the neighbourhood against. A z of 0 is the answer; a
    division that raises, or a spurious extreme, would not be.
    """
    path = tmp_path / "close.gpkg"
    gpd.GeoDataFrame(
        {"v": [4.0, 0.0, 0.0, 0.0]},
        geometry=[Point(0, 0), Point(1, 0), Point(2, 0), Point(3, 0)],
        crs="EPSG:32632",
    ).to_file(path, layer="p", driver="GPKG")

    out = tmp_path / "all.parquet"
    spatial_stats.hot_spots(
        str(path), str(out), value_field="v", weights="distance_band",
        distance_band=1000.0,
    )
    assert gpd.read_parquet(out)["gi_z"].tolist() == [0.0, 0.0, 0.0, 0.0]


def test_identical_values_produce_no_hot_spots_anywhere(tmp_path):
    path = _squares([5, 5, 5, 5, 5], tmp_path, "flat")
    out = tmp_path / "flat.parquet"
    result = spatial_stats.hot_spots(
        str(path), str(out), value_field="n", weights="contiguity"
    )
    assert result["significant"] == 0
    assert set(gpd.read_parquet(out)["hot_or_cold"]) == {"not significant"}


def test_the_multiple_testing_correction_removes_findings_and_says_how_many(tmp_path):
    """The number that gets published, and the number before the correction.

    A clustered high tail produces several individually significant features; the
    false-discovery-rate step-up keeps fewer. If it ever kept MORE, the step-up
    is implemented backwards and the map would carry more false clusters than no
    correction at all — which is what the check in the manifest is for.
    """
    counts = [1, 1, 1, 1, 1, 1, 1, 1, 40, 40]
    path = _squares(counts, tmp_path, "tail")
    out = tmp_path / "tail.parquet"
    result = spatial_stats.hot_spots(
        str(path), str(out), value_field="n", weights="contiguity"
    )
    assert result["significant"] <= result["significant_before_correction"]
    assert _named(out)["x-mapsmith:the_correction_only_removes_findings"]["passed"]
    manifest = _manifest(out)
    assert manifest["parameters"]["multiple_testing"].startswith("Benjamini-Hochberg")


def test_an_isolated_feature_is_reported_rather_than_scored_in_silence(tmp_path):
    path = tmp_path / "island.gpkg"
    gpd.GeoDataFrame(
        {"n": [1.0, 2.0, 3.0]},
        geometry=[
            box(0, 0, 100, 100),
            box(100, 0, 200, 100),
            box(9000, 9000, 9100, 9100),
        ],
        crs="EPSG:32632",
    ).to_file(path, layer="a", driver="GPKG")

    out = tmp_path / "island.parquet"
    result = spatial_stats.hot_spots(
        str(path), str(out), value_field="n", weights="contiguity"
    )
    assert result["isolated"] == 1
    check = _named(out)["x-mapsmith:every_feature_has_a_neighbour"]
    assert check["passed"] is False and check["critical"] is False
    assert any("no neighbours at all" in note for note in _manifest(out)["notes"])


def test_a_distance_band_in_degrees_is_refused(tmp_path):
    path = tmp_path / "geo.gpkg"
    gpd.GeoDataFrame(
        {"n": [1.0, 2.0, 3.0]},
        geometry=[Point(11, 45), Point(11.01, 45), Point(11.02, 45)],
        crs="EPSG:4326",
    ).to_file(path, layer="p", driver="GPKG")
    with pytest.raises(ValueError, match="DEGREES"):
        spatial_stats.hot_spots(
            str(path), str(tmp_path / "x.parquet"), value_field="n",
            weights="distance_band", distance_band=1000.0,
        )


def test_a_missing_value_is_refused_rather_than_read_as_zero(tmp_path):
    path = _squares([1.0, None, 3.0], tmp_path, "gappy")
    with pytest.raises(ValueError, match="missing value is not a zero"):
        spatial_stats.hot_spots(
            str(path), str(tmp_path / "x.parquet"), value_field="n",
            weights="contiguity",
        )


# --------------------------------------------------------- empirical Bayes

def test_equal_rates_shrink_to_themselves_exactly(tmp_path):
    """The fixed point. Every area has the same rate, so there is nothing for the
    estimator to borrow and the smoothed rate must equal the raw one — to the
    last bit, not approximately."""
    path = _squares([1, 2, 3], tmp_path, "same", {"pop": [100, 200, 300]})
    out = tmp_path / "same.parquet"
    result = spatial_stats.smooth_rates(
        str(path), str(out), count_field="n", population_field="pop"
    )
    got = gpd.read_parquet(out)
    assert got["raw_rate"].tolist() == pytest.approx([1000.0, 1000.0, 1000.0])
    assert got["smoothed_rate"].tolist() == pytest.approx([1000.0, 1000.0, 1000.0])
    assert result["global_rate"] == pytest.approx(1000.0)
    assert result["fully_shrunk"] is True
    assert any("no larger than sampling noise" in n for n in _manifest(out)["notes"])


def test_a_tiny_denominator_is_pulled_toward_the_global_rate_and_a_large_one_is_not(
    tmp_path
):
    """The whole point: one case in a village of 120 is 833 per 100,000 and the
    map of raw rates is a map of which districts are small.

    Village and city both have one case. The village's estimate must move far
    toward the global rate and the city's must barely move, and the shrinkage
    column has to make that visible rather than leaving the reader to guess
    which numbers are evidence.
    """
    path = _squares(
        [1, 1, 50, 60], tmp_path, "denominators",
        {"pop": [120, 500_000, 400_000, 500_000]},
    )
    out = tmp_path / "eb.parquet"
    spatial_stats.smooth_rates(
        str(path), str(out), count_field="n", population_field="pop"
    )
    got = gpd.read_parquet(out)
    village, city = got.iloc[0], got.iloc[1]

    assert village["raw_rate"] == pytest.approx(1 / 120 * 100_000)
    assert village["shrinkage"] < 0.05, (
        "a village of 120 carries almost no information about its own risk and "
        "must be shrunk hard"
    )
    assert city["shrinkage"] > village["shrinkage"] * 10
    assert village["smoothed_rate"] < village["raw_rate"] / 4, (
        "the 833-per-100,000 spike survived the smoothing"
    )
    assert _named(out)[
        "x-mapsmith:every_estimate_lies_between_its_rate_and_the_global_one"
    ]["passed"]


def test_a_zero_population_is_refused_with_somewhere_to_go(tmp_path):
    path = _squares([1, 2], tmp_path, "empty", {"pop": [0, 100]})
    with pytest.raises(ValueError, match="aggregate_to_threshold"):
        spatial_stats.smooth_rates(
            str(path), str(tmp_path / "x.parquet"), count_field="n",
            population_field="pop",
        )


# ----------------------------------------------------- disclosure control

def test_the_greedy_merge_follows_the_rule_step_by_step(tmp_path):
    """Counts [1, 1, 5, 1, 1] with a minimum of 2, traced by hand.

    The smallest under the minimum is index 0 (tie at 1, broken by order); its
    only neighbour is 1, so they merge to 2. Then index 3, whose neighbours are
    2 (five) and 4 (one): the smaller is 4, so they merge to 2. Nothing is left
    under the minimum. Three groups: 2, 5, 2 — and the total is still 9.
    """
    path = _squares([1, 1, 5, 1, 1], tmp_path, "strip")
    out = tmp_path / "merged.parquet"
    result = spatial_stats.aggregate_to_threshold(
        str(path), str(out), count_field="n", minimum=2
    )
    got = gpd.read_parquet(out)
    assert result["groups"] == 3
    assert got["n"].tolist() == pytest.approx([2.0, 5.0, 2.0])
    assert got["source_indices"].tolist() == ["0,1", "2", "3,4"]
    assert got["members"].tolist() == [2, 1, 2]

    checks = _named(out)
    assert checks["x-mapsmith:no_group_is_below_the_minimum"]["passed"]
    assert checks["x-mapsmith:the_total_count_is_unchanged"]["passed"]
    assert checks["x-mapsmith:every_input_area_is_in_exactly_one_group"]["passed"]


def test_the_same_input_merges_the_same_way_every_time(tmp_path):
    """A disclosure decision that changes between runs cannot be defended."""
    path = _squares([1, 1, 1, 1, 1, 1], tmp_path, "ties")
    first, second = tmp_path / "a.parquet", tmp_path / "b.parquet"
    for out in (first, second):
        spatial_stats.aggregate_to_threshold(
            str(path), str(out), count_field="n", minimum=3
        )
    assert (
        gpd.read_parquet(first)["source_indices"].tolist()
        == gpd.read_parquet(second)["source_indices"].tolist()
    )


def test_an_island_below_the_threshold_is_refused_not_published(tmp_path):
    """The failure this operation exists to prevent, in its purest form: one
    area that cannot be merged and would otherwise be emitted below the
    threshold, quietly, among a page of compliant ones."""
    path = tmp_path / "island.gpkg"
    gpd.GeoDataFrame(
        {"n": [5.0, 5.0, 1.0]},
        geometry=[
            box(0, 0, 100, 100),
            box(100, 0, 200, 100),
            box(9000, 9000, 9100, 9100),
        ],
        crs="EPSG:32632",
    ).to_file(path, layer="a", driver="GPKG")

    with pytest.raises(ValueError, match="no neighbour to merge into"):
        spatial_stats.aggregate_to_threshold(
            str(path), str(tmp_path / "x.parquet"), count_field="n", minimum=3
        )


# ---------------------------------------------------------------- thinning

def test_thinning_keeps_the_first_of_each_crowded_group(tmp_path):
    """Points at 0, 10, 20, 30 with a minimum of 15, traced by hand.

    Keep 0. The point at 10 is 10 away: drop. The point at 20 is 20 from the
    last kept: keep. The point at 30 is 10 from that: drop. Two survive.
    """
    path = tmp_path / "row.gpkg"
    gpd.GeoDataFrame(
        {"name": ["a", "b", "c", "d"]},
        geometry=[Point(x, 0) for x in (0, 10, 20, 30)],
        crs="EPSG:32632",
    ).to_file(path, layer="p", driver="GPKG")

    out = tmp_path / "thin.parquet"
    result = spatial_stats.thin_points(str(path), str(out), min_distance=15.0)
    assert gpd.read_parquet(out)["name"].tolist() == ["a", "c"]
    assert result["kept"] == 2 and result["removed"] == 2
    assert _named(out)[
        "x-mapsmith:no_two_kept_points_are_closer_than_the_minimum"
    ]["passed"]


def test_a_priority_field_keeps_the_capital_and_drops_the_hamlets(tmp_path):
    """Without it, thinning a label layer keeps whichever town came first in the
    file — which is how a map ends up naming three hamlets and no city.

    Same four positions, but the point at x=10 has the largest population. It is
    considered first and kept; 0 and 20 are within 15 of it and go; 30 is 20
    away from it and survives.
    """
    path = tmp_path / "towns.gpkg"
    gpd.GeoDataFrame(
        {"name": ["a", "capital", "c", "d"], "pop": [10, 900_000, 20, 30]},
        geometry=[Point(x, 0) for x in (0, 10, 20, 30)],
        crs="EPSG:32632",
    ).to_file(path, layer="p", driver="GPKG")

    out = tmp_path / "labels.parquet"
    spatial_stats.thin_points(
        str(path), str(out), min_distance=15.0, priority_field="pop"
    )
    assert gpd.read_parquet(out)["name"].tolist() == ["capital", "d"]


def test_thinning_says_in_the_manifest_that_it_removed_data(tmp_path):
    path = tmp_path / "many.gpkg"
    gpd.GeoDataFrame(
        {"id": list(range(20))},
        geometry=[Point(x, 0) for x in range(20)],
        crs="EPSG:32632",
    ).to_file(path, layer="p", driver="GPKG")

    out = tmp_path / "few.parquet"
    result = spatial_stats.thin_points(str(path), str(out), min_distance=5.0)
    notes = _manifest(out)["notes"]
    assert any("were removed" in note and "for drawing" in note for note in notes), (
        "a layer that has had points removed must say so, or a total computed "
        "from it is wrong in a way nobody can see"
    )
    assert _manifest(out)["parameters"]["removes_data"] is True
    assert result["kept"] == 4  # 0, 5, 10, 15


def test_a_thinning_distance_in_degrees_is_refused(tmp_path):
    path = tmp_path / "geo.gpkg"
    gpd.GeoDataFrame(
        {"id": [1, 2]}, geometry=[Point(11, 45), Point(11.01, 45)], crs="EPSG:4326"
    ).to_file(path, layer="p", driver="GPKG")
    with pytest.raises(ValueError, match="DEGREES"):
        spatial_stats.thin_points(
            str(path), str(tmp_path / "x.parquet"), min_distance=500.0
        )
