# MapSmith 🔨🗺️

[![CI](https://github.com/mapsmith-ai/MapSmith/actions/workflows/ci.yml/badge.svg)](https://github.com/mapsmith-ai/MapSmith/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/mapsmith)](https://pypi.org/project/mapsmith/)
[![Python](https://img.shields.io/pypi/pyversions/mapsmith)](https://pypi.org/project/mapsmith/)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)
[![Container](https://img.shields.io/badge/ghcr.io-mapsmith--ai%2Fmapsmith-2496ED?logo=docker&logoColor=white)](https://github.com/mapsmith-ai/MapSmith/pkgs/container/mapsmith)
[![MCP](https://img.shields.io/badge/Model_Context_Protocol-server-654FF0)](https://modelcontextprotocol.io)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![X](https://img.shields.io/badge/@mapsmith__ai-000000?logo=x&logoColor=white)](https://x.com/mapsmith_ai)
[![Bluesky](https://img.shields.io/badge/Bluesky-mapsmith.bsky.social-0285FF?logo=bluesky&logoColor=white)](https://bsky.app/profile/mapsmith.bsky.social)

**Professional-grade geoprocessing for AI agents — with provenance you can verify.**

MapSmith is an open-source [MCP](https://modelcontextprotocol.io) server that gives any AI agent (Claude, ChatGPT, Copilot, Cursor, or your own) real GIS analysis capabilities: not just "make me a map", but buffers, overlays, reprojections, zonal statistics, terrain and network analysis — executed by deterministic engines, never hallucinated by the model.

> Ask for the result. The agent picks the tools. Every output carries its full lineage.

## Why MapSmith

- **Real geoprocessing, not map CRUD.** Built on the proven open geospatial stack: GDAL, GeoPandas, Shapely, DuckDB Spatial, WhiteboxTools and exactextract ship today (more to come: PDAL, QGIS Processing via sidecar).
- **Provenance by design.** Every layer MapSmith produces ships with a machine-readable lineage manifest: source datasets (with checksums), every tool executed, exact parameters, CRS decisions, software versions, timestamps. Everything needed to re-run the analysis without the LLM is in there. No AI slop.
- **The LLM orchestrates, tools compute.** Geometry and numbers only ever come from deterministic tool executions — never from model output.
- **Semantic tools, not a tool dump.** A curated set of goal-level tools plus a searchable operation catalog (progressive discovery), because agent accuracy collapses when you expose hundreds of raw tools.
- **Model-agnostic infrastructure.** Claude, GPT, Qwen, Kimi, GLM — anything that speaks MCP, cloud or local. The leverage is better contracts (typed plans, actionable error codes, a searchable catalog), not weights we would have to maintain. See [the manifesto](MANIFESTO.md).

## Quickstart

```bash
# Docker (the supported path)
docker run -i --rm -v $(pwd)/data:/data -e MAPSMITH_WORKSPACE=/data ghcr.io/mapsmith-ai/mapsmith

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

One-click installs:

[![Install in Cursor](https://cursor.com/deeplink/mcp-install-dark.svg)](https://cursor.com/install-mcp?name=mapsmith&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyJtYXBzbWl0aCJdfQ%3D%3D)
[![Install in VS Code](https://img.shields.io/badge/VS_Code-Install_MapSmith-0098FF?logo=visualstudiocode&logoColor=white)](https://insiders.vscode.dev/redirect/mcp/install?name=mapsmith&config=%7B%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22mapsmith%22%5D%7D)

or from a terminal: `code --add-mcp '{"name":"mapsmith","command":"uvx","args":["mapsmith"]}'`

Then ask your agent things like:

> "Take parcels.gpkg, keep only the parcels within 300 m of the river in rivers.gpkg, and give me the result with the analysis lineage."

## Tools

| Tool | What it does |
|---|---|
| `describe_dataset` | CRS, geometry types, schema, extent, feature count of any vector dataset |
| `buffer_layer` | Metric buffer with automatic UTM estimation for geographic CRS |
| `clip_layer` | Clip a layer with a mask layer |
| `reproject_layer` | Reproject to any CRS (EPSG code or WKT) |
| `spatial_join` | Join by spatial predicate, auto-routed to the fastest engine (SedonaDB > DuckDB > GeoPandas) |
| `run_sql` | Spatial SQL (DuckDB dialect) over GeoParquet and GDAL formats |
| `zonal_statistics` | Raster statistics per vector zone with exact fractional pixel coverage (`[raster]` extra) |
| `hillshade` | Shaded relief from a DEM, in-memory Whitebox engine (`[whitebox]` extra) |
| `flow_accumulation` | D8 flow accumulation with automatic depression filling (`[whitebox]` extra) |
| `watershed` | Watershed delineation from a DEM and pour points (`[whitebox]` extra) |
| `preview_map` | Interactive in-chat map (MCP Apps) of any datasets, with per-layer provenance and verification badges |
| `validate_plan` | Statically validate a multi-step plan before running anything: operations, arguments, references, input files, simulated CRS flow |
| `execute_plan` | Validate then run a plan step by step, with per-step provenance and a plan-level manifest |
| `get_provenance` | Return the full lineage manifest of any MapSmith output |
| `list_operations` | BM25-ranked catalog search; `detail=true` returns parameters and worked examples |
| `server_info` | Version, license, available engines |

Every tool that writes an output also writes `<output>.provenance.json` next to it —
and runs deterministic verification (CRS, dimensions, value invariants) whose results
are recorded in the manifest *before* any failure is raised.

Verification runs on the way in as well as on the way out. The vector
operations (`buffer_layer`, `clip_layer`, `reproject_layer`, `spatial_join`,
`zonal_statistics`) check their inputs first for the failures that produce
*plausible* junk: a layer with no CRS — refused outright, before anything runs
— and an empty layer; where an operation takes two vector inputs, also extents
that cannot possibly intersect. Afterwards, where an operation carries geometry through
unchanged (reprojection, joins, zonal statistics), an output that is
mechanically broken is repaired deterministically — `make_valid`, at most two
rounds, written to a temporary file and swapped in only once it is complete —
and every attempt is recorded in the manifest, because a repaired output must
never look like one that was right first time. Failures needing judgement are
never "fixed": an empty result, or geometries eroded away by a wrong distance,
come back as named warnings with hints, in the tool result and not only in the
manifest, so the agent sees them instead of assuming success.

The Docker image ships with the `[raster]` and `[whitebox]` extras included. With
`uvx`, pick your extras: `uvx --from "mapsmith[raster,whitebox]" mapsmith`.

## See results inside the chat

![MapSmith's interactive map panel rendered inside Claude Desktop: OSM basemap, buffer and zone layers, and per-layer provenance cards with verification status](docs/images/map-panel.png)

`preview_map` renders your layers on an interactive map panel *inside* Claude,
ChatGPT, VS Code and every other client supporting the official
[MCP Apps](https://modelcontextprotocol.io/extensions/apps/overview) extension —
pan, zoom, toggle layers, and read each layer's provenance card (operation,
engine, verified ✓) right next to the geometry it explains. The panel is fully
self-contained — no CDN, no bundled libraries, no telemetry — with one
outbound request named here rather than buried: the OpenStreetMap background
tiles, which reveal the map view you are looking at (never your data), and
which the panel drops to a plain backdrop when the host blocks them. On clients
without MCP Apps the same call returns the preview as structured data.

## Plans: reject wrong analyses before they run

In GIS-agent benchmarks, up to ~47% of failed runs involve planning mistakes —
missing or mis-ordered operations — and CRS mismatches halve task success. MapSmith attacks
this where it's cheapest: the agent submits a **typed plan**, and static
validation rejects unknown operations (with suggestions), missing arguments,
forward references, absent input files and CRS-unsuitable steps **before
anything executes** — with machine-actionable error codes the agent can repair.

```json
{
  "goal": "buildings within 300 m of rivers",
  "steps": [
    {"id": "buf", "operation": "buffer_layer",
     "arguments": {"input_path": "rivers.gpkg", "distance_meters": 300,
                   "output_path": "rivers_300m.parquet"}},
    {"id": "cut", "operation": "clip_layer",
     "arguments": {"input_path": "buildings.parquet", "mask_path": "$buf",
                   "output_path": "at_risk.parquet"}}
  ]
}
```

`"$buf"` consumes the output of step `buf`; references may only point backwards,
so plans are acyclic by construction. `validate_plan` also simulates the CRS of
every intermediate dataset from the real input files. `execute_plan` then runs
the chain with per-step provenance plus a plan-level manifest
(`<output>.plan.json`) fingerprinting the exact plan that produced the result.

UNC hosts and NTFS alternate data streams are rejected in every path
*argument* of every tool call, before anything touches the filesystem (on Windows even an existence
check on a UNC path talks to an attacker-chosen host). Remote and virtual
forms (GDAL `/vsi*`, `https://` COGs) stay available in uncontained mode —
cloud-native data is a feature — and are refused once a workspace is set.
Validated plans are stricter by design and reject every non-local form.

Set `MAPSMITH_WORKSPACE=/data` to confine the server to one directory:

- every path argument of every tool must resolve inside the workspace
  (checked at the MCP boundary, and again by plan validation with stable
  error codes);
- the `run_sql` DuckDB connection is sandboxed at the engine level —
  filesystem whitelisted to the workspace (`allowed_directories` + external
  access off, which also covers GDAL-backed `ST_Read`), extension
  install/load refused, memory and temp-disk capped
  (`MAPSMITH_DUCKDB_MEMORY`, default 4GB; `MAPSMITH_DUCKDB_TEMP_LIMIT`,
  default 8GB), configuration locked. SQL text can name any path it likes;
  the engine refuses to open it.

Without a workspace the server is deliberately unconfined (fine for a local
stdio server on your own files); plan validation flags `run_sql` steps with a
`SQL_NOT_SANDBOXED` warning in that mode. Two fine-print notes: the jail
assumes a single trusted writer of the workspace filesystem (paths are
resolved at check time, so symlink swaps by another local process are out of
scope), and the spatial extension is fetched once per environment — for
air-gapped deployments pre-install it (`python -c "import duckdb;
duckdb.connect().install_extension('spatial')"`) before locking the network
down. Defense in depth still applies: for real isolation run MapSmith in a
container and mount only the data you want it to see; keep the HTTP
transport on loopback/trusted networks until authenticated remote mode
ships.

## We measured whether this actually helps

Claims about agent performance are cheap, so
[**docs/benchmarks.md**](docs/benchmarks.md) reports an A/B on
[GABench](https://github.com/GeoX-Lab/GABench) — 57 executable GIS tasks over a
133-tool server, scored by its deterministic evaluator — where the *only*
variable is whether the agent's typed plan is validated before the solver runs.

The honest headline is a **null result**, on a frontier model and on a small
one, and the interesting part is why:

| | Arm A (no gate) | Arm B (gate) |
|---|---|---|
| Sonnet 5 — TAO / PEA | 0.824 / 0.430 | 0.781 / 0.425 |
| Haiku 4.5 — TAO / PEA | 0.660 / 0.320 | 0.714 / 0.366 |

Haiku looks like a clean win until you notice the gate only fired on 4 of 57
plans, and that the 53 tasks it never touched moved by just as much: the
aggregate delta is run-to-run variance, and measuring that noise floor
(2–5 points per metric on a single repetition) is the reusable result. What
survives is narrower — on the plans it did repair, tool selection improved by
+0.19 TAO — and it points at where the failures actually are: PEA around 0.4
in every arm, i.e. wrong parameters and missing outputs at *execution* time,
which is why MapSmith enforces its plans at the execution boundary and verifies
inputs and outputs at runtime rather than advising an agent that improvises.

The harness is in [`benchmarks/gabench-ab/`](benchmarks/gabench-ab/), including
the `split_analysis.py` that took our own win apart.

## Notebook gallery

Three executable, self-contained walkthroughs in [`examples/`](examples/):
verified buffer+clip with provenance manifests, terrain & hydrology on the
Whitebox engine, and a deliberately wrong plan rejected before execution and
then repaired. Each generates its own synthetic data — install and run.

## Provenance example

```json
{
  "mapsmith_version": "0.2.0",
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
 │  · SQL/analytics: DuckDB Spatial (built-in) │
 │  · heavy joins: SedonaDB ([sedona] extra)   │
 │  · zonal stats: exactextract ([raster])     │
 │  · terrain/hydro: Whitebox NG ([whitebox])  │
 │  · qgis_process / GRASS sidecar (roadmap,   │
 │    GPL-isolated via subprocess)             │
 └─────────────────────────────────────────────┘
```

## Roadmap

- [x] Zonal statistics (exactextract, exact fractional coverage)
- [x] Whitebox Next Gen adapter: hillshade, flow accumulation, watershed (in-memory, open tier)
- [x] Typed analysis plans: static validation against the operation registry + simulated CRS flow before execution
- [ ] Bounded repair: feed verification failures back to the agent for limited retries
- [ ] More terrain & hydrology: slope/aspect, stream network extraction
- [ ] QGIS Processing sidecar (subprocess-isolated): ~900 algorithms
- [ ] Sandboxed code-execution tool for the long tail
- [x] MCP Apps in-chat map panel with provenance cards (self-contained, works under the default sandbox)
- [ ] Map panel: MapLibre vector rendering and shareable viewer URLs (raster OSM tiles already ship)
- [ ] Remote server (Streamable HTTP + OAuth), long-job progress via MCP Tasks

## Install support policy

**Docker (or `uvx` on a machine with working wheels) is the only supported installation path.** Geospatial native dependencies across three OSes are a support black hole; issues about broken local environments will be redirected here.

## License

- MapSmith server and engines: **AGPL-3.0-or-later** (see `LICENSE`)
- Client SDK and tool-schema definitions (future `sdk/`): **Apache-2.0**

You can self-host MapSmith freely, forever. If you modify it and offer it as a service, the AGPL asks you to share your changes — or [talk to us](mailto:mapsmith@proton.me) about a commercial license.

"MapSmith" is a trademark of the MapSmith project — see `TRADEMARKS.md`.

<!-- mcp-name: io.github.mapsmith-ai/mapsmith -->
