"""Routing over a line network, with the ways it silently lies made visible.

Two operations — the cheapest route between two places, and everything reachable
within a budget — over a network the caller supplies as a line layer. Both come
from the discovery benchmark, where six of the twenty-one requests neither
labeller could place were network questions: *"which residential blocks are
further than a 10-minute walk from any primary care clinic using the actual street
network"*, *"the cheapest path to lay underground line from Substation 4 that
avoids steep slopes"*, *"which emergency rooms become completely cut off if the
riverfront arterial roads submerge"*.

**The failure mode this module is built around is not a wrong number, it is a
disconnected graph.** Two street segments whose endpoints are a millimetre apart
are one junction on the ground and two nodes in a naive build, so the route goes
the long way round or is reported as impossible — and nothing raises, because a
graph with a gap in it is a perfectly valid graph. That is why `tolerance` has no
default, why the number of merged endpoints and the number of connected
components are in every manifest, and why snapping an origin four kilometres to
the nearest node is reported rather than absorbed.

No new dependency: the graph and Dijkstra are forty lines here, they are exactly
deterministic, and the alternative was to pull in a package whose tie-breaking
between equal-cost paths we would not control.
"""

from __future__ import annotations

import heapq
import math
from itertools import pairwise
from typing import Any

import geopandas as gpd

from .. import readers, verify
from ..provenance import InputRecord, ProvenanceRecord

#: Endpoint coordinates are rounded to this many decimals before being compared,
#: on top of the caller's tolerance. Purely defensive against float noise in a
#: file that has been through a reprojection: 1e-9 of a metre is not a place.
_COORDINATE_DECIMALS = 9


def _engine_info() -> dict[str, str]:
    import shapely

    return {"name": "mapsmith-dijkstra", "version": f"shapely {shapely.__version__}"}


class _Graph:
    """Nodes from snapped endpoints, edges from lines. Deliberately small.

    Undirected by default because a line layer without a direction field
    describes undirected connectivity, and inventing a direction is worse than
    not having one. `oneway_field` opts into the other reading, and says so in
    the manifest.
    """

    def __init__(self) -> None:
        self.nodes: dict[tuple[float, float], int] = {}
        self.positions: list[tuple[float, float]] = []
        self.adjacency: dict[int, list[tuple[int, float, int]]] = {}
        self.merged = 0

    def node_at(self, x: float, y: float, tolerance: float) -> int:
        """The node at this position, creating one only if none is within tolerance.

        Linear scan on purpose: a grid index would be faster and would also be a
        second place where "the same junction" is defined. On the networks these
        operations are for — a town's streets, a utility corridor — the scan is
        not what costs.
        """
        key = (round(x, _COORDINATE_DECIMALS), round(y, _COORDINATE_DECIMALS))
        if key in self.nodes:
            self.merged += 1
            return self.nodes[key]
        if tolerance > 0:
            for index, (nx, ny) in enumerate(self.positions):
                if math.hypot(nx - x, ny - y) <= tolerance:
                    self.nodes[key] = index
                    self.merged += 1
                    return index
        index = len(self.positions)
        self.positions.append((x, y))
        self.nodes[key] = index
        self.adjacency[index] = []
        return index

    def add_edge(self, a: int, b: int, cost: float, line_index: int, directed: bool):
        self.adjacency.setdefault(a, []).append((b, cost, line_index))
        if not directed:
            self.adjacency.setdefault(b, []).append((a, cost, line_index))

    def components(self) -> int:
        """How many disconnected pieces the network is in.

        One is what a caller almost always expects. More than one, unannounced,
        is the reason a route came back impossible.
        """
        seen: set[int] = set()
        count = 0
        for start in self.adjacency:
            if start in seen:
                continue
            count += 1
            stack = [start]
            while stack:
                node = stack.pop()
                if node in seen:
                    continue
                seen.add(node)
                stack.extend(n for n, _, _ in self.adjacency.get(node, []) if n not in seen)
        return count

    def dijkstra(self, source: int, budget: float | None = None):
        """Least cost to every node, with the edge each was reached by.

        Ties are broken by node index so two runs on the same data give the same
        path — a route that changes between runs cannot be put in a manifest.
        """
        best: dict[int, float] = {source: 0.0}
        came_from: dict[int, tuple[int, int]] = {}
        queue: list[tuple[float, int]] = [(0.0, source)]
        while queue:
            cost, node = heapq.heappop(queue)
            if cost > best.get(node, math.inf):
                continue
            for neighbour, weight, line_index in sorted(self.adjacency.get(node, [])):
                total = cost + weight
                if budget is not None and total > budget:
                    continue
                if total < best.get(neighbour, math.inf):
                    best[neighbour] = total
                    came_from[neighbour] = (node, line_index)
                    heapq.heappush(queue, (total, neighbour))
        return best, came_from


