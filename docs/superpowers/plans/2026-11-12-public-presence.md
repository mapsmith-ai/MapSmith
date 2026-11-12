# Public Presence Update Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the verified MapSmith work on GitHub and mapsmith.dev, with an accurate multi-step project description and no new release.

**Architecture:** Make one metadata change and prove it locally, then publish all pending commits in one push. Treat public GitHub metadata, CI, Pages and the live site as separate deliverables and verify each after the operation.

**Tech Stack:** Python 3.12, pytest, TOML, the existing static-site builder, Git, GitHub API and GitHub Pages.

**Spec:** `docs/superpowers/specs/2026-11-12-public-presence-design.md`

## Global Constraints

- Use the exact description: `An MCP server that turns a question about your data into a multi-step GIS analysis, run by deterministic engines, with a lineage manifest beside every output.`
- Keep `server.json` unchanged and make no release, version bump, social post or unrelated content change.
- Publish no private workspace material, credentials or local paths.
- Push only after local checks and the site build pass.
- Verify public HEAD, repository description, CI, Pages and live content separately after publication.

## Review Focus

- A metadata edit that does not express the multi-step deterministic difference: Task 1 pins the exact description with a failing-first test.
- A template updated while the generated site remains stale: Task 1 builds and inspects the actual output.
- A local push that does not update the remote branch: Task 2 compares remote and local HEAD.
- CI succeeding while Pages remains stale: Task 2 checks both the deployment and fetched live content.
- Private paths or Italian private tooling names reaching public files: Task 1 runs the English/content checks.

---

### Task 1: Metadata and local deliverable

**Files:**
- Create: `docs/superpowers/specs/2026-11-12-public-presence-design.md`
- Create: `docs/superpowers/plans/2026-11-12-public-presence.md`
- Modify: `pyproject.toml`
- Test: `tests/test_showcase.py`

**Interfaces:**
- Consumes: the exact project description in Global Constraints and `python site/build.py <output-dir>`.
- Produces: a local commit ready for publication and a generated site containing current values.

- [ ] **Step 1: Write the failing metadata test**

Add `test_project_description_names_the_multi_step_deterministic_analysis` to `tests/test_showcase.py`. Parse `pyproject.toml` with `tomllib` and assert exact equality with the description in Global Constraints.

- [ ] **Step 2: Verify RED**

Run: `PYTHONPATH=src .venv/Scripts/python -m pytest tests/test_showcase.py::test_project_description_names_the_multi_step_deterministic_analysis -q`
Expected: FAIL because `pyproject.toml` still says `Professional-grade GIS geoprocessing over MCP, with verifiable provenance`.

- [ ] **Step 3: Apply the description and verify GREEN**

Replace only `pyproject.toml`'s `description` value with the required text. Run the same test, then:
`PYTHONPATH=src .venv/Scripts/python -m pytest tests/test_showcase.py -q`
`PYTHONPATH=src .venv/Scripts/python -m ruff check .`
Run the workspace's English/content check from its private root.
Expected: all pass.

- [ ] **Step 4: Build and inspect the actual site**

Run: `PYTHONPATH=src .venv/Scripts/python site/build.py .method_choice/public-presence-site`
Expected: build succeeds. Inspect the generated HTML and manifest for version 0.12.1, 81 operations, the current discovery figures and Unreleased additions. Require no build-local path, unreplaced placeholder or private text.

- [ ] **Step 5: Commit the local deliverable**

Remove the generated output directory, stage only the four declared files, and commit with `git commit -m "Clarify the multi-step project description"`.

### Task 2: Publication and online verification

**Files:**
- No additional public file changes.

**Interfaces:**
- Consumes: the verified local commit from Task 1 and the existing authenticated GitHub API helper.
- Produces: `origin/main` at the same HEAD as local `main`, the new public repository description, green CI and a live current site.

- [ ] **Step 1: Publish main**

Run: `git push origin main`
Then compare `git rev-parse HEAD` with `git ls-remote origin refs/heads/main`; they must match.

- [ ] **Step 2: Update and verify the repository description**

Set the GitHub repository description to the exact text in Global Constraints through the existing authenticated API helper. Read the repository metadata afterward and require exact equality, with no credential output.

- [ ] **Step 3: Verify CI and Pages deployment**

Read the workflow runs for the pushed SHA. Require the complete CI workflow to succeed and the Site workflow to reach a successful deployment. Investigate any failure rather than treating a pushed commit as a published site.

- [ ] **Step 4: Verify the public result**

Fetch `https://mapsmith.dev` and `https://api.github.com/repos/mapsmith-ai/MapSmith`. Require the live page to show version 0.12.1, 81 operations and the Unreleased additions, and the API to show the new description and pushed HEAD. Record any remaining stale surface explicitly.

- [ ] **Step 5: Record the outcome and inspect final state**

Record the actual public result in the project's private task tracker through its normal procedure, without including private commands in public files. Confirm the public repository has no unintended file changes and no publication-created temporary files remain.
