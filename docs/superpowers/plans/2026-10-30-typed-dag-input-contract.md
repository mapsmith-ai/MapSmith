# Typed DAG Input Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove initial-input ambiguity and contradictory contracts from the typed-DAG method-choice benchmark, then measure and independently confirm valid exact plans and unsupported refusals.

**Architecture:** Keep the production plan grammar unchanged. Assign literal placeholders to initial benchmark inputs, reserve `$step_id` for earlier outputs, align typed scoring with the established output rules, and separate scorer corrections from new planner behavior through raw-response rescoring and fresh runs.

**Tech Stack:** Python 3.12+, pytest, Ruff, Pydantic plan models, the existing Anthropic benchmark harness.

**Spec:** `docs/superpowers/specs/2026-10-30-typed-dag-input-contract-design.md`

## Global Constraints

- Public code, identifiers, comments, and documentation are in English.
- Use the repository virtual environment and do not add dependencies.
- Do not weaken `Plan` validation or add external `$input_1` references.
- Do not change accepted truth in response to a new planner output.
- Do not claim temperature-zero behavior, determinism, or 65/65 without matching live evidence.
- New GIS operations require a separately demonstrated capability gap and the project operation recipe.
- Do not commit, push, tag, or release unless explicitly requested.

## Review Focus

- Multiple initial inputs must retain stable, role-distinct placeholders; covered by `test_prompt_assigns_literal_initial_input_placeholders` in Task 1.
- A descriptive result containing a requested number must not fail solely because its wire kind is `description`; covered by `test_typed_numeric_output_accepts_descriptive_answer` in Task 2.
- Execution-only steps must not be accepted as method choices; covered by `test_method_choice_truth_excludes_execution_steps` in Task 2.
- Independent final branches must each satisfy the requested output; keep `test_typed_dag_output_contract_does_not_require_a_linear_final_step` and the residual-branch checks in Task 2's focused run.
- API failures and unsupported cases must remain outside the 65-case exact denominator; covered by the raw-report reconciliation in Task 3 and the independent-report reconciliation in Task 4.

---

### Task 1: Literal Initial-Input Placeholders

**Files:**
- Modify: `benchmarks/method_choice.py:156-212`
- Test: `tests/test_method_choice.py`

**Interfaces:**
- Consumes: existing `prompt_for(case: dict[str, Any]) -> str` and `describe_inputs(inputs: list[dict[str, Any]]) -> str`.
- Produces: prompts that label initial inputs `input_1`, `input_2`, ... in case order and explain that these are literal values, not `$step` references.

- [ ] **Step 1: Write the failing prompt test**

```python
def test_prompt_assigns_literal_initial_input_placeholders(case):
    case["inputs"] = [
        {"kind": "vector", "geometry": "point", "role": "sample points"},
        {"kind": "vector", "geometry": "polygon", "role": "study area"},
    ]
    text = bench.prompt_for(case)
    assert "input_1" in text
    assert "input_2" in text
    assert text.index("input_1") < text.index("input_2")
    assert "$input_1" not in text
    assert "$input_2" not in text
    assert "literal" in text.lower()
    assert "earlier step" in text.lower()
```

- [ ] **Step 2: Run the test and verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_method_choice.py::test_prompt_assigns_literal_initial_input_placeholders -q`
Expected: FAIL because the prompt does not define `input_1` or `input_2`.

- [ ] **Step 3: Implement the input labels and prompt rule**

Modify `describe_inputs` to enumerate inputs from 1 and prefix each description with its placeholder. Modify `prompt_for` to state that initial-input placeholders are literal argument values and that only outputs of earlier steps use `$step_id`. Preserve the existing no-input text and all other prompt rules.

- [ ] **Step 4: Run focused parser and prompt tests**

Run: `.venv/Scripts/python -m pytest tests/test_method_choice.py -q -k "prompt or parser or residual"`
Expected: PASS.

### Task 2: Typed Output and Truth Contract Audit

**Files:**
- Modify: `benchmarks/method_choice.py:507-556`
- Modify: `benchmarks/method_choice_cases.json` (`vector_analysis-017`)
- Test: `tests/test_method_choice.py:74-77,151-255`

**Interfaces:**
- Consumes: `output_matches(case, got, steps)` and the public accepted sequences.
- Produces: typed numeric-output compatibility equivalent to the legacy rule, and method-choice truth containing no `execute_plan` step.

- [ ] **Step 1: Add failing regression tests**

```python
def test_typed_numeric_output_accepts_descriptive_answer():
    case = next(c for c in bench.load_cases() if c["id"] == "complete_classification-028")
    steps = [{
        "id": "describe_rivers",
        "operation": "describe_dataset",
        "arguments": {"path": "rivers"},
        "output": {"kind": "description", "cardinality": "one"},
    }]
    assert bench.output_matches(case, ["describe_dataset"], steps)


def test_method_choice_truth_excludes_execution_steps():
    for case in bench.load_cases():
        for sequence in case["accepted_sequences"]:
            assert "execute_plan" not in sequence, (case["id"], sequence)
