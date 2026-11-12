# Measles discovery label revision

## Intent

Close the stale measles discovery label now that `apportion_by_area` exists, without rewriting historical evidence or presenting model output as a human answer.

## Goals and constraints

- Record the user's 2026-11-05 revision: `apportion_by_area` is primary and `execute_plan` remains defensible.
- Explain in the human note that the earlier `overlay_layers` / `none` / `execute_plan` answer predated the operation, and that both independent 2026-11-04 model labellers selected `apportion_by_area`.
- Preserve `label_claude`, `label_gemini`, `label_fable`, their notes, and the raw relabelling artifact unchanged.
- Recompute the discovery report and update only the public figures that actually change.
- Add a regression check for the measles accepted set and its two current model labels.
- Make no commit, push, tag, release, or unrelated code change.

## Approaches

1. **Dated human revision (chosen).** Update the existing human answer with its reason and prior answer recorded in the note. This follows D-054: human answers remain authoritative, while model answers retain their own provenance.
2. Model-only relabelling. Already completed on 2026-11-04, but insufficient alone because `accepted_of` gives the historical human answer priority.
3. Prefer current model labels over human labels. Rejected: it would silently weaken D-054 and change the meaning of every human answer.

## Implementation

Change the measles row in `tests/data/discovery_queries.json` to `label_human: apportion_by_area` and `label_human_also: [execute_plan]`, with the dated revision note. Add the focused regression test, run `benchmarks/discovery_report.py`, and bring README, site, and catalogue documentation into agreement only where the recomputed values require it.

## Verification

Run the discovery and published-figure tests, Ruff, and the existing method-choice follow-up verification set. Run the complete suite before closing the board item. The deliverable is complete only when the report credits the measles case against the revised accepted set and every guarded public surface agrees with the report.
