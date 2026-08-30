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


NUMBER_WORDS = {
    0: "zero", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
    7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven", 12: "twelve",
    17: "seventeen", 27: "twenty-seven", 28: "twenty-eight", 29: "twenty-nine",
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
    sizes = [
        len(catalog.applicable(**discovery_report.facets_for(r["label_claude"], full)))
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
