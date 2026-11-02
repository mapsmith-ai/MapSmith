# calculate_field design

Date: 1 November 2026
Status: proposed for approval

## Intent

Add a typed, discoverable way to create or replace a vector attribute from a deterministic expression. This is the most frequent missing capability found in the public training-material audit (14 cases) and currently requires general `run_sql`.

The operation is for callers who want a new column or a replacement column, not arbitrary SQL. It preserves every input feature and its geometry, changes only the declared attribute, and records the complete expression and lineage.

## Goals

- Evaluate arithmetic, comparisons, boolean logic, `CASE`, `COALESCE`, and `NULLIF` over numeric and boolean fields.
- Create a new field or replace an existing non-geometry field explicitly.
- Preserve row count, CRS, geometries, and the replaced field's position.
- Reject unsafe, unsupported, ambiguous, or non-deterministic expressions before scanning the relation.
- Produce GeoParquet with complete provenance and deterministic verification.
- Work through the existing `run_operation` and typed-plan surfaces without adding another MCP tool.

## Non-goals

- Text, date, geometry, aggregate, window, or user-defined functions.
- Subqueries, joins, grouping, filtering, sorting, CTEs, multiple statements, or file readers inside expressions.
- Arbitrary Python, SQL fragments, or QGIS expression compatibility.
- A new general expression language implemented in Python.

## Anti-goals and failure modes

- Silently overwriting an existing column or the active geometry column.
- Implicitly accepting a reader such as `read_blob`, `random`, `sum`, a scalar subquery, or a window expression.
- Treating a syntactically valid text expression as numeric or boolean work through implicit casting.
- Delivering `NaN` or infinities as ordinary calculated values.
- Losing the original column position during replacement.
- Emitting a manifest that names `run_sql` rather than `calculate_field` or omits the actual input.

## Constraints

- DuckDB 1.5.x and the existing sandbox, SQL parser, provenance, verification, catalog, and plan bindings.
- No new dependency and no new MCP tool.
- Every expression is untrusted input and fails closed.
- The engine and verification order remains: preconditions, execution, verification, manifest, enforcement.
- Public artifacts and tests use English; numeric expectations are closed form.

## Approaches considered

### DuckDB with a restricted AST (chosen)

Build the relation and alias programmatically, parse the complete statement with `json_serialize_sql`, and accept only the approved expression nodes. DuckDB evaluates the values; MapSmith controls the contract.

This reuses the existing engine, path confinement, GeoParquet writer, and lineage. The new work is a narrow expression policy and an operation-specific writer.

### Python/GeoPandas evaluator (rejected)

A Python AST would avoid SQL construction but would require new parsing, null propagation, type coercion, and numeric semantics. That is more new behavior for no operational benefit.

### General `run_sql` only (rejected)

This adds no code, but leaves the frequent capability undiscoverable and gives callers no safe typed contract.

## Operation contract

Name: `calculate_field`

Arguments:

- `input_path: str` — required; one vector dataset with a CRS.
- `output_path: str` — required; GeoParquet output.
- `field_name: str` — required; destination attribute, non-empty.
- `expression: str` — required; restricted expression over input columns.
- `on_existing: str = "error"` — `error` or `replace`.

Creation appends `field_name` and refuses a collision. Replacement requires the field to exist and uses `SELECT * REPLACE`, preserving its original position and the source column's spelling. Destination matching and column-reference resolution are ASCII case-insensitive; ambiguous names are refused. No geometry column may be created or replaced. Multi-layer containers require an explicit layer choice through the existing reader policy.

The result includes the output path, feature count, field name, provenance path, and verification status. The operation declares vector output with the input geometry and `same_as_input` cardinality.

## Expression policy

Parse the complete generated statement and validate both its shape and the expression recursively. Limit expression length, AST node count, and depth.

Allowed:

- Unqualified input column references that resolve uniquely in the source schema.
- Numeric, boolean, and null constants, including DuckDB's canonical boolean casts.
- Unary and binary `+`, `-`, `*`, `/`, `//`, `%` with correct arity.
- Ordinary comparisons, `BETWEEN`, null-safe comparisons, `AND`, `OR`, and `NOT`.
- `CASE`, including DuckDB's rewrites of simple `CASE` and `IF`.
- `COALESCE` as `OPERATOR_COALESCE` and `IFNULL` after normalization.
- `NULLIF` as the sole named function, with exactly two arguments.

Rejected by default:

- Every other function, explicit cast, schema qualifier, star, list/struct access, parameter, lambda, subquery, aggregate, or window node.
- Text, date, blob, list, geometry, and other non-numeric/non-boolean constants or referenced columns.
- Any statement shape other than the exact creation or replacement `SELECT` constructed by MapSmith.

