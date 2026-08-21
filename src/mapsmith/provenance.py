"""Lineage manifests for every MapSmith output.

Every operation that writes a dataset also writes ``<output>.provenance.json``
next to it. The manifest is the product's core promise: any result can be
audited and re-run the analysis without an LLM in the loop.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_of(path: str | Path, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


REDACTED = "<redacted>"
_QUOTED_REDACTED = f"'{REDACTED}'"

# Names that carry a credential in the SQL dialects, map literals and URIs
# MapSmith touches: DuckDB secrets (CREATE SECRET ... KEY_ID '...'), httpfs/S3
# settings, ATTACH ... (PASSWORD '...'), signed-URL query parameters, and
# generic API tokens. Matched as the SUFFIX of a longer identifier too, because
# `s3_secret_access_key` ends in one of these and a plain word boundary misses
# it (an underscore is a word character).
_SECRET_NAMES = (
    "secret", "secret_access_key", "session_token", "password", "passwd", "pwd",
    "passphrase", "key_id", "access_key_id", "token", "api_key", "apikey",
    "auth", "authorization", "bearer", "client_secret", "private_key",
    "connection_string", "conninfo", "credential", "credentials", "sas",
    "signature",
)
_NAMES = "|".join(_SECRET_NAMES)

# A gap between a name and its value: whitespace, a block comment, a line
# comment. An audit hid a secret behind `SECRET /* c */ 'shh'`, which the first
# version of this matcher walked straight past.
_GAP = r"(?:\s|/\*.*?\*/|--[^\n]*(?:\n|$))+"

# Every spelling of a string literal these dialects allow, because getting this
# wrong is not a miss but a corruption: on `SECRET E'shh\'x'` the first version
# paired the quotes one argument off, redacted the WRONG value and kept the
# secret — a manifest both misleading and leaky.
_VALUE = (
    r"(?P<v>"
    r"[Ee]'(?:\\.|''|[^'\\])*'"                    # E'...' with backslash escapes
    r"|'(?:''|[^'])*'"                             # '...' with doubled quotes
    r'|"(?:""|[^"])*"'
    r"|\$(?P<tag>[A-Za-z0-9_]*)\$.*?\$(?P=tag)\$"  # $$...$$ and $tag$...$tag$
    # Bare token, masked only after `=`. It must stop at `&` and at a quote, or
    # a signed URL loses everything after its credential parameter *and* its
    # closing quote — `?X-Amz-Signature=abc&y=1'` came back as
    # `?X-Amz-Signature=<redacted>`, destroying the rest of the URL and leaving
    # SQL that no longer parses. Unquoted SQL values never contain either.
    r"|[^\s,;)'\"&]+"
    r")"
)

# `(?:GAP)?` and not `GAP?`: _GAP already ends in `+`, so appending `?` makes it
# `+?` — a LAZY quantifier that still requires one character — and `token='x'`
# with no space stopped matching at all. Caught by the test that had covered
# that exact form since the first version.
_OPTIONAL_GAP = "(?:" + _GAP + ")?"

_SECRET_ASSIGNMENT = re.compile(
    r"(?is)(?P<name>[A-Za-z0-9_.\-]*(?:" + _NAMES + r"))\b"
    r"(?P<sep>" + _OPTIONAL_GAP + r"(?::=|=)" + _OPTIONAL_GAP + r"|" + _GAP + r")"
    + _VALUE
)

# `MAP{'Authorization': 'Bearer x'}` and its JSON-shaped equivalents: here the
# credential name is a quoted KEY, not an identifier, so the rule above never
# sees it. This is the syntax an audit found in the one secret type MapSmith's
# sandbox can actually construct.
_QUOTED_PAIR = re.compile(
    r"(?is)(?P<key>['\"][A-Za-z0-9_.\-]*(?:" + _NAMES + r")['\"]\s*:\s*)"
    r"(?P<v>'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\")"
)

# userinfo in a URI: scheme://user:password@host
_URI_PASSWORD = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://[^\s:/@]+):([^\s@/]+)@")

# A signed URL carries its credential in the query string, and that reaches a
# manifest through `inputs[].path` rather than through SQL text.
_URI_QUERY = re.compile(
    r"(?i)([?&][A-Za-z0-9_.\-]*(?:" + _NAMES + r")=)([^&\s'\"]+)"
)


def _mask_assignment(match: re.Match) -> str:
    """Redact a credential value, but only where it really is one.

    Requires the value to be quoted (in any spelling above) or introduced by
    `=`, which covers every syntax MapSmith can meet while leaving
    `CREATE SECRET my_bucket (...)` readable — the secret's *name* is not a
    credential, and hiding it would cost the reader the one detail that says
    which statement ran.

    A quoted value is replaced by a QUOTED placeholder. A bare `<redacted>`
    where a string literal used to be leaves a statement that no longer parses,
    and a manifest people are invited to attach to a bug report should survive
    being pasted back into a client.
    """
    name, separator, value = match.group("name"), match.group("sep"), match.group("v")
    if value[:1] in "'\"$" or value[:2].lower() == "e'":
        return f"{name}{separator}{_QUOTED_REDACTED}"
    if "=" in separator:
        return f"{name}{separator}{REDACTED}"
    return match.group(0)


def redact_secrets(value: Any) -> Any:
    """Mask credential values inside anything recorded in a manifest.

    A provenance manifest is meant to be shared — attached to a review, a bug
    report, a paper — which is exactly why a credential must never reach one
    (issue #18).

    This is deliberately the *second* line of defence. SQL that configures a
    credential is refused before it runs (`sql_policy`), because redaction is a
    text scan without a parser and an adversarial audit escaped it four ways in
    minutes. What is left for this function is everything that reaches a
    manifest without being SQL at all: a signed URL in an input path, a
    connection string passed as a tool argument.

    The name is kept and only the value is masked, so the record still shows
    what ran; `parameters_redacted` on the record says it happened, because a
    manifest that quietly differs from what executed would be a worse bug than
    the leak. Applied to every ProvenanceRecord and every job-ledger row rather
    than at the call sites: a redaction a future engine can forget is not a
    redaction.

    Known limits, stated rather than implied: detection is name-based, so a
    secret passed as a bare positional value with no recognisable name is not
    detected, and neither is a URI that percent-encodes the colon of its own
    userinfo (`user%3Apass@host`).
    """
    if isinstance(value, str):
        masked = _URI_PASSWORD.sub(rf"\1:{REDACTED}@", value)
        masked = _URI_QUERY.sub(rf"\1{REDACTED}", masked)
        masked = _QUOTED_PAIR.sub(rf"\g<key>{_QUOTED_REDACTED}", masked)
        return _SECRET_ASSIGNMENT.sub(_mask_assignment, masked)
    if isinstance(value, dict):
        return {k: redact_secrets(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(redact_secrets(v) for v in value)
    return value


@dataclass
class InputRecord:
    path: str
    sha256: str
    crs: str | None = None

    @classmethod
    def from_path(cls, path: str | Path, crs: str | None = None) -> InputRecord:
        return cls(path=str(path), sha256=sha256_of(path), crs=crs)


@dataclass
class ProvenanceRecord:
    operation: str
    parameters: dict[str, Any]
    inputs: list[InputRecord]
    crs_decisions: dict[str, str] = field(default_factory=dict)
    engine: dict[str, str] = field(default_factory=dict)
    verification: list[dict[str, Any]] = field(default_factory=list)
    # deterministic repair attempts (issue #3): empty unless something was fixed
    repairs: list[dict[str, Any]] = field(default_factory=list)
    # disclosures about how the inputs were handled before the engine saw them
    notes: list[str] = field(default_factory=list)
    mapsmith_version: str = __version__
    started_at: str = field(default_factory=_utcnow)
    finished_at: str | None = None
    # Set when redaction changed anything in this record — parameters, CRS
    # decisions, notes or an input path. The name predates the other three
    # fields being covered and is kept on purpose: manifests already published
    # carry it, and readers key on it. A manifest that silently differs from
    # what ran would be worse than the leak it prevents.
    parameters_redacted: bool = False

    def __post_init__(self) -> None:
        # Not only `parameters`: a signed URL reaches a manifest as an input
        # path, and a CRS decision or a note quotes the argument it was made
        # about. Redacting one field and publishing the others would be a
        # redaction in name only.
        safe_params = redact_secrets(self.parameters)
        safe_decisions = redact_secrets(self.crs_decisions)
        safe_notes = redact_secrets(self.notes)
        safe_paths = [redact_secrets(i.path) for i in self.inputs]
        changed = (
            safe_params != self.parameters
            or safe_decisions != self.crs_decisions
            or safe_notes != self.notes
            or safe_paths != [i.path for i in self.inputs]
        )
        self.parameters = safe_params
        self.crs_decisions = safe_decisions
        self.notes = safe_notes
        for record, path in zip(self.inputs, safe_paths):
            record.path = path
        if changed:
            self.parameters_redacted = True

    def add_verification(self, checks: list[Any]) -> ProvenanceRecord:
        """Attach deterministic check results (objects with .as_dict())."""
        self.verification.extend(c.as_dict() for c in checks)
        return self

    def add_repairs(self, attempts: list[dict[str, Any]]) -> ProvenanceRecord:
        """Attach deterministic repair attempts (see verify.repair_and_reverify)."""
        self.repairs.extend(attempts)
        return self

    def finish(self) -> ProvenanceRecord:
        self.finished_at = _utcnow()
        return self

    def write_for(self, output_path: str | Path) -> Path:
        """Write the manifest next to the output it describes."""
        manifest_path = Path(f"{output_path}.provenance.json")
        manifest_path.write_text(
            json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return manifest_path


def read_provenance(output_path: str | Path) -> dict[str, Any]:
    """Read the lineage manifest of a MapSmith output, if present."""
    manifest_path = Path(f"{output_path}.provenance.json")
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"No provenance manifest found for {output_path}. "
            "Either it was not produced by MapSmith or the manifest was moved."
        )
    return json.loads(manifest_path.read_text(encoding="utf-8"))
