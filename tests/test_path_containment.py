"""Every path a caller can name has to be checked, and not by anyone remembering.

Path containment lived in two places that could disagree. The dedicated MCP tools
call `workspace.guard` on each argument by hand; `run_operation` and
`execute_plan` rely on the plan validator, which reads a per-binding tuple of
argument names. On 2026-08-29 those two disagreed on `merge_layers`: its
`input_paths` is a list, so it was invisible to the validator twice over — not in
`input_args`, and not a `str` — and a call the dedicated tool refused was executed
by `run_operation`, reading a file from outside `MAPSMITH_WORKSPACE` and writing
it inside, where the next `describe_dataset` hands it to the model.
`validate_plan` reported that step as `valid: true` with no errors.

The fix was one field. This file is why it does not come back: the failure was not
the missing tuple entry, it was that a hand-maintained enumeration of a growing
set is wrong somewhere between one addition and the next, and only a mechanical
check notices. Same shape as `test_no_new_driver_escapes_the_policy`.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from mapsmith import catalog
from mapsmith.plans import models, validator
from mapsmith.plans.registry import BINDINGS


def _declared_paths(entry: dict) -> list[str]:
    """Parameter names that carry a path, by the only signal the catalog gives.

    Naming, because the parameter vocabulary (`str`, `float`, `list[str]`, …)
    cannot say "this is a dataset path". That is itself the weakness the issue in
    the tracker proposes to fix; until then the convention is load-bearing and
    `test_a_path_parameter_is_recognisable_by_its_name` keeps it honest.
    """
    names = []
    for parameter in entry.get("parameters", []):
        name = parameter["name"] if isinstance(parameter, dict) else str(parameter)
        if "path" in name:
            names.append(name)
    return names


@pytest.mark.parametrize(
    "entry",
    [op for op in catalog.OPERATIONS if op["name"] in BINDINGS],
    ids=lambda op: op["name"],
)
def test_every_path_parameter_is_covered_by_its_binding(entry):
    """A path the validator does not know about is a path nobody checks."""
    binding = BINDINGS[entry["name"]]
    covered = set(binding.input_args) | set(binding.list_input_args)
    if binding.output_arg:
        covered.add(binding.output_arg)

    uncovered = [name for name in _declared_paths(entry) if name not in covered]
    assert not uncovered, (
        f"{entry['name']} declares {uncovered} in the catalog and its binding does "
        "not name them, so `run_operation` and `execute_plan` will not check them "
        "for workspace containment or for non-local forms. Add them to "
        "`input_args` (one path) or `list_input_args` (a list of paths)."
    )


def test_a_path_parameter_is_recognisable_by_its_name():
    """The convention the check above rests on, asserted rather than assumed.

    If an operation ever takes a dataset path under a name without "path" in it,
    the coverage test above goes quiet and stays green while the hole reopens. So
    the arguments the bindings DO name are required to follow the convention: the
    day one does not, this fails and the coverage rule gets rewritten rather than
    silently weakened.
    """
    offenders = []
    for name, binding in BINDINGS.items():
        for arg in (*binding.input_args, *binding.list_input_args):
            if "path" not in arg:
                offenders.append(f"{name}.{arg}")
    assert not offenders, (
        f"these binding arguments carry paths without saying so in their name: "
        f"{offenders}. The containment coverage test finds path parameters by "
        "name, so either rename them or replace that rule with a declared type."
    )


@pytest.mark.parametrize("operation", sorted(BINDINGS))
def test_no_operation_reads_outside_the_workspace_through_the_generic_path(
    operation, monkeypatch
):
    """The end-to-end claim, per operation: what the dedicated tool refuses, the
    generic path refuses too.

    Built as a plan and validated rather than executed — execution needs real
    fixtures per operation, while containment is decided before anything runs and
    that is the property under test. Every path argument is pointed at a file
    outside the workspace; the validator must object to each of them.
    """
    binding = BINDINGS[operation]
    inputs = [*binding.input_args, *binding.list_input_args]
    if not inputs:
        pytest.skip("no path inputs to point outside")

    with tempfile.TemporaryDirectory() as inside, tempfile.TemporaryDirectory() as outside:
        monkeypatch.setenv("MAPSMITH_WORKSPACE", inside)
        stranger = str(Path(outside) / "stranger.parquet")

        arguments: dict[str, object] = {}
        for arg in binding.input_args:
            arguments[arg] = stranger
        for arg in binding.list_input_args:
            arguments[arg] = [stranger, stranger]
        if binding.output_arg:
            arguments[binding.output_arg] = str(Path(inside) / "out.parquet")

        report = validator.validate(
            models.Plan.model_validate(
                {
                    "goal": "containment",
                    "steps": [
                        {"id": "s", "operation": operation, "arguments": arguments}
                    ],
                }
            )
        )
        codes = {issue.code for issue in report.errors}
        assert "PATH_OUTSIDE_WORKSPACE" in codes, (
            f"{operation}: every path argument points outside MAPSMITH_WORKSPACE and "
            f"the validator raised {sorted(codes) or 'nothing'}. The dedicated tool "
            "refuses this; the generic path must too."
        )


@pytest.mark.parametrize("operation", sorted(BINDINGS))
def test_no_operation_reaches_the_network_through_the_generic_path(
    operation, monkeypatch
):
    """Same, for the non-local forms. `/vsicurl/` is a VSI handler rather than a
    driver, so `gdal_policy` cannot stop it — the textual check is the defence,
    and it has to run on every path argument including the ones in lists."""
    binding = BINDINGS[operation]
    inputs = [*binding.input_args, *binding.list_input_args]
    if not inputs:
        pytest.skip("no path inputs to point at the network")

    with tempfile.TemporaryDirectory() as inside:
        monkeypatch.setenv("MAPSMITH_WORKSPACE", inside)
        remote = "/vsicurl/https://example.invalid/remote.geojson"

        arguments: dict[str, object] = {}
        for arg in binding.input_args:
            arguments[arg] = remote
        for arg in binding.list_input_args:
            arguments[arg] = [remote, remote]
        if binding.output_arg:
            arguments[binding.output_arg] = str(Path(inside) / "out.parquet")

        report = validator.validate(
            models.Plan.model_validate(
                {
                    "goal": "egress",
                    "steps": [
                        {"id": "s", "operation": operation, "arguments": arguments}
                    ],
                }
            )
        )
        codes = {issue.code for issue in report.errors}
        assert codes & {"NON_LOCAL_PATH", "PATH_OUTSIDE_WORKSPACE"}, (
            f"{operation}: a /vsicurl/ path passed validation with "
            f"{sorted(codes) or 'no errors'}. SECURITY.md states that any way to "
            "make GDAL reach the network from a workspace-confined server is a "
            "vulnerability."
        )


def test_the_wire_contract_refuses_a_list_of_non_strings():
    """Where the element type of `list[str]` is actually enforced.

    An audit read `PARAM_TYPES["list[str]"] = (list,)` and concluded that
    `[123, {...}]` reaches the engine. It does not: `models.ArgValue` refuses it
    while the Plan is being parsed, and `Plan` is the only way into the validator
    and into `run_operation`. So the guarantee is real and lives one layer up —
    which is worth a test rather than a second implementation, because a check
    that can never fire is a comment claiming a protection that is not there.

    It matters for containment and not only for tidiness: every path check below
    skips values that are not `str`, so a non-string element in a list of paths
    would be a path nobody examined.
    """
    import pydantic
    import pytest as _pytest

    with _pytest.raises(pydantic.ValidationError):
        models.Plan.model_validate(
            {
                "goal": "element types",
                "steps": [
                    {
                        "id": "s",
                        "operation": "merge_layers",
                        "arguments": {
                            "input_paths": [123, {"a": 1}, "fine.parquet"],
                            "output_path": "out.parquet",
                        },
                    }
                ],
            }
        )


def test_the_catalog_search_makes_no_request_under_a_workspace(monkeypatch, tmp_path):
    """`SECURITY.md` promises no network egress in sandbox mode, in writing, and
    calls any exception a vulnerability. 0.3.0 made an engine that fetches a
    model the default for `list_operations`, which is the first tool an agent
    calls. This pins the resolution: with a workspace set and a cold cache, the
    fetch does not happen and the answer says which engine gave it.
    """
    from mapsmith import catalog, retrieval

    monkeypatch.setenv("MAPSMITH_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("HF_HOME", str(tmp_path / "cold-cache"))
    retrieval._model.cache_clear()

    reached = []

    def refuse(*args, **kwargs):
        if not kwargs.get("local_files_only"):
            reached.append(kwargs)
        raise OSError("no cached snapshot")

    monkeypatch.setattr(retrieval, "_require", lambda: (refuse, object))

    answer = catalog.search(
        "steepness of the terrain", limit=3, input_kind="raster",
        produces="dataset:raster",
    )
    assert not reached, (
        "the model download was attempted without `local_files_only` while a "
        "workspace was set, which is the egress SECURITY.md defines as a "
        "vulnerability"
    )
    assert answer, "the search returned nothing instead of falling back"
    assert answer[0].get("engine") == "lexical", (
        "the fallback ran but the answer does not say which engine produced it"
    )
    retrieval._model.cache_clear()
