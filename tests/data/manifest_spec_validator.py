"""Vendored, unmodified, from mapsmith-ai/manifest-spec `validator/validate.py`.

A copy and not a dependency: the point of the standalone validator is that
checking a record needs no toolchain, and a test that pip-installs the spec
repository to prove it would be testing something else. The conformance agent
diffs this against upstream; drift is a finding, not a merge.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# Section 3.6: the closed core of check names. A producer performing one of
# these checks uses this name for it; anything else carries an `x-<producer>:`
# prefix. Kept as a literal list rather than derived from the schema, because
# this validator exists to be an INDEPENDENT implementation — two copies of one
# mistake agree with each other perfectly.
CORE_CHECK_NAMES = frozenset({
    "crs_present",
    "crs_matches",
    "geometry_valid",
    "feature_count_exact",
    "feature_count_bounded",
    "row_count_exact",
    "result_not_empty",
    "extent_within_expected",
    "shape_preserved",
    "values_in_expected_range",
    "input_crs_present",
    "input_not_empty",
    "inputs_share_crs",
    "inputs_may_intersect",
    "geometry_types",
})
EXTENSION_CHECK_NAME = re.compile(r"^x-[a-z0-9][a-z0-9_-]*:[a-z0-9][a-z0-9_]*$")


SPEC_VERSION = re.compile(r"^1\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?Z$")


def _optional(
    out: list[str], where: dict, field: str, kinds: type | tuple, label: str = ""
) -> bool:
    """A field that is not required but IS typed. Absent is fine; wrong is not.

    RECOMMENDED is not the same as unchecked, and until 2026-08-26 here it was:
    this file looked only at the required fields, so a record carrying
    `producer` as a string, or `crs_decisions.analysis_crs` as a number, passed
    here and failed against the schema. Twelve mutations out of fifteen
    disagreed that way — which made the claim at the top of this file, two
    implementations kept in agreement, false about its own subject.
    """
    if field not in where:
        return False
    if not isinstance(where[field], kinds):
        prefix = f"{label}." if label else ""
        out.append(f"`{prefix}{field}` has the wrong type")
        return False
    return True


def problems(record: object) -> list[str]:
    """Every way this record fails to conform, in schema order. Empty = conforming."""
    out: list[str] = []
    if not isinstance(record, dict):
        return ["the manifest must be a JSON object"]

    def need(field: str, kinds: type | tuple, where: dict = record, label: str = "") -> bool:
        prefix = f"{label}." if label else ""
        if field not in where:
            out.append(f"missing required field `{prefix}{field}`")
            return False
        if not isinstance(where[field], kinds):
            out.append(f"`{prefix}{field}` has the wrong type")
            return False
        return True

    if need("spec_version", str) and not SPEC_VERSION.fullmatch(record["spec_version"]):
        out.append(
            "`spec_version` must be semantic-versioned 1.x.y "
            f"(got {record['spec_version']!r})"
        )
    if need("operation", str) and not record["operation"]:
        out.append("`operation` must not be empty")
    need("parameters", dict)

    if need("inputs", list):
        for n, item in enumerate(record["inputs"]):
            label = f"inputs[{n}]"
            if not isinstance(item, dict):
                out.append(f"`{label}` must be an object")
                continue
            if need("path", str, item, label):
                if not item["path"]:
                    out.append(f"`{label}.path` must not be empty")
                if "\\" in item["path"]:
                    out.append(
                        f"`{label}.path` carries a backslash: paths use `/` as the only "
                        "separator on every platform, or two manifests for the same run "
                        "differ in a field that describes nothing about the computation"
                    )
            if need("sha256", str, item, label) and not SHA256.fullmatch(item["sha256"]):
                out.append(f"`{label}.sha256` must be 64 lowercase hex characters")
            for field in ("crs", "layer"):
                if item.get(field) is not None and not isinstance(item[field], str):
                    out.append(f"`{label}.{field}` must be a string or null")

    if need("engine", dict):
        for field in ("name", "version"):
            if need(field, str, record["engine"], "engine") and not record["engine"][field]:
                out.append(f"`engine.{field}` must not be empty")

    if need("verification", list):
        if not record["verification"]:
            out.append(
                "`verification` must hold at least one check: a manifest with no checks "
                "at all is a log entry wearing a manifest's clothes"
            )
        for n, check in enumerate(record["verification"]):
            label = f"verification[{n}]"
            if not isinstance(check, dict):
                out.append(f"`{label}` must be an object")
                continue
            if need("name", str, check, label):
                name = check["name"]
                if not name:
                    out.append(f"`{label}.name` must not be empty")
                elif name not in CORE_CHECK_NAMES and not EXTENSION_CHECK_NAME.match(name):
                    out.append(
                        f"`{label}.name` is {name!r}, which is neither a core check "
                        "name (section 3.6) nor an extension named "
                        "`x-<producer>:<name>`. An unconstrained vocabulary makes two "
                        "records incomparable, which is the point of having a format."
                    )
            need("passed", bool, check, label)
            need("detail", str, check, label)
            _optional(out, check, "critical", bool, label)
            for field in ("hint", "argument"):
                if check.get(field) is not None and not isinstance(check[field], str):
                    out.append(f"`{label}.{field}` must be a string or null")

    if "output" in record:
        out_field = record["output"]
        if not isinstance(out_field, dict):
            out.append("`output` must be an object")
        else:
            if need("path", str, out_field, "output"):
                if not out_field["path"]:
                    out.append("`output.path` must not be empty")
                if "\\" in out_field["path"]:
                    out.append("`output.path` carries a backslash: paths use `/` as the "
                               "only separator on every platform")
            if need("sha256", str, out_field, "output") and not SHA256.fullmatch(
                out_field["sha256"]
            ):
                out.append("`output.sha256` must be 64 lowercase hex characters")

    # The RECOMMENDED fields of section 3.4. Optional to emit, typed once
    # emitted: a consumer that finds `notes` holding a bare string instead of a
    # list has to guess, and guessing is what this format exists to remove.
    if _optional(out, record, "crs_decisions", dict):
        decisions = record["crs_decisions"]
        for field in ("analysis_crs", "reason"):
            # A specific message rather than the generic "wrong type": these
            # two are prose a reader checks, and saying so helps whoever emits.
            if field in decisions and not isinstance(decisions[field], str):
                out.append(f"`crs_decisions.{field}` must be a string")
        for field in ("source_crs", "target_crs"):
            if decisions.get(field) is not None and not isinstance(decisions[field], str):
                out.append(f"`crs_decisions.{field}` must be a string or null")
        # Section 3.7: the other values are deliberately NOT all strings. Before
        # draft.3 they were, and the cost was that "was this a ballpark
        # transformation?" could only be answered in prose -- which is what
        # section 7 faults other formats for.
        if _optional(out, decisions, "transformation", dict, "crs_decisions"):
            shift = decisions["transformation"]
            _optional(out, shift, "pipeline", str, "crs_decisions.transformation")
            _optional(out, shift, "is_ballpark", bool, "crs_decisions.transformation")
            accuracy = shift.get("accuracy_m")
            # `isinstance(True, int)` is True in Python, and a boolean accuracy is
            # not a number in JSON: the two implementations must agree on that.
            if accuracy is not None and (
                isinstance(accuracy, bool) or not isinstance(accuracy, (int, float))
            ):
                out.append("`crs_decisions.transformation.accuracy_m` must be a number or null")
    if _optional(out, record, "environment", dict):
        for key, value in record["environment"].items():
            if not isinstance(value, str):
                out.append(
                    f"`environment.{key}` must be a string: section 3.8 records configuration "
                    "as the engine reports it, not as a parsed value"
                )
    if _optional(out, record, "notes", list):
        for n, note in enumerate(record["notes"]):
            if not isinstance(note, str):
                out.append(f"`notes[{n}]` must be a string")
    if _optional(out, record, "repairs", list):
        for n, repair in enumerate(record["repairs"]):
            if not isinstance(repair, dict):
                out.append(f"`repairs[{n}]` must be an object")
    _optional(out, record, "parameters_redacted", bool)
    if _optional(out, record, "producer", dict):
        for field in ("name", "version"):
            _optional(out, record["producer"], field, str, "producer")

    stamps = {}
    for field in ("started_at", "finished_at"):
        if need(field, str) and not TIMESTAMP.fullmatch(record[field]):
            out.append(f"`{field}` must be RFC 3339 in UTC, ending in `Z`")
        elif field in record and isinstance(record[field], str):
            stamps[field] = record[field]
    if len(stamps) == 2 and stamps["finished_at"] < stamps["started_at"]:
        # String comparison is correct here BECAUSE the format is fixed-width
        # UTC: that is one of the reasons the spec requires it.
        out.append("`finished_at` precedes `started_at`")

    return out


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    failed = False
    for name in argv:
        try:
            record = json.loads(Path(name).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"{name}: unreadable ({exc})")
            failed = True
            continue
        found = problems(record)
        for reason in found:
            print(f"{name}: {reason}")
        if found:
            failed = True
        else:
            print(f"{name}: conforming")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
