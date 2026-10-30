"""Static plan validation: reject wrong plans BEFORE anything runs.

In GIS-agent benchmarks up to ~47% of failed runs involve planning mistakes
(missing or mis-ordered operations) and CRS mismatch halves task success. Every check here
turns one of those silent runtime failures into a machine-actionable error the
planning agent can repair: codes are stable strings, messages name the exact
step and argument, and unknown operations come back with BM25 suggestions.

The validator never writes anything. It does light pre-flight reads (file
existence, CRS metadata of existing inputs) because a plan that references a
missing dataset or drives meters-math into degrees is wrong *now*, not at
runtime. When a CRS cannot be determined it propagates "unknown" and the
downstream CRS checks stay silent — no false alarms.
"""

from __future__ import annotations

import difflib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import catalog, verify, workspace
from ..engines import dispatch
from .models import REFERENCE, Issue, Plan, SimulatedOutput, ValidationReport
from .registry import BINDINGS, PARAM_TYPES, Binding

UNKNOWN_CRS = verify.UNKNOWN_CRS
AUDIT_SIDECAR_SUFFIXES = workspace.AUDIT_SIDECAR_SUFFIXES
EXTRA_FOR_FLAG = {"exactextract": "raster", "whitebox": "whitebox"}
VECTOR_EXTENSIONS = {".parquet", ".gpkg"}
RASTER_EXTENSIONS = {".tif", ".tiff"}

#: Every extension a writer can honour for its kind. Anything else is REFUSED,
#: not warned about: a vector writer handed `out.tif` goes through
#: `GeoDataFrame.to_file`, which falls back to the Shapefile driver and turns
#: the path into a directory of `.shp` files -- and if that path was an input,
#: the input is gone and the call dies before a manifest exists (issue #31).
#: The canonical two above are still what a warning steers towards.
WRITABLE_EXTENSIONS = {
    "vector": VECTOR_EXTENSIONS | {".geojson", ".json", ".fgb", ".shp"},
    "raster": RASTER_EXTENSIONS,
}


def output_extension_refusal(kind: str | None, path: str) -> str | None:
    """Why `path` cannot be the output of an operation writing `kind`, or None."""
    if kind not in WRITABLE_EXTENSIONS:
        return None
    suffix = Path(path).suffix.lower()
    if suffix in WRITABLE_EXTENSIONS[kind]:
        return None
    return (
        f"'{path}' has extension '{suffix or '(none)'}' and the operation writes a "
        f"{kind} ({'/'.join(sorted(WRITABLE_EXTENSIONS[kind]))}). Refused before "
        "anything is written: a vector writer given another extension falls back to "
        "the Shapefile driver and turns the path into a directory."
    )
# operations whose paired inputs get auto-aligned at runtime (worth a note, not an error)
ALIGNED_PAIRS = {
    "clip_layer": ("input_path", "mask_path"),
    "spatial_join": ("left_path", "right_path"),
    "zonal_statistics": ("raster_path", "zones_path"),
    "watershed": ("dem_path", "pour_points_path"),
}


def _catalog_entry(name: str) -> dict[str, Any] | None:
    for op in catalog.OPERATIONS:
        if op["name"] == name:
            return op
    return None


def _check_output_contract(step: Any, entry: dict[str, Any], errors: list[Issue]) -> None:
    contract = step.output
    if contract is None:
        return
    allowed = {
        "vector": {"dataset:vector", "plan_result"},
        "raster": {"dataset:raster", "plan_result"},
        "map": {"dataset:vector", "plan_result"},
        "table": {"answer", "dataset:vector", "description", "plan_result"},
        "number": {"answer", "dataset:vector", "description", "plan_result"},
        "description": {"description", "answer"},
    }
    produced = entry.get("produces")
    if produced not in allowed[contract.kind]:
        errors.append(Issue(
            code="OUTPUT_KIND_MISMATCH",
            step_id=step.id,
            message=f"step declares output kind '{contract.kind}' but operation produces '{produced}'",
        ))
    geometry = entry.get("output_geometry")
    if geometry and contract.geometry and contract.geometry != geometry:
        errors.append(Issue(
            code="OUTPUT_GEOMETRY_MISMATCH",
            step_id=step.id,
            message=f"step declares geometry '{contract.geometry}' but operation produces '{geometry}'",
        ))
    if contract.cardinality == "one" and step.operation == "dissolve_layer" and step.arguments.get("by"):
        errors.append(Issue(
            code="OUTPUT_CARDINALITY_UNGUARANTEED",
            step_id=step.id,
            message="cardinality 'one' cannot be guaranteed when dissolving by a group key",
        ))


