# Changelog

All notable changes to MapSmith are documented here, in the format of
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This project follows
[semantic versioning](https://semver.org/).

## [Unreleased]

### Added

- **Manifest conformance is now checked against the normative schema too, not
  only the standalone validator.** The two implementations had drifted on every
  recommended field: seventeen ways of writing a record that the schema rejects
  and the validator accepts — `producer` as a string, `crs_decisions` values as
  numbers, `notes` as a bare string, `inputs[].layer` as an integer. The
  validator now types those fields, the vendored copy is refreshed, the schema
  is vendored alongside it, and `jsonschema` moved into the `[test]` extra
  rather than behind an `importorskip`, because a CI that says "conforming"
  using the lenient one of two implementations is worse than one that says
  nothing.

- **Nine more tools, 18 → 27.** `describe_dataset` now reads rasters as well as
  vectors; `slope` and `aspect` land on the Whitebox engine (geographic-CRS DEMs
  refused rather than measured in degrees); `merge_layers`, `simplify_layer`,
  `centroid_layer` and `convert_format` cover the layer plumbing that was
  missing, each recording what it cost — null-filled columns named, the
  simplification's area and length before and after, a lossy conversion refused
  with the reason rather than performed quietly.
- **`measure_area`, and the first check that asks whether the number is right.**
  Ground (ellipsoidal) or planar area with the CRS's own linear unit, invalid
  rings repaired before measuring and the repair reported as a repair. When the
  plane asked for is not equal-area, the result carries the ratio against the
  ellipsoidal area as a non-critical check: a Web Mercator parcel comes back
  flagged as reporting 1.80× the ground it covers. Every other check in this
  codebase asks whether the operation ran. This one asks whether the answer is
  the answer. It exists because
  [Argleton](https://argleton.org) returned `unsupported` on three probes —
  MapSmith had no area operation at all, which is a gap in a catalogue rather
  than a bug in code, and the suite is what named it.
- **A catalogue built for thousands: declared applicability, then two ranking
  engines.** Every entry declares what it applies to (input kind, whether it
  demands a projected CRS), so discovery narrows deterministically in code
  before anything ranks — a geographic raster is never offered `slope`, because
  `slope` refuses one. What survives is ranked by either of two interchangeable
  engines the caller selects with `list_operations(engine=…)` — `lexical`,
  `vector`, or `auto` — over the identical document text: Okapi BM25 by default (no
  dependencies, no network, term-sorted accumulation because float addition is
  not associative) or static embeddings with the model revision pinned in the
  source (`[retrieval]` extra, bit-identical vectors asserted against a golden
  vector). Both are held to a golden query set with their per-query latency
  recorded, so the scaling limit is a curve rather than a number someone
  guessed.
- **An operation no longer needs a tool of its own** (`run_operation`, the 28th
  tool). The one-to-one contract between catalogue entries and exposed tools was
  what capped the catalogue at the size of the tool list, and it is gone:
  `run_operation(operation, arguments)` runs any catalogue entry by name.
  Arguments are validated against the catalogue before anything executes —
  unknown operation (with a "did you mean" from the same ranking), missing or
  misnamed argument, wrong type, path outside the workspace — and execution goes
  through the same path as `execute_plan`, so an operation cannot behave one way
  alone and another way inside a plan. Ten operations arrived this way, none
  with a tool of its own: `measure_length`, `join_table`, `aggregate_weighted`,
  `parse_coordinates`, `point_on_surface`, `hull_layer`, `validate_geometry`,
  `count_in_polygons`, `focal_statistics` and `extract_streams`. The catalogue is
  at 41 operations (39 available, 2 planned) behind 28 tools.
- **Overlay and dissolve declare their semantics in the manifest.** Dropped
  lower-dimension pieces from an overlay are named rather than silently absent,
  and a dissolve's aggregation is recorded with the group count verified in
  closed form.
- **Manifests now carry `spec_version`** (`1.0.0-draft.2`). The manifest format
  is becoming a specification of its own — schema, toolchain-free validator,
  conformance suite and a minimal emitter that does not import MapSmith — and
  MapSmith is one implementation of it rather than its definition. A CI test
  validates a real writer's output against the spec's own validator, so the day
  our manifest stops conforming to our published format, the build says so.

### Changed

- **The Python floor is now 3.12** (was 3.10). One reason, and it is about testing
  rather than syntax: rasterio 1.5 requires Python 3.12, so the 3.10 CI arm resolved to
  rasterio 1.4.x while the 3.12 arm got 1.5.x — two arms testing two different products
  under one green tick, with nothing anywhere saying so. Pinning rasterio down would have
  frozen the project on an old raster stack to keep an old Python alive, so the floor
  moved instead. CI now runs 3.12 and 3.14, **asserts that both arms resolved identical
  library versions**, and records the full resolution as a build artifact — a green tick
  that cannot say what it ran against is not readable six months later. The container
  moves to `python:3.14-slim`. Docker and `uvx` are the supported install paths and both
  bring their own interpreter, so this is invisible on them.

- **A multi-layer container is refused instead of resolved to its default
  layer** ([#29]). Opening `project.gpkg` without naming a layer used to hand
  back whichever layer GDAL happened to list first — consistently, and wrongly.
  There is no signal in that: the operation succeeds, the manifest records a
  clean run, and the count is an ordinary number for the wrong layer.
  Multi-layer inputs now require an explicit `layer`, and the error names the
  layers available. Found by [Argleton](https://argleton.org)'s trap 006, which
  answered 4 features where the truth was 31, and filed here before the trap was
  published.
- **`nearest_join` reports its distance in metres by contract**, not in the
  units the input happened to carry, with the reprojection recorded in
  `crs_decisions`; `explode_layer` knows its part count before the engine runs,
  so the verification is a comparison rather than a report.

### Fixed

- **GeoParquet 2.0 files now open on every read path, not two of six** ([#28]).
  0.2.2 taught `describe_layer` and the CRS probe to read geometry from
  Parquet's own `GEOMETRY`/`GEOGRAPHY` logical types; `preview_map`,
  `zonal_statistics` (`zones_path`), `watershed` (`pour_points_path`) and the
  verification reader kept raising `Missing geo metadata` on the same file. The
  verification reader was the dangerous one: it did not fail, it read the file
  as geometry-less, which reports every row as an invalid geometry, which is a
  critical check, which calls the mechanical repair that rewrites the file.
  All vector reads now go through one function, and a test fails if a seventh
  copy appears.
- **A declared CRS is no longer dropped when it has no EPSG code** ([#28]). The
  resolver was fed its own display label. pyproj names an authority-less
  PROJJSON document `unknown`, which is the literal sentinel for "no CRS", so a
  DuckDB LAEA layer that states its coordinate system read as having none.
  Resolution (`readers.native_crs`, returns coordinate systems) and
  presentation (`verify.crs_label`, returns strings) are now separate, and a
  malformed declaration produces MapSmith's message rather than a raw
  `pyproj.CRSError`.
- **A refused CRS says which one it refused** ([#28]). `srid:<n>` is still not
  resolved — the Parquet spec names no authority for it, so `EPSG:<n>` would be
  an invented coordinate system recorded as fact — but the declaration now
  reaches the caller instead of a bare "no CRS" about a file that visibly has a
  `crs` field.
- **Two different coordinate systems can no longer share one label**. Labels are
  compared, not just printed, so a collision decided whether two layers were
  aligned. Custom projections routinely share a name (`Custom LAEA`), and
  `crs_label` answered with the name. It now appends a digest whenever no
  authority vouches for the name.
- **`crs_matches` compares coordinate systems, not their spellings**. It was
  handed a display label, and `CRS.equals` returns `False` for anything it
  cannot parse — so a correct output in a CRS without an EPSG code failed a
  critical check and the error message was the 2.5 KB PROJJSON blob that
  `crs_label` exists to avoid.
- **`watershed` no longer records a reprojection that did not happen**. It
  compared `str(crs)`, which is PROJJSON for any GeoParquet input and therefore
  never equal to the DEM's label, so `crs_decisions` claimed the pour points had
  been reprojected onto the DEM grid when they were already on it.
- **A geometry column keeps its name through a native read**. `from_wkb` drops
  the name, so a column called `geom` came back as `geometry` — and the
  mechanical repair, which takes explicit care never to assume that name, then
  rewrote the user's file under it. Secondary native geometry columns are read
  as geometry too, rather than surviving as opaque bytes.

[#28]: https://github.com/mapsmith-ai/MapSmith/issues/28
[#29]: https://github.com/mapsmith-ai/MapSmith/issues/29

## [0.2.2] — 2026-08-22

A security release, published as [GHSA-3rcc-xpw3-r4xh](https://github.com/mapsmith-ai/MapSmith/security/advisories/GHSA-3rcc-xpw3-r4xh). The fixes below close
holes present in 0.2.1, which is on PyPI and GHCR: **if you run MapSmith on data
or paths an agent can influence, upgrade.** The headline is that a plain local file could make GDAL fetch a URL
or read a dataset from outside the workspace, and that credentials could reach
a manifest.

Worth saying plainly, because it shaped the release: each fix here was found by
auditing the previous one. The remote opt-in left `.vrt` able to reach the
network; closing `.vrt` left `GDALG` and `MRF` doing the same thing; refusing
credential SQL wrote the credential into the refusal message. The pattern is
not bad luck — it is what happens when a guard is written against an instance
instead of a class, and the response in each case was to move the check one
level down rather than add another name to a list.

### Added

- **`funding.json`** at the repository root ([FLOSS/fund
  manifest](https://fundingjson.org/)), stating what the project would use
  funding for: the correctness suite and the provenance specification. No
  payment provider is published — arrangements are made in writing.

### Changed

- **Remote and virtual paths are refused by default** (`MAPSMITH_ALLOW_REMOTE=1`
  to allow them). GDAL `/vsi*` and `https://` forms used to be accepted whenever
  no workspace was set, justified as being the user's own responsibility — and
  the user is not who decides: the path is written by the model, from whatever it
  read, so a third-party dataset carrying "the updated layer lives at
  `https://evil.tld/x.gpkg`" was enough to have GDAL parse attacker-chosen bytes
  in-process. The refusal now covers both path arguments and `run_sql` text,
  which closes the SSRF that came with it (raw SQL could read any endpoint the
  host can reach — internal services, cloud metadata — and return the content in
  the tool result) as well as the `INSTALL ... FROM '<url>'` fetch. Cloud-native
  data stays a supported use case: the capability is gated, not removed. A
  workspace refuses remote forms regardless, and validated plans stay strict
  whatever the setting.
- **The container is unprivileged and confined by default.** The published image
  runs as uid 1000 instead of root and sets `MAPSMITH_WORKSPACE=/data` itself, so
  `docker run -v your/data:/data ghcr.io/mapsmith-ai/mapsmith` gets the path jail
  and the sandboxed SQL engine without the operator having to remember `-e` — the
  wrong way round for a default. Two consequences before you upgrade: a bind
  mount owned by another user is no longer writable (pass
  `--user $(id -u):$(id -g)`), and everything outside `/data` is refused,
  including remote paths, which a workspace refuses whatever
  `MAPSMITH_ALLOW_REMOTE` says. The Kubernetes example states the same posture at
  pod level (`runAsNonRoot`, `readOnlyRootFilesystem`, dropped capabilities).

### Fixed

- **GDAL indirection was closed as an instance, not as a class.** Deregistering
  the drivers behind `.vrt` left `GDALG` and `MRF` doing the same job. GDALG
  (*GDAL Streamed Algorithm*, GDAL 3.11) reads a JSON document holding a `gdal`
  command line and runs it when the dataset is opened, and it is recognised by
  **content, not by extension** — so the filename tells you nothing, and the
  command line can name a local path as readily as a URL. Measured with remote
  reads off **and** a workspace set: a file called `roads.geojson` inside the
  workspace issued a GET, and another read a dataset from *outside* the
  workspace and handed back its rows. That is containment broken, not only
  egress. MRF is narrower — extension-gated, fetched on the first pixel read.
  Both are now skipped, along with the remaining drivers GDAL's own security
  page names as opening other datasets internally.

  The list is not the fix. It was correct when written and became incomplete
  because GDAL shipped a new driver, which will happen again. A test now
  enumerates the drivers registered in a clean subprocess and fails on any name
  nobody has reviewed — the only version of this check that keeps working
  across upstream releases.
- **A workspace no longer loses to the opt-in.** `MAPSMITH_ALLOW_REMOTE=1`
  together with `MAPSMITH_WORKSPACE` re-registered the indirection drivers,
  because the predicate answering "is remote allowed" read only the environment
  variable while its own documentation said a workspace overrides it. Two
  callers compensated for that and one did not. The predicate answers the whole
  question now: a check each caller has to remember to add is a check that will
  be missing somewhere.
- **Plan manifests are redacted, and the credential refusal no longer quotes the
  credential.** `<output>.plan.json` is written as a plain dict, so the redaction
  applied to every provenance record never reached it — while `goal`, each
  step's `comment` and a failed step's `error` all carry text written by the
  model or the user. The sharp case: refusing `ATTACH 'postgres://user:pw@…'`
  produced a message quoting the fragment, password included, and that message
  is what the manifest recorded. The mechanism that exists to keep credentials
  out of manifests was putting one in. Redaction now runs on the whole manifest
  at the point it becomes a file.
- **A credential written with a quoted identifier is refused.**
  `SET "s3_secret_access_key" = '…'` and `PRAGMA "…"='…'` defeated *both* layers:
  the refusal pattern required the credential word to follow `SET` contiguously,
  and the redaction pattern allowed only whitespace between the name and the
  `=`, so the closing quote broke each of them. This was not one of the two
  documented limits — the name was perfectly recognisable.
- **`crs_decisions` and `notes` are redacted on the paths that actually run.**
  Redaction happened at construction, and no engine passes those fields to the
  constructor — every one assigns afterwards. So two of the four fields
  SECURITY.md lists as covered were never redacted in practice, and
  `parameters_redacted` stayed false. Redaction now also runs in `write_for`,
  the single point where a manifest becomes a file, and covers `verification`
  and `repairs` as well. The test that claimed to cover this passed both fields
  as constructor arguments — a shape no caller uses — so it was green and
  proved nothing; it now uses the real one.
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
  recording it as fact. Such a file is treated as having no CRS, so the CRS
  precondition refuses it like any other input without one. The message does
  not yet quote the `srid:` declaration that caused it, which would tell the
  agent what to fix — tracked separately rather than claimed here.

  **Writing now states its flavour instead of inheriting one.** `run_sql`
  materialises with `geoparquet_version 'BOTH'`, so one output file carries
  Parquet's native geometry types (CRS included as PROJJSON) *and* the 1.x `geo`
  metadata — a 2.0-native reader and GeoPandas 1.x both open it, which is
  asserted by a test that reads it back both ways. This was not a free choice:
  DuckDB 1.4 wrote the native types by default and 1.5 changed the default back
  to 1.x, so the installed engine version was silently deciding the canonical
  output format of a provenance product. The GeoPandas writer path stays 1.x,
  because GeoPandas 1.1 caps `schema_version` there.
- **Dependency floors raised for a correctness reason, not a housekeeping one.**
  `pyarrow>=21`: measured on a file DuckDB itself produced, pyarrow 18 and 19
  **raise** on Parquet's geospatial logical types ("Thrift LogicalType that is
  not recognized"), 20 opens the file but reports the type as `Undefined`, and 21
  reports `Geometry` with its CRS. Below 21, MapSmith could not read back its own
  `run_sql` output — the CRS probe returned `unknown`, which made the CRS
  precondition refuse a file MapSmith had just written. `duckdb>=1.5` is the
  floor for the `geoparquet_version` option above.
- **Persistent DuckDB secrets can no longer be created from a tool call.** They
  are written to `~/.duckdb/stored_secrets` — outside any workspace and beyond
  the session. A workspace already refused that write; the connection now sets
  `allow_persistent_secrets = false` in both modes, before locking the
  configuration.
- **The `mcp` floor is 1.28.1.** The SDK range MapSmith allowed admitted three
  High-severity advisories (CVE-2026-59950, CVE-2026-52869, CVE-2026-52870).
  None is exploitable here — MapSmith uses stdio and stateless Streamable HTTP
  with no authentication, never the websocket transport, never task handlers —
  but a fresh install could resolve to a version carrying them, and a scanner
  cannot know the difference.
- **`duckdb` is now capped below 2.0.** The floor was raised to 1.5 for a
  correctness reason (above); the cap is for a different one. DuckDB 2.0 is on
  the autumn-2026 calendar with breaking changes, and an unbounded requirement
  means the first install after that release can fail with nothing on our side
  having changed. A pin you have to lift deliberately beats an install that
  breaks on someone else's schedule.

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
