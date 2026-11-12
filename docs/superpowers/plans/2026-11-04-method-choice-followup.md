# Method-choice follow-up Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the final method-choice scores independently traceable, refresh model labels without rewriting history, and measure the token footprint of validated plans against single-operation calls.

**Architecture:** Publish the four frozen raw score/plan artifacts as benchmark evidence and verify their hashes and scores offline. Add a resumable two-provider relabelling harness that writes new dated fields while preserving the historical labels. Measure serialized tool-call and result payloads over the existing sixteen executable D-063 cases, using Anthropic's token-count endpoint and retaining per-case raw counts.

**Tech Stack:** Python 3.12, pytest, existing benchmark scripts, stdlib JSON/hashlib/urllib, Anthropic and Gemini APIs already used by `benchmarks/method_choice.py`.

**Spec:** `docs/benchmarks.md`, `tests/brief_cases.py`, and the existing artifact contracts in `tests/test_method_choice.py`.

## Global Constraints

- Public files, identifiers and comments are in English.
- Preserve the historical `label_claude` and `label_gemini` values unchanged.
- No new package dependency; use the repository virtual environment and existing API helpers.
- No commit, push, tag or release: the requested execution is local only.
- Do not publish local absolute paths, credentials or private workspace material.
- API failures and incomplete responses are not negative benchmark results and must not be silently scored.

## Review Focus

- A raw evidence file is missing or changed: its SHA-256 check must fail.
- Relabelling accidentally changes historical answers: a test must pin their pre-change digest.
- A provider returns incomplete or invalid JSON: the run remains explicitly incomplete and is not ingested.
- A token-measurement case fails execution: it contributes no savings claim.
- Frozen model outputs contain local paths or private text: the anti-leak scan must reject publication.

---

### Task 1: Trace final reports to frozen raw responses

**Files:**
- Create: `benchmarks/raw/method-choice/literal-inputs-final-v19/plans-claude-sonnet-5-anthropic-default.json`
- Create: `benchmarks/raw/method-choice/literal-inputs-final-v19/scores-claude-sonnet-5-anthropic-default.json`
- Create: `benchmarks/raw/method-choice/literal-inputs-independent-v19/plans-claude-sonnet-5-anthropic-default.json`
- Create: `benchmarks/raw/method-choice/literal-inputs-independent-v19/scores-claude-sonnet-5-anthropic-default.json`
- Modify: `benchmarks/method_choice_literal_inputs.json`
- Modify: `benchmarks/method_choice_literal_inputs_independent.json`
- Modify: `tests/test_method_choice.py`

**Interfaces:**
- Consumes: `method_choice.score_cases(cases, out, model, provider, temperature) -> dict` and the four frozen artifacts in `.worktrees/typed-dag-literal-inputs/.method_choice/`.
- Produces: `raw_evidence` in each public report, with `plans` and `scores` objects containing repo-relative `path` and `sha256`, plus `scoring_command`.

- [x] **Step 1: Write the failing evidence test**

Add `test_literal_inputs_raw_evidence_reproduces_scores` to `tests/test_method_choice.py`. For both public reports, assert that each evidence file exists, its SHA-256 matches, and `score_cases` recomputes the frozen raw score document without API access. Assert the scoring command names the matching raw directory and `--provider anthropic`.

