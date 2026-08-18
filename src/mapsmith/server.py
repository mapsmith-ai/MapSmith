"""Mapsmith MCP server (stdio).

Run with ``mapsmith`` (console script) or ``python -m mapsmith.server``.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from . import __version__, catalog
from .engines import vector
from .provenance import read_provenance

mcp = FastMCP(
    "mapsmith",
    instructions=(
        "Mapsmith is a deterministic geoprocessing toolbox. Geometry and numbers always "
        "come from tool executions, never from the model. Every output dataset has a "
        "lineage manifest (<output>.provenance.json) retrievable with get_provenance. "
        "Use list_operations to discover capabilities before improvising; if an operation "
        "is 'planned', say so instead of approximating it with the wrong tool. "
        "Datasets are file paths (GeoPackage recommended)."
    ),
)


@mcp.tool()
def describe_dataset(path: str) -> dict[str, Any]:
    """Inspect a vector dataset: CRS, geometry types, schema, extent, feature count.

    Call this before any analysis on a dataset you have not inspected yet.
    """
    return vector.describe(path)


@mcp.tool()
def buffer_layer(input_path: str, distance_meters: float, output_path: str) -> dict[str, Any]:
    """Buffer all features by a distance in meters.

    Geographic-CRS inputs are reprojected to an estimated UTM zone for the metric
    operation and back; the decision is recorded in the provenance manifest.
    """
    return vector.buffer(input_path, distance_meters, output_path)


@mcp.tool()
def clip_layer(input_path: str, mask_path: str, output_path: str) -> dict[str, Any]:
    """Clip a layer to the area of a mask layer. CRS are aligned automatically."""
    return vector.clip(input_path, mask_path, output_path)


@mcp.tool()
def reproject_layer(input_path: str, target_crs: str, output_path: str) -> dict[str, Any]:
    """Reproject a layer to a target CRS, e.g. 'EPSG:32632' or a WKT string."""
    return vector.reproject(input_path, target_crs, output_path)


@mcp.tool()
def spatial_join(
    left_path: str, right_path: str, output_path: str, predicate: str = "intersects"
) -> dict[str, Any]:
    """Join attributes from the right layer onto the left layer by spatial predicate.

    Predicate is one of: intersects, within, contains.
    """
    return vector.spatial_join(left_path, right_path, output_path, predicate)


@mcp.tool()
def get_provenance(output_path: str) -> dict[str, Any]:
    """Return the full lineage manifest of a Mapsmith output dataset."""
    return read_provenance(output_path)


@mcp.tool()
def list_operations(query: str = "") -> list[dict[str, str]]:
    """Search the catalog of available and planned operations (progressive discovery)."""
    return catalog.search(query)


@mcp.tool()
def server_info() -> dict[str, str]:
    """Mapsmith version and licensing information."""
    return {
        "name": "mapsmith",
        "version": __version__,
        "license": "AGPL-3.0-or-later",
        "homepage": "https://mapsmith.ai",
    }


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
