"""Executable operation registry: binds catalog operations to engine functions.

The catalog (`mapsmith.catalog`) is the documentation layer; this module is the
runtime layer. A test enforces that the two stay in sync (every `available`
catalog operation has exactly one binding here), so agent-facing docs and
executable reality cannot drift apart.

Loaders import lazily: operations behind optional extras must not break import
of the plans package when the extra is absent — availability is a validation
outcome, not an ImportError.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Binding:
    """Runtime binding of one catalog operation."""

    loader: Callable[[], Callable[..., dict[str, Any]]]
    input_args: tuple[str, ...]  # path arguments read by the operation
    output_arg: str | None  # path argument the operation writes (None = no dataset)
    engine_flag: str | None  # key in dispatch.available_engines(), None = core
    crs_effect: tuple[str, str] | None  # ("same_as"|"target", arg) | ("unknown","") | None
    output_kind: str | None = None  # "vector" | "raster" | None


def _describe() -> Callable[..., dict[str, Any]]:
    from ..engines import dispatch

    return dispatch.describe_routed


def _buffer() -> Callable[..., dict[str, Any]]:
    from ..engines import vector

    return vector.buffer


def _clip() -> Callable[..., dict[str, Any]]:
    from ..engines import vector

    return vector.clip


def _overlay() -> Callable[..., dict[str, Any]]:
    from ..engines import vector

    return vector.overlay


def _dissolve() -> Callable[..., dict[str, Any]]:
    from ..engines import vector

    return vector.dissolve


def _nearest_join() -> Callable[..., dict[str, Any]]:
    from ..engines import vector

    return vector.nearest_join


def _explode() -> Callable[..., dict[str, Any]]:
    from ..engines import vector

    return vector.explode


def _measure_area() -> Callable[..., dict[str, Any]]:
    from ..engines import vector

    return vector.measure_area


def _merge() -> Callable[..., dict[str, Any]]:
    from ..engines import vector

    return vector.merge


def _simplify() -> Callable[..., dict[str, Any]]:
    from ..engines import vector

    return vector.simplify


def _centroid() -> Callable[..., dict[str, Any]]:
    from ..engines import vector

    return vector.centroid


def _convert() -> Callable[..., dict[str, Any]]:
    from ..engines import vector

    return vector.convert


def _reproject() -> Callable[..., dict[str, Any]]:
    from ..engines import vector

    return vector.reproject


def _spatial_join() -> Callable[..., dict[str, Any]]:
    from ..engines import dispatch

    return dispatch.spatial_join_routed


def _run_sql() -> Callable[..., dict[str, Any]]:
    from ..engines import duckdb_engine

    return duckdb_engine.run_sql


def _resample_raster() -> Callable[..., dict[str, Any]]:
    from ..engines import raster

    return raster.resample


def _clip_raster() -> Callable[..., dict[str, Any]]:
    from ..engines import raster

    return raster.clip_raster


def _reclassify_raster() -> Callable[..., dict[str, Any]]:
    from ..engines import raster

    return raster.reclassify


def _zonal_statistics() -> Callable[..., dict[str, Any]]:
    from ..engines import raster

    return raster.zonal_statistics


def _hillshade() -> Callable[..., dict[str, Any]]:
    from ..engines import whitebox_engine

    return whitebox_engine.hillshade


def _slope() -> Callable[..., dict[str, Any]]:
    from ..engines import whitebox_engine

    return whitebox_engine.slope


def _aspect() -> Callable[..., dict[str, Any]]:
    from ..engines import whitebox_engine

    return whitebox_engine.aspect


def _flow_accumulation() -> Callable[..., dict[str, Any]]:
    from ..engines import whitebox_engine

    return whitebox_engine.flow_accumulation


def _watershed() -> Callable[..., dict[str, Any]]:
    from ..engines import whitebox_engine

    return whitebox_engine.watershed


def _get_provenance() -> Callable[..., dict[str, Any]]:
    from ..provenance import read_provenance

    return read_provenance


BINDINGS: dict[str, Binding] = {
    "describe_dataset": Binding(_describe, ("path",), None, None, None),
    "buffer_layer": Binding(
        _buffer, ("input_path",), "output_path", None, ("same_as", "input_path"), "vector"
    ),
    "clip_layer": Binding(
        _clip,
        ("input_path", "mask_path"),
        "output_path",
        None,
        ("same_as", "input_path"),
        "vector",
    ),
    "overlay_layers": Binding(
        _overlay,
        ("input_path", "overlay_path"),
        "output_path",
        None,
        ("same_as", "input_path"),
        "vector",
    ),
    "dissolve_layer": Binding(
        _dissolve, ("input_path",), "output_path", None, ("same_as", "input_path"), "vector"
    ),
    "nearest_join": Binding(
        _nearest_join,
        ("left_path", "right_path"),
        "output_path",
        None,
        ("same_as", "left_path"),
        "vector",
    ),
    "explode_layer": Binding(
        _explode, ("input_path",), "output_path", None, ("same_as", "input_path"), "vector"
    ),
    "measure_area": Binding(
        _measure_area, ("input_path",), "output_path", None, ("same_as", "input_path"), "vector"
    ),
    # merge_layers takes a LIST of input paths; static analysis tracks only
    # string path arguments, so its inputs are opaque here, like run_sql's.
    "merge_layers": Binding(_merge, (), "output_path", None, ("unknown", ""), "vector"),
    "simplify_layer": Binding(
        _simplify, ("input_path",), "output_path", None, ("same_as", "input_path"), "vector"
    ),
    "centroid_layer": Binding(
        _centroid, ("input_path",), "output_path", None, ("same_as", "input_path"), "vector"
    ),
    "convert_format": Binding(
        _convert, ("input_path",), "output_path", None, ("same_as", "input_path"), "vector"
    ),
    "reproject_layer": Binding(
        _reproject, ("input_path",), "output_path", None, ("target", "target_crs"), "vector"
    ),
    "spatial_join": Binding(
        _spatial_join,
        ("left_path", "right_path"),
        "output_path",
        None,
        ("same_as", "left_path"),
        "vector",
    ),
    # run_sql reads whatever the query names: inputs are opaque to static analysis.
    "run_sql": Binding(_run_sql, (), "output_path", None, ("unknown", ""), "vector"),
    "resample_raster": Binding(
        _resample_raster,
        ("input_path",),
        "output_path",
        "exactextract",
        ("same_as", "input_path"),
        "raster",
    ),
    "clip_raster": Binding(
        _clip_raster,
        ("raster_path", "mask_path"),
        "output_path",
        "exactextract",
        ("same_as", "raster_path"),
        "raster",
    ),
    "reclassify_raster": Binding(
        _reclassify_raster,
        ("input_path",),
        "output_path",
        "exactextract",
        ("same_as", "input_path"),
        "raster",
    ),
    "zonal_statistics": Binding(
        _zonal_statistics,
        ("raster_path", "zones_path"),
        "output_path",
        "exactextract",
        ("same_as", "zones_path"),
        "vector",
    ),
    "hillshade": Binding(
        _hillshade, ("dem_path",), "output_path", "whitebox", ("same_as", "dem_path"), "raster"
    ),
    "slope": Binding(
        _slope, ("dem_path",), "output_path", "whitebox", ("same_as", "dem_path"), "raster"
    ),
    "aspect": Binding(
        _aspect, ("dem_path",), "output_path", "whitebox", ("same_as", "dem_path"), "raster"
    ),
    "flow_accumulation": Binding(
        _flow_accumulation,
        ("dem_path",),
        "output_path",
        "whitebox",
        ("same_as", "dem_path"),
        "raster",
    ),
    "watershed": Binding(
        _watershed,
        ("dem_path", "pour_points_path"),
        "output_path",
        "whitebox",
        ("same_as", "dem_path"),
        "raster",
    ),
    "get_provenance": Binding(_get_provenance, ("output_path",), None, None, None),
}

# Python types accepted for each catalog parameter type declaration.
PARAM_TYPES: dict[str, tuple[type, ...]] = {
    "str": (str,),
    "float": (int, float),
    "int": (int,),
    "bool": (bool,),
    "list[str]": (list,),
}
