# MapSmith contributor instructions

Read `CLAUDE.md` before changing code. It is the canonical contributor guide and defines the non-negotiable invariants: deterministic engines, provenance on every writer, verification before enforcement, explicit CRS discipline, the GPL process boundary, a small semantic tool surface, and GeoParquet as the analytical format.

## Layout

- `src/mapsmith/server.py`: MCP boundary and tool contracts.
- `src/mapsmith/catalog.py`: searchable capability declarations.
- `src/mapsmith/plans/`: typed plans, registry, validation and execution.
- `src/mapsmith/engines/`: deterministic geoprocessing engines.
- `src/mapsmith/readers.py`: the only vector dataset reading layer.
- `src/mapsmith/provenance.py`, `verify.py`, `workspace.py`: lineage, verification and containment.
- `tests/`: closed-form behavior and rejection-path tests.

Keep catalog entries, runtime bindings, tool exposure and tests synchronized. Treat every path and SQL statement as untrusted input. Verify third-party APIs against primary documentation before using them.

## Environment and checks

Python 3.12 or newer is required. Use the repository virtual environment when present:

```bash
.venv/Scripts/python -m ruff check .
.venv/Scripts/python -m pytest -q
```

The supported installation paths are Docker or `uvx` with working wheels. Do not add dependencies without confirming that the capability is missing and recording the version constraints.

Public artifacts, including code identifiers, comments and commit messages, are in English. Do not copy private workspace documentation, credentials, local paths or third-party course material into this repository. Do not commit, push, tag or release unless explicitly requested.