def _build(
    lines: gpd.GeoDataFrame,
    tolerance: float,
    cost_field: str | None,
    oneway_field: str | None,
) -> tuple[_Graph, list[float]]:
    graph = _Graph()
    costs: list[float] = []
    for index, row in enumerate(lines.itertuples()):
        geometry = row.geometry
        if geometry is None or geometry.is_empty:
            costs.append(math.nan)
            continue
        coords = list(geometry.coords) if geometry.geom_type == "LineString" else None
        if coords is None:
            raise ValueError(
                "the network holds a MultiLineString, which has no single pair of "
                "endpoints to make a junction out of. Run explode_layer first, so "
                "the split is a recorded step rather than a guess made in here."
            )
        start = graph.node_at(coords[0][0], coords[0][1], tolerance)
        end = graph.node_at(coords[-1][0], coords[-1][1], tolerance)
        if cost_field is None:
            cost = geometry.length
        else:
            cost = getattr(row, cost_field)
            if cost is None or (isinstance(cost, float) and math.isnan(cost)):
                raise ValueError(
                    f"row {index} has no value in the cost field {cost_field!r}. A "
                    "missing cost is not a free edge: decide what it should be "
                    "rather than having this treat it as zero."
                )
            cost = float(cost)
            if cost < 0:
                raise ValueError(
                    f"row {index} has a negative cost ({cost}) in {cost_field!r}. "
                    "Dijkstra is only correct on non-negative costs, and would "
                    "return a confident wrong answer rather than fail."
                )
        costs.append(cost)
        directed = bool(getattr(row, oneway_field)) if oneway_field else False
        graph.add_edge(start, end, cost, index, directed)
    return graph, costs


def _nearest_node(graph: _Graph, x: float, y: float) -> tuple[int, float]:
    best, distance = 0, math.inf
    for index, (nx, ny) in enumerate(graph.positions):
        d = math.hypot(nx - x, ny - y)
        if d < distance:
            best, distance = index, d
    return best, distance


def _snap_check(name: str, distance: float, tolerance: float) -> verify.Check:
    """How far the position moved to get onto the network.

    Not critical, because snapping is the point — but a clinic that snapped four
    kilometres to the nearest junction produces a service area for somewhere
    else, and the only evidence is this number.
    """
    return verify.Check(
        f"x-mapsmith:{name}_is_close_to_the_network",
        distance <= max(tolerance * 10, 50.0),
        f"{name} snapped {distance:.1f} to the nearest junction",
        critical=False,
        hint="if that is far, the position is probably not on this network at all",
    )


def _network_checks(graph: _Graph, tolerance: float) -> list[verify.Check]:
    components = graph.components()
    return [
        verify.Check(
            "x-mapsmith:network_is_one_connected_piece",
            components == 1,
            f"{components} connected component(s), {len(graph.positions)} junctions",
            critical=False,
            hint="more than one piece means some destinations are unreachable by "
            "construction; a larger tolerance may be joining ends that are meant "
            "to be joined, or the network may genuinely be in pieces",
        ),
        verify.Check(
            "x-mapsmith:endpoints_were_merged_into_junctions",
            True,
            f"{graph.merged} endpoint(s) merged at tolerance {tolerance}",
            critical=False,
        ),
    ]


