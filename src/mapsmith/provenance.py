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
from datetime import UTC, datetime
from pathlib import Path, PurePath
from typing import Any

from . import __version__

# The manifest specification this record targets. MapSmith is one
# implementation of that format, not its definition: the spec, its schema, a
# toolchain-free validator and the conformance suite live in their own
# repository, and a CI test validates real MapSmith output against them.
SPEC_VERSION = "1.0.0-draft.3"

#: One `crs_decisions` key for "a secondary input was brought into the analysis
#: CRS", and a structured value rather than a sentence.
#:
#: Written here rather than at the two call sites because there were two names
#: for it — `reference_reprojected` in `snap_layer`, `second_layer_reprojected`
#: in `line_intersections` — which is the defect D-077 removed from the
#: specification's vocabulary, reappearing one level down in our own. The value
#: is a list of `{"argument": ..., "from": ...}`: section 3.7 argues at length
#: that `is_ballpark` is a boolean because a consumer has to branch on it, and
#: `"EPSG:4326 -> EPSG:32632"` is prose in a key for the same reason it should
#: not be. Where it went is already `analysis_crs`, so only where it came from
#: is worth recording.
INPUTS_REPROJECTED = "x-mapsmith:inputs_reprojected"


def _utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    # Azure's shared-access signature spells it `sig`, and the Planetary
    # Computer hands out exactly that form: `?sv=...&sig=...&se=...`. Its AWS
    # equivalent (`X-Amz-Signature`) was already covered, so the same signed URL
    # was redacted from one cloud and printed in clear from the other. Safe to
    # add because every rule below anchors the name — on a query parameter it
    # must be followed by `=`, and `?design=` ends in `ign`, not `sig`.
    "sig",
)
# `api_key` and `x-api-key` are the same name. The list is written with
# underscores because that is how SQL spells it, and an HTTP header spells it
# with hyphens — so `x_api_key` was masked and `x-api-key` went through
# untouched. Each underscore accepts either separator.
_NAMES = "|".join(name.replace("_", "[-_]") for name in _SECRET_NAMES)

#: A whole identifier that names a credential: an optional prefix ending in a
#: separator, then one of the names above, and nothing of the identifier after
#: it. Written once because the key matcher and the assignment matcher have to
#: agree — they did not, and the disagreement was visible inside a single
#: record.
#:
#: The left boundary carries as much weight as the right. Without it `.search`
#: finds `sas` in the middle of `arkansas`, and a column named after the state
#: comes back `<redacted>`.
_KEY_NAME = r"(?<![A-Za-z0-9_.\-])(?:[A-Za-z0-9_.\-]*[_.\-])?(?:" + _NAMES + r")"

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

#: A dictionary key that names a credential. Same vocabulary as the assignment
#: scanner, and the same restraint: bare `key` is deliberately NOT in the list,
#: because `sort_key`, `primary_key` and `key` itself are ordinary field names,
#: and a redaction that fires on those teaches its reader to distrust the mask.
#:
#: **The name must END with the credential word**, and until 2026-09-06 it did
#: not: the word could sit anywhere with a separator on each side, so `sig_figs`,
#: `token_count`, `signature_file` and `signature_field` were all masked. Two of
#: those are GIS vocabulary — a spectral signature file is a real thing, and the
#: day MapSmith ships supervised classification the manifest would hide the name
#: of the training file and raise `parameters_redacted` on a record no secret
#: ever passed through. Masking `sig_figs: 4` also changed the value's JSON type
#: from a number to a string.
#:
#: Worse than either: the assignment scanner already required the suffix, so one
#: name got two answers in one record — `sig_figs = 4` inside a note went
#: through, `{"sig_figs": 4}` in parameters came back redacted. A mask that
#: answers differently about the same word is a mask its reader stops believing,
#: which is the whole asset being protected here.
_SECRET_KEY = re.compile(r"^" + _KEY_NAME + r"$", re.IGNORECASE)

