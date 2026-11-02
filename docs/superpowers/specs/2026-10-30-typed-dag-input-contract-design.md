# Typed DAG planner input contract

Date: 2026-10-30
Status: proposed

## Intent

Make the method-choice benchmark exercise executable typed DAGs without letting
ambiguous initial-input notation or contradictory benchmark contracts hide the
planner's real residual defects. The immediate target is a valid, exact plan with
a satisfied output contract for every supported case and a correct refusal for
every unsupported case, confirmed independently rather than claimed from one run.

The current raw responses confirm 38/65 exact sequences, 36/65 with both an exact
sequence and a satisfied output contract, 3/8 unsupported refusals, 57/65 repeated
operation sequences, 12/65 repeated typed plans, and 0/65 repeated full responses.
All 16 `UNKNOWN_REFERENCE` cases are invented references to initial inputs, not
broken links between earlier steps.

## Goals

- Give every initial benchmark input an explicit literal placeholder and reserve
  `$step_id` exclusively for outputs of earlier steps.
- Keep the production `Plan` and validator contract unchanged.
- Add regression tests for initial-input notation and typed output feasibility.
- Audit accepted sequences against the prompt and the typed output contract before
  another live run. In particular, resolve the `number` versus `description`
  scoring mismatch in `complete_classification-028` and the forbidden
  `execute_plan` step in `vector_analysis-017`.
- Measure exact sequence, exact-plus-output, structural validity, unsupported
  refusals, typed-plan reproducibility, and full-response reproducibility as
  separate metrics.
- Confirm the result on an independent run without further prompt or truth edits.

## Non-goals and anti-goals

- No external `$input_1` references in the public plan grammar.
- No fine-tuning, release, or weakening of structural validation.
- No score obtained by accepting an invalid plan or by tailoring truth to a new
  model response.
- A score of 65/65 is the target, not a result to assume or force.
- New GIS operations are not required for the 16 initial-input failures. They are
  allowed only when a residual demonstrates a real capability gap; that work must
  follow the normal operation recipe and be reported separately from prompt fixes.

## Approaches

### Literal initial-input placeholders (chosen)

Name initial inputs explicitly in the prompt, for example `input_1` and `input_2`,
and instruct the planner to use those strings as literal argument values. Only a
previous step's output may use the `$step_id` form. This matches production plans,
whose initial datasets are literal paths, and confines the change to the benchmark
and its tests.

### Typed external-input references (rejected)

Extend `Plan`, structural validation, and execution binding with named external
references. This would make the DAG notation more uniform, but changes a public
production contract to solve a benchmark-prompt ambiguity and has a much larger
verification surface.

## Design

1. `prompt_for` and `describe_inputs` will assign stable placeholders in input
   order and explain the distinction between literal initial inputs and earlier
   step references. Tests will cover one and multiple inputs and reject invented
   initial-input references in scored responses.
2. The benchmark audit will require every accepted sequence to admit a typed plan
   compatible with its requested output and the planner rules. The numeric-output
   compatibility check will be aligned with the existing legacy rule for
   descriptive answers. The method-choice truth will exclude plan execution,
   which the prompt explicitly puts outside scope. Every contract correction will
   be documented before generating new responses.
3. Existing raw responses will be rescored where possible to separate scorer
   corrections from planner changes. New responses will then be collected twice
   per case with the same model and dataset, followed by an independent run.
4. Remaining semantic failures will be classified as planner defects, benchmark
   contract defects, or genuine missing GIS capabilities. Only the last category
   can justify a new operation.

## Verification

- Focused method-choice and plan-validation tests, then Ruff and the full pytest
  suite.
- A machine check that all accepted sequences have a feasible typed output
  contract and contain no step forbidden by the planner prompt.
- Published counts must reconcile with per-case raw responses: 65 supported and 8
  unsupported cases, all non-exact verdicts accounted for, and structural-error
  occurrences distinguished from invalid-plan counts.
- Reports must preserve residual ids and state clearly whether each comparison is
  like-for-like. No temperature-zero or determinism claim without a matching live
  measurement.
