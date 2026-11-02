# calculate_field Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a typed operation that creates or replaces a vector attribute from a restricted deterministic expression, with complete provenance and verification.

**Architecture:** Build a focused `field_calculation` engine helper around DuckDB's existing sandbox and `json_serialize_sql`. Validate a complete generated SELECT and its source schema before evaluation, then write GeoParquet through an operation-specific provenance record. Expose the operation through `run_operation` and typed plans, not a new MCP tool.

**Tech Stack:** Python 3.12+, DuckDB 1.5.x, GeoPandas/Shapely, PyArrow, Pydantic plans, pytest, Ruff.

**Spec:** `docs/superpowers/specs/2026-11-01-calculate-field-design.md`

## Global Constraints

- No new dependency, MCP tool, text/date/geometry functions, arbitrary SQL, or Python evaluation.
- Fail closed on unknown AST shapes and ambiguous fields.
- Preserve CRS, feature count, attributes, geometries, and replacement-column position; no row-order guarantee.
- Use `verify.audited(..., repair=False)` and write the manifest before enforcement.
- Public code and documentation are in English.
- Use `PYTHONPATH=src` in this worktree so tests do not import the main checkout.
- Do not commit, push, tag, or release without explicit user approval.

## Review Focus

- Empty inputs and active/secondary geometries with non-default names must retain schema and GeoParquet metadata; covered by Task 2.
- DuckDB's normalized `CASE`, `COALESCE`, and boolean-cast forms must be accepted without admitting arbitrary functions; covered by Task 1.
- Existing-field collisions and ASCII case ambiguity must be rejected deterministically; covered by Tasks 1-2.
- `NaN` and infinity must fail after writing a retained manifest; covered by Task 2.
- Catalog discovery and typed-plan execution must reach the real operation, not a validation-only stub; covered by Task 3.

---

### Task 1: Restricted expression and statement policy

**Files:**
- Create: `src/mapsmith/engines/field_calculation.py`
- Test: `tests/test_calculate_field.py`

**Interfaces:**
- Consumes: `duckdb_engine._connect()`, `_quote()`, `_rel()`.
- Produces: `_statement(relation, field_name, expression, on_existing) -> str`; `_validate_statement(query, con, columns, field_name, on_existing) -> None`, where `columns` maps lowercase names to `(source_name, duckdb_type)`.

- [ ] **Step 1: Write failing AST characterization and adversarial tests**

Create a `columns` fixture containing `a` and `b` as `DOUBLE`, `flag` as `BOOLEAN`, and `label` as `VARCHAR`. Assert acceptance of arithmetic, comparisons, `CASE`, `COALESCE`, `IFNULL`, `NULLIF`, `BETWEEN`, null-safe comparisons, and canonical boolean literals. Assert rejection of `random()`, `read_blob`, `sum`, window functions, scalar subqueries, explicit non-boolean casts, qualified references, multiple statements, string/date constants, and references to `label`.

- [ ] **Step 2: Run the tests and verify RED**

Run: `PYTHONPATH=src ../../.venv/Scripts/python -m pytest tests/test_calculate_field.py -q`
Expected: FAIL because `field_calculation` does not exist.

- [ ] **Step 3: Implement the policy**

Create `_quote_identifier`, `_statement`, and `_validate_statement`. Limit expression length, AST nodes, and depth. Accept only the spec's constants, column refs, arithmetic, comparisons, `BETWEEN`, conjunctions, `CASE`, `COALESCE`, `NULLIF`, and canonical boolean casts. Check the complete statement shape and reject all other nodes. Resolve references against `columns` with ASCII case-insensitive uniqueness and numeric/boolean types.

- [ ] **Step 4: Run the policy tests and verify GREEN**

Run the same command. Expected: all Task 1 tests PASS.

### Task 2: Engine writer, provenance, and output verification

**Files:**
- Modify: `src/mapsmith/engines/field_calculation.py`
- Test: `tests/test_calculate_field.py`
- Modify: `tests/test_verify.py` (spec fixture)

**Interfaces:**
- Consumes: Task 1 statement and validation helpers.
- Produces: `calculate_field(input_path: str, output_path: str, field_name: str, expression: str, on_existing: str = "error") -> dict[str, Any]`.

- [ ] **Step 1: Add failing closed-form writer tests**

Cover: `7 / 2 = 3.5` and `7 // 2 = 3`; `CASE` plus `COALESCE` returning 42; replacement preserving position and source spelling; collision and geometry overwrite refusal; non-numeric reference refusal; non-finite output refusal with manifest retained; empty input; active geometry named `geom`, primary geometry not first, and secondary geometry metadata.

