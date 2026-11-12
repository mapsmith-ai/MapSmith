# Measles Discovery Label Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record the approved human revision of the measles discovery request and recompute every affected public figure without rewriting historical labels.

**Architecture:** Reuse the completed two-provider relabelling evidence and the validated `ingest_answers.py` path. Pin the revised accepted set with a regression test, then use the existing report and showcase guards to derive and verify every surface update.

**Tech Stack:** Python 3.12, JSON benchmark data, pytest, existing discovery/reporting scripts.

**Spec:** `docs/superpowers/specs/2026-11-05-measles-discovery-label-design.md`

## Global Constraints

- Preserve `label_claude`, `label_gemini`, `label_fable`, their notes, and `benchmarks/discovery_labels_2026-11-04.json` unchanged.
- Record `apportion_by_area` as primary and `execute_plan` as the only secondary answer, with the user's dated revision and the previous answer explained in `label_human_note`.
- Public files, identifiers, comments, and notes are in English.
- Add no dependency and make no unrelated product-code change.
- Make no commit, push, tag, or release.
- Preserve all pre-existing worktree and private-workspace changes, especially social-owned files.

## Review Focus

- A model answer is presented as human provenance: Task 1 checks the dated human revision note and separately checks both existing model labels.
- The historical answer is silently rewritten: Task 1 pins the three historical primary labels.
- Old secondary answers survive the revision: Task 1 asserts the exact accepted tuple.
- The report changes but a public surface does not: Task 2 runs the existing README, site, and catalogue-spec guards.
- An unrelated in-progress change is overwritten: every task limits edits to its declared files and Task 3 inspects the final status/diff.

---

### Task 1: Revised measles answer

**Files:**
- Create temporarily: `.method_choice/measles-answer-2026-11-05.json`
- Modify: `tests/data/discovery_queries.json` (the measles row only)
- Test: `tests/test_discovery_report.py`

**Interfaces:**
- Consumes: `discovery_report.accepted_of(query) -> tuple[str, ...]`, `discovery_report.model_label(query, labeller) -> str | None`, and `benchmarks/ingest_answers.py <answers.json> --write`.
- Produces: `accepted_of(measles) == ("apportion_by_area", "execute_plan")` with historical model/Fable labels intact.

- [x] **Step 1: Write the failing regression test**

Add `test_measles_answer_tracks_the_new_apportion_operation` to `tests/test_discovery_report.py`. Load the request containing `per-capita measles incidence rates` and assert:

```python
assert discovery_report.accepted_of(row) == ("apportion_by_area", "execute_plan")
assert discovery_report.model_label(row, "claude") == "apportion_by_area"
assert discovery_report.model_label(row, "haiku") == "apportion_by_area"
assert row["label_claude"] == "overlay_layers"
assert row["label_gemini"] == "aggregate_weighted"
assert row["label_fable"] == "none"
assert "2026-11-05" in row["label_human_note"]
assert "overlay_layers" in row["label_human_note"]
```

- [x] **Step 2: Run the test and verify it fails**

Run: `PYTHONPATH=src ../../.venv/Scripts/python -m pytest tests/test_discovery_report.py::test_measles_answer_tracks_the_new_apportion_operation -q`
Expected: FAIL because the accepted tuple is still `("overlay_layers", "none", "execute_plan")` and the note has no 2026-11-05 revision.

- [x] **Step 3: Ingest the approved human revision**

Write a one-row answer export for the measles request with `label: apportion_by_area`, `also: [execute_plan]`, and a note stating that on 2026-11-05 the user revised the earlier `overlay_layers` / `none` / `execute_plan` answer because `apportion_by_area` now owns the operation; cite the independent 2026-11-04 Sonnet 5 and Haiku 4.5 labels as supporting evidence, not as human provenance.

Run the ingest without `--write` and inspect the one intended change. Then run it with `--write`. Remove the temporary answer export after ingestion.

- [x] **Step 4: Run the focused and discovery tests**

Run: `PYTHONPATH=src ../../.venv/Scripts/python -m pytest tests/test_discovery_report.py -q`
Expected: PASS, including historical-label and complete current-label coverage checks.

### Task 2: Derived public figures

**Files:**
- Modify only where recomputation requires: `README.md`
- Modify only where recomputation requires: `site/index.template.html`
- Modify only where recomputation requires: `docs/benchmarks.md`
- Modify only where recomputation requires: `docs/catalog-entry-spec.md`
- Modify only where recomputation requires: `src/mapsmith/catalog.py`
- Modify only where recomputation requires: `src/mapsmith/server.py`

**Interfaces:**
- Consumes: the revised measles row from Task 1 and `benchmarks/discovery_report.py`.
- Produces: public figures identical to the recomputed report on every guarded surface.

- [x] **Step 1: Recompute the report and identify stale surfaces**

Run: `PYTHONPATH=src ../../.venv/Scripts/python benchmarks/discovery_report.py`
Run: `PYTHONPATH=src ../../.venv/Scripts/python -m pytest tests/test_discovery_report.py tests/test_published_figures.py -q`
Expected: the report succeeds; any failed showcase assertion identifies a value or surface to update. Do not infer replacement percentages by hand.

- [x] **Step 2: Apply only derived updates**

Update each stale value to the report's computed value. Do not change historical figures, operation documentation, or claims unrelated to the measles revision.

- [x] **Step 3: Verify the discovery deliverable and lint**

Run: `PYTHONPATH=src ../../.venv/Scripts/python -m pytest tests/test_discovery_report.py tests/test_published_figures.py tests/test_catalog_entry_spec.py -q`
Run: `PYTHONPATH=src ../../.venv/Scripts/python -m ruff check .`
Expected: PASS with no stale discovery figures.

### Task 3: Full verification and board closure

**Files:**
- Modify after green verification: `../../../.claude/bacheca.json` (the measles board item only)
- Regenerate: `../../../docs/progetto/17-todo-master.md`

**Interfaces:**
- Consumes: the verified worktree from Task 2.
- Produces: a board item closed with the actual result and a regenerated local snapshot.

- [x] **Step 1: Run the existing follow-up verification set**

Run: `PYTHONPATH=src ../../.venv/Scripts/python -m pytest tests/test_method_choice.py tests/test_discovery_report.py tests/test_published_figures.py tests/test_plan_token_savings.py -q`
Expected: PASS, preserving all three existing follow-up deliverables.

- [x] **Step 2: Run the complete suite**

Run: `PYTHONPATH=src ../../.venv/Scripts/python -m pytest -q`
Expected: PASS. Report skips and any pre-existing failure exactly; do not close the item with a red relevant suite.

- [x] **Step 3: Inspect the delivered diff**

Run: `git status --short`
Run: `git diff --check`
Confirm that all edits are scoped to the approved measles work plus the pre-existing follow-up files, and remove only temporary artifacts created by this execution.

- [x] **Step 4: Close the board item with evidence**

Append the actual 2026-11-05 outcome to the measles item in `../../../.claude/bacheca.json` and set `fatto: true`. Record the human revision, preserved historical labels, recomputed figures, and actual test results.

From the private workspace root run, without pipes:
`python .claude/crea_bacheca.py --applica`
`python .claude/snapshot_bacheca.py`
`python .claude/controlla_bacheca.py`
Expected: synchronization and snapshot succeed; investigate and report any unrelated control finding rather than editing generated files by hand.
