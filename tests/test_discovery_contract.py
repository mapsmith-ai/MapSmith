"""Every operation must be findable. One contract per operation, not one metric.

A catalogue-wide average hides the entry nobody can reach: 90% found@3 over
fifty operations means five are invisible, and the average will not say which.
D-037 made the catalogue the reachability layer — an operation `list_operations`
cannot surface does not exist for an agent — so discoverability is a property
each entry has to hold on its own.

The probe is the entry's own first worked example. That is deliberate and it is
free: every available entry already carries at least two, written as goals
rather than as invocations, and they are what the catalogue itself claims the
operation is for. If an operation cannot be found by the example it advertises,
one of the two is wrong — and the first time this test ran, it was the example.

`centroid_layer` advertised "label points for a polygon layer" and ranked below
`point_on_surface`. The ranking was right: a centroid can fall outside its own
polygon, which is Argleton trap 014, so advertising it for labels was our own
catalogue recommending the defect our suite measures. The example changed, not
the ranking.
"""

from __future__ import annotations

import pytest

from mapsmith import catalog

AVAILABLE = [
    op for op in catalog.OPERATIONS if op["status"] == "available" and op.get("examples")
]


def _facets(op: dict) -> dict:
    """What a caller would declare, from what the entry itself says it takes.

    Not a hint given to the test: every field here is one the calling agent is
    asked for in `list_operations`, and one it knows without being told.
    """
    facets: dict[str, str] = {}
    kinds = [k for k in op["applicability"]["inputs"] if k not in ("dataset", "none")]
    if kinds:
        facets["input_kind"] = kinds[0]
    if op.get("produces"):
        facets["produces"] = op["produces"]
    facets["category"] = op["category"]
    return facets


@pytest.mark.parametrize("op", AVAILABLE, ids=lambda op: op["name"])
def test_each_operation_is_findable_by_the_example_it_advertises(op):
    """With its facets declared, an operation must be in the top 3 for its own goal.

    The facets are the point. Without them this passes for 45 of 49 entries;
    with them, 48 of 49 — and the three it rescues are the ones crowded out by
    near neighbours, which is the failure that grows with the catalogue.
    """
    goal = op["examples"][0]["goal"]
    hits = catalog.search(goal, limit=3, **_facets(op))
    assert hits, f"{op['name']}: its own example goal returns nothing at all"
    if hits[0].get("status") == "unsure":
        pytest.fail(
            f"{op['name']}: the two engines agree on nothing for the goal this entry "
            f"advertises — {goal!r}. Either the goal describes a different operation, "
            "or the entry's own words are too far from it."
        )
    names = [hit["name"] for hit in hits]
    assert op["name"] in names, (
        f"{op['name']} is not in the top 3 for the goal it advertises — {goal!r} "
        f"returned {names}. Before adjusting the wording, check the ranking is not "
        "right: an entry that loses to a neighbour may be advertising the neighbour's "
        "job, which is how the centroid/label-point defect was found."
    )


def test_declaring_the_facets_is_worth_more_than_not_declaring_them():
    """The claim the whole design rests on, kept under measurement.

    If this ever fails, the facets have stopped earning their place and the
    docstring of `list_operations` — which tells the calling model they are the
    most useful thing it can provide — has become a false promise.
    """
    def found(with_facets: bool) -> int:
        total = 0
        for op in AVAILABLE:
            goal = op["examples"][0]["goal"]
            hits = catalog.search(goal, limit=3, **(_facets(op) if with_facets else {}))
            if hits and hits[0].get("status") == "unsure":
                continue
            total += op["name"] in [hit["name"] for hit in hits]
        return total

    bare, faceted = found(False), found(True)
    assert faceted >= bare, (
        f"declaring input kind, output kind and family made discovery WORSE "
        f"({faceted} against {bare} of {len(AVAILABLE)}), which inverts the argument "
        "the catalogue's scaling plan is built on"
    )


def test_the_facets_an_entry_declares_are_ones_the_filter_understands():
    """A facet the filter cannot read narrows nothing and lies in the schema."""
    known_categories = {op["category"] for op in catalog.OPERATIONS}
    for op in catalog.OPERATIONS:
        assert op.get("produces") in catalog.PRODUCES_KINDS, (
            f"{op['name']} declares produces={op.get('produces')!r}, which "
            f"`applicable()` will refuse: it must be one of {sorted(catalog.PRODUCES_KINDS)}"
        )
        assert op["category"] in known_categories


def test_produces_agrees_with_what_the_operation_actually_writes():
    """The declaration is checked against the binding, not trusted.

    `produces` is a filter input, so a wrong one makes an operation unreachable
    for the caller who declares correctly — a silent failure with no error
    anywhere. The registry knows whether a dataset is written and of which kind;
    the two have to say the same thing.
    """
    from mapsmith.plans.registry import BINDINGS

    for op in catalog.OPERATIONS:
        binding = BINDINGS.get(op["name"])
        if binding is None:
            continue  # planned, or reachable only as a dedicated tool
        declared = op["produces"]
        if binding.output_arg is None:
            assert declared in ("answer", "description", "plan_result"), (
                f"{op['name']} writes no dataset but declares produces={declared!r}"
            )
        else:
            assert declared == f"dataset:{binding.output_kind}", (
                f"{op['name']} writes a {binding.output_kind} dataset but declares "
                f"produces={declared!r}"
            )