# Path-safety rules are shared with the runtime jail at the MCP boundary:
# one definition of "local path" and "inside the workspace" for plans & tools.
_workspace = workspace.root
_nonlocal_reason = workspace.nonlocal_reason
_outside_workspace_shared = workspace.is_outside


def _canon(path: str) -> str:
    """Filesystem identity of a local path (case/separator/relative-insensitive).

    Collision and overwrite checks must compare identities, not raw strings:
    'OUT.PARQUET' and 'out.parquet' are the same file on Windows.
    """
    try:
        return os.path.normcase(str(Path(path).resolve()))
    except (OSError, ValueError):
        return os.path.normcase(path)


def _outside_workspace(path: str, ws: Path) -> bool:
    return _outside_workspace_shared(path, ws)


def _parse_crs(value: str) -> str | None:
    """Normalize a CRS string ('EPSG:32632', WKT, ...) or None if unparseable."""
    from pyproj.exceptions import CRSError

    from .. import crs_policy

    try:
        crs = crs_policy.parse(value)  # refuses a file or a host before PROJ reads `+init=`
    except (CRSError, ValueError):
        return None
    epsg = crs.to_epsg()
    return f"EPSG:{epsg}" if epsg else crs.name


def _is_geographic(crs_value: str) -> bool | None:
    """True/False when determinable, None when the CRS is unknown/unparseable."""
    if crs_value == UNKNOWN_CRS:
        return None
    from pyproj.exceptions import CRSError

    from .. import crs_policy

    try:
        return crs_policy.parse(crs_value).is_geographic
    except (CRSError, ValueError):
        return None


def _check_contract(step_id: str, name: str, value: Any, param: dict[str, Any], errors: list[Issue]) -> None:
    if isinstance(value, str) and REFERENCE.match(value):
        return  # dataset references are checked by the data-flow validator, not as literal choices

    def refuse(rule: str) -> None:
        errors.append(Issue(code="INVALID_ARGUMENT_VALUE", step_id=step_id, message=f"{rule}, got {value!r}"))

    choices = param.get("choices")
    if choices:
        values = value if isinstance(value, list) else [value]
        if any(item not in choices for item in values):
            refuse(f"argument '{name}' must be one of {choices}")
    if "min_items" in param and len(value) < param["min_items"]:
        refuse(f"argument '{name}' must contain at least {param['min_items']} items")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        import math

        bounded = any(key in param for key in ("minimum", "maximum", "exclusive_minimum", "exclusive_maximum"))
        if bounded and not math.isfinite(value):
            refuse(f"argument '{name}' must be finite")
            return
        if "minimum" in param and value < param["minimum"]:
            refuse(f"argument '{name}' must be >= {param['minimum']}")
        if "maximum" in param and value > param["maximum"]:
            refuse(f"argument '{name}' must be <= {param['maximum']}")
        if "exclusive_minimum" in param and value <= param["exclusive_minimum"]:
            refuse(f"argument '{name}' must be > {param['exclusive_minimum']}")
        if "exclusive_maximum" in param and value >= param["exclusive_maximum"]:
            refuse(f"argument '{name}' must be < {param['exclusive_maximum']}")
    if param.get("odd") and value % 2 == 0:
        refuse(f"argument '{name}' must be odd")
    extensions = param.get("extensions")
    if extensions and isinstance(value, str) and Path(value).suffix.lower() not in extensions:
        refuse(f"argument '{name}' must end with one of {extensions}")


def _matches(actual: Any, expected: Any) -> bool:
    if isinstance(expected, list):
        if isinstance(actual, list):
            return any(item in expected for item in actual)
        return actual in expected
    return actual == expected


