# How to describe an operation so an agent can find it

A specification for one catalogue entry, and the measurements each field is
required by. Normative schema: [`schema/operation.schema.json`](../schema/operation.schema.json).
Every entry in MapSmith's catalogue validates against it in CI.

This exists because tool discovery stops working before anybody notices. A server
with fifty operations feels fine; the same server with eight hundred returns three
plausible names and the right one is not among them, and nothing in the protocol
says so. The fields below are what we measured our way to, including the ones that
turned out to be worth nothing.

## The measurement everything rests on

Eight hundred real GIS operations — ours plus a library that ships hundreds with
their own descriptions — queried the way somebody with a problem asks, not the way
a catalogue is written. *"The coastline is 400 000 nodes and the browser dies"*,
not *"simplify the geometry"*.

| what the caller declares | candidates left | found@3 |
|---|---|---|
| nothing — words alone | 800 | 20% |
| the input kind | 259 | 40% |
| + what it should produce | 132 | 55% |
| + which family | **16** | **70%** |

**Ranking is not the mechanism; narrowing is.** No ranker recovers 20% to 70%, and
the same numbers hold at 200 operations and at 800 — declaring the facets makes the
size of the catalogue stop mattering. So the specification is mostly about what an
entry *declares*, and only then about how it is written.

## 1. Facets — the fields a filter reads

These are machine-read and exact. A wrong value here does not degrade a result, it
**removes the operation from the caller's world**, and nothing raises.

The two live under `applicability`, which is one object so that "what can this be
pointed at" stays one question.

### `applicability.inputs` — what it can be pointed at

`vector`, `raster`, `dataset` (either), `plan`, or `none`.

`none` is not an empty list. It means the operation takes no dataset at all — a
question about a coordinate system, a distance between two coordinates — and an
entry declaring it is kept for *every* input kind. An empty list would read as
"applies to nothing" and drop it from every result, which is the opposite of true.

### `applicability.requires_projected_crs` — whether it refuses degrees

True only when the engine actually refuses. **Verify it by executing**, not by
reading the code: reading ours once reported five operations as refusing because
the word "geographic" appeared in a warning rather than in a `raise`.

### `produces` — what the caller gets back

`dataset:vector`, `dataset:raster`, `answer`, `description`, `plan_result`.

Worth 15 points of found@3 on top of the input kind, and it is a facet the caller
always knows: they know whether they want a file, a number, or an account of
something they already have. Check it against what the code actually writes — a
declaration that disagrees makes the operation unreachable for the caller who
filters correctly.

### `category` — the family

Measured the single most informative facet: on its own it cuts 800 candidates to
23 and reaches the same 70%. Keep the vocabulary small and stable; a family with
one member in it is a filter nobody will guess.

## 2. Text — the fields a ranker reads

Both ranking engines index the same document, so this text is the corpus, not
documentation that happens to be nearby.

### `summary` and `description`

Write the description for an agent deciding whether to call it: what it needs, what
it refuses, and **what goes wrong silently if it is used for the wrong thing**. The
last part is the one that helps most, because it is the part no other entry says.

### `phrasings` — how a caller names the problem

Never a synonym of the operation's name. `"too many vertices"`, not `"simplify"`. A
synonym of the name only helps somebody who already knows the name, which is
somebody who does not need to search.

**Honest measurement**: adding these moved found@3 from 40% to 100% on the queries
they were written against, and from 10/20 to 10/20 on queries written afterwards.
The effect on unseen phrasing is not measurable. The field stays because it costs
nothing and because writing it forces the author to ask how somebody would look for
this — not because it fixed retrieval. **It is in this specification as a
requirement with a null result attached, which is the honest way to keep a field.**

### `distinguishes` — what it is NOT

The recommended field, and the one that matters most in a crowded catalogue.

After the facets, about twenty operations of the same family remain, and inside
that residue ranking is close to random: asked *"which vineyards sit under the
proposed reservoir footprint"*, the ranker returned `centroid_layer`, `buffer_layer`
and `hull_layer`, and the answer was `overlay_layers`. Every one of those takes a
layer and returns a layer, so nothing in the shape of the entry separates them.

