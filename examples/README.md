# MapSmith notebook gallery

Executable, self-contained walkthroughs — every notebook generates its own
synthetic data, so `pip install mapsmith` (plus the extras noted below) is all
you need. Each one showcases the thing MapSmith is built around: **outputs you
can verify**, with provenance manifests and deterministic checks on disk.

| Notebook | What it shows | Requires |
|---|---|---|
| [01 — Verified geoprocessing](01_verified_geoprocessing.ipynb) | Buffer + clip with automatic UTM handling, provenance manifests, verification checks | `mapsmith` |
| [02 — Terrain and hydrology](02_terrain_hydrology.ipynb) | Hillshade, D8 flow accumulation, watershed delineation, zonal statistics on a synthetic DEM | `mapsmith[raster,whitebox]` |
| [03 — Validated plans](03_validated_plans.ipynb) | A deliberately wrong multi-step plan rejected with machine-actionable errors, then fixed, validated and executed with a plan-level manifest | `mapsmith` |

These notebooks call the Python engines directly so you can run them anywhere;
in an MCP client (Claude, ChatGPT, VS Code, …) the same operations are exposed
as tools, and `preview_map` renders the results on an interactive in-chat map.
