"""The numbers on the README are recomputed here, not remembered.

Two of them were wrong for a day and one for half a day, in both cases because a
measurement changed and a page did not. The pages also say the numbers can be
checked rather than believed, which was not quite true while the request set was
published and the harness that turns it into percentages was not.

So `benchmarks/discovery_report.py` recomputes everything from files in this
repository, and this test asserts the README carries what it produces. Editing
one without the other fails here.

Marked slow: it runs a real search per request per facet level, which loads the
embedding model.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
sys.path.insert(0, str(ROOT / "benchmarks"))


@pytest.fixture(scope="module")
def report():
    import discovery_report

    queries = discovery_report.load()
    answerable = discovery_report.answerable(queries)
    return {
        "agreement": discovery_report.agreement(queries),
        "ablation": discovery_report.ablation(answerable),
        "n": len(answerable),
    }


@pytest.mark.slow
def test_the_readme_ablation_table_is_what_the_harness_computes(report):
    """Every row of the facet table, against the arithmetic that produces it."""
    prose = README.read_text(encoding="utf-8")
    rows = re.findall(
        r"^\|[^|]+\|\s*\**(\d+)\**\s*\|\s*\**(\d+)%\**\s*\|\s*\**(\d+)%\**\s*\|$",
        prose,
        re.MULTILINE,
    )
    computed = [
        (str(r["candidates"]), str(r["found_at_3"]), str(r["delivered"]))
        for r in report["ablation"]
    ]
    assert rows, (
        "the facet table is no longer in the README in a shape this can read, so "
        "nothing is checking the numbers it publishes"
    )
    for row in rows:
        assert row in computed, (
            f"the README publishes candidates={row[0]}, found@3={row[1]}%, "
            f"delivered={row[2]}% — the harness computes {computed}. Run "
            "`python benchmarks/discovery_report.py` and put back what it says."
        )


@pytest.mark.slow
def test_the_ceiling_is_quoted_over_the_same_population_as_everything_else(report):
    """Two numbers in one table must be over one denominator.

    Agreement over ALL requests counts the pairs where both labellers said the
    request is unanswerable — true, and the easy half. Quoted next to found@3,
    which is computed over the answerable ones only, it silently compares two
    populations. It did, for half a day: 68% of 155 sitting above 48% of 118.
    """
    prose = README.read_text(encoding="utf-8")
    same_all, n_all = report["agreement"]["all"]
    same_ans, n_ans = report["agreement"]["answerable"]
    over_all = round(100 * same_all / n_all)
    over_answerable = round(100 * same_ans / n_ans)

    assert f"{over_answerable}%" in prose, (
        f"the README does not quote {over_answerable}%, the agreement between the two "
        f"labellers over the same {n_ans} requests every other figure uses"
    )
    if over_all != over_answerable and f"{over_all}%" in prose:
        assert str(n_all) in prose, (
            f"the README quotes {over_all}%, which is agreement over all {n_all} "
            f"requests including the ones both labellers called unanswerable, without "
            f"saying so. Either quote {over_answerable}% (same population as the rest) "
            f"or name the {n_all}."
        )


@pytest.mark.slow
def test_the_population_is_named_wherever_a_percentage_is(report):
    """The count of requests has to be on the page carrying the percentages."""
    prose = README.read_text(encoding="utf-8")
    assert str(report["n"]) in prose, (
        f"the README no longer says how many requests these percentages are over "
        f"({report['n']}), which turns a measurement into a claim"
    )
