"""Catalog retrieval: closed-form BM25 math, deterministic ranking, doc completeness."""

import math

import pytest

from mapsmith import catalog


def test_bm25_closed_form():
    """Two tiny documents, hand-computed Okapi BM25 score.

    docs: d1=[map, map, forge] (len 3), d2=[forge] (len 1); query=[map]
    N=2, df(map)=1 -> idf = ln((2-1+0.5)/(1+0.5)+1) = ln(2)
    avgdl=2, k1=1.5, b=0.75 -> length_norm(d1) = 1.5*(0.25+0.75*3/2) = 2.0625
    score(d1) = ln(2) * 2*(1.5+1)/(2+2.0625) = ln(2)*5/4.0625 = 0.85310...
    """
    scores = catalog.bm25_scores(["map"], [["map", "map", "forge"], ["forge"]])
    assert scores[0] == pytest.approx(math.log(2) * 5 / 4.0625)
    assert scores[0] == pytest.approx(0.8531, abs=1e-4)
    assert scores[1] == 0.0


def test_ranking_puts_the_right_operation_first():
    assert catalog.search("statistics of a raster inside polygons")[0]["name"] == (
        "zonal_statistics"
    )
    assert catalog.search("join layers by spatial predicate")[0]["name"] == "spatial_join"
    assert catalog.search("sql query")[0]["name"] == "run_sql"
    assert catalog.search("buffer distance meters")[0]["name"] == "buffer_layer"
    assert catalog.search("lineage manifest audit")[0]["name"] == "get_provenance"


def test_scores_are_present_and_descending():
    results = catalog.search("raster statistics")
    assert all("score" in r for r in results)
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)


def test_empty_query_lists_whole_catalog_compact():
    results = catalog.search()
    assert len(results) == len(catalog.OPERATIONS)
    assert all(set(r) == {"name", "status", "category", "summary"} for r in results)


def test_no_match_returns_empty():
    assert catalog.search("nonexistent-xyz") == []


def test_limit_is_respected():
    assert len(catalog.search("layer", limit=2)) <= 2


def test_detail_returns_parameters_and_examples():
    results = catalog.search("buffer", detail=True)
    top = results[0]
    assert top["name"] == "buffer_layer"
    assert any(p["name"] == "distance_meters" for p in top["parameters"])
    assert len(top["examples"]) >= 2


def test_describe_operation_exact_and_suggestions():
    doc = catalog.describe_operation("spatial_join")
    assert doc["category"] == "vector"
    assert len(doc["parameters"]) == 5
    with pytest.raises(ValueError, match="buffer_layer"):
        catalog.describe_operation("buffer")


def test_available_operations_have_complete_docs():
    """Every available entry must carry agent-usable structured docs."""
    for op in catalog.OPERATIONS:
        if op["status"] != "available":
            continue
        assert op.get("description"), f"{op['name']} has no description"
        assert op.get("parameters"), f"{op['name']} has no parameters"
        assert len(op.get("examples", [])) >= 2, f"{op['name']} needs >=2 examples"
        declared = {p["name"] for p in op["parameters"]}
        assert "tool" in op, f"{op['name']} must declare its tool (a name, or None)"
        for example in op["examples"]:
            if op["tool"] is None:
                # An operation with no tool of its own is called through
                # run_operation, and its examples must show exactly that —
                # otherwise the catalog documents a call that does not exist.
                assert example["call"]["tool"] == "run_operation", (
                    f"{op['name']} has no dedicated tool: its examples must call "
                    "run_operation"
                )
                assert example["call"]["arguments"]["operation"] == op["name"]
                used = set(example["call"]["arguments"]["arguments"])
            else:
                assert example["call"]["tool"] == op["tool"]
                used = set(example["call"]["arguments"])
            assert used <= declared, (
                f"{op['name']} example uses undeclared params: {used - declared}"
            )


def test_every_declared_tool_actually_exists_on_the_server():
    """A catalog entry naming a tool that is not registered documents a call
    the agent cannot make; one naming None must be reachable through
    run_operation, which is only true if the registry binds it."""
    import asyncio

    from mapsmith import server
    from mapsmith.plans.registry import BINDINGS

    registered = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert "run_operation" in registered, "the catch-all tool must be exposed"
    for op in catalog.OPERATIONS:
        if op["status"] != "available":
            continue
        if op["tool"] is None:
            assert op["name"] in BINDINGS, (
                f"{op['name']} has no tool and no binding: it is unreachable"
            )
        else:
            assert op["tool"] in registered, f"{op['name']} names a tool that does not exist"
