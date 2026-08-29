"""The page a person answers questions on has to work with the network off.

It is generated from a checkout, opened from a file path, and the answers it
collects are the only ones in this project that do not come from a language
model. Three properties keep it usable: it fetches nothing, it cannot be broken
by the text of a query, and it carries the same figures the report computes.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "benchmarks"))


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    """One real build, log included. Around five seconds, so built once."""
    import discovery_dashboard

    log = tmp_path_factory.mktemp("log") / "discovery.jsonl"
    log.write_text(
        "\n".join(
            json.dumps(record)
            for record in [
                {
                    "at": "2026-08-29T06:39:04+00:00",
                    "query": "one label point per parcel </script><script>stolen()</script>",
                    "declared": {"input_kind": "vector", "dataset_inputs": 1},
                    "engine": "vector",
                    "status": "choose",
                    "delivered": ["point_on_surface", "centroid_layer"],
                    "chose": "centroid_layer",
                    "position_of_choice": 2,
                    "searches_ago": 0,
                    "matched_by": "most recent search whose delivered set held this operation",
                },
                {
                    "at": "2026-08-29T06:40:00+00:00",
                    "query": "send this to my accountant",
                    "declared": {},
                    "engine": "vector",
                    "status": "unsure",
                    "delivered": [],
                    "chose": None,
                    "position_of_choice": None,
                    "searches_ago": None,
                    "matched_by": None,
                },
            ]
        ),
        encoding="utf-8",
    )
    return discovery_dashboard.build(log)


def embedded(page: str) -> dict:
    """The data blob the page draws itself from."""
    match = re.search(
        r'<script type="application/json" id="data">(.*?)</script>', page, re.DOTALL
    )
    assert match, "the page no longer carries its data as JSON"
    return json.loads(match.group(1))


def test_the_page_fetches_nothing(page):
    """No CDN, no font host, no analytics — it has to work on a plane.

    Also the reason it is one file: a page whose stylesheet lives next to it is
    a page that arrives broken when somebody sends only the page.
    """
    for attribute in ("src", "href"):
        for value in re.findall(rf'{attribute}="([^"]+)"', page):
            assert not value.startswith(("http://", "https://", "//")), (
                f"the page loads {value} from the network, so it stops working offline "
                "and tells a third party when it is opened"
            )
    assert "@import" not in page


def test_a_query_cannot_break_out_of_the_data_block(page):
    """Queries are text somebody else wrote. Here that text becomes a page.

    The recorded query in the fixture contains a literal `</script>`, which
    without escaping would close the JSON block early and run what follows. The
    check is that the page still parses as JSON and the tag is not there raw.
    """
    data = embedded(page)
    assert any("stolen()" in case["query"] for case in data["recorded"]), (
        "the fixture query is gone, so this test is no longer testing anything"
    )
    block = re.search(
        r'<script type="application/json" id="data">(.*?)</script>', page, re.DOTALL
    ).group(1)
    assert "</script>" not in block
    assert "\\u003c/script>" in block or "\\u003c/script\\u003e" in block


def test_the_recorded_cases_arrive_with_what_a_reader_needs(page):
    data = embedded(page)
    assert len(data["recorded"]) == 2, "a search nothing followed must be kept, not dropped"
    ran = next(case for case in data["recorded"] if case["chose"])
    assert ran["delivered"] == ["point_on_surface", "centroid_layer"]
    assert ran["position"] == 2, (
        "without the position, a reader cannot see that the ranking was overruled, "
        "which is the one thing a recorded case says that a generated one cannot"
    )


def test_the_split_rows_are_the_ones_the_labellers_disagreed_on(page):
    """The panel's whole job: the requests where the ground truth is a guess."""
    data = embedded(page)
    split = data["disagreements"]
    assert split, "no disagreements, so the page has nothing for a person to settle"
    for row in split:
        assert row["claude"] != row["gemini"]
        assert row["claude"] and row["gemini"]
    # Agreement plus disagreement is every row where both labellers answered.
    same, both = data["measurements"]["agreement_all"]
    assert len(split) == both - same, (
        f"{len(split)} split rows against {both - same} the report counts as "
        "disagreements — the page and the report are looking at different sets"
    )


def test_the_figures_on_the_page_are_the_ones_the_report_computes(page):
    """Two places publishing the same number is two places to be wrong in."""
    import discovery_report

    data = embedded(page)
    queries = discovery_report.load()
    assert data["measurements"]["agreement_all"] == list(
        discovery_report.agreement(queries)["all"]
    )
    assert data["measurements"]["answerable"] == len(discovery_report.answerable(queries))
    lexical = discovery_report.ablation(
        discovery_report.answerable(queries), engine="lexical"
    )
    assert data["measurements"]["ablation_lexical"] == lexical


def test_the_page_says_the_answers_stay_on_the_machine(page):
    """A promise that has to be on the page, not only in the docstring.

    Somebody is about to type judgements about their own work into it, and the
    file holds their queries verbatim.
    """
    assert "nothing is uploaded" in page.lower()
    assert "localStorage" in page


def test_every_operation_is_reachable_in_the_picker(page):
    """A chip list is a shortcut; the answer might be none of the shortcuts.

    If the full catalogue is not in the page, an honest answer that happens to
    sit outside the ranked ten cannot be given at all — which would bias the
    labels toward what the ranker already believes.
    """
    from mapsmith import catalog

    data = embedded(page)
    assert {op["name"] for op in data["catalog"]} == {
        op["name"] for op in catalog.OPERATIONS
    }
    assert all(op["summary"] for op in data["catalog"]), (
        "an operation without its summary is a name in a dropdown, which is not "
        "enough to choose by"
    )
