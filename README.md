# MapSmith 🔨🗺️

**Professional-grade geoprocessing for AI agents — with provenance you can verify.**

MapSmith is an open-source [MCP](https://modelcontextprotocol.io) server that gives any AI agent (Claude, ChatGPT, Copilot, Cursor, or your own) real GIS analysis capabilities: not just "make me a map", but buffers, overlays, reprojections, zonal statistics, terrain and network analysis — executed by deterministic engines, never hallucinated by the model.

> Ask for the result. The agent picks the tools. Every output carries its full lineage.

## Why MapSmith

- **Real geoprocessing, not map CRUD.** Built on the proven open geospatial stack (GDAL, GeoPandas, Shapely, and more to come: WhiteboxTools, PDAL, DuckDB Spatial, QGIS Processing via sidecar).
- **Provenance by design.** Every layer MapSmith produces ships with a machine-readable lineage manifest: source datasets (with checksums), every tool executed, exact parameters, CRS decisions, software versions, timestamps. Re-run it bit-identical, without the LLM. No AI slop.
- **The LLM orchestrates, tools compute.** Geometry and numbers only ever come from deterministic tool executions — never from model output.
- **Semantic tools, not a tool dump.** A curated set of goal-level tools plus a searchable operation catalog (progressive discovery), because agent accuracy collapses when you expose hundreds of raw tools.

## Quickstart

```bash
# Docker (the supported path)
docker run -i --rm -v $(pwd)/data:/data ghcr.io/mapsmith-ai/mapsmith

# or from PyPI
uvx mapsmith
```

Add to Claude Desktop / any MCP client (stdio):

```json
{
  "mcpServers": {
    "mapsmith": {
      "command": "uvx",
      "args": ["mapsmith"]
    }
  }
}
```

Then ask your agent things like:

> "Take parcels.gpkg, keep only the parcels within 300 m of the river in rivers.gpkg, and give me the result with the analysis lineage."

## Tools (v0.1)

| Tool | What it does |
|---|---|
| `describe_dataset` | CRS, geometry types, schema, extent, feature count of any vector dataset |
| `buffer_layer` | Metric buffer with automatic UTM estimation for geographic CRS |
| `clip_layer` | Clip a layer with a mask layer |
| `reproject_layer` | Reproject to any CRS (EPSG code or WKT) |
| `spatial_join` | Join attributes by spatial predicate (intersects/within/contains) |
| `get_provenance` | Return the full lineage manifest of any MapSmith output |
| `list_operations` | Searchable catalog of available operations (progressive discovery) |

Every tool that writes an output also writes `<output>.provenance.json` next to it.

## Provenance example

```json
{
  "mapsmith_version": "0.1.0",
  "operation": "buffer_layer",
  "parameters": {"distance_meters": 300.0},
  "inputs": [{"path": "rivers.gpkg", "sha256": "9f2c…", "crs": "EPSG:4326"}],
  "crs_decisions": {"analysis_crs": "EPSG:32632", "reason": "estimated UTM zone for metric buffering"},
  "engine": {"name": "geopandas", "version": "1.0.1"},
  "started_at": "2026-08-18T10:15:03Z",
  "finished_at": "2026-08-18T10:15:04Z"
}
```

## Architecture

```
 AI agent (Claude / ChatGPT / Copilot / your app)
        │  MCP (stdio local · Streamable HTTP remote)
        ▼
 ┌─────────────────────────────────────────────┐
 │ MapSmith server                             │
 │  · semantic tools + operation catalog       │
 │  · parameter validation, CRS discipline     │
 │  · provenance recorder (lineage manifests)  │
 ├─────────────────────────────────────────────┤
 │ Engines                                     │
 │  · vector: GeoPandas/Shapely (built-in)     │
 │  · raster: Rasterio (roadmap)               │
 │  · terrain/hydro: WhiteboxTools (roadmap)   │
 │  · qgis_process / GRASS sidecar (roadmap,   │
 │    GPL-isolated via subprocess)             │
 └─────────────────────────────────────────────┘
```

## Roadmap

- [ ] Raster engine (Rasterio): zonal stats, clip, hillshade, algebra
- [ ] WhiteboxTools adapter: terrain & hydrology (500+ permissive tools)
- [ ] QGIS Processing sidecar (subprocess-isolated): ~900 algorithms
- [ ] Sandboxed code-execution tool for the long tail
- [ ] Remote server (Streamable HTTP + OAuth), long-job progress via MCP Tasks
- [ ] Map rendering: shareable MapLibre viewer URLs, MCP Apps

## Install support policy

**Docker (or `uvx` on a machine with working wheels) is the only supported installation path.** Geospatial native dependencies across three OSes are a support black hole; issues about broken local environments will be redirected here.

## License

- MapSmith server and engines: **AGPL-3.0-or-later** (see `LICENSE`)
- Client SDK and tool-schema definitions (future `sdk/`): **Apache-2.0**

You can self-host MapSmith freely, forever. If you modify it and offer it as a service, the AGPL asks you to share your changes — or [talk to us](mailto:mapsmith@proton.me) about a commercial license.

"MapSmith" is a trademark of the MapSmith project — see `TRADEMARKS.md`.