def _check_step_contracts(step_id: str, operation: str, arguments: dict[str, Any], errors: list[Issue]) -> None:
    for rule in catalog.STEP_CONTRACTS.get(operation, []):
        if any(not _matches(arguments.get(name), expected) for name, expected in rule.get("when", {}).items()):
            continue
        if "when_present" in rule and arguments.get(rule["when_present"]) is None:
            continue
        if "when_nonempty" in rule and not arguments.get(rule["when_nonempty"]):
            continue
        for name in rule.get("required", []):
            if arguments.get(name) is None:
                errors.append(Issue(
                    code="MISSING_ARGUMENT", step_id=step_id,
                    message=f"argument '{name}' is required by this operation's declared rule {rule}",
                ))
        for name in rule.get("required_one_present", []):
            if all(arguments.get(candidate) is None for candidate in rule["required_one_present"]):
                errors.append(Issue(
                    code="MISSING_ARGUMENT", step_id=step_id,
                    message=f"at least one of {rule['required_one_present']} is required",
                ))
        for name, expected in rule.get("required_any", {}).items():
            if not _matches(arguments.get(name), expected):
                errors.append(Issue(
                    code="INVALID_ARGUMENT_VALUE", step_id=step_id,
                    message=f"argument '{name}' must include one of {expected}, got {arguments.get(name)!r}",
                ))
        for name, expected in rule.get("forbidden", {}).items():
            if _matches(arguments.get(name), expected):
                errors.append(Issue(
                    code="INVALID_ARGUMENT_VALUE", step_id=step_id,
                    message=f"argument '{name}' must not be {expected!r} with these arguments",
                ))
        for lower_name, upper_name in rule.get("ordered", {}).items():
            import math

            lower, upper = arguments.get(lower_name), arguments.get(upper_name)
            numeric = (
                isinstance(lower, (int, float)) and not isinstance(lower, bool)
                and isinstance(upper, (int, float)) and not isinstance(upper, bool)
                and math.isfinite(lower) and math.isfinite(upper)
            )
            if numeric and lower > upper:
                errors.append(Issue(
                    code="INVALID_ARGUMENT_VALUE", step_id=step_id,
                    message=f"argument '{lower_name}' must not exceed '{upper_name}', got {lower!r} > {upper!r}",
                ))
        for base_name, spacing_name in rule.get("multiple_of", {}).items():
            if base_name not in arguments:
                continue
            import math

            base, spacing = arguments[base_name], arguments.get(spacing_name)
            if (
                not isinstance(base, (int, float)) or isinstance(base, bool)
                or not isinstance(spacing, (int, float)) or isinstance(spacing, bool)
                or not math.isfinite(base) or not math.isfinite(spacing) or spacing <= 0
            ):
                continue  # the parameter validator has already reported the invalid value
            steps = base / spacing
            whole = round(steps)
            if whole < 1 or abs(steps - whole) > 1e-9 * max(1.0, steps):
                errors.append(Issue(
                    code="INVALID_ARGUMENT_VALUE", step_id=step_id,
                    message=f"argument '{base_name}' must be a whole number of '{spacing_name}' steps, "
                    f"got {base!r} over {spacing!r}",
                ))


def _check_arguments(
    step_id: str, entry: dict[str, Any], arguments: dict[str, Any], errors: list[Issue]
) -> None:
    params = {p["name"]: p for p in entry.get("parameters", [])}
    for param in entry.get("parameters", []):
        if param.get("required") and param["name"] not in arguments:
            errors.append(
                Issue(
                    code="MISSING_ARGUMENT",
                    step_id=step_id,
                    message=f"required argument '{param['name']}' is missing",
                )
            )
    for name, value in arguments.items():
        if name not in params:
            errors.append(
                Issue(
                    code="UNKNOWN_ARGUMENT",
                    step_id=step_id,
                    message=f"unknown argument '{name}'; declared: {sorted(params)}",
                )
            )
            continue
        param = params[name]
        expected = PARAM_TYPES.get(param["type"])
        if expected is None:
            continue
        bad_bool = isinstance(value, bool) and bool not in expected
        if bad_bool or not isinstance(value, expected):
            errors.append(
                Issue(
                    code="WRONG_TYPE",
                    step_id=step_id,
                    message=f"argument '{name}' must be {param['type']}, "
                    f"got {type(value).__name__}",
                )
            )
            continue
        _check_contract(step_id, name, value, param, errors)
    _check_step_contracts(step_id, entry["name"], arguments, errors)



