# Public Presence Update Design

## Intent

Show current, verifiable project activity on GitHub and mapsmith.dev without manufacturing a release or new claims.

## Goals and constraints

- Publish the five verified local commits already on `main`.
- Align the GitHub repository description and `pyproject.toml` to the existing showcase proposal: `An MCP server that turns a question about your data into a multi-step GIS analysis, run by deterministic engines, with a lineage manifest beside every output.`
- Keep `server.json` unchanged; its shorter description is already accurate.
- Build the site locally before pushing, then verify CI, Pages deployment, live content and the public GitHub metadata after publication.
- Make no release, version bump, social post or unrelated content change.
- Do not include private workspace material, credentials or local paths in public artifacts.

## Approaches

1. **Publish main, site and About (chosen).** Make the metadata edit locally, run focused checks and the site build, push everything once, update the GitHub description through the existing authenticated API helper, and verify the public result. This shows real activity with minimal new surface area.
2. Publish only main and the site. Lower effort, but leaves the vague `Professional-grade` description unresolved.
3. Cut a new release. Rejected for this purpose: benchmark evidence and documentation do not justify the cost and ceremony of PyPI, GHCR, Registry and Zenodo publication solely to signal activity.

## Implementation

Add the design and plan documents, update the `pyproject.toml` description, run the focused showcase tests and Ruff, and build `site/build.py` into a temporary output directory. Inspect the generated page for the current version, operation count and Unreleased notes. Push `main` to `origin`, then update the repository description with the same text. Record the task in the private board through its normal procedure without putting those private commands in public files.

## Verification

Require local checks to pass before publication. After pushing, read the GitHub commit and workflow runs, wait for the Pages deployment, and fetch the live site and repository metadata. Success means GitHub shows the new HEAD and description, while mapsmith.dev serves version 0.12.1 with the current operation count and the Unreleased additions generated from the changelog. Any failed workflow or stale live page remains open and is reported rather than described as published.
