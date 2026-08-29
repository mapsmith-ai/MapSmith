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
def report(vector_engine):
    """Needs both rankers, because the table publishes both.

    Skips rather than fails where the embedding model cannot be had: the page
    carries a column for each engine precisely so that neither the numbers nor
    this check depend on what a machine could download, and a build that goes
    red on someone else's rate limit teaches people to ignore red builds."""

    import discovery_report

    queries = discovery_report.load()
    answerable = discovery_report.answerable(queries)
    return {
        "agreement": discovery_report.agreement(queries),
        "ablation": discovery_report.ablation(answerable, engine="lexical"),
        "ablation_vector": discovery_report.ablation(answerable, engine="vector"),
        "n": len(answerable),
    }


@pytest.mark.slow
def test_the_readme_ablation_table_is_what_the_harness_computes(report):
    """Every row of the facet table, against the arithmetic that produces it."""
    prose = README.read_text(encoding="utf-8")
    # Five cells now: candidates, BM25, embeddings, delivered. The engine is
    # named in the table because the default picks between the two by whether a
    # model loads, and a single column made the published figures a measurement
    # of the machine — a CI run that met a rate limit recomputed the first row
    # as 28% where the page said 18%.
    rows = re.findall(
        r"^\|[^|]+\|\s*\**(\d+)\**\s*\|\s*\**(\d+)%\**\s*\|"
        r"\s*\**(\d+)%\**\s*\|\s*\**(\d+)%\**\s*\|$",
        prose,
        re.MULTILINE,
    )
    computed = [
        (
            str(lex["candidates"]),
            str(lex["found_at_3"]),
            str(vec["found_at_3"]),
            str(lex["delivered"]),
        )
        for lex, vec in zip(report["ablation"], report["ablation_vector"], strict=True)
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


def test_the_prose_around_the_table_is_checked_too():
    """The sentences, not only the numbers in the grid.

    Two claims went stale unnoticed and were found on 2026-08-29 while adding
    two operations, not by anybody checking: the page said the surviving set
    "has a median of 26 and never exceeded it" when the median was 14 and three
    of the 118 requests exceeded 30, and it called the curve a *median* while
    publishing the *mean*. Both were false before the catalogue reached 72, and
    the reason nobody noticed is that this file read the table and stopped.

    A sentence asserting a property that does not hold is worse than no
    sentence, because people stop thinking about it. So the prose is parsed for
    the numbers it states and each one is compared with the harness.
    """
    import re
    import statistics

    from mapsmith import catalog

    prose = README.read_text(encoding="utf-8")
    import discovery_report

    rows = discovery_report.answerable(discovery_report.load())
    full = discovery_report.LEVELS[3][1]
    sizes = [
        len(catalog.applicable(**discovery_report.facets_for(r["label_claude"], full)))
        for r in rows
    ]
    over = sum(1 for size in sizes if size > catalog.CHOOSABLE)

    match = re.search(
        r"its median is (\d+) and it exceeds 30\s*\n?\s*for (\w+) of them", prose
    )
    assert match, (
        "the sentence stating the median surviving set and how often it exceeds "
        "the threshold has been reworded; update this test with it rather than "
        "deleting it — it exists because that sentence was false for two "
        "catalogue sizes running"
    )
    words = {"none": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
    assert int(match.group(1)) == statistics.median(sizes)
    assert words[match.group(2)] == over

    # The curve: every point must be the average the harness computes at that
    # catalogue size. Only the last one can be recomputed here — the earlier
    # ones are history — but the last one is the one that goes stale.
    curve = re.search(r"9 at 51 operations, 14 at 61, 16 at 72, (\d+) at (\d+)", prose)
    assert curve, "the scaling curve sentence has been reworded; update this test"
    assert int(curve.group(2)) == len(catalog.OPERATIONS)
    assert int(curve.group(1)) == round(sum(sizes) / len(sizes))


def test_the_site_carries_the_same_facet_numbers_as_the_harness():
    """The site is a showcase of its own, and its numbers are hand-written.

    On 2026-08-29 the page said the facets take the candidates "from 51 to 21"
    while its own table two screens down said 74 to 31; claimed the right
    operation comes back "on all 118 requests" where the README spends a
    paragraph explaining why it is 115; and quoted a median of 14 inside a
    sentence about a measurement made at 61 operations, where the number was 9.
    Three contradictions on one page, none of them reachable by the check that
    reads the README.
    """
    import re

    from mapsmith import catalog

    template = ROOT / "site" / "index.template.html"
    prose = template.read_text(encoding="utf-8")

    import discovery_report

    rows = discovery_report.answerable(discovery_report.load())
    facets = discovery_report.LEVELS[2][1]  # input kind + produces
    narrowed = [
        len(catalog.applicable(**discovery_report.facets_for(r["label_claude"], facets)))
        for r in rows
    ]
    full = discovery_report.LEVELS[3][1]
    surviving = [
        len(catalog.applicable(**discovery_report.facets_for(r["label_claude"], full)))
        for r in rows
    ]
    delivered = sum(1 for size in surviving if size <= catalog.CHOOSABLE)

    match = re.search(r"takes the candidates from (\d+) to (\d+)", prose)
    assert match, "the narrowing sentence has been reworded; update this test with it"
    assert int(match.group(1)) == len(catalog.OPERATIONS)
    assert int(match.group(2)) == round(sum(narrowed) / len(narrowed))

    match = re.search(r"On (\d+) of those (\d+) requests the right", prose)
    assert match, "the delivery sentence has been reworded; update this test with it"
    assert int(match.group(1)) == delivered
    assert int(match.group(2)) == len(rows)
