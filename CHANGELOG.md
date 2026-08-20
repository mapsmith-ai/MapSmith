# Changelog

All notable changes to MapSmith are documented here, in the format of
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This project follows
[semantic versioning](https://semver.org/).

## [0.2.0] — 2026-08-20

The first release you can point an agent at and trust the answer: results are
verified on the way in and on the way out, plans are checked before anything
runs, and the server can be confined to a single directory.

### Added

- **Interactive map inside the chat.** `preview_map` renders your layers on a
  pan/zoom map panel in any client implementing the
  [MCP Apps](https://modelcontextprotocol.io/extensions/apps/overview)
  extension (field-tested on Claude Desktop), with an OpenStreetMap backdrop
  and a provenance card per layer showing operation, engine and one of three
  states: `verified ✓`, `verification failed`, or `not verifiable` when no
  critical check ran. Fully self-contained; on clients without MCP Apps the
  same call returns structured data.
- **Typed plans.** `validate_plan` statically checks a multi-step analysis —
  operations exist and are installed, arguments complete and well-typed,
  `$step` references resolve backwards, input files exist, outputs don't
  collide, and the CRS of every intermediate is simulated from the real
  inputs — and returns machine-actionable error codes. `execute_plan` then
  runs the validated plan with per-step provenance plus a plan-level manifest
  fingerprinting the exact plan that produced the result.
- **Terrain and hydrology** on the Whitebox Workflows engine (`[whitebox]`
  extra): `hillshade`, `flow_accumulation` (D8, with depression filling) and
  `watershed` (many pour points at once).
- **Zonal statistics** with exact fractional pixel coverage via exactextract
  (`[raster]` extra).
- **A searchable operation catalog.** `list_operations` ranks capabilities by
  relevance (BM25) so an agent can discover what exists — including what is
  planned but not yet available — instead of guessing from a wall of tools.
- **Workspace confinement.** Set `MAPSMITH_WORKSPACE` and every path argument
  must resolve inside it, `run_sql`'s DuckDB connection is sandboxed to that
  directory with extension loading refused and memory/temp-disk capped, and
  UNC hosts and NTFS alternate data streams are refused in every mode.
- **Verification on the way in.** Operations check their inputs for the
  failures that produce plausible junk: a missing CRS is refused outright, and
  empty inputs or extents that cannot possibly intersect come back as named
  warnings with hints — in the tool result, not only in the manifest.
- **Bounded deterministic repair.** Mechanically broken output geometry is
  repaired (`make_valid`, at most two rounds, written atomically) and every
  attempt is recorded in the manifest: a repaired output never looks like one
  that was right the first time.
- **A notebook gallery** (`examples/`) and a
  [benchmarks page](docs/benchmarks.md) with the harness that produced it.

### Fixed

- **Wrong terrain results from ordinary compressed rasters.** Whitebox
  Workflows 2.x does not undo the TIFF predictor when reading, so any DEM
  saved with `PREDICTOR=2` or `3` — the standard encoding for elevation data —
  produced hillshades and flow accumulations that looked like terrain and were
  not. MapSmith now detects the predictor and converts the input first,
  recording it in the manifest.
  ([upstream report](https://github.com/jblindsay/whitebox_next_gen/issues/32))
- **GeoParquet outputs from the vector engines** were written through a GDAL
  path that produced unreadable files.
- `run_sql` materialisations and the DuckDB/SedonaDB join fast paths now run
  the same deterministic verification as every other writer, and record their
  CRS decisions.

### Changed

- `spatial_join` with `engine="auto"` falls back to GeoPandas when the inputs'
  CRS differ or are unknown, instead of joining mismatched coordinates.
- The planning-failure figure quoted in the docs is stated as the upper bound
  it is ("up to ~47%"), since the underlying study counts errors multi-label.

## [0.1.0] — 2026-08-18

First public release: the engine dispatcher (SedonaDB / DuckDB / GeoPandas),
`run_sql`, the job ledger, stateless Streamable HTTP transport, and provenance
manifests with deterministic verification on every writer.
