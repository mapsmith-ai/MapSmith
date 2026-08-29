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

WHAT THIS FILE ASSERTS CHANGED ON 2026-08-28, and the reason is worth keeping.

It used to require each operation to rank in the top 3 for its own example. That
looks like a discovery contract and is really a ranking contract, with one bad
property: the only way to repair a failure is to reword the example until the
ranker likes it. Fifty entries tuned that way score well on the examples we wrote
and nineteen points worse on requests written by anyone else -- measured, on 155
of them. A test whose repair procedure is "fit the text to the scorer"
manufactures the number it then reports.

So the gate moved to the part that is deterministic and ours: the facets an entry
declares must never drop that entry, and the entry must reach the caller. Rank
INSIDE the delivered set is still measured, because it is useful, but it does not
fail a build -- an ordering is a hint, and hints are not contracts.
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
    facets: dict[str, object] = {}
    kinds = [k for k in op["applicability"]["inputs"] if k not in ("dataset", "none")]
    if kinds:
        facets["input_kind"] = kinds[0]
    if op.get("produces"):
        facets["produces"] = op["produces"]
    facets["category"] = op["category"]
    # How many datasets the caller is holding. Added when the catalogue passed
    # sixty operations and the other facets stopped being enough to hand the set
    # over: it is a fact about the caller's situation, like the two above it, and
    # not a guess about our taxonomy like the family.
    facets["dataset_inputs"] = op["applicability"]["dataset_inputs"]
    return facets


@pytest.mark.parametrize("op", AVAILABLE, ids=lambda op: op["name"])
def test_the_facets_an_entry_declares_never_drop_that_entry(op):
    """The one hard contract, because it is the one with no model in it.

    The filter is built from the entry's OWN declarations. If it then removes the
    entry, a caller who describes their data correctly can never reach the
    operation — no error, no empty result, just an operation that has quietly
    stopped existing.
    """
    survivors = {o["name"] for o in catalog.applicable(**_facets(op))}
    assert op["name"] in survivors, (
        f"{op['name']} is filtered out by facets taken from its own entry "
        f"({_facets(op)}). Whatever it declares does not match what `applicable` "
        "reads, so nobody who declares correctly can find it."
    )


@pytest.mark.parametrize("op", AVAILABLE, ids=lambda op: op["name"])
def test_each_operation_reaches_the_caller_for_the_goal_it_advertises(op):
    """It has to be IN the answer. Where in the answer is the caller's business.

    Under `status: "choose"` the whole surviving set is delivered, so this is a
    weaker claim than the top-3 gate it replaced — deliberately. The strong claim
    was measuring our ranker against text we had tuned for our ranker.
    """
    goal = op["examples"][0]["goal"]
    answer = catalog.search(goal, limit=3, **_facets(op))
    assert answer, f"{op['name']}: its own example goal returns nothing at all"
    if answer[0].get("status") == "unsure":
        pytest.fail(
            f"{op['name']}: the two engines agree on nothing for the goal this entry "
            f"advertises — {goal!r}, and the set was too large to hand over. Either "
            "the goal describes a different operation, or the entry's own words are "
            "too far from it."
        )
    delivered = [hit["name"] for hit in catalog.entries(answer)]
    assert op["name"] in delivered, (
        f"{op['name']} does not reach the caller for the goal it advertises — "
        f"{goal!r} delivered {delivered}."
    )


def test_declaring_the_facets_is_worth_more_than_not_declaring_them():
    """The claim the whole design rests on, kept under measurement.

    Measured on the entries' own examples, so the absolute numbers flatter us and
    are published nowhere; what the comparison is good for is the SIGN. The
    published figures (24% bare, 51% faceted) come from
    `tests/data/discovery_queries.json`, written by two other model families that
    never saw this catalogue.

    If this ever fails, the facets have stopped earning their place and the
    docstring of `list_operations` — which tells the calling model they are the
    most useful thing it can provide — has become a false promise.
    """
    def found(with_facets: bool) -> int:
        total = 0
        for op in AVAILABLE:
            goal = op["examples"][0]["goal"]
            answer = catalog.search(goal, limit=3, **(_facets(op) if with_facets else {}))
            if answer and answer[0].get("status") == "unsure":
                continue
            total += op["name"] in [h["name"] for h in catalog.entries(answer)[:3]]
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


