"""Terrain results must be correct whatever layout the input GeoTIFF uses.

whitebox_workflows 2.x only computes correct values from *plain* GeoTIFFs —
uncompressed and strip-organised. Compressed or tiled input yields shaded
relief that looks like terrain and has the right CRS, shape and value range,
so no postcondition can catch it. Real DEMs are compressed, tiled, or both
(every Cloud-Optimized GeoTIFF is tiled), so this is the normal path, and it
is checked here against an independent reference rather than by comparing two
runs to each other — consistency between two wrong answers would pass.
"""

import itertools

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

wb = pytest.importorskip("whitebox_workflows")

from mapsmith.engines import whitebox_engine

SIZE = 300  # wide enough to span two 256-cell tile boundaries
RES = 20.0
LAYOUTS = list(itertools.product(("float32", "int16"), (None, "deflate", "lzw"), (False, True)))


def _dem() -> np.ndarray:
    x, y = np.meshgrid(np.arange(SIZE, dtype="float32"), np.arange(SIZE, dtype="float32"))
    return 40 * np.sin(x / 11) * np.cos(y / 13) + 18 * np.sin(x / 29 + 0.7) + 0.5 * (SIZE - y) + 500


def _reference_hillshade(z: np.ndarray) -> np.ndarray:
    """Horn's method, azimuth 315, altitude 30 — the defaults of our tool."""
    dzdx = (np.roll(z, -1, 1) - np.roll(z, 1, 1)) / (2 * RES)
    dzdy = (np.roll(z, -1, 0) - np.roll(z, 1, 0)) / (2 * RES)
    slope = np.arctan(np.hypot(dzdx, dzdy))
    aspect = np.arctan2(dzdy, -dzdx)
    az, alt = np.radians(360 - 315 + 90), np.radians(30)
    shade = np.cos(alt) * np.cos(slope) + np.sin(alt) * np.sin(slope) * np.cos(az - aspect)
    return shade[2:-2, 2:-2].ravel()


def _write(path, data, dtype, compress, tiled):
    profile = {
        "driver": "GTiff", "height": SIZE, "width": SIZE, "count": 1, "dtype": dtype,
        "crs": "EPSG:32632", "transform": from_origin(500000, 5000000, RES, RES),
        "nodata": -32768.0, "tiled": tiled,
    }
    if tiled:
        profile.update(blockxsize=256, blockysize=256)
    if compress:
        profile.update(compress=compress, predictor=2)
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(data.astype(dtype), 1)


@pytest.mark.parametrize("dtype,compress,tiled", LAYOUTS,
                         ids=[f"{d}-{c or 'raw'}-{'tiled' if t else 'striped'}"
                              for d, c, t in LAYOUTS])
def test_hillshade_is_correct_for_every_input_layout(tmp_path, dtype, compress, tiled):
    data = _dem()
    src, out = tmp_path / "dem.tif", tmp_path / "hs.tif"
    _write(src, data, dtype, compress, tiled)

    whitebox_engine.hillshade(str(src), str(out))

    with rasterio.open(out) as ds:
        got = ds.read(1).astype("float64")[2:-2, 2:-2].ravel()
    r = float(np.corrcoef(got, _reference_hillshade(data.astype(dtype).astype("float64")))[0, 1])
    assert r > 0.95, (
        f"hillshade of a {dtype} {compress or 'uncompressed'} "
        f"{'tiled' if tiled else 'striped'} DEM correlates only {r:.3f} with the "
        "reference: the engine was fed a layout it mishandles"
    )


def test_conversion_is_disclosed_only_when_it_happened(tmp_path):
    """A result computed on a converted copy must say so — and one that was
    not must not claim it."""
    import json

    data = _dem()
    tiled, plain = tmp_path / "tiled.tif", tmp_path / "plain.tif"
    _write(tiled, data, "int16", "deflate", True)
    _write(plain, data, "int16", None, False)

    whitebox_engine.hillshade(str(tiled), str(tmp_path / "a.tif"))
    notes = json.loads((tmp_path / "a.tif.provenance.json").read_text(encoding="utf-8"))["notes"]
    assert any("compressed" in n and "tile-organised" in n for n in notes)

    whitebox_engine.hillshade(str(plain), str(tmp_path / "b.tif"))
    clean = json.loads((tmp_path / "b.tif.provenance.json").read_text(encoding="utf-8"))["notes"]
    assert clean == []


def test_flow_accumulation_agrees_across_layouts(tmp_path):
    """Flow routing propagates a bad cell downstream, so a layout defect here
    is not confined to a seam."""
    data = _dem()
    a, b = tmp_path / "raw.tif", tmp_path / "cog.tif"
    _write(a, data, "float32", None, False)
    _write(b, data, "float32", "deflate", True)

    whitebox_engine.flow_accumulation(str(a), str(tmp_path / "a.tif"))
    whitebox_engine.flow_accumulation(str(b), str(tmp_path / "b.tif"))
    with rasterio.open(tmp_path / "a.tif") as x, rasterio.open(tmp_path / "b.tif") as y:
        assert np.array_equal(x.read(1), y.read(1))


def test_no_temporary_copies_are_left_behind(tmp_path):
    data = _dem()
    src = tmp_path / "dem.tif"
    _write(src, data, "int16", "deflate", True)
    whitebox_engine.hillshade(str(src), str(tmp_path / "hs.tif"))
    assert [p.name for p in tmp_path.rglob("*.plain.tif")] == []
