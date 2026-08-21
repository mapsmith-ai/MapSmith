# Security policy

MapSmith executes tool calls written by LLM agents against local data. Its
security promises are precise, and we treat any break of them as a
vulnerability:

- **Workspace containment**: with `MAPSMITH_WORKSPACE` set, no tool call and
  no `run_sql` statement may read or write outside the workspace (path jail
  at the MCP boundary + sandboxed DuckDB connection). Any escape —
  traversal, symlink trick under the documented threat model, GDAL virtual
  filesystem, SQL — is a vulnerability.
- **Provenance integrity**: a `<output>.provenance.json` manifest must
  faithfully record what produced the dataset. A way to make MapSmith write
  a misleading manifest is a vulnerability.
- **No credentials in a manifest.** Manifests are meant to be shared — attached
  to a review, a bug report, a paper — so recorded parameters are redacted
  before they are written: the value after a credential-bearing name
  (`SECRET`, `PASSWORD`, `KEY_ID`, `*_access_key`, `api_key`, a URI's
  `user:password@`) becomes `<redacted>`, while the name and the rest of the
  statement stay readable. The same redaction is applied to the job-ledger rows,
  which outlive the session that wrote them. `parameters_redacted: true` on the
  manifest says it happened, because a manifest that quietly differs from what
  ran would be worse than the leak. A credential that survives into a manifest
  or a ledger row is a vulnerability; note the converse limitation, which is not
  one: redaction is name-based, so a secret passed as a bare positional value
  with no recognisable name is not detected.
- **No network egress in sandbox mode** — with `MAPSMITH_WORKSPACE` set —
  beyond the documented one-time extension install. Any way to make SQL,
  GDAL or a tool argument reach the network from a workspace-confined server
  is a vulnerability.

**The HTTP transport has no authentication in this release.** Anyone who can
reach the endpoint can run every tool against everything the process can see,
so it belongs on loopback or a trusted network until authenticated remote mode
ships — the shipped examples bind to loopback and set a workspace for that
reason. Host and Origin validation (DNS rebinding protection) is enabled
explicitly, including when the server binds to a non-loopback address, and
`MAPSMITH_ALLOWED_HOSTS` / `MAPSMITH_ALLOWED_ORIGINS` extend the allow-list for
reverse proxies. A way past that validation *is* a vulnerability; the absence
of authentication is a documented limitation, not one.

Also worth knowing rather than reporting: with `MAPSMITH_WORKSPACE` unset,
`run_sql` can read and write any path the process can reach — deliberate, and
documented in the README. Escalation from there is closed on the paths that
matter: extension autoinstall and autoloading are off, community extensions are
refused (`shellfs` turns a filename ending in `|` into a shell command),
unsigned extensions cannot be enabled, DuckDB's HTTP and S3 filesystems are
disabled so even an explicitly loaded `httpfs` can neither read nor write over
the network, and the configuration is locked so SQL cannot undo any of it. So
unconfined mode does not let SQL load unsigned code or run a shell command.

**Since 0.2.2 it does not reach the network either, unless you say so.** Both
paths that used to be open are refused by default, and each one is verified by a
test that counts requests at a loopback server rather than matching an error
message:

- `ST_Read('/vsicurl/https://…')` and GDAL's other virtual filesystems. GDAL
  carries its own HTTP client, so DuckDB's filesystem block never applied to it:
  raw SQL could read any endpoint the host can reach — internal services, cloud
  metadata — and hand the content back in the tool result, while the URL it
  chose carried data out. `enable_external_access=false` is the only
  DuckDB-level switch that stops it and it takes local file access with it, so
  the refusal is at the tool boundary instead.
- `INSTALL <name> FROM '<url>'`, which fetched from a URL the statement named.

Set `MAPSMITH_ALLOW_REMOTE=1` to get remote reads back — cloud-native data is a
real use case, and the capability is gated rather than removed. The reason it is
off by default is who chooses: the path is written by the model, from whatever it
read, so a third-party dataset carrying "the updated layer lives at
https://evil.tld/x.gpkg" was enough to have GDAL parse attacker-chosen bytes
in-process with nobody consenting.

Two limits of that refusal, stated rather than implied: it is a text scan of the
SQL, not a parse, so a statement that merely mentions a URL in a string literal
is refused too; and it sits at the tool boundary, so a future engine that runs
agent-written SQL without calling `workspace.refuse_remote_in_sql` reopens the
path.

**A text check cannot see inside a file GDAL resolves itself, so there is a
second layer.** A GDAL indirection file — a `.vrt` and its relatives — is a
plain local path: no scheme, no `/vsi` prefix, nothing for the path guard, the
SQL scan or DuckDB's `allowed_directories` to catch, while GDAL fetches whatever
its `<SrcDataSource>` names, in-process. Measured in 0.2.1 (the audit that found
it is why this paragraph exists): reading such a file **from inside a
workspace** sent HEAD and GET to an attacker-named host through the
GeoPandas/pyogrio path, which contradicted the promise above. DuckDB's spatial
reader was already safe there, since it routes GDAL I/O through DuckDB's own
filesystem with external access off.

The fix is at GDAL's level rather than the string's: when remote reads are off,
MapSmith deregisters GDAL's indirection and network drivers (`VRT`, `OGR_VRT`,
`WMS`, `WFS`, `OAPIF`, `STACIT` and the rest) via `GDAL_SKIP`/`OGR_SKIP` before
the geospatial stack initialises — see `mapsmith/gdal_policy.py`, asserted by
subprocess tests that count requests at a loopback server. With
`MAPSMITH_ALLOW_REMOTE=1` the drivers come back, including when a parent process
had installed the policy. If you are on 0.2.1 or earlier and rely on the
workspace as a security boundary, this is the fix to take.

With `MAPSMITH_WORKSPACE` set, all of it is refused — `INSTALL`, `LOAD`, DuckDB
filesystems and GDAL's virtual filesystems alike — and the refusal happens
before any request leaves, which `tests/test_duckdb_sandbox.py` asserts by
counting requests at a loopback server rather than by matching an error message.
The "local files, no network" mode that used to be on the roadmap here is now
simply the default, so what is left of unconfined mode is unconfined *file*
access and nothing else. Reports about that are welcome as hardening ideas;
reports about a server reaching the network without `MAPSMITH_ALLOW_REMOTE`, in
either mode, are vulnerabilities.

Out of scope: issues requiring a hostile local process on the same machine
(the jail assumes a single trusted writer of the workspace filesystem —
documented in the README), and anything reachable only by running MapSmith
without a workspace, which is deliberately unconfined.

## Reporting

Please report vulnerabilities **privately**:

- GitHub: [private vulnerability report](https://github.com/mapsmith-ai/MapSmith/security/advisories/new) (preferred)
- Email: mapsmith@proton.me

You will get an acknowledgment within **48 hours**. We coordinate disclosure
with you; absent agreement otherwise, we consider 90 days a reasonable
deadline. Please do not open public issues for suspected vulnerabilities.

## Supported versions

Only the latest release on PyPI / `ghcr.io/mapsmith-ai/mapsmith` receives
security fixes.