Contrastive text does. Write **"not X, which does Y"**, naming the neighbour:

> `centroid_layer` — collapses each feature to one point for a distance calculation.
> **Not for map labels**: the centroid of a concave shape falls outside it, and
> `point_on_surface` is the one that cannot.

**Measured, and the honest answer is that it does not help retrieval.** Written
for six entries after seeing which six failed, it took a twenty-query set from 14
correct to 17 and silent misses from one to zero. Written properly — thirty-two
entries, contrasting the neighbour rather than echoing a query — the same set comes
back **14 of 20, unchanged**. The first number was contamination.

It does move BM25, by a little: 10 of 20 found@3 to 12. That direction is the
diagnostic. Our embedding engine is a *static* model — a token table with pooling and
no context — so it behaves more like a smarter BM25 than like a transformer, and text
that is semantically right but lexically different is precisely what it cannot use.
Writing better prose will not fix that; a different class of model might, and that is
a decision with a cost rather than a field in a specification.

One caution learned here: naming the neighbour puts the neighbour's words in this
entry's document, so an entry becomes findable by its neighbour's query. Measured, the
net effect on BM25 was still positive — but write the contrast around what the
operation DOES differently, not around a list of names.

**So why is the field here at all.** Two reasons that are not retrieval:

1. **It is read at selection time, not at search time.** The search returns three
   candidates; the agent then reads them and picks. Three entries that all say "one
   point per polygon" give it nothing to choose on. `distinguishes` is the only field
   written to be read against its neighbours, and that is the moment it pays.
2. **Writing it catches catalogue defects.** Forcing an author to name the neighbour
   is what surfaced that `centroid_layer` was advertising `point_on_surface`'s job —
   and recommending a defect our own correctness suite measures as a trap.

It is `RECOMMENDED` and not required, and the null result stays in this document
rather than being quietly dropped. A specification that only reports the fields that
worked is an advertisement.

### `parameters`

Type, whether it is required, and a sentence each. **Where a wrong value fails
silently, say so in the parameter's own description** — that is where somebody
reads it at the moment they are choosing a value. A parameter with no safe default
should have no default at all, and the description is where the reason goes.

`planned` entries have none, and are not required to: an entry that does not run
yet cannot describe arguments it does not have. The schema requires the full
description only of `available`.

## 3. `examples` — the discoverability contract

At least two, and **the first is a test probe, not decoration**. The contract runs a
search with that goal and the entry's own facets, and requires the entry in the top
three. Write the *goal a caller has*, not the invocation.

A catalogue-wide average will not do here: 90% found@3 over fifty entries means five
are unreachable and the average does not say which. The contract is per entry, so a
new operation is under it the moment it is added.

**It found a real defect on its first run.** `centroid_layer` advertised *"label
points for a polygon layer"* and ranked below `point_on_surface`. The ranking was
right — a centroid can fall outside its own polygon, which is a defect our own
correctness suite measures as a trap. Our catalogue was recommending it. Before
adjusting wording to make the contract pass, **check whether the ranking is right**:
an entry that loses to a neighbour may be advertising the neighbour's job.

## 4. Growth

An operation very close to an existing one competes with it forever, in every
future search. Before adding one, either distinguish them in text — by what they do
differently to the *number*, not to the prose — or ask whether it should be a
parameter of the existing operation instead.

## What this specification does not claim

It is measured on one catalogue, in one domain, with queries written by the people
who wrote the catalogue and then deliberately re-written to avoid its vocabulary.
The mechanism generalises — facets narrow, contrastive text separates neighbours,
one probe per entry beats an average — but the exact percentages are ours.

The measurements live in `tests/test_retrieval_degradation.py`,
`tests/test_retrieval_at_scale.py` and `tests/test_discovery_contract.py`, and they
run in CI, so the numbers above are checkable rather than remembered.