Boolean literals require special handling because DuckDB serializes them as boolean casts of the strings `t` and `f`; this is the only accepted cast shape. `COALESCE` is an operator in the AST, while `NULLIF` is a function.

Column references are checked against the source schema before execution, so textual columns cannot pass through implicit combination casting. Division semantics are documented in the catalog: `/` is floating-point division, `//` is integer division on integral types and floating-point division on `DOUBLE`, and `NULLIF` can be used to turn a zero denominator into null.

## Engine and provenance

Implement in `duckdb_engine.py` or a small helper module that reuses its connection and SQL policies. Do not call `run_sql` and relabel its result: `calculate_field` gets its own `ProvenanceRecord` with `operation="calculate_field"`.

Record:

- the actual input with digest and `argument="input_path"`;
- `field_name`, the exact `expression`, and `on_existing`;
- source and analysis CRS, with the reason that calculation does not transform coordinates;
- DuckDB and version;
- source and output feature counts and deterministic checks.

Creation uses `SELECT *, <expression> AS <field>`. Replacement uses `SELECT * REPLACE (<expression> AS <field>)`. Identifiers are double-quoted with embedded quotes doubled. Paths use the existing relation quoting and policies.

## Verification

Before execution, validate the input path, CRS, source schema, referenced columns, destination collision, and expression. Count the input features through DuckDB rather than loading the layer for inspection.

After writing, verify:

- output exists with the source feature count;
- same CRS and exact geometry types;
- destination field exists once, at the required position;
- all other attributes and geometries are unchanged;
- calculated values have no `NaN` or infinity;
- GeoParquet metadata preserve the active geometry, its CRS, and any secondary geometry columns;
- input and output lineage match.

No row-order guarantee is made: the supported input formats do not expose a universal order key, and an unordered SQL `SELECT` cannot provide one.

Use `verify.audited(..., repair=False)` so invalid source geometries are not modified. A dedicated zero-row path preserves the complete source schema and geospatial metadata, including an active geometry not named `geometry`, rather than relying on the single-origin `run_sql` empty-output helper. Write the manifest before enforcement so failed critical checks, including non-finite calculated values, leave an audit trail.

## Catalog and plan integration

Add one `available` catalog entry exposed through `run_operation`, with:

- `inputs=["vector"]`, `produces="dataset:vector"`, `dataset_inputs=1`;
- no projected-CRS requirement and no geometry restriction;
- summary, distinctions from `select_features` and `run_sql`, and caller-oriented phrasings such as adding a column, deriving an attribute, and calculating a value from existing fields;
- a first example whose goal is direct discovery of attribute calculation.

Add a binding with `input_args=("input_path",)`, `output_arg="output_path"`, `crs_effect=("same_as", "input_path")`, vector output, and `output_cardinality="same_as_input"`. Declare the two `on_existing` choices and the `.parquet` extension. Add `field_name` to the executor's echoed result keys. Do not add a dedicated server tool.

Add a fixture to `tests/test_verify.py::_spec_fixtures` so the emitted manifest is exercised by the generic conformance test rather than silently skipped.

## Tests

Use `tests/test_calculate_field.py` with small vector fixtures whose results can be calculated by hand:

1. Arithmetic and column position, including `/` versus `//`.
2. Comparisons, boolean logic, `CASE`, `BETWEEN`, and null-safe comparisons.
3. `COALESCE`, `IFNULL`, and `NULLIF`, including null and zero denominators.
4. Replacement preserving position and source spelling; creation/replacement collision behavior and ASCII case handling.
5. Rejection of geometry overwrite, missing CRS, unknown or ambiguous columns, and non-numeric columns.
6. Rejection of `random`, `read_blob`, `sum`, window functions, subqueries, explicit casts, qualified references, multiple statements, and non-numeric constants before scanning the relation.
7. `NaN` and infinity rejected with a manifest retained.
8. Closed-form provenance, output CRS, feature count, typed-plan reference resolution, and generic manifest conformance.
9. Empty inputs, an active geometry named `geom`, primary geometry not first, secondary geometry columns, and non-numeric attributes preserved alongside the calculated one.
10. Characterization tests for DuckDB's actual AST forms, especially boolean casts, `CASE`, and `COALESCE`.

Run the discovery, plan, datum, DuckDB, verification, and full suites plus Ruff. Review the final diff independently before any integration.

## Builder decisions still open

No further product decision is required for the first version. Implementation details may choose helper boundaries inside the DuckDB engine, but they must not widen the expression or operation contracts above.
