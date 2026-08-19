# MapSmith — Contributor guide for AI assistants

MapSmith gives AI agents professional-grade geoprocessing via MCP, with **verifiable provenance**. Every design rule below protects that promise.

## Non-negotiable invariants

1. **Deterministic outputs only**: geometry and numbers always come from engine executions, never from a model.
2. **Provenance on every writer**: any function that writes a dataset emits `<output>.provenance.json` via `ProvenanceRecord` (inputs with sha256, exact parameters, motivated CRS decisions, engine+version).
3. **Deterministic verification**: writers run `verify.verify_vector_output(...)` with operation-appropriate expectations, record checks in the manifest FIRST, then `verify.enforce(...)` — the audit trail must survive failures.
4. **Explicit CRS discipline**: reject inputs without a CRS (helpful message); never run metric ops on geographic CRS without a recorded reprojection decision.
5. **GPL boundary**: QGIS/GRASS only via subprocess (CLI/files/JSON), never in-process imports.
6. **Few semantic tools**: extend existing tools or the catalog; don't multiply MCP tools (agent accuracy collapses near ~100 exposed tools).
7. **GeoParquet is the canonical analytical format**; exchange between engines via Arrow.

## Practical conventions

- Optional engines live behind extras (`[sedona]`, `[raster]`, `[postgres]`) with import guards whose error message names the extra.
- Version pins are deliberate: `mcp>=1.19,<2` (1.19 is the floor for tool `annotations`+`meta`; v2 renamed FastMCP→MCPServer; migration planned with MCP Tasks), `ruff>=0.16,<0.17` (new ruff minors add default rules and break CI).
- Tests use closed-form expected values (e.g., a known 5×5 raster block → mean=22, sum=550) plus rejection-path tests; `pytest.importorskip` for extras.
- Run `python -m ruff check .` before committing; CI runs lint + tests on Python 3.10/3.12 + Docker build.
- Docker (or `uvx` where wheels work) is the only supported install path — keep it true in docs.
- Verify external-library APIs against primary documentation before coding against them; this repo has already been bitten by from-memory APIs three times.

## Layout

- `src/mapsmith/server.py` — MCP tools (stdio default; `MAPSMITH_TRANSPORT=http` for stateless Streamable HTTP)
- `src/mapsmith/engines/` — dispatch + engines (vector/GeoPandas, duckdb, sedona, raster/exactextract, whitebox)
- `src/mapsmith/plans/` — typed plan DAG: models (wire contract), registry (op→engine bindings, kept in sync with the catalog by a test), static validator (stable error codes, simulated CRS flow), sequential executor (plan-level manifest)
- `src/mapsmith/provenance.py`, `verify.py`, `jobs.py`, `catalog.py`
- `tests/` — closed-form tests · `deploy/k8s/` — generic example manifests
