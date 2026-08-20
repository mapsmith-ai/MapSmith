# MapSmith notebook gallery

Executable, self-contained walkthroughs: nothing to download. Two notebooks
generate their own synthetic data; the terrain one ships with a real DEM
(196 KB, in `fixtures/`), because shaded relief of a synthetic surface shows
the code running but not what the tools actually do. Each one showcases the thing MapSmith is built around: **outputs you
can verify**, with provenance manifests and deterministic checks on disk.

| Notebook | What it shows | Requires |
|---|---|---|
| [01 — Verified geoprocessing](01_verified_geoprocessing.ipynb) | Buffer + clip with automatic UTM handling, provenance manifests, verification checks | `mapsmith` |
| [02 — Terrain and hydrology](02_terrain_hydrology.ipynb) | Hillshade, D8 flow accumulation, six delineated catchments and zonal statistics on a real DEM of **Mount St. Helens** | `mapsmith[raster,whitebox]` |
| [03 — Validated plans](03_validated_plans.ipynb) | A deliberately wrong multi-step plan rejected with machine-actionable errors, then fixed, validated and executed with a plan-level manifest | `mapsmith` |

These notebooks call the Python engines directly so you can run them anywhere;
in an MCP client (Claude, ChatGPT, VS Code, …) the same operations are exposed
as tools, and `preview_map` renders the results on an interactive in-chat map.

## Data

`fixtures/mount_st_helens_dem.tif` is a 520 x 520 clip at 23.7 m of the
**USGS 3D Elevation Program** (U.S. Geological Survey, public domain),
reprojected to UTM zone 10N and rounded to whole metres. It is stored with
DEFLATE and `PREDICTOR=2` — the standard encoding for integer rasters, and
deliberately so: the terrain engine mishandles the TIFF predictor, so MapSmith
converts the input first, and the notebook shows that conversion being
disclosed in the provenance manifest.
