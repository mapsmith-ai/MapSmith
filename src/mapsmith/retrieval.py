"""Optional embedding retrieval for the operation catalog.

BM25 (:func:`mapsmith.catalog.rank`) stays the default: deterministic,
dependency-free, and sufficient at the current catalog size. This module is
the embedding layer the catalog docstring promised could be "layered on top of
rank() without changing the public API" — built early, while the catalog is
small, so that its quality can be *measured as the catalog grows* instead of
argued about later.

The model is a static embedder (a token lookup plus pooling — no transformer,
no GPU, and no network at query time): ``potion-retrieval-32M``, MIT, 512
dimensions, pinned to an exact revision below. Determinism has two halves and
only one belongs to the model: the artifact is pinned by revision, and a
golden-vector test pins the numbers themselves, so drift in any layer of the
stack fails a test instead of an analysis. Encoding is bit-identical across
calls in one process (measured); multiprocessing is disabled on purpose.

Optional extra: ``pip install mapsmith[retrieval]``. The model file (~130 MB
of artifacts, once) is fetched from the Hugging Face Hub on first use and
cached; offline installs simply keep BM25.
"""

from __future__ import annotations

from functools import cache
from typing import Any

MODEL_ID = "minishlab/potion-retrieval-32M"
# Pinned 2026-08-25 (MIT). Bump deliberately, never implicitly: the golden
# vectors in tests/test_retrieval.py will fail on any change, on purpose.
MODEL_REVISION = "6fc8051fab2a1e0ee76689cf08c853792ac285e7"


def _require():
    try:
        from huggingface_hub import snapshot_download
        from model2vec import StaticModel
    except ImportError as exc:
        raise ImportError(
            "embedding retrieval requires the retrieval extra: "
            "pip install mapsmith[retrieval]"
        ) from exc
    return snapshot_download, StaticModel


@cache
def _model():
    snapshot_download, StaticModel = _require()
    path = snapshot_download(MODEL_ID, revision=MODEL_REVISION)
    return StaticModel.from_pretrained(path)


def embed(texts: list[str]):
    """Embed texts to unit-norm float32 vectors, deterministically."""
    return _model().encode(list(texts), use_multiprocessing=False)


@cache
def _index():
    """Embedded catalog corpus, built once per process from the same text BM25 reads."""
    import numpy as np

    from . import catalog

    vectors = embed([catalog.document_text(op) for op in catalog.OPERATIONS])
    norms = np.linalg.norm(vectors, axis=-1, keepdims=True)
    norms[norms == 0] = 1.0
    return vectors / norms


def rank(query: str, limit: int = 10) -> list[tuple[dict[str, Any], float]]:
    """Catalog entries ranked by cosine similarity to the query.

    Same signature and tie-break as :func:`mapsmith.catalog.rank`, so the two
    engines are interchangeable behind one interface and comparable in one
    harness.
    """
    import numpy as np

    from . import catalog

    vector = embed([query])[0]
    norm = float(np.linalg.norm(vector))
    if norm == 0:
        return [(op, 0.0) for op in catalog.OPERATIONS][:limit]
    scores = _index() @ (vector / norm)
    ranked = sorted(
        zip(catalog.OPERATIONS, (float(s) for s in scores), strict=True),
        key=lambda pair: (-pair[1], pair[0]["name"]),
    )
    return ranked[:limit]
