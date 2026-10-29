"""Protocol-level, bounded repair attempts linked by durable plan manifests."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .. import workspace
from .models import Plan, RepairRequest
from .registry import BINDINGS

MAX_REPAIR_ATTEMPTS = 3
MAX_PREVIOUS_MANIFEST_BYTES = 1_000_000


class RepairRefusal(ValueError):
    def __init__(self, code: str, message: str, repair: dict[str, Any]):
        super().__init__(message)
        self.code = code
        self.repair = repair


def initial_context() -> dict[str, Any]:
    return {
        "budget": MAX_REPAIR_ATTEMPTS,
        "attempt": 1,
        "attempts_remaining": MAX_REPAIR_ATTEMPTS - 1,
        "previous_manifest": None,
        "history": [],
    }


def _invalid(raw: str, exc: Exception) -> RepairRefusal:
    return RepairRefusal(
        "INVALID_REPAIR_CHAIN",
        f"the previous manifest is not a valid retryable plan failure: {exc}",
        {**initial_context(), "can_retry": False, "previous_manifest": raw},
    )


def _load(raw: str) -> tuple[Path, dict[str, Any]]:
    try:
        workspace.guard(raw, "previous_manifest")
        path = Path(raw)
        if not path.name.endswith(".plan.json"):
            raise ValueError("not a plan manifest")
        if path.stat().st_size > MAX_PREVIOUS_MANIFEST_BYTES:
            raise ValueError("plan manifest exceeds the one-megabyte repair limit")
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise TypeError("plan manifest is not an object")
        return path, document
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise _invalid(raw, exc) from exc


def _summary(path: Path, document: dict[str, Any]) -> dict[str, Any]:
    return {
        "plan_sha256": document.get("plan_sha256"),
        "executed": False,
        "failed_step": document.get("failed_step"),
        "manifest": str(path),
    }


def _canon(path: str) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def prepare(request: RepairRequest | None, plan: Plan) -> dict[str, Any]:
    if request is None:
        return initial_context()
    raw = request.previous_manifest
    linked: list[tuple[Path, dict[str, Any]]] = []
    next_manifest = raw
    while next_manifest is not None:
        if len(linked) >= MAX_REPAIR_ATTEMPTS:
            raise _invalid(raw, ValueError("more than three linked attempts"))
        path, document = _load(next_manifest)
        linked.append((path, document))
        repair = document.get("repair")
        if not isinstance(repair, dict):
            raise _invalid(raw, ValueError("missing repair metadata"))
        next_manifest = repair.get("previous_manifest")
        if next_manifest is not None and not isinstance(next_manifest, str):
            raise _invalid(raw, ValueError("previous_manifest is not a path"))

    history: list[dict[str, Any]] = []
    for attempt, (path, document) in enumerate(reversed(linked), start=1):
        repair = document["repair"]
        failure = document.get("failure")
        if not isinstance(failure, dict):
            raise _invalid(raw, TypeError("failure metadata is not an object"))
        if (
            repair.get("attempt") != attempt
            or repair.get("history") != history
            or document.get("executed") is not False
            or failure.get("kind") != "verification"
            or (attempt < MAX_REPAIR_ATTEMPTS and repair.get("can_retry") is not True)
        ):
            raise _invalid(raw, ValueError(f"inconsistent attempt {attempt}"))
        history.append(_summary(path, document))

    context = {
        "budget": MAX_REPAIR_ATTEMPTS,
        "attempt": len(linked) + 1,
        "attempts_remaining": max(0, MAX_REPAIR_ATTEMPTS - len(linked) - 1),
        "previous_manifest": raw,
        "history": history,
    }
    if len(linked) >= MAX_REPAIR_ATTEMPTS:
        raise RepairRefusal(
            "RETRY_BUDGET_EXHAUSTED",
            f"the {MAX_REPAIR_ATTEMPTS}-attempt repair budget is exhausted; "
            "no fourth attempt was executed",
            {**context, "can_retry": False},
        )

    candidates = {
        _canon(f"{step.arguments[binding.output_arg]}{suffix}")
        for step in plan.steps
        if (binding := BINDINGS.get(step.operation))
        and binding.output_arg
        and isinstance(step.arguments.get(binding.output_arg), str)
        for suffix in ("", *workspace.AUDIT_SIDECAR_SUFFIXES)
    }
    root = workspace.root()
    for _, document in linked:
        steps = document.get("steps")
        if not isinstance(steps, list) or any(not isinstance(step, dict) for step in steps):
            raise _invalid(raw, TypeError("plan steps are not a list of objects"))
        for step in steps:
            previous_outputs = [step.get("attempted_output"), step.get("output")]
            for path in previous_outputs:
                if path is None:
                    continue
                if not isinstance(path, str):
                    raise _invalid(raw, TypeError("a previous output is not a string"))
                reason = workspace.nonlocal_reason(path)
                if reason or (root and workspace.is_outside(path, root)):
                    raise _invalid(raw, ValueError(f"unsafe previous output path: {path}"))
            previous_artifacts = {
                _canon(f"{path}{suffix}")
                for path in previous_outputs if path is not None
                for suffix in ("", *workspace.AUDIT_SIDECAR_SUFFIXES)
            }
            if candidates.intersection(previous_artifacts):
                raise RepairRefusal(
                    "REPAIR_OUTPUT_COLLISION",
                    "a repair must use new output paths; reusing an earlier output "
                    "would overwrite its provenance and break the attempt chain",
                    {**context, "can_retry": False},
                )
    return context


def metadata(
    context: dict[str, Any], *, executed: bool, failure: dict[str, Any] | None,
    manifest_path: str | None,
) -> dict[str, Any]:
    verification_failure = not executed and failure and failure.get("kind") == "verification"
    return {
        **context,
        "can_retry": bool(
            verification_failure
            and manifest_path is not None
            and context["attempt"] < MAX_REPAIR_ATTEMPTS
        ),
    }
