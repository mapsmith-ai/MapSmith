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
