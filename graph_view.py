# -*- coding: utf-8 -*-
"""Live graph board for the add-on: a disposable node tree with one node
per tracked group (coloured by sync status) and real links between the
groups that reference each other.

The board is intentionally ephemeral: the trees carry no fake user and
nothing in the file references them, so Blender purges them on save/reload.
Rebuilding replaces them; ``clear_graph_trees`` removes them on demand.
"""

from __future__ import annotations

import bpy

from .graph_utils import STATUS_COLORS, STATUS_LABELS, layered_layout, \
    neighborhood
from .sync_manager import sync_manager

GRAPH_TREE_NAME = "GNT Graph"
PORT_TREE_PREFIX = "GNT Graph Port"
MAX_PORTS = 12
LEGEND_ORDER = ("synced", "blend_modified", "json_modified", "conflict",
                "orphan", "json_missing", "untracked")


def clear_graph_trees() -> int:
    removed = 0
    for tree in list(bpy.data.node_groups):
        if (tree.name == GRAPH_TREE_NAME
                or tree.name.startswith(PORT_TREE_PREFIX)):
            bpy.data.node_groups.remove(tree)
            removed += 1
    return removed


def _fitted_width(label: str) -> float:
    """Node width that fits *label* without truncation (Node Arrange-style).

    Node Arrange measures the real text with ``blf``; here a generous
    per-character estimate keeps the code headless-safe and deterministic.
    The clamps match the SVG poster's so both share the exact same widths.
    """
    return max(170.0, min(340.0, 30.0 + 7.4 * len(label)))


def _port_tree(k: int):
    k = max(0, min(int(k), MAX_PORTS))
    name = f"{PORT_TREE_PREFIX} {k}"
    tree = bpy.data.node_groups.get(name)
    if tree is None:
        tree = bpy.data.node_groups.new(name, "GeometryNodeTree")
        for i in range(k):
            tree.interface.new_socket(f"in_{i}", in_out='INPUT',
                                      socket_type='NodeSocketGeometry')
        tree.interface.new_socket("out", in_out='OUTPUT',
                                  socket_type='NodeSocketGeometry')
    return tree


def live_graph():
    """(names, edges, name_status, externals) from the live tracked set.

    Edges are (consumer, dependency) pairs read from the live trees'
    GeometryNodeGroup nodes; externals are referenced names that are not
    tracked.  Statuses come from the (cached) status check.
    """
    tracked = sync_manager.metadata.get("tracked_groups", {})
    statuses = sync_manager.check_all_statuses()
    names = []
    name_status = {}
    edges = set()
    for uid, info in tracked.items():
        name = info.get("blend_name", "")
        if not name:
            continue
        names.append(name)
        name_status[name] = statuses.get(uid)
        tree = bpy.data.node_groups.get(name)
        if tree is None:
            continue
        for node in tree.nodes:
            if node.bl_idname != "GeometryNodeGroup":
                continue
            ref_tree = getattr(node, "node_tree", None)
            ref = ref_tree.name if ref_tree is not None else None
            if ref and ref != name:
                edges.add((name, ref))
    known = set(names)
    externals = {b for _a, b in edges if b not in known}
    return names, edges, name_status, externals


def collect_board_model(mode: str = "PROJECT", focus: str = "",
                        hops: int = 2, include_externals: bool = True) -> dict:
    """Plain model for a board/poster: names, edges, status keys, extras.

    Shared by the node-editor board and the SVG poster; externals are the
    referenced-but-untracked names (kept only when *include_externals*).
    """
    names, edges, name_status, externals = live_graph()
    keep = set(names)
    if mode == "NEIGHBORHOOD" and focus:
        keep &= neighborhood(edges, focus, hops)
    kept_names = [n for n in names if n in keep]
    kept_edges = sorted((a, b) for a, b in edges if a in keep)
    extra_externals = sorted({b for _a, b in kept_edges if b in externals})
    if include_externals:
        kept_names += extra_externals
    else:
        kept_edges = [(a, b) for a, b in kept_edges if b not in externals]
    status_keys = {}
    for n in kept_names:
        status = name_status.get(n)
        status_keys[n] = status.value if status is not None else "untracked"
    return {
        "names": kept_names,
        "edges": kept_edges,
        "status_keys": status_keys,
        "externals": extra_externals,
        "focus": focus if mode == "NEIGHBORHOOD" else "",
    }