def _collect_literal_outputs(plan: Plan) -> dict[str, tuple[int, str]]:
    """Canonical output path -> (step index, step id), first writer wins."""
    outputs: dict[str, tuple[int, str]] = {}
    for index, step in enumerate(plan.steps):
        binding = BINDINGS.get(step.operation)
        if binding and binding.output_arg:
            value = step.arguments.get(binding.output_arg)
            if (
                isinstance(value, str)
                and value
                and not REFERENCE.match(value)
                and not _nonlocal_reason(value)
            ):
                outputs.setdefault(_canon(value), (index, step.id))
    return outputs


def _collect_external_inputs(plan: Plan, literal_outputs: dict[str, tuple[int, str]]) -> set[str]:
    """Literal input paths that refer to data existing BEFORE the plan runs.

    A path also written by a later step still counts as external when the file
    already exists on disk: that step would clobber a real input mid-run, which
    the output checks flag as OUTPUT_OVERWRITES_INPUT.
    """
    externals: set[str] = set()
    for index, step in enumerate(plan.steps):
        binding = BINDINGS.get(step.operation)
        if not binding:
            continue
        for arg in binding.input_args:
            value = step.arguments.get(arg)
            if (
                not isinstance(value, str)
                or not value
                or REFERENCE.match(value)
                or _nonlocal_reason(value)
            ):
                continue
            key = _canon(value)
            dependency = literal_outputs.get(key)
            if dependency is None or (dependency[0] >= index and Path(value).exists()):
                externals.update(
                    _canon(f"{value}{suffix}")
                    for suffix in ("", *AUDIT_SIDECAR_SUFFIXES)
                )
    return externals


@dataclass(frozen=True)
class _StepContext:
    """Everything a per-input check needs to know about the plan so far."""

    index: int
    order: dict[str, int]
    simulated: dict[str, SimulatedOutput]
    literal_outputs: dict[str, tuple[int, str]]
    workspace: Path | None
    crs_matters: bool  # the step's operation propagates/consumes a CRS


def _check_reference(
    step_id: str, arg: str, target: str, context: _StepContext, errors: list[Issue]
) -> str | None:
    """Validate a '$target' reference; return the referenced CRS when sound."""
    if target not in context.order:
        errors.append(
            Issue(
                code="UNKNOWN_REFERENCE",
                step_id=step_id,
                message=f"'{arg}' references '${target}' but no step has that id",
            )
        )
    elif context.order[target] >= context.index:
        errors.append(
            Issue(
                code="FORWARD_REFERENCE",
                step_id=step_id,
                message=f"'{arg}' references '${target}' which runs later — "
                f"move step '{target}' before '{step_id}'",
            )
        )
    elif target not in context.simulated:
        errors.append(
            Issue(
                code="REF_TO_NO_OUTPUT",
                step_id=step_id,
                message=f"'{arg}' references '${target}' but that step produces no dataset",
            )
        )
    else:
        return context.simulated[target].crs
    return None


