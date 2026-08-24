"""Generate the site's figures from real MapSmith outputs.

Nothing on this site is an illustration. Every image is rendered from a dataset
MapSmith produced, and the manifest shown beside it is the one that landed on
disk with it. This script runs in CI, so the pictures cannot quietly drift away
from what the software actually does — which, on a site whose argument is that
you should not have to take an output on trust, is the only defensible way to
make one.

    python site/build.py [output-dir]
"""

from __future__ import annotations

import json
import shutil
import struct
import sys
import tempfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEM = ROOT / "examples" / "fixtures" / "mount_st_helens_dem.tif"

# Muted, high-contrast, colour-blind safe. Assigned by basin index and never by
# value, so the picture cannot imply an ordering the data does not have — the
# same discipline the manifests apply to numbers.
BASIN_COLOURS = [
    (0xE6, 0x7E, 0x22), (0x27, 0xAE, 0x60), (0x29, 0x80, 0xB9),
    (0x8E, 0x44, 0xAD), (0xC0, 0x39, 0x2B), (0xF1, 0xC4, 0x0F),
]


def run_pipeline(workdir: Path) -> dict[str, Path]:
    """Hillshade and watersheds on a real USGS DEM of Mount St. Helens.

    The same two operations the terrain notebook runs, executed here so the
    figure and its manifest come out of the same process that made them.
    """
    sys.path.insert(0, str(ROOT / "src"))
    import geopandas as gpd
    import rasterio
    from shapely.geometry import Point

    from mapsmith.engines import whitebox_engine

    dem = workdir / "dem.tif"
    shutil.copy(DEM, dem)

    # Where the water actually leaves. A first version put the pour points on a
    # fixed grid, which is defensible and produced almost nothing: most grid
    # positions land on a ridge, and a ridge drains one cell. Flow accumulation
    # says where the drainages are, so the outlets are derived from the terrain
    # instead of asserted over it — and it is a MapSmith operation, so the
    # figure is the product doing a two-step analysis rather than a picture.
    flowacc = workdir / "flowacc.tif"
    whitebox_engine.flow_accumulation(str(dem), str(flowacc))
    outlets_path = workdir / "outlets.gpkg"
    with rasterio.open(flowacc) as ds:
        crs, transform = ds.crs, ds.transform
        outlets = _principal_outlets(ds.read(1, masked=True), transform, count=6)
    gpd.GeoDataFrame(
        {"id": range(1, len(outlets) + 1)},
        geometry=[Point(x, y) for x, y in outlets],
        crs=crs,
    ).to_file(outlets_path, layer="outlets", driver="GPKG")

    hillshade = workdir / "hillshade.tif"
    basins = workdir / "basins.tif"
    whitebox_engine.hillshade(str(dem), str(hillshade))
    whitebox_engine.watershed(str(dem), str(outlets_path), str(basins))
    return {"hillshade": hillshade, "basins": basins, "dem": dem}


def _principal_outlets(accumulation, transform, count: int, separation: int = 60):
    """The `count` highest-accumulation cells, kept `separation` cells apart.

    Without the separation the top cells are all neighbours on one river and
    the result is six nested versions of the same basin. Greedy and
    deterministic: same raster in, same points out, which is what lets the
    figure be a build product rather than a screenshot someone took once.
    """
    import numpy as np

    values = np.ma.filled(accumulation.astype("float64"), -1.0)
    ranked = np.argsort(values, axis=None)[::-1]
    chosen: list[tuple[int, int]] = []
    for flat in ranked:
        row, col = divmod(int(flat), values.shape[1])
        if values[row, col] <= 0:
            break
        if all(max(abs(row - r), abs(col - c)) >= separation for r, c in chosen):
            chosen.append((row, col))
            if len(chosen) == count:
                break
    return [transform * (col + 0.5, row + 0.5) for row, col in chosen]


def png_rgb(pixels: bytes, width: int, height: int) -> bytes:
    """A minimal, deterministic PNG encoder (colour type 2, 8-bit RGB).

    Twenty lines of stdlib instead of a dependency, and deterministic byte for
    byte — which matters because this file is a build product: two builds of the
    same commit produce the same image, so the site cannot drift from the data
    without a diff saying so. `preview.py` carries a sibling of this for
    grayscale-plus-alpha; they stay apart because one is a product feature with
    a lifetime and the other is a page.
    """

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = b"".join(
        b"\x00" + pixels[y * width * 3:(y + 1) * width * 3] for y in range(height)
    )
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def render(hillshade: Path, basins: Path, destination: Path) -> tuple[int, int]:
    """Shaded relief with the watersheds tinted over it.

    Rendered from the arrays rather than through a plotting library, so what is
    on the page is the pixels of the data: no axes, no resampling the reader
    cannot see, no colour ramp doing work the data does not support. Served at
    native resolution for the same reason — upscaling a terrain image invents
    detail, which would be a strange thing to do on this particular site.
    """
    import numpy as np
    import rasterio

    with rasterio.open(hillshade) as ds:
        shade = ds.read(1, masked=True).astype("float32")
    with rasterio.open(basins) as ds:
        basin_data = ds.read(1, masked=True)

    low, high = float(shade.min()), float(shade.max())
    grey = (shade - low) / (high - low) if high > low else shade * 0
    grey = np.ma.filled(grey, 0.0)
    # Lift the black end: pure black hides the terrain in the shadows, which is
    # exactly where the watershed boundaries are most worth seeing.
    canvas = np.stack([0.09 + 0.88 * grey] * 3, axis=-1)

    for index, colour in enumerate(BASIN_COLOURS, start=1):
        mask = np.ma.filled(basin_data == index, False)
        if not mask.any():
            continue
        tint = np.array(colour, dtype="float32") / 255.0
        # Multiply rather than paint over: an overlay that hid the shading would
        # be decoration, and the relief is the evidence.
        canvas[mask] = canvas[mask] * 0.42 + tint * 0.58

    height, width = canvas.shape[:2]
    raw = (np.clip(canvas, 0, 1) * 255).astype("uint8").tobytes()
    destination.write_bytes(png_rgb(raw, width, height))
    return width, height