def build_graph_tree(mode: str = "PROJECT", focus: str = "",
                     hops: int = 2, include_externals: bool = True) -> dict:
    """(Re)build the disposable graph tree; returns a report dict.

    ``mode``: PROJECT (all tracked groups) or NEIGHBORHOOD (the focus and
    everything within *hops* hops, both directions).
    """
    clear_graph_trees()
    model = collect_board_model(mode, focus, hops, include_externals)
    kept_names = model["names"]
    kept_edges = model["edges"]
    status_keys = model["status_keys"]
    focus = model["focus"]
    extra_externals = model["externals"]

    indegree = {n: 0 for n in kept_names}
    for _a, b in kept_edges:
        if b in indegree:
            indegree[b] += 1
    ext_set = set(extra_externals)
    labels = {}
    for name in kept_names:
        label = f"{name} (untracked)" if name in ext_set else name
        labels[name] = ("* " + label) if name == focus else label
    widths = {n: _fitted_width(labels[n]) for n in kept_names}
    layout = layered_layout(kept_names, kept_edges, widths=widths)

    graph = bpy.data.node_groups.new(GRAPH_TREE_NAME, "GeometryNodeTree")
    graph.use_fake_user = False

    nodes = {}
    for i, name in enumerate(kept_names):
        node = graph.nodes.new("GeometryNodeGroup")
        node.name = f"n{i:04d}"
        node.label = labels[name]
        node.node_tree = _port_tree(indegree.get(name, 0))
        x, y = layout.get(name, (0.0, 0.0))
        node.location = (x, y)
        node.hide = True
        node.width = widths[name]
        key = status_keys.get(name, "untracked")
        node.use_custom_color = True
        node.color = STATUS_COLORS.get(key, STATUS_COLORS["untracked"])
        nodes[name] = node

    clipped = 0
    used = {n: 0 for n in kept_names}
    for a, b in kept_edges:
        src = nodes.get(a)
        dst = nodes.get(b)
        if src is None or dst is None:
            continue
        slot = used[b]
        if slot >= min(indegree.get(b, 0), MAX_PORTS):
            clipped += 1
            continue
        try:
            graph.links.new(src.outputs[0], dst.inputs[slot])
        except Exception:
            clipped += 1
            continue
        used[b] = slot + 1

    # Legend: one small node per status at the right edge of the board.
    max_x = max((n.location[0] for n in nodes.values()), default=0.0)
    legend_x = max_x + 460.0
    for i, key in enumerate(LEGEND_ORDER):
        node = graph.nodes.new("GeometryNodeGroup")
        node.name = f"legend_{key}"
        node.label = STATUS_LABELS[key]
        node.node_tree = _port_tree(0)
        node.location = (legend_x, -i * 64.0)
        node.hide = True
        node.width = _fitted_width(node.label)
        node.use_custom_color = True
        node.color = STATUS_COLORS[key]

    return {
        "tree": graph.name,
        "nodes": len(kept_names),
        "edges": len(kept_edges),
        "links": len(graph.links),
        "clipped": clipped,
        "externals": len(extra_externals),
        "focus": focus if mode == "NEIGHBORHOOD" else "",
    }


def show_in_editor(context, tree) -> bool:
    """Pin a Geometry node editor to *tree* (verified recipe: pin + path)."""
    screen = getattr(context, "screen", None)
    for area in (screen.areas if screen else []):
        if area.type != 'NODE_EDITOR':
            continue
        space = area.spaces.active
        if space.tree_type != 'GeometryNodeTree':
            continue
        space.pin = True
        try:
            space.path.clear()
            space.path.start(tree)
        except Exception:
            space.node_tree = tree
        area.tag_redraw()
        return True
    return False


def unpin_graph_editors(context) -> None:
    """Release editors pinned to the graph artifacts."""
    screen = getattr(context, "screen", None)
    for area in (screen.areas if screen else []):
        if area.type != 'NODE_EDITOR':
            continue
        space = area.spaces.active
        current = space.node_tree.name if space.node_tree else ""
        if current == GRAPH_TREE_NAME or current.startswith(PORT_TREE_PREFIX):
            space.pin = False
            try:
                space.path.clear()
            except Exception:
                pass
            area.tag_redraw()