- [x] **Step 2: Run the test and verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_method_choice.py::test_literal_inputs_raw_evidence_reproduces_scores -q`
Expected: FAIL because `raw_evidence` is absent.

- [x] **Step 3: Publish and link the frozen evidence**

Copy the four v19 files into the paths above without editing their contents. Add `raw_evidence` to both reports and the exact offline scoring commands. Scan all four files for local paths, credentials and private workspace markers before adding them.

- [x] **Step 4: Verify the evidence offline**

Run: `.venv/Scripts/python -m pytest tests/test_method_choice.py -q`
Expected: PASS, including the pre-existing frozen-result tests.

### Task 2: Relabel discovery requests against the current catalogue

**Files:**
- Create: `benchmarks/relabel_discovery.py`
- Create: `benchmarks/discovery_labels_2026-11-04.json`
- Modify: `tests/data/discovery_queries.json`
- Modify: `benchmarks/discovery_report.py`
- Modify: `tests/test_discovery_report.py`
- Modify: current-label claims in `README.md`, `site/index.template.html`, `docs/benchmarks.md`, `docs/catalog-entry-spec.md`, `src/mapsmith/server.py` and `src/mapsmith/catalog.py`

**Interfaces:**
- Consumes: `make_labelling_pack.INSTRUCTIONS`, `catalogue_table()`, `method_choice.ask_anthropic`, `method_choice.ask_gemini`, and `ingest_answers` validation conventions.
- Produces: `label_claude_2026_11` and `label_gemini_2026_11` string fields on all 155 requests; raw provider responses, parsed labels, model names, prompt/catalogue hashes and completeness in `benchmarks/discovery_labels_2026-11-04.json`.

- [x] **Step 1: Write failing relabelling tests**

Add tests that require complete coverage of all 155 requests for both new fields, only valid catalogue/non-answer labels, unchanged digests of the historical label columns, and current-label use by `agreement`, `answerable`, `accepted_of` and `truth_of`. Keep the historical-field fallback tests for old fixtures.

- [x] **Step 2: Run the tests and verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_discovery_report.py -q`
Expected: FAIL because the dated fields and current-label selection do not exist.

- [x] **Step 3: Implement the resumable two-provider harness**

Implement block-wise labelling with the existing pack instructions and catalogue. Store every raw response before parsing, resume completed blocks, require a JSON array keyed by stable question id, reject unknown or missing labels, and never ingest a partial run. Use temperature zero where the provider supports it. Write the dated labels only after both providers have complete valid answers.

- [x] **Step 4: Run both providers and ingest the completed artifact**

Run the harness with the existing credential environment, without printing secrets. Generate `benchmarks/discovery_labels_2026-11-04.json`; if either provider is incomplete, stop and report the blocker instead of publishing partial labels.

- [x] **Step 5: Recompute and verify current-label figures**

Update `discovery_report.py` to prefer the dated fields and retain old-field compatibility for historical fixtures. Run `python benchmarks/discovery_report.py`, update generated/public figures only through the existing checks, then run:

`.venv/Scripts/python -m pytest tests/test_discovery_report.py tests/test_published_figures.py -q`

Expected: PASS, with the historical labels byte-identical and current coverage 155/155 per provider.

### Task 3: Measure validated-plan token savings

**Files:**
- Create: `benchmarks/plan_token_savings.py`
- Create: `benchmarks/plan_token_savings.json`
- Create: `tests/test_plan_token_savings.py`
- Modify: `docs/benchmarks.md`

**Interfaces:**
- Consumes: `brief_cases.case(index, workdir)`, `execute_plan_requests()`, `server.validate_plan`, `server.execute_plan`, `server.run_operation`, and `POST /v1/messages/count_tokens`.
- Produces: per-case request/result character and token totals for `validate_plan + execute_plan` versus N `run_operation` calls, plus aggregate savings and explicit payload/tokenizer metadata.

- [x] **Step 1: Write the failing report-arithmetic test**

Create `tests/test_plan_token_savings.py`: require exactly the sixteen D-063 cases, successful execution in both arms, equal operation counts, correct request/result and aggregate sums, and savings computed as `(single - plan) / single`. Require the report to name the model and the serialized-payload measurement contract.

- [x] **Step 2: Run the test and verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_plan_token_savings.py -q`
Expected: FAIL because the measurement artifact is absent.

- [x] **Step 3: Implement the measurement harness**

For every case, execute both arms in isolated temporary workspaces. Record compact JSON tool-call and result payloads, excluding no request or result. Count characters offline and tokens with Anthropic's official count endpoint, separating calls from results. Store per-case counts and payload SHA-256 values; do not store random local paths. Failed cases must be marked and excluded from any savings claim.

- [x] **Step 4: Measure and publish the actual result**

Run the harness with the Anthropic credential environment. Write `benchmarks/plan_token_savings.json` and a dated `docs/benchmarks.md` section that states the payload definition, model tokenizer, sixteen-case population, actual totals and savings; do not borrow code-execution percentages.

- [ ] **Step 5: Verify all three deliverables**

Run: `.venv/Scripts/python -m pytest tests/test_method_choice.py tests/test_discovery_report.py tests/test_published_figures.py tests/test_plan_token_savings.py -q`
Run: `.venv/Scripts/python -m ruff check .`
Expected: PASS. Then run the complete suite with `.venv/Scripts/python -m pytest -q` and report its actual result.