def highlight(value, indent: int = 0) -> str:
    """The manifest as coloured, escaped HTML — the real one, not a sample.

    Emitted from the parsed object rather than by running regular expressions
    over pretty-printed text. The first version did the latter and produced no
    highlighting at all, because `html.escape` had already turned every quote
    into `&quot;` and none of the patterns matched any more. Walking the data
    cannot have that class of bug: the escaping happens at the leaves, where the
    text actually is.

    Rendered at build time rather than fetched by script, because the evidence
    on a page arguing for checkable evidence should not need JavaScript to
    appear.
    """
    import html
    import re

    pad, inner = "  " * indent, "  " * (indent + 1)
    if isinstance(value, dict):
        if not value:
            return "{}"
        rows = [
            f'{inner}"<span class="k">{html.escape(str(k))}</span>": '
            f"{highlight(v, indent + 1)}"
            for k, v in value.items()
        ]
        return "{\n" + ",\n".join(rows) + f"\n{pad}}}"
    if isinstance(value, list):
        if not value:
            return "[]"
        rows = [f"{inner}{highlight(v, indent + 1)}" for v in value]
        return "[\n" + ",\n".join(rows) + f"\n{pad}]"
    if value is None:
        return '<span class="hash">null</span>'
    if isinstance(value, bool):
        return f'<span class="b">{"true" if value else "false"}</span>'
    if isinstance(value, (int, float)):
        return f'<span class="n">{value}</span>'
    text = html.escape(str(value))
    # A checksum gets its own class so it can recede without being hidden: it is
    # the point of the record, but it is not what the eye should land on first.
    cls = "hash" if re.fullmatch(r"[0-9a-f]{64}", str(value)) else "s"
    return f'"<span class="{cls}">{text}</span>"'


def _git(*args: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", "-C", str(ROOT), *args],
        capture_output=True, text=True, check=False,
    ).stdout.strip()


def main(destination: Path) -> int:
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mapsmith-site-") as tmp:
        workdir = Path(tmp)
        outputs = run_pipeline(workdir)
        dims = render(outputs["hillshade"], outputs["basins"], destination / "terrain.png")
        manifest = json.loads(
            Path(str(outputs["basins"]) + ".provenance.json").read_text(encoding="utf-8")
        )

    # Published as it was written, with only the temporary directory stripped
    # from the input paths. The reader is meant to check this file, not admire
    # it, so nothing else about it is touched.
    for entry in manifest.get("inputs", []):
        entry["path"] = Path(entry["path"]).name
    (destination / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    sys.path.insert(0, str(ROOT / "src"))
    from mapsmith import __version__

    # Counted from the source, never typed into the page. This number was wrong
    # in our own notes once — 54 instead of 16 — because someone counted regex
    # hits, and a claim on a front page has to be produced the same way a result
    # is. Exposed MCP tools, not catalogue entries: the sentence next to it is
    # about what an agent has to choose between.
    tool_count = (ROOT / "src" / "mapsmith" / "server.py").read_text(
        encoding="utf-8"
    ).count("@mcp.tool(")
    test_count = sum(
        int(row.rsplit(":", 1)[1])
        for row in _git("grep", "-c", "^def test_", "--", "tests/").splitlines()
    )

    page = (Path(__file__).parent / "index.template.html").read_text(encoding="utf-8")
    for placeholder, value in {
        "{{MANIFEST}}": highlight(manifest),
        "{{VERSION}}": __version__,
        "{{TOOL_COUNT}}": str(tool_count),
        "{{TEST_COUNT}}": str(test_count),
        "{{COMMIT}}": _git("rev-parse", "--short", "HEAD") or "unknown",
        "{{BUILT}}": manifest["finished_at"][:10],
    }.items():
        page = page.replace(placeholder, value)
    leftovers = [s for s in ("{{",) if s in page]
    if leftovers:
        raise RuntimeError(f"unreplaced placeholder in the template: {page[page.index('{{'):][:40]}")
    (destination / "index.html").write_text(page, encoding="utf-8")
    (destination / ".nojekyll").write_text("", encoding="utf-8")
    (destination / "CNAME").write_text("mapsmith.dev\n", encoding="utf-8")

    weight = (destination / "terrain.png").stat().st_size
    checks = manifest["verification"]
    print(
        f"terrain.png {dims[0]}x{dims[1]} {weight // 1024} KB | "
        f"manifest.json {len(checks)} checks, "
        f"{sum(c['passed'] for c in checks)} passed | "
        f"engine {manifest['engine']['name']} {manifest['engine']['version']} | "
        f"{tool_count} tools, {test_count} tests | index.html "
        f"{(destination / 'index.html').stat().st_size // 1024} KB"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(
        main(Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "site" / "generated"))
    )