def _check_literal_input(
    step_id: str,
    arg: str,
    value: str,
    context: _StepContext,
    errors: list[Issue],
    warnings: list[Issue],
    notes: list[Issue],
) -> str | None:
    """Validate a literal input path; return its (probed or simulated) CRS."""
    reason = _nonlocal_reason(value)
    if reason:
        errors.append(
            Issue(
                code="NON_LOCAL_PATH",
                step_id=step_id,
                message=f"'{arg}': {reason}: {value}",
            )
        )
        return None
    if context.workspace and _outside_workspace(value, context.workspace):
        errors.append(
            Issue(
                code="PATH_OUTSIDE_WORKSPACE",
                step_id=step_id,
                message=f"'{arg}' resolves outside MAPSMITH_WORKSPACE: {value}",
            )
        )
        return None
    # The one gate, here too: `probe_crs` below opens the file with GDAL, which
    # reads the SRS and the grid it names before anything else (fourth audit
    # pass, 2026-10-23: 21 opens from `validate_plan`, and `valid: true`).
    try:
        from .. import crs_policy

        crs_policy.refuse_declared_in_file(value)
    except ValueError as exc:
        errors.append(Issue(code="CRS_REFUSED", step_id=step_id, message=f"'{arg}': {exc}"))
        return None
    dependency = context.literal_outputs.get(_canon(value))
    if dependency and dependency[1] != step_id:
        dep_index, dep_id = dependency
        if dep_index >= context.index:
            if Path(value).exists():
                # a real pre-existing file that a later step would overwrite:
                # readable now; the writing step gets OUTPUT_OVERWRITES_INPUT.
                return verify.probe_crs(value)
            errors.append(
                Issue(
                    code="FORWARD_REFERENCE",
                    step_id=step_id,
                    message=f"'{arg}' uses '{value}', written by later step "
                    f"'{dep_id}' — move that step before '{step_id}'",
                )
            )
            return None
        notes.append(
            Issue(
                code="PREFER_REFERENCE",
                step_id=step_id,
                message=f"'{arg}' names the output of step '{dep_id}'; "
                f"prefer the explicit form '${dep_id}'",
            )
        )
        simulated = context.simulated.get(dep_id)
        return simulated.crs if simulated else None
    if not Path(value).exists():
        errors.append(
            Issue(
                code="INPUT_NOT_FOUND",
                step_id=step_id,
                message=f"'{arg}' points to a file that does not exist: {value}",
            )
        )
        return None
    crs = verify.probe_crs(value)
    if crs == UNKNOWN_CRS and context.crs_matters:
        warnings.append(
            Issue(
                code="MISSING_CRS",
                step_id=step_id,
                message=f"the CRS of '{arg}' could not be determined statically; "
                "engines reject missing-CRS inputs at runtime",
            )
        )
    return crs


def _check_one_input(
    step_id: str,
    arg: str,
    value: str,
    context: _StepContext,
    errors: list[Issue],
    warnings: list[Issue],
    notes: list[Issue],
) -> str | None:
    match = REFERENCE.match(value)
    if match:
        return _check_reference(step_id, arg, match.group(1), context, errors)
    return _check_literal_input(step_id, arg, value, context, errors, warnings, notes)


def _resolve_operation(
    step: Any, errors: list[Issue]
) -> tuple[dict[str, Any], Binding] | None:
    """Resolve a step to (catalog entry, binding); record why when impossible."""
    entry = _catalog_entry(step.operation)
    binding = BINDINGS.get(step.operation)
    if entry is None and binding is None:
        # typos first (edit distance), then semantic neighbors (BM25 over the docs)
        names = [op["name"] for op in catalog.OPERATIONS]
        close = difflib.get_close_matches(step.operation, names, n=3, cutoff=0.6)
        ranked = close or [op["name"] for op, _ in catalog.rank(step.operation, limit=3)]
        hint = f" Did you mean: {', '.join(ranked)}?" if ranked else ""
        errors.append(
            Issue(
                code="UNKNOWN_OPERATION",
                step_id=step.id,
                message=f"operation '{step.operation}' does not exist.{hint}",
            )
        )
        return None
    if entry is not None and entry["status"] != "available":
        errors.append(
            Issue(
                code="OPERATION_NOT_AVAILABLE",
                step_id=step.id,
                message=f"operation '{step.operation}' is planned but not implemented "
                "yet — tell the user instead of substituting another tool",
            )
        )
        return None
    if entry is not None and entry.get("category") == "planning":
        errors.append(
            Issue(
                code="NESTED_PLAN",
                step_id=step.id,
                message="plans cannot contain planning operations — put the steps "
                "directly in this plan",
            )
        )
        return None
    if entry is not None and entry.get("category") == "visualization":
        errors.append(
            Issue(
                code="NOT_PLANNABLE",
                step_id=step.id,
                message=f"'{step.operation}' is interactive — call it directly "
                "after the plan has executed",
            )
        )
        return None
    if binding is None or entry is None:
        errors.append(
            Issue(
                code="UNKNOWN_OPERATION",
                step_id=step.id,
                message=f"operation '{step.operation}' has no executable binding",
            )
        )
        return None
    if binding.engine_flag == "qgis" and not dispatch.available_engines().get("qgis"):
        errors.append(
            Issue(
                code="ENGINE_NOT_AVAILABLE",
                step_id=step.id,
                message="qgis_processing needs QGIS with qgis_process installed and discoverable",
            )
        )
        return entry, binding
    if binding.engine_flag and not dispatch.available_engines().get(binding.engine_flag):
        extra = EXTRA_FOR_FLAG.get(binding.engine_flag, binding.engine_flag)
        errors.append(
            Issue(
                code="MISSING_EXTRA",
                step_id=step.id,
                message=f"'{step.operation}' needs an engine that is not installed: "
                f"pip install mapsmith[{extra}]",
            )
        )
    return entry, binding