def _common_preamble(
    network_path: str, tolerance: float, cost_field: str | None, oneway_field: str | None
):
    if tolerance < 0:
        raise ValueError(f"tolerance must be zero or positive, got {tolerance}")
    lines = readers.read_vector(network_path)
    if lines.crs is None:
        raise ValueError(
            readers.no_crs_message(
                lines, f"{network_path} has no CRS, so a tolerance in its units and a "
                "cost derived from length both mean nothing."
            )
        )
    if lines.crs.is_geographic and cost_field is None:
        raise ValueError(
            f"{network_path} is in a geographic CRS and no cost_field was given, so "
            "the cost of each edge would be its length in DEGREES — which is not a "
            "distance and varies with latitude. Reproject first, or pass a cost "
            "field whose units you control."
        )
    kinds = set(lines.geom_type.dropna().unique())
    if not kinds <= {"LineString", "MultiLineString"}:
        raise ValueError(
            f"a network is a line layer; {network_path} holds {sorted(kinds)}."
        )
    for field in (cost_field, oneway_field):
        if field is not None and field not in lines.columns:
            raise ValueError(
                f"{network_path} has no column {field!r}. Columns: "
                f"{sorted(c for c in lines.columns if c != lines.geometry.name)}"
            )
    graph, costs = _build(lines, tolerance, cost_field, oneway_field)
    return lines, graph, costs


def network_shortest_path(
    network_path: str,
    output_path: str,
    from_x: float,
    from_y: float,
    to_x: float,
    to_y: float,
    tolerance: float,
    cost_field: str | None = None,
    oneway_field: str | None = None,
) -> dict[str, Any]:
    """The cheapest route between two positions over a line network.

    `tolerance` has no default and it is the parameter that decides whether the
    answer means anything: it is how far apart two line endpoints can be and
    still be the same junction. Too small and the network is silently in pieces,
    so the route detours or comes back impossible; too large and it welds
    together a bridge and the road beneath it. Look at the two checks in the
    manifest — the component count and the merged-endpoint count — before
    believing the number.

    `cost_field` is any non-negative numeric column: minutes, euros, a
    slope-weighted length. Without it the cost is geometric length, which needs
    a projected CRS. The output carries `cumulative_cost` per segment in the
    order the route uses them.
    """
    lines, graph, costs = _common_preamble(
        network_path, tolerance, cost_field, oneway_field
    )
    source, from_distance = _nearest_node(graph, from_x, from_y)
    target, to_distance = _nearest_node(graph, to_x, to_y)

    record = ProvenanceRecord(
        operation="network_shortest_path",
        parameters={
            "from": [from_x, from_y],
            "to": [to_x, to_y],
            "tolerance": tolerance,
            "cost_field": cost_field,
            "oneway_field": oneway_field,
            "cost_unit": "the cost field's own unit"
            if cost_field
            else f"length in {verify.crs_label(lines.crs)} units",
        },
        inputs=[InputRecord.from_path(network_path, crs=verify.crs_label(lines.crs))],
        engine=_engine_info(),
    )
    record.crs_decisions = {
        "analysis_crs": verify.crs_label(lines.crs),
        "reason": "the network is routed in its own CRS; tolerance and any "
        "length-based cost are read in that CRS's units",
    }

    best, came_from = graph.dijkstra(source)
    if target not in best:
        raise ValueError(
            f"no route: the destination is in a different part of the network from "
            f"the origin. The graph has {graph.components()} connected component(s) "
            f"at tolerance {tolerance} — if the streets really do join, the "
            "tolerance is too small to see it."
        )

    used: list[int] = []
    node = target
    while node != source:
        previous, line_index = came_from[node]
        used.append(line_index)
        node = previous
    used.reverse()

    route = lines.iloc[used].copy().reset_index(drop=True)
    route["step"] = range(1, len(used) + 1)
    route["segment_cost"] = [costs[i] for i in used]
    running = 0.0
    cumulative = []
    for i in used:
        running += costs[i]
        cumulative.append(running)
    route["cumulative_cost"] = cumulative

    if str(output_path).endswith(".parquet"):
        route.to_parquet(output_path)
    else:
        route.to_file(output_path)

    total = best[target]
    manifest, extras = verify.audited(
        record,
        output_path,
        operation="network_shortest_path",
        preconditions=verify.verify_loaded_inputs(
            "network_shortest_path", network_path=lines
        ),
        checks_fn=lambda: [
            *verify.verify_vector_output(
                output_path,
                expect_crs=verify.crs_label(lines.crs),
                expect_count=len(used),
                expect_geometry={"LineString"},
            ),
            # Closed form: the segment costs of the route must add up to the cost
            # Dijkstra reported, or the path reconstruction is not the path that
            # was costed — which produces a plausible route with the wrong total.
            verify.Check(
                "x-mapsmith:segment_costs_add_up_to_the_route_cost",
                math.isclose(cumulative[-1] if cumulative else 0.0, total, rel_tol=1e-9),
                f"segments sum to {cumulative[-1] if cumulative else 0.0}, "
                f"Dijkstra says {total}",
            ),
            _snap_check("origin", from_distance, tolerance),
            _snap_check("destination", to_distance, tolerance),
            *_network_checks(graph, tolerance),
        ],
    )
    return {
        "output": str(output_path),
        "total_cost": total,
        "segments": len(used),
        "origin_snap_distance": from_distance,
        "destination_snap_distance": to_distance,
        "junctions": len(graph.positions),
        "components": graph.components(),
        "provenance": str(manifest),
        **extras,
    }


