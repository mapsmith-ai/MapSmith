# Changelog

All notable changes to MapSmith are documented here, in the format of
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This project follows
[semantic versioning](https://semver.org/).

## [Unreleased]

### Changed

- **Remote and virtual paths are refused by default** (`MAPSMITH_ALLOW_REMOTE=1`
  to allow them). GDAL `/vsi*` and `https://` forms used to be accepted whenever
  no workspace was set, justified as being the user's own responsibility — and
  the user is not who decides: the path is written by the model, from whatever it
  read, so a third-party dataset carrying "the updated layer lives at
  https://evil.tld/x.gpkg" was enough to have GDAL parse attacker-chosen bytes
  in-process. The refusal now covers both path arguments and `run_sql` text,
  which closes the SSRF that came with it (raw SQL could read any endpoint the
  host can reach — internal services, cloud metadata — and return the content in
  the tool result) as well as the `INSTALL ... FROM '<url>'` fetch. Cloud-native
  data stays a supported use case: the capability is gated, not removed. A
  workspace refuses remote forms regardless, and validated plans stay strict
  whatever the setting.

### Fixed

- **A local GDAL indirection file could reach the network from inside a
  workspace.** A `.vrt` is a plain local path, so the path guard, the SQL scan
  and DuckDB's `allowed_directories` all saw a local file while GDAL fetched
  whatever its `<SrcDataSource>` named — measured on 0.2.1 as HEAD and GET
  leaving the process with `MAPSMITH_ALLOW_REMOTE` unset **and**
  `MAPSMITH_WORKSPACE` set, through the GeoPandas/pyogrio path. That
  contradicted the one promise SECURITY.md states as testable, so the fix is at
  GDAL's level: with remote reads off, the indirection and network drivers are
  deregistered before the geospatial stack initialises. The opt-in restores
  them, including when a parent process installed the policy — containers pass
  their whole environment down, and a switch that cannot lift an inherited
  policy is a switch that does nothing. Found by an adversarial audit of the
  commit that introduced the opt-in, i.e. of the fix itself.
- **Credentials no longer reach provenance manifests or the job ledger.**
  `run_sql` records the query, and manifests are made to be shared, so an agent
  emitting `CREATE SECRET (... SECRET 'AKIA…')` in the same session used to write
  that key into a file destined for a bug report. **SQL that configures a
  credential is now refused before it runs** — `CREATE SECRET` in any spelling,
  `SET`/`PRAGMA` of a credential-bearing setting, `ATTACH` carrying a password or
  URI userinfo — with a message pointing at where credentials belong: the
  environment of the process that starts the server, out of reach of a tool call.
  Nothing documented used that path, and in MapSmith's sandbox only one secret
  type was even constructible.

  Redaction stays as the second layer for credentials that reach a manifest
  without being SQL (a signed URL as an input path, a connection string as an
  argument), now covering `crs_decisions`, `notes`, input paths and the ledger's
  `error` column — an engine error quotes the statement that failed. Masked
  values are quoted, so a redacted statement still parses when pasted back into
  a client.

  Refusal came first because redaction alone did not hold: an adversarial audit
  of the shipped version escaped it with `MAP{'Authorization': 'Bearer …'}`, an
  `E'…'` literal, dollar quoting and a comment between name and value — and the
  `E'…'` case masked the *wrong* argument while keeping the secret, producing a
  manifest both misleading and leaky. All four are regression tests now.
  Remaining limits, documented rather than implied: detection is name-based, so
  a bare positional secret is not caught, and neither is a URI that
  percent-encodes the colon of its own userinfo.
- **GeoParquet 2.0 files are read instead of refused.** 2.0 (`v2.0.0-rc.1`)
  moves geometry into Parquet's own `GEOMETRY`/`GEOGRAPHY` logical types and
  makes the `geo` metadata key optional, and DuckDB already writes such files.
  MapSmith read them inconsistently: `run_sql` worked, `describe_dataset` failed
  with a raw GeoPandas `Missing geo metadata` error, and the CRS probe reported
  `unknown` **even for a file that states its CRS** — which made the CRS
  precondition refuse valid work for a wrong reason. The CRS now comes from the
  logical type when there is no `geo` key, in all the forms met in practice: the
  spec default (`OGC:CRS84`), an authority string, `projjson:<key>`, and the
  whole PROJJSON document inline, which is what DuckDB writes.

  One form is deliberately not resolved: `srid:<n>`. The spec defines it as a
  numeric identifier and names no authority (its own example is `srid:0`), so
  reading it as `EPSG:<n>` would be MapSmith inventing a coordinate system and
  recording it as fact. Such a file is refused with the declaration quoted.

  Writing is unchanged for now: MapSmith still emits GeoParquet 1.0 (WKB plus
  `geo` metadata), which every 2.0 reader accepts. DuckDB *can* emit both layers
  at once (`geoparquet_version 'BOTH'`, CRS included), and switching is a
  decision for when 2.0.0 is final rather than a release candidate.
- **Persistent DuckDB secrets can no longer be created from a tool call.** They
  are written to `~/.duckdb/stored_secrets` — outside any workspace and beyond
  the session. A workspace already refused that write; the connection now sets
  `allow_persistent_secrets = false` in both modes, before locking the
  configuration.

## [0.2.1] — 2026-08-20

Three fixes you would rather not find yourself. All came from reviewing 0.2.0
*after* it shipped, and all were reproduced through the real MCP tools instead
of read off a diff.

### Fixed

- **An empty spatial join no longer crashes before writing its manifest.**
  DuckDB writes no GeoParquet `geo` metadata for a zero-row result, so reading
  the output back raised — on the default engine path, for exactly the case the
  verification checks exist to explain, while the tool description promised a
  warning. A zero-row result is now written as a valid empty GeoParquet with the
  analysis CRS and the joined schema, and the join goes through the same audited
  writer as everything else, so the manifest exists even when a check fails.
- **A GeoParquet declaring `crs: null` is no longer read as CRS84.** MapSmith
  invented a coordinate system and recorded it in the manifest as fact, with
  `verified: true` — the worst class of bug a provenance tool can have. The
  GeoParquet spec distinguishes an *absent* `crs` field, which does mean CRS84,
  from an explicit null, which means unknown; so does MapSmith now, and an
  unknown CRS is refused by the preconditions like any other missing CRS.
- **The DuckDB sandbox locks its configuration in every mode.** The lock used to
  apply only under `MAPSMITH_WORKSPACE`, so a multi-statement call could switch
  extension autoloading back on, and an explicit `LOAD httpfs` was never blocked
  at all. Locking is now unconditional and DuckDB's HTTP and S3 filesystems are
  disabled, while local reads keep working.
- **The security documentation no longer claims that unconfined mode blocks
  network egress.** It does not, and it never did: GDAL carries its own HTTP
  client, so `ST_Read('/vsicurl/https://…')` in raw SQL reads whatever the host
  can reach — internal services and metadata endpoints included — and the URL it
  names can carry data out. Remote reads are deliberately available while the
  server is unconfined, because cloud-native data is a feature; the claim that
  the network was closed anyway was the bug. README and SECURITY.md now state
  the price of that choice, and two tests pin both halves of it: without a
  workspace the read succeeds, and with one it is refused *before any request
  leaves*, asserted by counting requests at a loopback server instead of
  matching an error message. If you do not trust your `run_sql` input, set a
  workspace.
- **The multi-layer guard fails closed.** A container whose layer list could not
  be read looked like a single-layer file, and mechanical geometry repair would
  then have destroyed the other layers while recording success.
- **`execute_plan` reports `repairs` per step.** Geometry MapSmith rewrote was
  visible in a single-operation result and invisible at plan level.
- **Manifests record `EPSG:32632`, not 2.5 KB of PROJJSON**, when a GeoParquet
  input carries its CRS as an embedded projection object.
- **The example `docker-compose.yml` binds MinIO to loopback.** It published
  ports 9000/9001 on every interface with the documented development
  credentials.

### Changed

- **The provenance badge has three states instead of two.** "All critical checks
  passed" is vacuously true when no critical check ran — a `run_sql` manifest,
  for one — so an output whose only check had *failed* rendered with the same
  green tick as a verified buffer. `provenance_summary` now reports `verified`,
  `failed` or `unchecked`, with the reason, and the map panel renders all three.
  The `verified` boolean stays in the payload, computed correctly, so a client
  reading it gets a fix rather than a breaking change.
- **The README says when *not* to use MapSmith**, and the
  [benchmark results](docs/benchmarks.md) are linked from it — they were public
  for a day with nothing pointing at them.

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