def validate(plan: Plan) -> ValidationReport:
    """Statically validate a plan against the operation registry and real inputs."""
    errors: list[Issue] = []
    warnings: list[Issue] = []
    notes: list[Issue] = []
    simulated: dict[str, SimulatedOutput] = {}
    workspace = _workspace()

    order: dict[str, int] = {}
    for index, step in enumerate(plan.steps):
        if step.id in order:
            errors.append(
                Issue(
                    code="DUPLICATE_STEP_ID",
                    step_id=step.id,
                    message=f"step id '{step.id}' is used more than once",
                )
            )
        order.setdefault(step.id, index)

    literal_outputs = _collect_literal_outputs(plan)
    external_inputs = _collect_external_inputs(plan, literal_outputs)
    seen_outputs: dict[str, str] = {}  # output path -> step id

    for index, step in enumerate(plan.steps):
        resolved = _resolve_operation(step, errors)
        if resolved is None:
            continue
        entry, binding = resolved

        _check_arguments(step.id, entry, step.arguments, errors)
        _check_output_contract(step, entry, errors)
        _check_step_extras(step, binding, workspace, errors, warnings)

        context = _StepContext(
            index=index,
            order=order,
            simulated=simulated,
            literal_outputs=literal_outputs,
            workspace=workspace,
            crs_matters=binding.crs_effect is not None
            and binding.crs_effect[0] != "unknown",
        )
        input_crs: dict[str, str] = {}
        for arg in binding.input_args:
            value = step.arguments.get(arg)
            if isinstance(value, str) and value:
                crs = _check_one_input(step.id, arg, value, context, errors, warnings, notes)
                if crs is not None:
                    input_crs[arg] = crs

        # Path arguments that arrive as a list get the identical treatment,
        # element by element. Skipping them was a containment hole: see the
        # comment on `Binding.list_input_args`. The CRS of an element is not
        # recorded under the argument name, because several elements share it
        # and the operations taking a list declare `crs_effect ("unknown", "")`
        # anyway — the containment checks are what matter here.
        for arg in binding.list_input_args:
            values = step.arguments.get(arg)
            if not isinstance(values, list):
                continue
            for index, value in enumerate(values):
                if isinstance(value, str) and value:
                    _check_one_input(
                        step.id, f"{arg}[{index}]", value, context, errors, warnings, notes
                    )

        output_path = _resolve_output(
            step, binding, workspace, seen_outputs, external_inputs, errors, warnings
        )

        # --- CRS simulation and suitability ----------------------------------------
        output_crs = _simulate_crs(step, binding, input_crs, errors)
        _crs_suitability(step, input_crs, errors, notes)

        if output_path:
            simulated[step.id] = SimulatedOutput(output=output_path, crs=output_crs)

    return ValidationReport(
        valid=not errors,
        errors=errors,
        warnings=warnings,
        notes=notes,
        simulated_outputs=simulated,
    )