def test_the_delivered_set_is_a_choice_and_says_so():
    """The response shape a caller has to handle, pinned.

    Three things separate handing over a set from dumping one: the answer says it
    is a choice, nothing is truncated, and every candidate carries what separates
    it from its neighbours where the entry has that text.
    """
    answer = catalog.search(
        "the coastline has far too many points",
        limit=3, input_kind="vector", produces="dataset:vector", category="vector",
        dataset_inputs=1,
    )
    assert len(answer) == 1 and answer[0]["status"] == "choose"
    candidates = answer[0]["candidates"]
    # Compared WITHOUT category, because search does not filter on it: the family
    # is an ordering there, so every operation that takes a vector layer and
    # returns one is still in the set.
    survivors = catalog.applicable(
        input_kind="vector", produces="dataset:vector", dataset_inputs=1
    )
    assert {c["name"] for c in candidates} == {o["name"] for o in survivors}, (
        "the choice was truncated or padded: `limit` governs a ranking, and there "
        "is no ranking here to truncate"
    )
    with_text = [c for c in candidates if c.get("distinguishes")]
    assert len(with_text) >= len(candidates) // 2, (
        "most candidates arrive without the field written to be read against the "
        "others, which is the field the choice is made on"
    )


def test_a_query_far_from_the_catalog_warns_even_when_it_is_a_choice(vector_engine):
    """The disagreement signal survived the change from refusal to note.

    Below the threshold the search no longer refuses — it is not deciding, so it
    has nothing to refuse — but the evidence that the request is out of place
    must not be discarded. It moves to `order_is_weak`.
    """
    answer = catalog.search(
        "book me a flight to Lisbon on Tuesday",
        input_kind="vector", produces="dataset:vector", category="vector",
        dataset_inputs=1,
    )
    assert answer[0]["status"] == "choose"
    assert "order_is_weak" in answer[0], (
        "an obviously out-of-domain request produced a confidently ordered set "
        "with no warning attached"
    )


def test_the_declared_family_orders_the_set_and_does_not_cut_it():
    """The facet a caller has to GUESS must never delete the answer.

    `input_kind` and `projected` are facts about the data in hand and `produces`
    is what the caller wants back; all three are safe to filter on. `category` is
    a guess about our taxonomy, and on the independent query set every request
    has 4.4 plausible families — so a hard filter on it removes the right
    operation most of the time it is wrong, with no error and a confident answer
    made of neighbours. That is the silent-failure class Argleton measures in
    other people's systems.

    So it sorts. Declaring the family puts it first; declaring the WRONG family
    costs positions and nothing else.
    """
    facts = {"input_kind": "vector", "produces": "dataset:vector", "dataset_inputs": 2}
    query = "which parcels fall inside the flood zone"

    # BM25 pinned: the ordering under test is `category` lifting its own
    # members, and comparing two orderings is only meaningful against a ranker
    # that is the same on every machine. The default engine is the embedding one
    # where the model loads and BM25 where it does not, which made this test a
    # test of what was downloadable.
    facts = {**facts, "engine": "lexical"}
    plain = catalog.entries(catalog.search(query, **facts))
    # `terrain` is one operation in this set of nine, which is what makes it a
    # good probe: if declaring it does not lift that one entry to the front,
    # the ordering is doing nothing.
    guided = catalog.entries(catalog.search(query, category="terrain", **facts))
    assert {o["name"] for o in plain} == {o["name"] for o in guided}, (
        "declaring a family changed WHICH operations came back; it is only "
        "allowed to change the order"
    )

    how_many = sum(1 for o in guided if o["category"] == "terrain")
    leading = [o["category"] for o in guided[:how_many]]
    assert leading and set(leading) == {"terrain"}, (
        "the declared family did not come first, so declaring it bought nothing"
    )

    # And the wrong guess: the operation this query really wants is still there.
    wrong = catalog.entries(catalog.search(query, category="network", **facts))
    assert "clip_layer" in {o["name"] for o in wrong}, (
        "a wrong family guess removed an operation, which is the failure this "
        "design exists to avoid"
    )