def service_area(
    network_path: str,
    output_path: str,
    from_x: float,
    from_y: float,
    budget: float,
    tolerance: float,
    cost_field: str | None = None,
    oneway_field: str | None = None,
) -> dict[str, Any]:
    """Every stretch of network reachable from a position within a cost budget.

    This is the "ten-minute walk" question, and the reason it is a network
    operation rather than a buffer is the whole point: a circle of 800 m around
    a clinic includes the far side of a river with no bridge, and excludes the
    house 900 m away along a straight road. `buffer_layer` answers a different
    question and answers it confidently.

    The last segment of each branch is **cut where the budget runs out** rather
    than included or dropped whole, so the edge of the area is where the walk
    actually ends. Each output segment carries `cost_at_start` and
    `cost_at_end`; a segment that was cut is marked `partial`.
    """
    if budget <= 0:
        raise ValueError(f"budget must be positive, got {budget}")
    lines, graph, costs = _common_preamble(
        network_path, tolerance, cost_field, oneway_field
    )
    source, snap_distance = _nearest_node(graph, from_x, from_y)

    record = ProvenanceRecord(
        operation="service_area",
        parameters={
            "from": [from_x, from_y],
            "budget": budget,
            "tolerance": tolerance,
            "cost_field": cost_field,
            "oneway_field": oneway_field,
            "cost_unit": "the cost field's own unit"
            if cost_field
            else f"length in {verify.crs_label(lines.crs)} units",
        },
        inputs=[InputRecord.from_path(network_path, crs=verify.crs_label(lines.crs))],
        engine=_engine_info(),
    )
    record.crs_decisions = {
        "analysis_crs": verify.crs_label(lines.crs),
        "reason": "reachability is computed in the network's own CRS; the budget is "
        "read in the cost field's unit, or in that CRS's length unit without one",
    }

    reached, _ = graph.dijkstra(source)
    pieces = []
    for index, row in enumerate(lines.itertuples()):
        geometry = row.geometry
        if geometry is None or geometry.is_empty:
            continue
        coords = list(geometry.coords)
        a = graph.nodes[
            (
                round(coords[0][0], _COORDINATE_DECIMALS),
                round(coords[0][1], _COORDINATE_DECIMALS),
            )
        ]
        b = graph.nodes[
            (
                round(coords[-1][0], _COORDINATE_DECIMALS),
                round(coords[-1][1], _COORDINATE_DECIMALS),
            )
        ]
        cost = costs[index]
        for start_node, end_node in ((a, b), (b, a)):
            if start_node not in reached:
                continue
            at_start = reached[start_node]
            if at_start > budget:
                continue
            remaining = budget - at_start
            if cost <= remaining:
                # Whole segment fits, but only add it once — from whichever end
                # reaches it more cheaply, so a two-way edge is not duplicated.
                if end_node in reached and reached[end_node] < at_start:
                    continue
                pieces.append(
                    {
                        "line_index": index,
                        "geometry": geometry if start_node == a else geometry.reverse(),
                        "cost_at_start": at_start,
                        "cost_at_end": at_start + cost,
                        "partial": False,
                    }
                )
                break
            if remaining <= 0 or cost <= 0:
                continue
            walked = geometry if start_node == a else geometry.reverse()
            fraction = remaining / cost
            cut = _substring(walked, fraction)
            if cut is not None:
                pieces.append(
                    {
                        "line_index": index,
                        "geometry": cut,
                        "cost_at_start": at_start,
                        "cost_at_end": budget,
                        "partial": True,
                    }
                )

    out = gpd.GeoDataFrame(
        {
            "line_index": [p["line_index"] for p in pieces],
            "cost_at_start": [p["cost_at_start"] for p in pieces],
            "cost_at_end": [p["cost_at_end"] for p in pieces],
            "partial": [p["partial"] for p in pieces],
        },
        geometry=[p["geometry"] for p in pieces],
        crs=lines.crs,
    )
    if str(output_path).endswith(".parquet"):
        out.to_parquet(output_path)
    else:
        out.to_file(output_path)

    manifest, extras = verify.audited(
        record,
        output_path,
        operation="service_area",
        preconditions=verify.verify_loaded_inputs("service_area", network_path=lines),
        checks_fn=lambda: [
            *verify.verify_vector_output(
                output_path,
                expect_crs=verify.crs_label(lines.crs),
                expect_geometry={"LineString"},
                on_empty="warn",
            ),
            verify.Check(
                "x-mapsmith:nothing_exceeds_the_budget",
                all(p["cost_at_end"] <= budget + 1e-9 for p in pieces),
                f"{sum(1 for p in pieces if p['cost_at_end'] > budget + 1e-9)} "
                "segment(s) end beyond the budget",
            ),
            _snap_check("origin", snap_distance, tolerance),
            *_network_checks(graph, tolerance),
        ],
    )
    return {
        "output": str(output_path),
        "segments": len(pieces),
        "partial_segments": sum(1 for p in pieces if p["partial"]),
        "budget": budget,
        "reachable_junctions": sum(1 for cost in reached.values() if cost <= budget),
        "junctions": len(graph.positions),
        "origin_snap_distance": snap_distance,
        "components": graph.components(),
        "provenance": str(manifest),
        **extras,
    }


def _substring(line, fraction: float):
    """The first `fraction` of a line, by length. None if it degenerates."""
    from shapely.geometry import LineString

    if fraction <= 0:
        return None
    if fraction >= 1:
        return line
    target = line.length * fraction
    coords = list(line.coords)
    kept = [coords[0]]
    walked = 0.0
    for previous, current in pairwise(coords):
        step = math.dist(previous, current)
        if walked + step >= target:
            share = (target - walked) / step if step else 0.0
            kept.append(
                (
                    previous[0] + (current[0] - previous[0]) * share,
                    previous[1] + (current[1] - previous[1]) * share,
                )
            )
            break
        walked += step
        kept.append(current)
    return LineString(kept) if len(kept) > 1 else None