def _check_step_extras(
    step: Any,
    binding: Binding,
    workspace: Path | None,
    errors: list[Issue],
    warnings: list[Issue],
) -> None:
    """Step-shape checks that don't fit the input/output path pipeline."""
    for key, value in step.arguments.items():
        if (
            isinstance(value, str)
            and REFERENCE.match(value)
            and key not in binding.input_args
            and key != binding.output_arg
        ):
            errors.append(
                Issue(
                    code="REFERENCE_NOT_ALLOWED",
                    step_id=step.id,
                    message=f"'{key}' cannot take a '$step' reference — only dataset "
                    "input arguments can",
                )
            )
    if step.operation == "spatial_join":
        requested = step.arguments.get("engine", "auto")
        if (
            isinstance(requested, str)
            and requested in dispatch.SPATIAL_JOIN_ENGINES
            and requested not in ("auto", "")
            and not dispatch.available_engines().get(requested, False)
        ):
            installed = [k for k, v in dispatch.available_engines().items() if v]
            errors.append(
                Issue(
                    code="ENGINE_NOT_AVAILABLE",
                    step_id=step.id,
                    message=f"engine '{requested}' is not installed; "
                    f"available: {installed} or 'auto'",
                )
            )
    if step.operation == "run_sql" and not workspace:
        # With MAPSMITH_WORKSPACE set the DuckDB connection itself is
        # sandboxed (allowed_directories + external access off), so SQL file
        # access is confined even though the query text is opaque here.
        warnings.append(
            Issue(
                code="SQL_NOT_SANDBOXED",
                step_id=step.id,
                message="without MAPSMITH_WORKSPACE the SQL engine is unconfined: "
                "the query text may read or write any path the process can reach "
                "— review it before executing, or set MAPSMITH_WORKSPACE to "
                "sandbox the connection",
            )
        )


def _resolve_output(
    step: Any,
    binding: Binding,
    workspace: Path | None,
    seen_outputs: dict[str, str],
    external_inputs: set[str],
    errors: list[Issue],
    warnings: list[Issue],
) -> str | None:
    """Validate the step's output argument; return the output path when usable."""
    if not binding.output_arg:
        return None
    value = step.arguments.get(binding.output_arg)
    if not isinstance(value, str) or not value:
        if step.operation == "run_sql":
            warnings.append(
                Issue(
                    code="SQL_PREVIEW_ONLY",
                    step_id=step.id,
                    message="run_sql without output_path only previews rows; "
                    "set output_path to materialize a referencable dataset",
                )
            )
        return None
    if REFERENCE.match(value):
        errors.append(
            Issue(
                code="OUTPUT_IS_REFERENCE",
                step_id=step.id,
                message=f"'{binding.output_arg}' must be a new path, not a '$step' reference",
            )
        )
        return None
    _check_output(step, binding, value, workspace, seen_outputs, external_inputs,
                  errors, warnings)
    return value


def _check_output(
    step: Any,
    binding: Binding,
    value: str,
    workspace: Path | None,
    seen_outputs: dict[str, str],
    external_inputs: set[str],
    errors: list[Issue],
    warnings: list[Issue],
) -> None:
    reason = _nonlocal_reason(value)
    if reason:
        errors.append(
            Issue(
                code="NON_LOCAL_PATH",
                step_id=step.id,
                message=f"'{binding.output_arg}': {reason}: {value}",
            )
        )
        return
    if workspace and _outside_workspace(value, workspace):
        errors.append(
            Issue(
                code="PATH_OUTSIDE_WORKSPACE",
                step_id=step.id,
                message=f"'{binding.output_arg}' resolves outside MAPSMITH_WORKSPACE: {value}",
            )
        )
        return
    key = _canon(value)
    artifacts = {
        _canon(f"{value}{suffix}") for suffix in ("", *AUDIT_SIDECAR_SUFFIXES)
    }
    collisions = sorted(artifacts.intersection(seen_outputs))
    if collisions:
        collided = collisions[0]
        errors.append(
            Issue(
                code="OUTPUT_COLLISION",
                step_id=step.id,
                message=f"output '{value}' or one of its audit sidecars is already "
                f"written by step '{seen_outputs[collided]}'",
            )
        )
    seen_outputs.update({artifact: step.id for artifact in artifacts})
    if key in external_inputs:
        errors.append(
            Issue(
                code="OUTPUT_OVERWRITES_INPUT",
                step_id=step.id,
                message=f"output '{value}' would overwrite a plan input mid-run",
            )
        )
    if not Path(value).parent.exists():
        errors.append(
            Issue(
                code="OUTPUT_DIR_MISSING",
                step_id=step.id,
                message=f"output directory does not exist: {Path(value).parent}",
            )
        )
    suffix = Path(value).suffix.lower()
    expected = RASTER_EXTENSIONS if binding.output_kind == "raster" else VECTOR_EXTENSIONS
    refusal = output_extension_refusal(binding.output_kind, value)
    if refusal:
        errors.append(Issue(code="OUTPUT_EXTENSION_REFUSED", step_id=step.id, message=refusal))
    elif binding.output_kind and suffix not in expected:
        warnings.append(
            Issue(
                code="SUSPICIOUS_OUTPUT_EXTENSION",
                step_id=step.id,
                message=f"'{value}' has extension '{suffix or '(none)'}' but "
                f"'{step.operation}' writes a {binding.output_kind} "
                f"({'/'.join(sorted(expected))})",
            )
        )