# Same name shape as the key matcher, from the same definition, so the two
# cannot drift apart again. Measured on 2026-09-06 against 31 real credential
# names and 23 ordinary ones: every credential still masked, every ordinary name
# left alone, and `arkansas='x'` — which the old form redacted — left alone too.
_SECRET_ASSIGNMENT = re.compile(
    r"(?is)(?P<name>" + _KEY_NAME + r")\b"
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
        # The KEY is a name too. `{"AWS_SECRET_ACCESS_KEY": "AKIA..."}` used to
        # pass through untouched, because the scanner looks for `name=value`
        # INSIDE a string and here the name is not inside the string — it is
        # the key. The gap belongs to every dict a manifest carries, and
        # `environment` is simply the field that makes it obvious, holding
        # environment variables by definition.
        return {
            k: REDACTED if _SECRET_KEY.search(str(k)) else redact_secrets(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(redact_secrets(v) for v in value)
    return value


@dataclass
class InputRecord:
    path: str
    sha256: str
    crs: str | None = None
    # Which layer was read, for containers holding more than one. The spec asks
    # for it in exactly these words: "without it, an auditor holding a
    # five-layer container and this record cannot tell which layer produced the
    # numbers." Since #29 MapSmith knows the answer — it refuses containers
    # with no chosen layer — so leaving the field empty was recording ignorance
    # it no longer had.
    layer: str | None = None

    @classmethod
    def from_path(
        cls, path: str | Path, crs: str | None = None, layer: str | None = None
    ) -> InputRecord:
        return cls(
            path=posix_path(path), sha256=sha256_of(path), crs=crs, layer=layer
        )


def posix_path(path: str | Path) -> str:
    """The path as the manifest records it: `/` as separator, on every host.

    A lineage record exists to be held next to someone else's. Storing
    ``str(path)`` gave a backslash path on Windows and a forward-slash one
    elsewhere, for the same run on the same bytes, so two correct manifests
    differed in a field that describes nothing about the computation, and any
    consumer keying on the path had two entries for one file (#30).

    Only the separator is normalised, and only on a host that has one:
    ``PurePath`` is ``PureWindowsPath`` on Windows and ``PurePosixPath``
    elsewhere, so a backslash arriving on Linux is left alone — there it is a
    legal character in a filename, and rewriting it would corrupt a real path.
    That is the right shape anyway: the normalisation happens on the only host
    that can produce the problem.

    The path is otherwise recorded as it was given. Rewriting an absolute path
    to a relative one, or the reverse, would misstate what actually ran.
    """
    return PurePath(str(path)).as_posix()


@dataclass
class ProvenanceRecord:
    operation: str
    parameters: dict[str, Any]
    inputs: list[InputRecord]
    # First among the defaulted fields on purpose: a reader meeting the record
    # needs the format version before anything else means anything.
    spec_version: str = SPEC_VERSION
    # `Any` and not `str`: since 2026-09-06 one value is a list of objects
    # (`x-mapsmith:inputs_reprojected`), and section 3.7 permits that on
    # purpose -- only `analysis_crs` and `reason` are strings there, because
    # the question a consumer most needs answered (`is_ballpark`) has to be a
    # boolean it can branch on. The annotation said `str` for a day.
    crs_decisions: dict[str, Any] = field(default_factory=dict)
    engine: dict[str, str] = field(default_factory=dict)
    verification: list[dict[str, Any]] = field(default_factory=list)
    # deterministic repair attempts (issue #3): empty unless something was fixed
    repairs: list[dict[str, Any]] = field(default_factory=list)
    # disclosures about how the inputs were handled before the engine saw them
    notes: list[str] = field(default_factory=list)
    # Section 3.8 of the specification: the configuration that influenced the
    # result and lives neither in the data nor in the call — a georeferencing
    # source, a GDAL variable that changes how a file is read, the presence of
    # a datum grid on this machine.
    #
    # It exists because `parameters` holds what the caller asked for and
    # `engine` holds what computed it, and neither holds the state of the
    # machine — which can change the number. The same thousand-metre square
    # measures 1,000,530.603 m2 or exactly 1,000,000 depending on a project
    # setting that appears in no argument and no output.
    #
    # **What goes in it is what a producer KNOWS influenced the result, never a
    # dump of the environment.** A record that listed forty variables would
    # bury the one that mattered, and the reader would learn to skip the field.
    # Empty or absent claims nothing, exactly like an absent `crs_decisions`.
    #
    # Argleton trap 030 is the measurement that made this concrete rather than
    # theoretical: a GeoTIFF and the `.aux.xml` beside it declare different
    # georeferencing, GDAL prefers the sidecar by documented design, and until
    # this field existed nothing in a MapSmith manifest could say which of the
    # two produced the number.
    environment: dict[str, str] = field(default_factory=dict)
    # `producer` is the spec's field for "the software that emitted this
    # record, as distinct from the engine that computed the result", and until
    # now MapSmith declared its version in a field of its own invention. Both
    # are emitted: the spec field so a third-party reader finds what the spec
    # told it to look for, and `mapsmith_version` because manifests already on
    # disk carry it and something out there may key on it.
    producer: dict[str, str] = field(
        default_factory=lambda: {"name": "mapsmith", "version": __version__}
    )
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
        self._redact()

    def _redact(self) -> None:
        """Mask credentials in every field that can carry one. Idempotent.

        Not only `parameters`: a signed URL reaches a manifest as an input
        path, and a CRS decision or a note quotes the argument it was made
        about. Redacting one field and publishing the others would be a
        redaction in name only.

        Run at construction AND again in `write_for`, which is the single point
        where a manifest becomes a file. Construction alone was not enough and
        the gap was invisible: no engine passes `crs_decisions` or `notes` to
        the constructor — every one of them assigns after — so those two fields
        were never actually redacted on any shipped path, while SECURITY.md
        said they were. The test that covered them passed both as constructor
        arguments, a shape no caller uses: green, and proving nothing.

        Redacting twice costs nothing because masking an already-masked value
        is a no-op, and it means a field added later is covered without anyone
        having to remember this method exists.
        """
        safe_params = redact_secrets(self.parameters)
        safe_decisions = redact_secrets(self.crs_decisions)
        safe_notes = redact_secrets(self.notes)
        # An environment variable carries a credential as readily as a path
        # does — `AWS_SECRET_ACCESS_KEY` is an environment variable — so the
        # field joins the others here rather than being remembered later.
        safe_environment = redact_secrets(self.environment)
        safe_paths = [redact_secrets(i.path) for i in self.inputs]
        safe_layers = [
            redact_secrets(i.layer) if i.layer is not None else None for i in self.inputs
        ]
        safe_verification = redact_secrets(self.verification)
        safe_repairs = redact_secrets(self.repairs)
        changed = (
            safe_params != self.parameters
            or safe_decisions != self.crs_decisions
            or safe_notes != self.notes
            or safe_paths != [i.path for i in self.inputs]
            or safe_layers != [i.layer for i in self.inputs]
            or safe_verification != self.verification
            or safe_repairs != self.repairs
            or safe_environment != self.environment
        )
        self.parameters = safe_params
        self.crs_decisions = safe_decisions
        self.notes = safe_notes
        self.verification = safe_verification
        self.repairs = safe_repairs
        self.environment = safe_environment
        for record, path, layer in zip(self.inputs, safe_paths, safe_layers, strict=True):
            record.path = path
            record.layer = layer
        if changed:
            self.parameters_redacted = True

    def _record_environment(self) -> None:
        """Fill `environment` from the inputs, for anything that changed the answer.

        Today that means one thing: a raster georeferenced twice, where GDAL's
        documented preference for a `.aux.xml` sidecar decides the numbers and
        nothing else in the record could say so (Argleton trap 030).

        Costs a `stat` per input in the ordinary case — the sidecar check is a
        file-existence test that returns nothing when there is no second source.
        Anything already set by the operation is kept: an engine that knows
        something about its own environment knows it better than this does.
        """
        from . import grid

        for entry in self.inputs:
            try:
                found = grid.georeferencing_source(entry.path)
            except Exception:  # noqa: BLE001, S112 — see below
                # An input that is not a raster has nothing to disambiguate, and
                # a failure to LOOK is not a finding. Swallowing cannot hide a
                # defect here: nothing downstream reads a value this did not set.
                continue
            for key, value in found.items():
                self.environment.setdefault(key, value)

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

    def write_for(self, output_path: str | Path, *, with_output_digest: bool = True) -> Path:
        """Write the manifest next to the output it describes.

        The record carries the OUTPUT's digest too, computed here — the one
        moment the final bytes certainly exist (verification and any repair
        have already run). Without it a consumer cannot check that the sidecar
        describes the bytes next to it, and the record could not become the
        predicate of an in-toto attestation, whose subject requires a digest.

        `with_output_digest=False` drops that one field, and exists for exactly
        one caller: `verify.audit_on_failure`, writing after an engine crash.
        Hashing the output is the only part of this method that reads a file the
        crashed engine may still hold open, so it is the part most likely to
        raise — and section 3.1 says a manifest MUST be written even then. A
        record without `output` is valid (the schema allows its absence) and
        useless only for checking bytes; a record that was never written is
        useless for everything.
        """
        # Section 3.8, filled here for the same reason redaction runs here: this
        # is the single point where a manifest becomes a file. `verify.audited`
        # looked like the place — it is where the invariants are made
        # unmissable — and it is not: seventeen writers of fifty-seven build their
        # record by hand, and they are concentrated in raster, which is exactly
        # where georeferencing decides the numbers. A hook a quarter of the
        # writers bypass is not a hook.
        #
        # And the same sentence is why the empty-verification case is caught
        # here. The specification wants two things that met in one place and
        # got one at the other's expense: §3 requires at least one check ("a
        # manifest with no checks at all is a log entry wearing a manifest's
        # clothes") and §4 requires a record for every dataset written.
        # `verify.audited` closed that on its own path on 2026-09-05 by
        # recording the absence as a failed check — but those seventeen writers
        # never reach it, and `verify.enforce([])` does not raise, so a caller
        # was handed `success` beside a manifest BOTH implementations reject.
        # That is strictly worse than what `audited` used to do, which was at
        # least loud.
        #
        # Appending rather than raising, and the direction is the whole point:
        # raising here would recreate the defect one level down, because the
        # dataset is already on disk by the time any manifest is written. What
        # stops this from becoming a quiet excuse is not politeness, it is
        # `test_no_shipped_operation_reaches_the_absent_verification_fallback`:
        # if a real operation ever lands here, the suite goes red rather than
        # shipping a manifest whose only content is "nobody looked".
        if not self.verification:
            from .verify import verification_absent

            self.verification = [verification_absent(self.operation).as_dict()]
        self._record_environment()
        self._redact()
        record = asdict(self)
        if with_output_digest and Path(output_path).exists():
            # After `inputs`, where a reader expects it; through the same
            # redaction as everything else, because an output path can carry a
            # signed URL exactly like an input path can.
            entry = redact_secrets(
                {"path": posix_path(output_path), "sha256": sha256_of(output_path)}
            )
            record = {}
            for key, value in asdict(self).items():
                record[key] = value
                if key == "inputs":
                    record["output"] = entry
        manifest_path = Path(f"{output_path}.provenance.json")
        manifest_path.write_text(
            json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
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
