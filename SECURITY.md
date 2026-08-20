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
- **No network egress in sandbox mode** beyond the documented one-time
  extension install.

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
unsigned extensions cannot be enabled, the HTTP and S3 filesystems are disabled
so even an explicitly loaded `httpfs` can neither read nor write over the
network, and the configuration is locked so SQL cannot undo any of it. What
remains reachable in that mode — and only in that mode — is `INSTALL <name>` of
a *signed* extension: DuckDB fetches it, from a repository URL the statement is
allowed to name, before refusing anything unsigned. So unconfined mode does let
SQL trigger one outbound request; it does not let it load unsigned code or move
data over the network. With `MAPSMITH_WORKSPACE` set, both `INSTALL` and `LOAD`
are refused outright.

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