def _simulate_crs(
    step: Any, binding: Binding, input_crs: dict[str, str], errors: list[Issue]
) -> str:
    if not binding.crs_effect:
        return UNKNOWN_CRS
    kind, arg = binding.crs_effect
    if kind == "same_as":
        return input_crs.get(arg, UNKNOWN_CRS)
    if kind == "target":
        value = step.arguments.get(arg)
        if isinstance(value, str) and value:
            parsed = _parse_crs(value)
            if parsed is None:
                errors.append(
                    Issue(
                        code="INVALID_CRS",
                        step_id=step.id,
                        message=f"'{arg}' is not a CRS pyproj understands: {value!r}",
                    )
                )
                return UNKNOWN_CRS
            return parsed
    return UNKNOWN_CRS


def _crs_suitability(
    step: Any, input_crs: dict[str, str], errors: list[Issue], notes: list[Issue]
) -> None:
    entry = _catalog_entry(step.operation)
    binding = BINDINGS[step.operation]
    if (
        entry
        and entry.get("applicability", {}).get("requires_projected_crs")
        and binding.input_args
    ):
        argument = "line_path" if step.operation == "elevation_profile" else binding.input_args[0]
        crs = input_crs.get(argument, UNKNOWN_CRS)
        if _is_geographic(crs):
            errors.append(
                Issue(
                    code="CRS_UNSUITABLE",
                    step_id=step.id,
                    message=f"'{step.operation}' requires '{argument}' in a projected CRS, "
                    f"got {crs}; reproject that input before running the plan",
                )
            )
    if (
        step.operation == "flow_accumulation"
        and step.arguments.get("out_type") == "sca"
        and _is_geographic(input_crs.get("dem_path", UNKNOWN_CRS))
    ):
        errors.append(
            Issue(
                code="CRS_UNSUITABLE",
                step_id=step.id,
                message="out_type='sca' divides by cell width, meaningless in "
                "degrees — reproject the DEM to a projected CRS first or use "
                "out_type='cells'",
            )
        )
    if step.operation == "buffer_layer" and _is_geographic(
        input_crs.get("input_path", UNKNOWN_CRS)
    ):
        notes.append(
            Issue(
                code="CRS_NOTE",
                step_id=step.id,
                message="input is in a geographic CRS: the engine buffers via an "
                "estimated UTM zone and records the decision in provenance",
            )
        )
    pair = ALIGNED_PAIRS.get(step.operation)
    if pair:
        left, right = (input_crs.get(pair[0], UNKNOWN_CRS), input_crs.get(pair[1], UNKNOWN_CRS))
        if UNKNOWN_CRS not in (left, right) and left != right:
            notes.append(
                Issue(
                    code="CRS_ALIGNMENT",
                    step_id=step.id,
                    message=f"'{pair[0]}' ({left}) and '{pair[1]}' ({right}) differ: "
                    "the engine aligns them automatically and records the decision",
                )
            )
