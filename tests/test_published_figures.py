"""Every figure this project publishes, checked against what produces it.

Seven published numbers turned out to be false on 2026-08-29, and every one of
them was found by accident while updating something else. The reason they
survived is not that nobody checked: `test_showcase.py` checks that the same
strings appear on both surfaces, and `test_discovery_report.py` checks the
ablation table and the sentences around it. What nobody checked was **whether a
figure stated in one more place was the current one**.

So the shape here is different from both. A figure is declared once, with the
code that recomputes it and the patterns that match wherever a page *states the
claim*. Every surface is then swept for every pattern, and each match must equal
the recomputed value. Adding a fifth place to say "our ranking gets 48%" cannot
go stale quietly, because the sweep does not care which file it is in.

Two rules make it work, and both were learned from checks that passed while the
thing they guarded was wrong:

* **Match the claim, not the number.** A pattern anchored on the sentence
  ("puts the answer in the top three") finds the figure wherever it is said. A
  check that only asks whether "53%" appears somewhere on the page passes while
  the headline table says 48% two screens away — which is exactly what happened.
* **A pattern that matches nothing is a failure.** Prose gets rewritten, and a
  pattern that quietly stops matching is a guard that has been switched off
  without anybody deciding to.

What this does NOT cover, deliberately: figures that are dated measurements.
"On 26 August the run said X" does not go stale — it says what it is. The rule
for a page is that a number is either recomputed here or attached to a date.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ARGLETON = ROOT.parent / "argleton"
sys.path.insert(0, str(ROOT / "benchmarks"))

#: Every page a reader can reach. Adding one is a deliberate act: a surface
#: nobody sweeps is where the next stale figure will live.
SURFACES = {
    "README.md": ROOT / "README.md",
    "site": ROOT / "site" / "index.template.html",
    "docs/benchmarks.md": ROOT / "docs" / "benchmarks.md",
    "docs/catalog-entry-spec.md": ROOT / "docs" / "catalog-entry-spec.md",
    # A page of claims that no sweep touched until 2026-09-01, and the one
    # read by people deciding whether to give the project money: it still
    # said "16 semantic tools, 336 closed-form tests" and still asked for
    # EUR 35000 to build two artifacts that had shipped, one of them with a DOI.
    "funding.json": ROOT / "funding.json",
    "argleton/README.md": ARGLETON / "README.md",
    "argleton/site": ARGLETON / "site" / "index.template.html",
}


@dataclass(frozen=True)
class Figure:
    """One published claim, its true value, and where pages say it."""

    what: str
    patterns: tuple[str, ...]
    #: Set when the claim is stated in words rather than digits ("seventy-four").
    spelled: tuple[str, ...] = field(default=())


#: Spelled-out forms, because a page writes "sixteen" where a table writes 16.
#: Filled in as values appear rather than exhaustively — and that has a cost
#: worth stating: a value with no entry here cannot be recognised in its written
#: form, so the check reports the page as stale and names the word it found. That
#: is the safe direction (it complains rather than passing), and it happened on
#: 2026-09-01 when a figure moved from seventeen to sixteen.
NUMBER_WORDS = {
    0: "zero", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
    7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven", 12: "twelve",
    13: "thirteen", 14: "fourteen", 15: "fifteen", 16: "sixteen",
    17: "seventeen", 18: "eighteen", 19: "nineteen", 20: "twenty",
    27: "twenty-seven", 28: "twenty-eight", 29: "twenty-nine", 30: "thirty",
    31: "thirty-one", 49: "forty-nine", 72: "seventy-two", 74: "seventy-four",
}


def _catalogue():
    from mapsmith import catalog

    operations = catalog.OPERATIONS
    return {
        "total": len(operations),
        "available": sum(1 for o in operations if o["status"] == "available"),
        "toolless": sum(1 for o in operations if o.get("tool") is None),
    }


@pytest.fixture(scope="module")
def measured():
    """Everything recomputed once, from the repository."""
    import discovery_report

    from mapsmith import catalog

    rows = discovery_report.answerable(discovery_report.load())
    lexical = discovery_report.ablation(rows, engine="lexical")
    full = discovery_report.LEVELS[3][1]
    # Through `accepted_of`, which is what the harness measures against. Reading
    # `label_claude` here was a second implementation of the same number, and on
    # 2026-09-01 the two disagreed: this file computed 115 while
    # `test_discovery_report` computed 116, each right by its own arithmetic,
    # both checking a page against a number the page's own generator does not
    # produce. One number, one computation.
    sizes = [
        len(
            catalog.applicable(
                **discovery_report.facets_for(discovery_report.accepted_of(r)[0], full)
            )
        )
        for r in rows
    ]
    counts = _catalogue()
    agreed, of_those = discovery_report.agreement(discovery_report.load())["answerable"]
    return {
        **counts,
        "answerable": len(rows),
        "all_requests": len(discovery_report.load()),
        "ranker_top3": lexical[3]["found_at_3"],
        "delivered_pct": lexical[3]["delivered"],
        "delivered_count": sum(1 for s in sizes if s <= catalog.CHOOSABLE),
        "candidates_bare": lexical[0]["candidates"],
        "candidates_two_facets": lexical[2]["candidates"],
        "candidates_full": lexical[3]["candidates"],
        # What `category` would remove if it filtered rather than ordered:
        # the gap between the two last rows. D-054 measured it and refused
        # the filter; the number it refused on has to stay current.
        "category_removes": lexical[3]["candidates"],
        # Over the ANSWERABLE rows, not all 155: agreement over everything
        # counts the pairs where both labellers said "no operation fits",
        # which is true and easy, and quoting it beside figures measured on
        # the answerable set compares two populations.
        "agreement": round(100 * agreed / of_those),
    }


#: The registry. `{n}` in a pattern is replaced by a capture group; whatever it
#: captures has to equal the value.
FIGURES: dict[str, Figure] = {
    "total": Figure(
        "operations in the catalogue",
        patterns=(
            r"can perform — {n} today",
            r"nothing — words alone \| {n} \|",
            r"nothing declared\s*\n?\s*→ {n} candidates",
            r"holds at {w} operations, not at eight hundred",
            # The worked example's own framing, which sat at fifty-one for three
            # catalogue sizes: the site takes it from a placeholder, the README
            # typed it by hand, and only one of the two aged.
            r"operations picked out of {n}\b",
            r"goes from {n} operations to a handful",
            r"a catalogue of {n} operations",
        ),
    ),
    "toolless": Figure(
        "operations with no tool of their own",
        patterns=(r"and {n} of them have no tool of their own",),
    ),
    "ranker_top3": Figure(
        "how often the ranker puts the answer in the top three",
        patterns=(
            r"puts the answer in the top three \| \*\*{n}%\*\*",
            r"ranker puts the answer in the top three \| {n}%",
            r"<b>{n}%</b><span>OUR RANKING",
            r"<strong>\d+, {n}% ranked and \d+% delivered</strong>",
            r"\*\*{n}%\*\* \| \*\*\d+%\*\* \| \*\*\d+%\*\* \|",
            # The same claim as a comparison, which is where it hid: the entry
            # spec quoted 48% against the model's 69% for four catalogue sizes,
            # and the headline table two hundred lines above said 48% too.
            r"against {n}% for the ranker putting it in the",
            r"beside a {n}% computed over the",
        ),
    ),
    "delivered_pct": Figure(
        "how often the surviving set is small enough to hand over whole",
        patterns=(
            r"\*\*\d+%\*\* \| \*\*\d+%\*\* \| \*\*{n}%\*\* \|",
            r"how many datasets \| \*\*\d+\*\* \| \*\*\d+%\*\* \| \*\*{n}%\*\*",
            r"{n}% ranked and \d+% delivered".replace("{n}%", r"\d+%").replace(
                r"\d+% delivered", "{n}% delivered"
            ),
            r"delivery to {n}%",
            # Added 2026-09-01: this phrasing sat two screens from the table and
            # went stale with it, unseen. A figure restated in prose needs its
            # own pattern — the table's does not cover it.
            r"which is the {n}% in the table above",
        ),
    ),
    "answerable": Figure(
        "requests the retrieval figures are measured on",
        patterns=(
            r"over (?:those|the same) {n} requests",
            r"measured on {n} requests written by",
            r"All three (?:are )?over the same {n} requests",
        ),
    ),
    "delivered_count": Figure(
        "requests whose surviving set could be handed over whole",
        patterns=(r"On {n} of those \d+ requests the right",),
    ),
    "agreement": Figure(
        "where the two model labellers agree with each other",
        patterns=(
            r"agree \*\*with each other\*\* \| \*\*{n}%\*\*",
            r"agreeing \*\*with each other\*\* \| \*\*{n}%\*\*",
            r"<b>{n}%</b><span>WHERE THE TWO MODEL LABELLERS",
        ),
    ),
    "candidates_two_facets": Figure(
        "candidates left after the two facets a caller always knows",
        patterns=(
            r"takes the candidates from \d+ to {n}",
            r"what I want back \| {n} \|",
            r"what it should produce \| {n} \|",
        ),
    ),
    "category_removes": Figure(
        "candidates the family facet removes when used as a filter",
        patterns=(
            r"removes six candidates out of\s*{w}",
            r"removed six candidates out of\s*{w}",
        ),
    ),
    "candidates_full": Figure(
        "candidates left once every facet the caller knows is declared",
        patterns=(
            r"how many datasets I have\*\* \| \*\*{n}\*\*",
            r"how many datasets \| \*\*{n}\*\*",
            r"<strong>{n}, \d+% ranked",
            r"how many datasets I have →\s*\n?\s*<strong>{n},",
        ),
    ),
}


def _expand(pattern: str, value: int) -> str:
    return pattern.replace("{n}", f"({value}|\\d+)").replace(
        "{w}", f"({NUMBER_WORDS.get(value, '@@')}|[a-z-]+)"
    )


@pytest.mark.parametrize("key", sorted(FIGURES))
def test_every_surface_states_the_current_value(key, measured):
    """One figure, every page, every place it is said."""
    figure = FIGURES[key]
    value = measured[key]
    wrong, seen = [], 0

    for name, path in SURFACES.items():
        if not path.exists():  # pragma: no cover — argleton is a sibling checkout
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in figure.patterns:
            for match in re.finditer(_expand(pattern, value), text):
                seen += 1
                stated = match.group(1)
                # A page may spell the number ("seventeen") or print it ("17"),
                # and both are the same claim. The pattern says which it is.
                expected = (
                    NUMBER_WORDS.get(value, str(value))
                    if "{w}" in pattern
                    else str(value)
                )
                if stated != expected:
                    line = text[: match.start()].count("\n") + 1
                    wrong.append(f"{name}:{line} says {stated}, it is {expected}")

    assert seen, (
        f"no page states «{figure.what}» in a shape this recognises. Either the "
        "claim was removed from every surface, or it was reworded and this guard "
        "silently stopped guarding — update the patterns rather than deleting them."
    )
    assert not wrong, (
        f"«{figure.what}» is {value} and these pages say otherwise:\n  "
        + "\n  ".join(wrong)
    )


def test_the_sweep_covers_every_public_surface():
    """A page nobody sweeps is where the next stale figure lives.

    Both showcases of both projects, plus the two documents that are presented
    as normative — `docs/catalog-entry-spec.md` is where a stale table hurt
    most on 2026-08-29, because it is the page written for somebody else to
    implement.
    """
    missing = [name for name, path in SURFACES.items() if not path.exists()]
    assert missing in ([], ["argleton/README.md", "argleton/site"]), (
        f"surfaces this sweep expects are gone: {missing}"
    )
    assert (ROOT / "README.md") in SURFACES.values()
    assert (ROOT / "site" / "index.template.html") in SURFACES.values()
    assert (ROOT / "docs" / "catalog-entry-spec.md") in SURFACES.values()


def test_the_funding_manifest_dates_its_claims_and_does_not_overstate_them():
    """`funding.json` is read by people deciding whether to fund the work.

    It went unswept until 2026-09-01, and by then it said "16 semantic tools,
    336 closed-form tests" — 16 and 336 against 28 and more than 1500 — and
    carried two *active* plans totalling EUR 35000 to build a trap suite and a
    provenance specification. Both had shipped, both are archived with a DOI,
    and both were built with no funding. A funder opening that file read a
    request for money for delivered work, on a project whose entire subject is
    that a claim should be checkable.

    Two rules, matching the ones at the top of this file: the volatile figures
    are attached to a date, and the one that cannot be swept for equality — the
    test count, which changes with every commit — is stated as a floor and
    checked as a floor.
    """
    import json
    import re

    text = (ROOT / "funding.json").read_text(encoding="utf-8")
    described = json.loads(text)["entity"]["description"]

    assert re.search(r"\b20\d\d-\d\d-\d\d\b", described), (
        "the entity description carries figures that go stale and no date. "
        "Either recompute them here or attach the date they were true on."
    )

    claimed = re.search(r"more than (\d[\d,]*) closed-form tests", described)
    assert claimed, (
        "the test-count claim was reworded; this guard stopped guarding. Update "
        "the pattern rather than dropping it."
    )
    floor = int(claimed.group(1).replace(",", ""))
    collected = sum(1 for _ in (ROOT / "tests").rglob("test_*.py"))
    assert collected, "no test files found, so the floor cannot be checked"
    import subprocess

    run = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", str(ROOT / "tests")],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,  # collection alone; a non-zero exit is read from the output
    )
    total = re.search(r"(\d+) tests collected", run.stdout)
    assert total, f"could not count the suite: {run.stdout[-400:]}"
    assert int(total.group(1)) >= floor, (
        f"funding.json claims more than {floor} tests and the suite collects "
        f"{total.group(1)}. A floor that is no longer true is a false claim, not "
        "a conservative one."
    )


def test_no_funding_plan_asks_for_work_that_is_already_delivered():
    """A plan is `active` or it is not, and the file has to mean it.

    The schema allows exactly two statuses, so "we already did this" has to be
    said in the status and in the prose, not left as an active ask that ages
    into a lie. Both delivered plans are kept rather than deleted, because what
    was estimated against what it actually cost is the strongest thing this
    project can show a funder — but they are kept *inactive*.
    """
    import json

    plans = json.loads((ROOT / "funding.json").read_text(encoding="utf-8"))["funding"][
        "plans"
    ]
    assert {p["status"] for p in plans} <= {"active", "inactive"}, (
        "the schema allows only active and inactive"
    )
    for plan in plans:
        delivered = "DELIVERED" in plan["description"]
        assert delivered == (plan["status"] == "inactive" and plan["amount"] > 0), (
            f"plan {plan['guid']!r} says status={plan['status']!r} and its "
            "description does not agree about whether the work is done"
        )
