"""Shared fixtures, and one rule about the embedding engine.

The rule exists because of a build that went red on 2026-08-29 for a reason that
had nothing to do with this repository: Hugging Face answered 429 to the model
download and six tests failed, four of them because the vector engine could not
be had and two because the numbers they check are produced by whichever engine
was available.

`catalog.search` is built to survive that — it falls back to BM25 and says so in
the `engine` field — so the product behaved correctly and only the tests did not.
A test that asserts what the vector engine does must skip when the vector engine
cannot be had, exactly as it already skips when `model2vec` is not installed.
Someone else's rate limit is not a defect in our code, and a suite that reports
it as one teaches people to ignore red builds.
"""

from __future__ import annotations

import re

import pytest


@pytest.fixture(scope="module")
def vector_engine():
    """The embedding engine, or a skip with the reason.

    Covers both ways it can be absent: the package missing (an install without
    it) and the model not loading (no network, a cold cache, a proxy, a rate
    limit, a workspace refusing the fetch by design since 0.3.0).
    """
    pytest.importorskip("model2vec")
    from mapsmith import retrieval

    try:
        retrieval.embed(["a warm-up query, so the failure happens here"])
    except Exception as failure:  # noqa: BLE001 - any failure to load is a skip
        pytest.skip(f"the embedding model could not be loaded: {failure}")
    return retrieval

# --------------------------------------------------------------------------
# Conformance, shared: moved here on 2026-09-06 when a second test file needed
# it. It lived in `test_verify.py`, and the rule it encodes -- validate against
# BOTH implementations -- is exactly the kind that gets re-implemented halfway
# by whoever needs it next.


def _spec_problems(record: dict) -> list[str]:
    """Every way this record fails the spec, according to BOTH implementations.

    The standalone validator alone is not enough, and until 2026-08-26 it was
    all this file used: the schema is the NORMATIVE implementation, and the two
    had drifted on every recommended field. A CI that says "conforming" using
    the lenient one of two implementations is worse than one that says nothing,
    because it is the sentence a reader trusts.
    """
    import json
    import sys
    from pathlib import Path

    data = Path(__file__).parent / "data"
    sys.path.insert(0, str(data))
    from manifest_spec_validator import problems

    found = list(problems(record))
    try:
        import jsonschema
    except ImportError:  # pragma: no cover - jsonschema is in the test extra
        return found
    schema = json.loads((data / "manifest-v1.schema.json").read_text(encoding="utf-8"))
    checker = jsonschema.Draft202012Validator(schema)
    found += [f"schema: {error.message}" for error in checker.iter_errors(record)]
    return found


def _spec_crs_keys() -> frozenset[str]:
    """The `crs_decisions` keys section 3.7 fixes, read from the normative schema.

    Not restated here. A test that polices a vocabulary against its own copy of
    that vocabulary polices nothing -- and this one exists precisely because two
    operations wrote synonyms of these names for a week without anything
    noticing.
    """
    import json
    from pathlib import Path

    schema = json.loads(
        (Path(__file__).parent / "data" / "manifest-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    keys = frozenset(schema["properties"]["crs_decisions"]["properties"])
    assert {"analysis_crs", "reason", "source_crs", "target_crs"} <= keys, sorted(keys)
    return keys


#: The shape D-077 requires of a `crs_decisions` key that is MapSmith's own.
#: The syntax is the one section 3.6 of the specification makes a MUST for
#: check names; applying it to FIELDS is a MapSmith rule that the specification
#: only recommends (section 3.5 says a producer prefix "does this well", and
#: says it as a SHOULD). Both guards import this so that one rule has one
#: predicate: the first version wrote `startswith` in one and a regex in the
#: other, and they disagreed on `x-mapsmith:Bad Name!`.
_EXTENSION_KEY = re.compile(r"^x-mapsmith:[a-z0-9][a-z0-9_]*$")
