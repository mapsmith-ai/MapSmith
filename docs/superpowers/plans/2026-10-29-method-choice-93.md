# Method-Choice 93-Case Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure the existing Anthropic planner on all 93 reviewed public cases without rerunning the 20 cases already measured.

**Architecture:** Preserve the 20 current case objects and prompts. Add the other 73 from the reviewed QGIS Training Manual mapping, with an opt-in per-case flag for repeated operations so the existing 20 prompts remain byte-identical.

**Tech Stack:** Python 3.12+, pytest, JSON, Anthropic Messages API.

**Spec:** `benchmarks/method_choice.py`, `benchmarks/method_choice_contracts.json`, and the reviewed 93-case mapping.

## Global Constraints

- Do not read or print API credentials; use the existing environment.
- Do not rerun any of the original 20 cases.
- API errors and unattempted cases never become wrong plans.
- Public data and code are in English; no private filesystem paths enter the repository.

## Review Focus

- Repeated operations are allowed only when the case declares them; original prompts stay unchanged.
- All 93 accepted sequences use available operations and match the output contract.
- Split counts remain balanced and the original 20 keep their splits.
- The baseline is complete only when all 93 have a current answer or an explicitly reported API/invalid-response outcome.

---

### Task 1: Case-Scoped Repetition and Dataset Expansion

**Files:** `benchmarks/method_choice.py`, `benchmarks/method_choice_cases.json`, `tests/test_method_choice.py`

- [ ] **Step 1: Write failing tests** proving repeated operations are refused by default, accepted with `allow_repeated_operations`, and do not alter an original prompt.
- [ ] **Step 2: Run the tests and verify the expected failures** with `python -m pytest tests/test_method_choice.py -q`.
- [ ] **Step 3: Implement the per-case prompt and validation flag**, then build all 93 cases while preserving the original 20 objects.
- [ ] **Step 4: Verify counts, splits, output contracts and repeated-operation coverage**, then commit the dataset and harness change.

### Task 2: Complete Baseline and Error Analysis

**Files:** `.method_choice/`, `benchmarks/method_choice_contracts.json`, `docs/benchmarks.md`, `tests/test_method_choice.py`

- [ ] **Step 1: Copy the existing response store and run only cases lacking two current answers**, calibration and evaluation, two responses per case.
- [ ] **Step 2: Score both splits and verify no original case was called again**; record any API or invalid-response outcomes separately.
- [ ] **Step 3: Update the complete artifact and public analysis** with exact rate, first-step, output, assumptions, reproducibility and concrete failure categories.
- [ ] **Step 4: Run method-choice tests, Ruff and the full suite**, review and publish only after green CI.
