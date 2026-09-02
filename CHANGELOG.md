# Changelog

All notable changes to MapSmith are documented here, in the format of
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). This project follows
[semantic versioning](https://semver.org/).

## [Unreleased]

Everything here is on `main` and not in a release. It is almost entirely
defects found in shipped code by reviews that ran *after* 0.4.0 went out, and
the shape they share is worth more than any one of them: **in every case a
guard existed and could not fail.**

### Fixed

- **A dataset on disk with no manifest, from one invisible character.** With a
  workspace set, writing to `out.parquet.` wrote `out.parquet` and *then*
  raised: Windows strips a trailing dot when it creates a file, so the data
  landed where the manifest was not. That is invariant 2 — provenance on every
  writer — broken by a character nobody sees in review. The trailing-space form
  did the same with a different exception, and `CON` did not merely fail: it
  created a file called CON in the workspace. All of these are refused now, on
  **every** platform rather than only on Windows, because a manifest is meant to
  travel and a path that names two different files on two operating systems is
  not one path.
- **A crash could write a manifest saying everything passed.** On the ~59 code
  paths that verify their inputs before running, `audit_on_failure` recorded its
  failure check only when there were *no* preconditions, so an engine crash
  produced a record made entirely of green checks — conforming, and misleading
  in the one way that matters. The failure check is appended now, and its text
  states that the other checks ran *before* the failure and claim nothing about
  the result.
- **The crash manifest could fail to be written, silently.** Making the crash
  record's *contents* honest (above) left its *existence* best-effort: the write
  sat inside `suppress(Exception)`, so the audit trail this path exists to save
  could vanish with nothing said. Section 3.1 of the manifest specification uses
  a MUST, and a MUST behind a blanket suppression is not one. Hashing the output
  is the only part of the write that reads a file the crashed engine may still
  hold open, so that failure now falls back to writing the record *without*
  `output` — valid, and merely uncheckable against bytes — and if even that
  fails the loss is attached to the exception the caller already receives.
- **A band-math expression could hang the host before a pixel was read.**
  `b1*0+9**9**9**9` passes a character whitelist and names a band, then asks
  CPython for an integer power with hundreds of millions of digits. The rule is
  not "no exponentiation" — squaring a band is ordinary — it is that the exponent
  must be a plain small constant, which is what separates `(b1-b2)**2` from a
  tower: `**` is right-associative, so a tower's outer exponent is itself an
  expression. Parsed with `ast`, for the same reason the SQL policy was.
- **Two credentials travelled in clear.** `x_api_key` was masked and
  `x-api-key` was not, because the vocabulary was written the way SQL spells
  names while HTTP spells them with hyphens. And Azure's shared-access signature
  is `sig`, absent while `X-Amz-Signature` was present — so the same signed URL
  was redacted from one cloud and printed from the other, on a URL form
  Microsoft's Planetary Computer hands out.
- **The refusal of ambiguously georeferenced rasters covered nine operations of
  twenty-five; it now covers twelve.** The three added are the ones that read
  the grid to place a coordinate — `sample_raster_at_points` above all, which
  answered 10.0 and 30.0 with no sidecar and 2.0 and 6.0 with a 40 m one.
- **`run_operation` logged the wrapper instead of the operation.** It recorded
  `"run_operation"`, which is not a catalogue name, so the row could never pair
  with the search that preceded it and came back `chose: null` — on the 46
  operations of 74 reachable only by name.
- **`run_sql` wrote its manifest and never enforced it**, so a failing critical
  check was recorded and not raised. Sixteen other hand-written writers
  open-code the sequence correctly; this one was the exception, and two tests
  now hold the line — one fails if any hand-written writer skips
  `verify.enforce`, the other ratchets their count downwards only.

### Changed

- **Guards that enumerate are now guards that derive.** The parametrised list
  behind the georeferencing refusal read one module; it reads all of
  `engines/` and fails if the field it derives from is renamed — otherwise it
  would have stopped guarding exactly when the work it protects got done. The
  same treatment went to the notebook-gallery check (which summed across
  notebooks, so one notebook satisfied it) and to the published-figure census,
  which now sweeps `funding.json` too.
- **`funding.json` says what has been delivered.** It claimed 16 tools and 336
  tests against 28 and more than 1500, and carried two *active* plans totalling
  €35,000 to build a trap suite and a provenance specification — both of which
  had shipped weeks earlier and are archived with DOIs. The plans are kept
  rather than deleted and marked inactive with what came of them: an estimate
  beside its actual cost is a stronger thing to show than an ask that ages into
  a lie.
- **`CLAUDE.md` no longer tells contributors something false.** It said writers
  go through `verify.audited` "so the audit-trail-first invariant cannot be
  bypassed". That was untrue for seventeen writers of fifty-seven.
- Documentation and site now quote the Argleton run that covers all
  twenty-nine families: **0.00 silent errors over 31 traps, nothing skipped**.

## [0.4.0] — 2026-08-31

### Added

- **The caller chooses the geoprocessing stack once, at the start, and it is
  never swapped underneath them.** `MAPSMITH_STACK` takes `opensource` (the
  default: GDAL, GeoPandas, DuckDB, Whitebox) or `esri`, which routes to ArcPy
  on a machine that has a licence for it. The rule that makes the choice worth
  making is what happens at the edges: when the chosen stack cannot do
  something, MapSmith says which of three things is true — the tool does not
  exist, this licence tier does not include it, or it would need an online
  service — and falls back only where the caller asked for a fallback, naming
  the substitution in the manifest. An engine quietly replaced by another is a
  result whose provenance record is true and whose number came from software
  nobody chose.
- **Citation metadata** (`CITATION.cff`). Every release from here on is
  archived and assigned a DOI, so the file is a release artifact: a test fails
  if its version is not the version being released, or if its release date is
  in the future.
- **A manifest can now say which configuration produced the numbers**
  (`environment`, section 3.8 of the manifest specification). The field existed
  in the published specification and in the schema, and nothing in MapSmith
  could produce it — a whole section describing something the reference
  implementation could not do.

  What made it concrete: a GeoTIFF and the `.aux.xml` beside it can declare
  different georeferencing, and GDAL prefers the sidecar by documented design,
  because that is how somebody overrides georeferencing they know to be wrong.
  Both readings are the library behaving exactly as written. On one fixture the
  same file gives 40,000 m² or 160,000 m² and an origin a hundred kilometres
  apart, and until now nothing MapSmith wrote could say which of the two it
  read. There is nothing upstream to fix and everything to state.

  So: `describe_dataset` reports both sources when a raster has two, and the
  nine operations that read a raster's grid directly refuse instead, naming
  both and saying how to choose. The terrain and sampling operations are not
  covered yet and the README says so rather than implying otherwise; the
  terrain engine stops on the same file for a different reason, because its own
  reading of the grid disagrees with GDAL's, and its message names neither the
  sidecar nor the way out. That split is the multi-layer refusal (#29)
  applied to a second axis — the format's default answers a question the caller
  never asked, and a manifest could not honestly say which data produced the
  numbers. Describing is different from computing: a file with two
  georeferencings is a thing to be told about.

  The field is filled where a manifest becomes a file rather than in
  `verify.audited`, and the difference matters: seventeen writers of fifty-seven
  build their record by hand and never reach `audited`, and they are
  concentrated in raster — exactly where georeferencing decides the numbers.
  Empty when there is nothing to say, because a field that appears on every
  operation is a field its reader learns to skip.
- **Redaction now reads dictionary keys, not only values.** Adding a field that
  holds environment variables exposed a gap that was never about that field:
  `{"AWS_SECRET_ACCESS_KEY": "AKIA…"}` passed through untouched, because the
  scanner looks for `name=value` *inside a string* and in a dictionary the name
  is the key. That was true of every dictionary a manifest carries. Bare `key`
  stays off the list on purpose — `sort_key` and `primary_key` are ordinary
  field names, and a mask that fires on those teaches its reader to distrust it.
- **`select_features` and `extract_layer`: two remedies MapSmith was already
  recommending without offering.** The mixed-geometry warning said *select the
  features the question is about*; the multi-layer refusal said *extract the
  layer you mean into its own dataset*. Both then handed over a `run_sql`
  incantation, which is a long way round for "keep the lines". Now they name an
  operation, and the hints were rewritten to do so — an error message that
  points at a longer route than the one that exists is a defect of its own,
  because it is the only thing a blocked caller reads.
  - `select_features` keeps features by geometry family (`line` keeps
    MultiLineString too), by exact geometry type, or by an attribute condition
    (`field_equals`, `field_in`, `field_between`, inclusive at both ends).
    Deliberately no expression language: that is `run_sql` with extra steps and
    its own way of going wrong. Filter values arriving as text against a numeric
    column are **converted and recorded**, and the conversion tries `int` before
    `float` because it has to: `float("9007199254740993")` is 9007199254740992,
    so going through float would return **the row next to** the one asked for —
    one feature kept, every check green, and a manifest naming a number nobody
    typed. OSM ids, BIGINT keys and cadastral references all live past 2^53.
    Five more inputs are refused rather than answered with an empty dataset,
    because an empty dataset with a complete manifest reads as a finding:
    `"high"` against an integer column, `"nan"` (which `float()` accepts and
    which equals nothing, including itself), `"inf"`, `field_equals` with no
    value, and a range whose minimum is above its maximum. Matching nothing for
    a reason that is not one of those is reported as a warning, with the hint
    that a typo and a genuinely empty result look identical.
  - A **family selection says what it could not keep**. `line` keeps
    MultiLineString because a MultiLineString is line-shaped — and by the same
    argument a GeometryCollection holding a polygon is a polygon to everybody
    except its type name, so dropping it without a word would be that care
    applied to half the problem. Types no family covers, and null geometries,
    are counted in a non-critical check that names them.
  - `extract_layer` copies one named layer out of a multi-layer container. The
    refusal it resolves (issue #29) stays exactly as strict; what changes is
    that the way out is now one call whose manifest names the layer, the
    container and the layers left behind.
- **A bounding box that spans the planet for data two degrees wide** is now
  reported with the sentence that makes it readable (`antimeridian.py`).
  RFC 7946 §3.1.9 splits a geometry at the antimeridian, so the bounds computed
  from its coordinates come out as the ordinary west-to-east form spanning the
  world; §5.2 defines the crossing form as the one with west greater than east.
  Nothing in a planar geometry library can produce the second from the first,
  so `describe_dataset` reported `(-180, -17.5, 180, -16.5)` for a Fijian survey
  zone — arithmetically correct, and the wrong answer to the question anybody
  asked. The plain box is still returned unchanged; when the data crosses, a
  `true_extent`, a width and a note saying what a filter on the plain box would
  select come with it. Distinguishing a real crossing from a dataset that
  genuinely covers the world needs a geometry probe, not arithmetic: a single
  global rectangle has only ±180 as longitudes and they wrap onto one value.
  Detection deliberately carries **no threshold on the plain span**: the first
  version required 350° before looking further, which only ever fires on data
  split exactly at ±180 — the well-formed case. Buoys at 170°E and 140°W span
  310° and were reported in silence. What replaces it is a rule with a meaning:
  a crossing layer reads narrower when the seam is treated as continuous *and*
  occupies less than half the world going the short way, so points scattered at
  -179, -90, 0, 90 and 179 are global data rather than a crossing. Only
  degree-based geographic systems are considered — EPSG:4807 is geographic in
  grads, where wrapping at 360 means nothing.

- **`locate_extreme_cell`** answers where the lowest or highest value of a
  raster is, as a coordinate. No operation could be asked that before, which is
  why MapSmith reported `unsupported` twice on Argleton's `grid-registration`
  family — the one whose whole question is whether a system knows where its own
  cells are. Nodata is excluded rather than competing for the minimum, and a tie
  is reported rather than broken in silence: two cells at the same extreme is a
  plateau, a flat pond or a saturated sensor, and naming one of them is a
  confident answer to a question about a region.

- **Ten more operations, and this time the gap they fill is a shape rather
  than a subject.** Of sixty-one catalogue entries, three produced an `answer`
  and none of them took a vector layer — so the commonest question in GIS, *how
  much land is there*, had no operation for anybody holding parcels, and a
  search declaring it came back empty. Four operations close that:
  `summarize_field` (totals, averages and extremes of an attribute, per group if
  asked), `spatial_autocorrelation` (global Moran's I — whether the map holds a
  pattern at all, which is the question to ask before `hot_spots` shows where),
  `nearest_neighbour_index` (Clark-Evans R, with the study area treated as the
  decision it is rather than taken silently from the points' own extent), and
  `compare_layers` (what actually changed between two versions of a delivery).
  None of them writes a file, so none carries a manifest: there is no artefact
  to attach one to, and inventing a file to have something to sign would be
  worse than the gap.

  Three answer requests from the discovery benchmark that neither labeller could
  place. `contour_lines` turns a surface into isolines. `least_cost_path` routes
  across a cost raster where `network_shortest_path` needs roads — charging per
  unit of DISTANCE, so a diagonal step costs sqrt(2) and not 1, which is the
  difference between a route a person would walk and a staircase.
  `transform_by_control_points` puts a survey on real ground by fitting a
  transform from points known in both systems: *"my traverse is sitting in the
  middle of the river"* has an answer, and the answer that matters is the
  residual, which is in the manifest per control point with the worst one named.
  An exactly determined fit says in words that its zero residual is evidence of
  nothing.

  And three that existed only inside other operations, which means they existed
  only for whoever already knew where to look: `snap_layer`, `points_along_lines`
  (chainage, what `elevation_profile` does before it reads a surface) and
  `line_intersections`.

  All ten are catalogue-only. The exposed tool list stays at 28, because that is
  the count with a ceiling and capability is the count without one.

- **MapSmith can record its own discovery cases, and deliberately does not
  learn from them.** Set `MAPSMITH_DISCOVERY_LOG=<path>` and every catalogue
  search is written as one JSON line together with the operation run after it:
  the query, the facets declared, which engine ranked it, every candidate
  delivered, and where in that list the chosen one sat.
  `benchmarks/log_to_cases.py` turns those lines into rows shaped like the
  discovery benchmark and flags the two worth reading — a choice the ranking did
  not put first, and a search nothing followed.

  The figures MapSmith publishes rest on 155 requests written by two language
  models, which is the best set obtainable without users and is not what users
  ask. This closes that gap without closing a feedback loop: a ranker trained on
  what callers pick learns from an ordering it produced, and the pinned model
  revision is what makes a search reproducible a year later. So the log produces
  benchmark rows for a person to accept or discard, and the improvement lands in
  the catalogue text as a reviewable diff. Off unless the variable is set; holds
  queries and operation names but never dataset paths or arguments; confined by
  `MAPSMITH_WORKSPACE` like any other path MapSmith writes; never leaves the
  machine.

- **Ten operations, chosen by measurement rather than by taste.** The discovery
  benchmark holds 155 requests written by other model families; twenty-one of
  them were marked `none` by BOTH independent labellers, meaning the catalogue
  could not serve them at all. Grouped, those twenty-one are six families, and
  these ten cover four of them:

  `sample_raster_at_points`, `elevation_profile` and `line_of_sight` read a
  surface at a place, along a line, and between two positions. Every one counts
  the positions it could not read and puts that count in the manifest, because a
  null that reads as a value is how a nodata of -9999 ends up in an average.
  `line_of_sight` requires an explicit answer on earth curvature: over 30 km the
  planet drops 62 m net of refraction, and there is no way to guess whether the
  caller has a rooftop survey or a radio link.

  `network_shortest_path` and `service_area` route over a line network the caller
  supplies. The failure they are built around is not a wrong number, it is a
  disconnected graph: two street segments a millimetre apart are one junction on
  the ground and two nodes in a naive build, so the route detours or is reported
  impossible and nothing raises. `tolerance` therefore has no default, and every
  manifest carries the component count, the merged-endpoint count and how far
  each end snapped to the network. `service_area` cuts the last segment where the
  budget runs out — a ten-minute walk ends where the walk ends, not at the next
  junction — and it is a network operation rather than a buffer because a circle
  includes the far bank of a bridgeless river and excludes the house 900 m away
  along a straight road.

  `viewshed` maps what a set of stations can see. **Its output is a COUNT of
  stations, not a 0/1 flag**, which contradicts Whitebox's own documentation:
  the help says "a Boolean raster, containing 1's and 0's", and measured on 2.0.6
  with two stations on flat ground every cell comes back 2.0. Second time this
  library's prose has described the reverse of what its code does, after the D8
  pointer table.

  `hot_spots`, `smooth_rates`, `aggregate_to_threshold` and `thin_points` are for
  the moment before somebody publishes a map of noise. Getis-Ord Gi* with a
  Benjamini-Hochberg correction, because at 0.05 over 300 districts about fifteen
  hot spots appear from nothing and they will be somewhere. Empirical-Bayes rates,
  because a choropleth of raw rates over small populations is a map of population
  size. Disclosure-control aggregation that refuses rather than publishing the one
  island it could not merge. And deterministic point thinning that says in the
  manifest that it removed data.

- **A dashboard for the whole project** (`benchmarks/dashboard.py`), written
  as one self-contained HTML file: no CDN, no fonts, no analytics, works with
  the network off. Six panels — what exists, whether every operation can
  actually be found, the retrieval figures and the degradation curve, Argleton's
  traps and what each engine does with them, the open questions answered by
  clicking, and a trend appended on each generation.

  It exists because the numbers were spread across a report nobody reruns, a
  test that prints a curve nobody reads, and a suite in another repository. The
  tuning panel is the point: an operation nothing reaches does not exist, and
  this says which ones, at what rank, with words alone and with the facets a
  caller would declare.

  Two things it caught immediately, both about itself. It drew ten working
  operations as unreachable by collapsing "the search declined because the two
  rankers shared nothing" into "not found" — three outcomes, one label. And the
  MapSmith adapter appeared as `adapters.mapsmith:Adapter` because the label was
  read from an import path. With those apart: 51 of 61 operations found by words
  alone, 55 in the top three once facets are declared, none unreachable.

### Fixed

- **A DEM whose rows run south to north no longer produces a slope eight times
  too steep.** A GeoTIFF's geotransform may have a positive fifth element,
  meaning row 0 is the southern edge — NetCDF, GRIB and HDF index latitude
  upwards, so a straight conversion produces one. whitebox-workflows cannot
  express that and does not say so: it discards the georeferencing and reads the
  grid as unit cells at the origin. MapSmith answered **45 degrees where the
  ground is 5.71**, wrote the output raster at the origin at a tenth of the
  site's size, and **passed all five of its own checks** — `crs_matches` among
  them, because the coordinate system survived and only the geotransform did
  not.

  Such a file is now rewritten north-up before the engine sees it, the same
  mechanism already used for the TIFF predictor, and the rewrite is disclosed in
  the manifest. Behind that is the general guard: MapSmith now compares the
  engine's idea of the grid with GDAL's and **refuses** if they differ for any
  other reason, because a number computed on a grid nobody can reconcile carries
  a correct-looking CRS on top of it and nothing downstream can tell.

  Found by [Argleton](https://argleton.org) trap 026.

- **Measuring a layer that holds more than one kind of geometry now says so.**
  A GeoPackage layer may declare its type as GEOMETRY and hold whatever it likes;
  only the shapefile enforced one type per file, which is why mixed layers
  arrive exactly when data is converted out of shapefiles. `length` on a polygon
  is its perimeter — true, and the right answer to a question nobody asked — so
  a pipe network with a treatment plant in the same layer totalled **3000 m
  where the pipe is 2000**.

  `measure_length` and `measure_area` now carry a non-critical check naming the
  geometry types present, with the remedy. It is not an error: measuring a mixed
  layer is a legitimate request and MapSmith cannot know which features the
  question was about. What it can do is refuse to let the total arrive without
  the sentence — especially here, where **every individual row is still
  correct**, so a spot check of the data confirms the data.

  Found by Argleton trap 027.

- **MapSmith now reads `AREA_OR_POINT`, everywhere at once.** GeoTIFF records
  which of two conventions a grid uses — `RasterPixelIsArea`, where a value
  describes the cell it fills, and `RasterPixelIsPoint`, where it is a sample at
  a grid node — and they differ by half a pixel. Every USGS elevation product is
  point-registered. **No line of MapSmith read the tag.** Every place that
  turned a cell index into a coordinate did what `rasterio.xy` does, which is to
  answer as if the file were area-registered: half a cell, 15 m on a 30 m DEM,
  systematic, and with nothing in any output to say so.

  That is the shape of #28 again — "open a vector file" as six copies of one
  decision, four of them missing a branch — so the fix has the same shape as its
  fix. `mapsmith/grid.py` is now the only place that decides where a value sits,
  a test fails if a second copy appears, and every operation that converts
  between cells and coordinates records the registration in its manifest,
  including the ordinary case: a manifest that mentions the convention only when
  it is unusual leaves a reader unable to tell *area* from *nobody looked*.

  What changed behind that: sampling reads at the nodes on a point-registered
  grid, so `sample_raster_at_points`, `elevation_profile` and `line_of_sight`
  stop being half a cell off; `least_cost_path` starts, routes and ends on the
  right cells; `contour_lines` applies its half-cell correction in the direction
  the registration calls for, where an unconditional one put a USGS DEM's
  contours a **whole** cell out; `zonal_statistics` offsets the zones for the
  coverage computation so each cell is weighted around its own sample, and hands
  back the caller's own geometry; and every raster MapSmith writes carries the
  input's registration forward, where `profile.copy()` had been silently
  converting point to area on the way out.

  Found by building [Argleton](https://argleton.org) trap 024, which measures
  exactly this and which MapSmith could not attempt. It can now, and it answers
  both halves of the pair — the trap at 412090 and its clean twin at 412105,
  fifteen metres apart, from files that differ in one metadata tag.

- **Whitebox places contour vertices half a cell from where they belong, and
  MapSmith now corrects it.** Measured on the installed 2.0.6 with a planar ramp:
  the contour for height 3 came back on the WEST EDGE of the column holding 3
  rather than at its centre. On a 30 m DEM that is 15 m of horizontal error on
  every contour — plausible, well-formed and wrong, which is the third time this
  library's behaviour has diverged from its description here after the TIFF
  predictor and the D8 pointer table.

  The correction is verified rather than trusted: the DEM is read back at the
  finished vertices and the elevation must equal the height each line claims, so
  a future library version that fixes its own registration fails a critical
  check instead of being silently double-corrected. `contour_lines` also ships
  with Whitebox's default smoothing filter OFF, because a contour whose vertices
  have been moved to look better no longer passes through the elevation it
  names — smoothing is available, recorded in the manifest, and demotes that
  check to non-critical, because then the output is a drawing rather than a
  measurement.

- **A search whose facets leave nothing now says which declaration did it.**
  Zero surviving candidates fell into the hand-over branch and came back as
  `status: "choose"` with an empty list and the sentence "0 operations survive
  what you declared, which is few enough to read" — nonsense as prose, and read
  by an agent as *MapSmith cannot do this*. It was false in the case that found
  it: "how much land is in each of these parcels" with `produces="answer"` left
  nothing, while `measure_area` computes exactly that and declares
  `dataset:vector` because it writes the areas into a column. That case is now
  `status: "none_apply"`, listing each declaration with the number of operations
  that would survive without it, smallest first. Arithmetic, not ranking. Found
  by the discovery log above on its first recorded session.

### Changed

- **`dataset_inputs` is a new facet, and a new REQUIRED field of the published
  catalogue-entry specification.** How many datasets the caller is holding: 0, 1
  or 2. It exists because the ten operations above broke discovery and it is the
  thing that fixed it — measured, on the 118 independent requests: the surviving
  set for the commonest facet combination went from 26 candidates to 34, over the
  threshold at which the whole set is handed over, and `delivered` fell from 100%
  to 45% with found@3 from 48% to 36%. With arity declared the median set is 9,
  found@3 is 60% and delivered is back to 100%.

  It is the right *kind* of facet, which is the part that generalises: a fact
  about the caller's own situation rather than a guess about our taxonomy, and
  derivable from each operation's signature so
  `test_the_declared_arity_matches_the_binding` checks the declaration against
  the code instead of trusting it.

### Security

- **A statement that says `INSTALL` or `LOAD` is now refused in both modes, and
  this is a breaking change for SQL that names an extension.** Until 0.4.0 only
  the *implicit* forms were off, and an audit before this tag used `run_sql` in
  the default mode to install DuckDB's `aws` extension and read the host's real
  cloud credentials back through a tool result. An `INSTALL` is an HTTPS fetch of
  a native binary executed in the server's process, on a statement written by a
  model, so it is not a thing to do by default in either mode.

  Extensions already loaded keep working, `spatial` included: MapSmith loads it
  through the Python API before the configuration is locked, so no statement has
  to ask for it. **What breaks** is SQL that says `LOAD spatial` or acquires
  another extension. To allow specific ones, name them where the agent cannot
  reach: `MAPSMITH_ALLOW_EXTENSIONS=postgres,azure` in the environment of the
  process that starts the server — named extensions rather than a switch,
  because "allow everything" is how a geoprocessing server ends up holding a
  credential reader.

  [SECURITY.md](SECURITY.md) carries the account, including why none of the four
  layers that already existed saw it, and the fact that this is the second
  promise in that file the code contradicted. Both were found by auditing before
  a release rather than reported from outside, and both are recorded rather than
  quietly corrected.

## [0.3.0] — 2026-08-29

Eleven operations, a catalogue that hands over a set instead of a ranking, and a
published specification for how an entry has to be written. Two behaviour changes
are in `Changed` and one of them affects anybody passing `category=`.

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

- **Ten more tools, 18 → 28.** `describe_dataset` now reads rasters as well as
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
  `vector`, or `auto` — over the identical document text: Okapi BM25 (no
  dependencies, no network, term-sorted accumulation because float addition is
  not associative) or static embeddings with the model revision pinned in the
  source (bit-identical vectors asserted against a golden vector). **The
  embedding engine is the default and a hard dependency now**, because the
  measurement said so and a default behind an extra is not a default — with two
  exceptions that keep the network promise: under `MAPSMITH_WORKSPACE` the model
  is used only if already cached, and any machine that cannot load it falls back
  to BM25 with `engine: "lexical"` in the answer. The container ships the
  weights. Both are held to a golden query set with their per-query latency
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
  at 74 operations (72 available, 2 planned) behind 28 tools.
- **Overlay and dissolve declare their semantics in the manifest.** Dropped
  lower-dimension pieces from an overlay are named rather than silently absent,
  and a dissolve's aggregation is recorded with the group count verified in
  closed form.
- **Manifests now carry `spec_version`** (`1.0.0-draft.3`). The manifest format
  is becoming a specification of its own — schema, toolchain-free validator,
  conformance suite and a minimal emitter that does not import MapSmith — and
  MapSmith is one implementation of it rather than its definition. A CI test
  validates a real writer's output against the spec's own validator, so the day
  our manifest stops conforming to our published format, the build says so.

### Fixed before release

The pre-release review found five defects in code that had never been published.
They are listed because the point of reviewing before a tag is that the fixes cost
a morning instead of an advisory, and because two of them contradicted promises
this project makes in writing.

- **A list of paths escaped the workspace jail through `run_operation` and
  `execute_plan`.** `merge_layers` takes `input_paths`, and the plan validator
  checked only the arguments a binding names as inputs, skipping anything that is
  not a string — so a list of paths was invisible to it twice over. The dedicated
  `merge_layers` tool refused a file outside `MAPSMITH_WORKSPACE`; the generic
  path read it and wrote it INSIDE the workspace, where the next
  `describe_dataset` hands it to the model. `validate_plan` reported that step as
  `valid: true` with no errors, and the same gap let a `/vsicurl/` path reach the
  network from a workspace-confined server, which `SECURITY.md` defines as a
  vulnerability.

  Neither operation exists in any published version, so no advisory is owed and
  no released artifact is affected.

  The fix is one field on the binding. What stops it recurring is
  `tests/test_path_containment.py`: it enumerates every catalog parameter whose
  name carries a path and fails when a binding does not cover it, then points
  every path argument of every operation outside the workspace and at the
  network and requires the validator to object. The defect was not the missing
  entry — it was that a hand-maintained enumeration of a growing set is wrong
  somewhere between one addition and the next.

- **The new default catalog search reached huggingface.co from a
  workspace-confined server.** Making the embedding engine the default put a
  first-use model download on `list_operations`, the first tool an agent calls,
  in the mode `SECURITY.md` promises makes no requests. Under a workspace the
  model is now used only if it is already cached; otherwise discovery falls back
  to BM25 and says `engine: "lexical"`. The container image ships the weights, so
  the supported path keeps the better default, and `retrieval.warm_cache()` fills
  the cache deliberately for anyone else.

- **`resample_raster` and `reproject_raster` delivered a cell size other than the
  one requested, and said otherwise.** Both derived the grid from the extent —
  `round(extent / resolution)` cells across the same ground — so 30 m asked of a
  100 m extent arrived as 33.33 m, and reprojection produced cells that were not
  even square. The manifest recorded `"resolution": 30.0`, and a check named
  `x-mapsmith:shape_matches_resolution` passed, because it compared the shape on
  disk to the shape we had computed rather than to the resolution in its own
  name. An 11% cell error is a 23% area error for anyone multiplying by cell
  size.

  The grid is now anchored to the requested cell size and the extent grows
  outward to the next whole cell, which is what `gdalwarp -tr` does and what a
  caller means. A new check, `x-mapsmith:cell_size_is_what_was_asked`, compares
  the delivered transform to the request, and a note records when the output
  covers more ground than the input. This one is worth naming plainly: a
  well-formed, confidently reported, wrong number under a green tick is the
  failure this project measures in other people's systems.

- **`idw_interpolation` accepted a geographic CRS.** IDW weights every sample by
  distance, so in degrees at 41°N — where a degree of longitude covers 0.75 of
  the ground a degree of latitude does — the weighting is anisotropic by a third
  and the surface is stretched east-west, with all checks green and nothing in
  the output to show for it. It now refuses, names the input when the input is
  the one without a CRS, records that CRS in the manifest, and declares
  `requires_projected_crs: true` so the applicability filter stops offering it to
  a caller who honestly says their data is in degrees.

- **`voronoi_polygons` met duplicate points with a raw GEOS exception.** Its
  precondition said "at least 2 distinct points" and only ever counted them.
  Repeated coordinates are ordinary — two sensors at one address — and now get a
  refusal that names the coordinate and says why the cell is undefined, rather
  than `Multiple input coordinates in cell at 0 0`.

### Changed

- **`list_operations` returns a set to choose from, not a ranking — and
  `category` no longer removes anything.** Both are behaviour changes for an
  existing caller, and the second one changes results for anybody passing
  `category=`.

  Below thirty surviving operations the response is now a single entry with
  `status: "choose"` carrying every candidate, each with the sentence that
  separates it from its neighbours, plus a statement that the order is a hint.
  Above thirty it is the ranked list it always was. Read either shape with
  `catalog.entries(result)`; a caller that indexes `result[0]["name"]` will now
  read the envelope instead of an operation.

  Why: measured over the 118 requests in `tests/data/discovery_queries.json`,
  written by two other model families, our ranking puts the right operation in
  the top three 48% of the time and a model handed the same candidates and asked
  to choose gets its first pick right 69% — while the two labellers who produced
  the ground truth agree with each other 70%. The ceiling is the point. Where two
  competent labellers disagree three times in ten there is no single right answer
  to rank toward, so the honest response shows the alternatives. Both labellers
  are language models; no GIS analyst has tried it, so these are agreement figures
  and not accuracy, and the pages say so.

  `category` was a hard filter and is now an ordering. It is the only facet a
  caller cannot read off their own data — input kind and projected-CRS are facts
  about what they hold, `produces` is what they want back, the family is a guess
  about our taxonomy. As a filter it removed six candidates out of twenty-one and,
  on a wrong guess, removed the right operation with no error at all, leaving a
  confident answer assembled from neighbours; every request in that set has 4.4
  plausible families. `catalog.applicable(category=...)` still cuts hard for a
  caller who means it.

  Engine disagreement below the threshold is no longer a refusal: it arrives as
  `order_is_weak` on the delivered set. Refusing made sense while the search was
  deciding.

- **Two published discovery numbers were wrong and are corrected in place.** The
  facet ablation and the found@3 figures had been measured on queries written by
  whoever wrote the catalogue, which measures word overlap dressed as retrieval;
  and "the facets leave sixteen candidates at 800 operations" was true but
  produced almost entirely by `category`, the facet that must not filter. The
  guarantee holds at fifty-one operations, not at eight hundred. Both corrections
  are on the README, the site and `docs/catalog-entry-spec.md` rather than
  removed, and `benchmarks/discovery_report.py` now recomputes every figure from
  files in the repository so the pages can be checked instead of believed.

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
