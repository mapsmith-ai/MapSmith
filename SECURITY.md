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