- [ ] **Step 2: Run the new tests and verify RED**

Run: `PYTHONPATH=src ../../.venv/Scripts/python -m pytest tests/test_calculate_field.py -q`
Expected: Task 1 tests remain green; new writer tests FAIL because `calculate_field` is absent.

- [ ] **Step 3: Implement the operation**

Read source schema and CRS, validate the statement, then execute it through the sandboxed connection. Use `SELECT *` for creation and `SELECT * REPLACE` for replacement. Record `operation="calculate_field"`, exact arguments, input digest/CRS, and engine version. Use `verify.audited(..., repair=False)` with checks for feature count, CRS, exact geometry types, destination position, unchanged attributes/geometries, non-finite calculated values, and GeoParquet metadata. Add a dedicated zero-row writer preserving the full source schema and active/secondary geometry metadata.

- [ ] **Step 4: Run engine and generic manifest tests**

Run: `PYTHONPATH=src ../../.venv/Scripts/python -m pytest tests/test_calculate_field.py tests/test_verify.py -q`
Expected: PASS, including the new `_spec_fixtures` entry for `calculate_field`.

### Task 3: Catalog, discovery, and typed-plan integration

**Files:**
- Modify: `src/mapsmith/catalog.py`
- Modify: `src/mapsmith/plans/registry.py`
- Modify: `src/mapsmith/plans/executor.py` (echo `field_name`)
- Modify: `src/mapsmith/plans/validator.py` only if a rule cannot be expressed through catalog contracts
- Test: `tests/test_plans.py`, `tests/test_discovery_contract.py`, `tests/test_calculate_field.py`

**Interfaces:**
- Consumes: Task 2 public `calculate_field`.
- Produces: catalog operation `calculate_field` and a plan binding with `input_args=("input_path",)`, `output_arg="output_path"`, `crs_effect=("same_as", "input_path")`, vector output, and `output_cardinality="same_as_input"`.

- [ ] **Step 1: Add failing discovery and execution tests**

Assert the operation is available, accepts the two `on_existing` choices, requires `.parquet`, and is discoverable from a direct attribute-calculation goal. Build and execute a two-step plan whose second step uses `$calculated` as `input_path`, then verify the resulting attribute and manifest.

- [ ] **Step 2: Run the integration tests and verify RED**

Run: `PYTHONPATH=src ../../.venv/Scripts/python -m pytest tests/test_plans.py tests/test_discovery_contract.py tests/test_calculate_field.py -q`
Expected: FAIL because the catalog entry/binding is absent.

- [ ] **Step 3: Add catalog and plan wiring**

Add the available entry with vector input/output, one dataset input, no projected-CRS or geometry restriction, clear distinctions from `select_features` and `run_sql`, direct phrasings, and a discoverable first example. Add the registry loader/binding and echo `field_name`.

- [ ] **Step 4: Run integration and datum tests**

Run: `PYTHONPATH=src ../../.venv/Scripts/python -m pytest tests/test_plans.py tests/test_discovery_contract.py tests/test_calculate_field.py tests/test_datum.py -q`
Expected: PASS.

### Task 4: Final verification and independent review

**Files:**
- Modify only as required by review evidence: implementation and tests above.

**Interfaces:**
- Consumes: all previous public contracts.

- [ ] **Step 1: Run lint and the complete suite**

Run: `PYTHONPATH=src ../../.venv/Scripts/python -m ruff check .`
Run: `PYTHONPATH=src ../../.venv/Scripts/python -m pytest -q`
Expected: Ruff PASS; every test PASS with no unexpected skip for `calculate_field`.

- [ ] **Step 2: Exercise the public operation end to end**

Execute one real `run_operation` call and one typed plan using arithmetic, `CASE`, and replacement. Verify output values, position, CRS, and manifest. Expected: closed-form values from the spec.

- [ ] **Step 3: Request independent review and handle findings**

Review the complete branch against the spec, especially AST bypasses, geometry metadata, zero rows, collision semantics, provenance, and validation-only stubs. Fix Critical/Important findings with RED→GREEN tests and rerun the full suite. Record deferred Minor findings rather than expanding scope.

- [ ] **Step 4: Present integration choices**

After a clean review and green suite, present the standard choices: merge locally, push and create a PR, or keep the branch. No integration without explicit user selection.