```

- [ ] **Step 2: Run both tests and verify the current contradictions**

Run: `.venv/Scripts/python -m pytest tests/test_method_choice.py::test_typed_numeric_output_accepts_descriptive_answer tests/test_method_choice.py::test_method_choice_truth_excludes_execution_steps -q`
Expected: the numeric test FAILS on kind compatibility; the truth test FAILS on `vector_analysis-017`.

- [ ] **Step 3: Apply the two approved contract corrections**

Add `description` to the typed kinds compatible with a requested `number`, matching the legacy mapping. Remove only `execute_plan` from the accepted sequence of `vector_analysis-017`; remove its corresponding entry in `RESIDUAL_BRANCHES`. Do not alter any other accepted operation sequence.

- [ ] **Step 4: Strengthen and run the residual output checks**

In the residual-branch tests, also assert that the synthesized typed branches satisfy the case's requested output through `output_matches`. Run:

Run: `.venv/Scripts/python -m pytest tests/test_method_choice.py -q -k "output_contract or residual or execution_steps or descriptive_answer"`
Expected: PASS.

- [ ] **Step 5: Rescore the existing typed-DAG responses before any new prompt run**

Run the existing scorer against `.worktrees/typed-output-dag/.method_choice/typed-dag-signatures` without making API calls. Record the old-response summary separately as the like-for-like scorer baseline; do not overwrite `benchmarks/method_choice_typed_dag.json`.

Expected: the original 38/65 exact count remains distinct from exact-plus-output; `complete_classification-028` no longer fails output matching solely for using `description`.

### Task 3: Fresh Typed-DAG Measurement

**Files:**
- Create: `benchmarks/method_choice_literal_inputs.json`
- Modify: `docs/benchmarks.md`
- Test: `tests/test_method_choice.py` only if a report-consistency assertion already has a matching pattern.

**Interfaces:**
- Consumes: the prompt and scorer from Tasks 1-2, plus the unchanged 73-case generalization dataset and `claude-sonnet-5` at its default temperature.
- Produces: raw responses under `.method_choice/literal-inputs/` and a public report with exact, exact-plus-output, invalid, unsupported, residual-id, and reproducibility metrics.

- [ ] **Step 1: Run the complete focused test set before spending API calls**

Run: `.venv/Scripts/python -m pytest tests/test_method_choice.py tests/test_plans.py -q`
Expected: PASS.

- [ ] **Step 2: Collect two fresh responses for each of the 73 newer cases**

Run: `.venv/Scripts/python benchmarks/method_choice.py plan --data .method_choice/generalization-cases.json --provider anthropic --repeat 2 --out .method_choice/literal-inputs`
Expected: all 73 cases attempted; API and invalid-response events reported separately, never converted into wrong plans.

- [ ] **Step 3: Score and reconcile the raw responses**

Run: `.venv/Scripts/python benchmarks/method_choice.py score --data .method_choice/generalization-cases.json --provider anthropic --out .method_choice/literal-inputs`
Expected: 65 supported rows and 8 unsupported outcomes; verdict counts sum to 65, unsupported refusal plus with-plan counts sum to 8, and residual ids match the per-case rows. Recompute typed-plan and full-response reproducibility from raw responses.

- [ ] **Step 4: Publish the measurement without overwriting the previous run**

Write `benchmarks/method_choice_literal_inputs.json` with dataset/catalogue hashes, run completeness, the old-response rescored baseline, fresh summary, structural-error occurrences, residual ids, and both reproducibility measures. Add a dated section to `docs/benchmarks.md` that distinguishes prompt effects, scorer corrections, and remaining defects. Do not claim 65/65 unless the raw rows show 65 exact-plus-output cases.

### Task 4: Residual Correction and Independent Confirmation

**Files:**
- Modify only as required by evidence: `benchmarks/method_choice.py`, `tests/test_method_choice.py`, `benchmarks/method_choice_cases.json`
- Create: `benchmarks/method_choice_literal_inputs_independent.json`
- Modify: `docs/benchmarks.md`

**Interfaces:**
- Consumes: residual ids and raw responses from Task 3.
- Produces: an independent report using the final frozen prompt, scorer, catalog, and truth; no edits between the final measurement and confirmation.

- [ ] **Step 1: Classify every remaining residual before editing**

For each non-exact, output-failing, or unsupported-with-plan case, label the cause as planner defect, benchmark contract defect, or genuine missing GIS capability. Preserve the existing accepted truth unless a contract contradiction is documented independently of the desired score. A GIS capability gap stops the prompt-only path and requires the separate operation workflow.

- [ ] **Step 2: Apply evidence-led fixes with failing tests first**

For each robust planner or scorer fix, add its regression test, verify that it fails on the pre-fix code, implement the smallest change, and rerun the focused tests. Do not batch unrelated truth edits or add an operation merely to raise the score.

- [ ] **Step 3: Repeat the complete measurement after any change**

Repeat Task 3's collection and reconciliation into a new raw-output directory. Freeze the prompt, truth, catalog, and scorer once the final measurement is recorded.

- [ ] **Step 4: Collect and score the independent confirmation**

Run the same two commands with `--out .method_choice/literal-inputs-independent` and no intervening edits. Write `benchmarks/method_choice_literal_inputs_independent.json`, including transitions against the final first run and all residual ids. Target 65/65 exact-plus-output and 8/8 unsupported refusals; report the actual result even if lower.

- [ ] **Step 5: Run final verification and update the public narrative**

Run: `.venv/Scripts/python -m ruff check .`
Run: `.venv/Scripts/python -m pytest -q`
Expected: both PASS. Update `docs/benchmarks.md` with first-run and independent figures, the distinction between sequence and typed-plan reproducibility, any unresolved residuals, and any GIS capability work deferred to its own task.
