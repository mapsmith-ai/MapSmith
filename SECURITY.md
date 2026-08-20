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

**It does let SQL reach the network, and that is worth understanding before you
deploy it.** Two paths remain open in that mode, both verified by test:

- `ST_Read('/vsicurl/https://…')`, and GDAL's other virtual filesystems. Remote
  reads are deliberately available while the server is unconfined, because
  cloud-native data (COGs over https) is a feature — but GDAL carries its own
  HTTP client, so DuckDB's filesystem block does not apply to it. Consequences
  worth stating plainly: raw SQL can read any HTTP endpoint the host can reach,
  including internal services and cloud metadata endpoints, and can put
  arbitrary text in the URL, which makes it a low-bandwidth way out for data as
  well as in.
- `INSTALL <name> FROM '<url>'` of a *signed* extension: DuckDB fetches it from
  a repository URL the statement is allowed to name before refusing anything
  unsigned. Nothing comes back into the query, but the request goes out with a
  path the statement chose.

With `MAPSMITH_WORKSPACE` set, all of it is refused — `INSTALL`, `LOAD`, DuckDB
filesystems and GDAL's virtual filesystems alike — and the refusal happens
before any request leaves, which `tests/test_duckdb_sandbox.py` asserts by
counting requests at a loopback server rather than by matching an error message.
Reports about unconfined mode are welcome as hardening ideas (an explicit
"local files, no network" mode is on the roadmap); reports about a
workspace-confined server reaching the network are vulnerabilities.

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
