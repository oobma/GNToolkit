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
last_report: dict = {}
PORT_TREE_PREFIX = "GNT Graph Port"
MAX_PORTS = 12
COL_GAP = 40.0        # air between the widest node of two columns
BAR_HEIGHT = 28.0     # collapsed bar with up to three sockets
SOCKET_ROW = 10.0     # every socket beyond the third grows the bar
BAR_GAP = 16.0        # air between stacked bars
REROUTE_W = 10.0      # requested dot box width
REROUTE_H = 30.0      # requested dot box height (suppresses the 100-default
                      # drop: the dot draws ~50 px below location when the
                      # height stays at its 100 px default)
REROUTE_ROW = 110.0   # lane height reserved per routing point: the dot can
                      # draw anywhere within ±55 px of its location, so the
                      # row is sized for the worst case and can never touch
                      # a neighbouring bar (16 px of air still guaranteed)


def _bar_height(sockets: int) -> float:
    """Drawn height of a collapsed group node, measured on 5.1/5.2.

    Blender keeps a compact socket slot per socket beyond the third
    (1-3 sockets -> 28 px, then +10 px each), so a node with twelve
    inputs draws ~118 px tall and the layout must reserve that.
    """
    return BAR_HEIGHT + max(0, int(sockets) - 3) * SOCKET_ROW


def focus_from_board(tree) -> str:
    """The starred group of a board tree (its neighbourhood focus)."""
    for node in tree.nodes:
        if (node.label or "").startswith("* "):
            return _raw_name(node.label)
    return ""


STATUS_PREFIXES = {
    "blend_modified": "~ ",
    "json_modified": "J ",
    "conflict": "! ",
    "orphan": "M ",
    "json_missing": "F ",
    "untracked": "? ",
}


def _raw_name(label: str) -> str:
    """Undo the board's label decorations ("* ", status mark, untracked)."""
    name = label
    if name.startswith("* "):
        name = name[2:]
    for prefix in STATUS_PREFIXES.values():
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    if name.endswith(" (untracked)"):
        name = name[:-12]
    return name


def board_positions() -> tuple:
    """Positions and routing of the on-screen board, as displayed.

    Returns ``(positions, routes)`` read from the current ``GNT Graph``
    tree — so a board arranged externally (an external arranger, hand
    moves) can be exported exactly as it looks.  ``positions`` maps raw
    group name → (x_center, y_top); ``routes`` maps (consumer, dependency)
    → waypoint list for cables that travel through Reroute nodes.  Empty
    dicts when there is no board.
    """
    tree = bpy.data.node_groups.get(GRAPH_TREE_NAME)
    if tree is None:
        return {}, {}
    positions = {}
    for node in tree.nodes:
        if node.bl_idname == "NodeReroute" or node.type == 'FRAME':
            continue
        if node.name.startswith("legend_"):
            continue
        label = node.label or ""
        if not label:
            continue
        positions[_raw_name(label)] = (node.location[0] + node.width / 2.0,
                                       node.location[1])
    routes = {}
    for link in tree.links:
        dst = link.to_node
        if dst.bl_idname == "NodeReroute" or dst.type == 'FRAME':
            continue
        if not (dst.label or ""):
            continue
        chain = []
        cur = link.from_node
        steps = 0
        while (cur is not None and cur.bl_idname == "NodeReroute"
               and steps < 200):
            chain.append((cur.location[0] + REROUTE_W / 2.0,
                          cur.location[1] - REROUTE_H / 2.0))
            back = next((l for l in tree.links if l.to_node == cur), None)
            cur = back.from_node if back else None
            steps += 1
        if not chain or cur is None or cur.type == 'FRAME':
            continue
        if not (cur.label or ""):
            continue
        chain.reverse()
        routes[(_raw_name(cur.label), _raw_name(dst.label))] = chain
    return positions, routes
LEGEND_ORDER = ("synced", "blend_modified", "json_modified", "conflict",
                "orphan", "json_missing", "untracked")


def clear_graph_trees() -> int:
    global last_report
    removed = 0
    for tree in list(bpy.data.node_groups):
        if (tree.name == GRAPH_TREE_NAME
                or tree.name.startswith(PORT_TREE_PREFIX)):
            bpy.data.node_groups.remove(tree)
            removed += 1
    _history_diff_backup.clear()
    last_report = {}
    return removed


_HISTORY_DIFF_COLOR = (0.05, 0.75, 0.70)
_HISTORY_FRAME_COLOR = (0.008, 0.020, 0.165)  # #02052A, subtle frame
_history_diff_backup: dict = {}
_history_diff_frame: str = ""


def _add_history_frame(tree, label: str) -> str:
    """Frame the board content with a labelled history-mode banner."""
    xs, xe, ys, ye = [], [], [], []
    for node in tree.nodes:
        if node.type == 'FRAME' or node.name.startswith("legend_"):
            continue
        xs.append(node.location[0])
        xe.append(node.location[0] + node.width)
        ye.append(node.location[1])
        ys.append(node.location[1] - node.height)
    if not xs:
        return ""
    pad = 60.0
    frame = tree.nodes.new("NodeFrame")
    frame.name = "history_frame"
    frame.label = label or "HISTORY MODE"
    frame.location = (min(xs) - pad, max(ye) + pad)
    frame.width = (max(xe) - min(xs)) + 2 * pad
    frame.height = (max(ye) - min(ys)) + 2 * pad
    frame.use_custom_color = True
    frame.color = _HISTORY_FRAME_COLOR
    return frame.name


def mark_history_diff(names, label: str = "") -> int:
    """Recolor the board bars in *names* (time-machine revision diff).

    Returns the number of marked bars; previous marks are restored
    first, so repeated calls never stack.  With marks applied, a
    labelled frame wraps the board so the history mode is evident.
    ``clear_history_diff`` puts the original colors back and removes
    the frame.
    """
    clear_history_diff()
    tree = bpy.data.node_groups.get(GRAPH_TREE_NAME)
    if tree is None or not names:
        return 0
    wanted = set(names)
    marked = 0
    for node in tree.nodes:
        if (node.type != 'GROUP' or not node.label
                or node.name.startswith("legend_")):
            continue
        if _raw_name(node.label) in wanted:
            _history_diff_backup[node.name] = (bool(node.use_custom_color),
                                               tuple(node.color))
            node.use_custom_color = True
            node.color = _HISTORY_DIFF_COLOR
            marked += 1
    if marked:
        global _history_diff_frame
        _history_diff_frame = _add_history_frame(tree, label)
        base = tree.nodes.get("legend_synced")
        if base is not None:
            legend = tree.nodes.new("GeometryNodeGroup")
            legend.name = "legend_history"
            legend.label = "History diff"
            legend.node_tree = _port_tree(0)
            legend.location = (base.location[0], base.location[1] - 7 * 64.0)
            legend.hide = True
            legend.width = _fitted_width(legend.label)
            legend.use_custom_color = True
            legend.color = _HISTORY_DIFF_COLOR
    return marked


def clear_history_diff() -> None:
    """Restore the colors of bars marked by ``mark_history_diff``."""
    global _history_diff_frame
    tree = bpy.data.node_groups.get(GRAPH_TREE_NAME)
    if tree is not None:
        for name, (use, color) in _history_diff_backup.items():
            node = tree.nodes.get(name)
            if node is not None:
                node.use_custom_color = use
                node.color = color
        if _history_diff_frame:
            frame = tree.nodes.get(_history_diff_frame)
            if frame is not None:
                tree.nodes.remove(frame)
        legend = tree.nodes.get("legend_history")
        if legend is not None:
            tree.nodes.remove(legend)
    _history_diff_backup.clear()
    _history_diff_frame = ""


def recolor_board() -> int:
    """Repaint the existing board bars from the (cached) sync statuses.

    The board bakes its colors when it is built; this refreshes them in
    place (e.g. after Refresh Status) without rebuilding the layout.
    Bars carrying an active history-diff mark are left alone.  Returns
    the number of bars repainted.
    """
    tree = bpy.data.node_groups.get(GRAPH_TREE_NAME)
    if tree is None:
        return 0
    statuses = sync_manager.check_all_statuses()
    tracked = sync_manager.metadata.get("tracked_groups", {})
    name_key = {}
    for uid, info in tracked.items():
        name = info.get("blend_name", "")
        if not name:
            continue
        status = statuses.get(uid)
        name_key[name] = status.value if status is not None else "untracked"
    changed = 0
    for node in tree.nodes:
        if (node.type != 'GROUP' or not node.label
                or node.name.startswith("legend_")):
            continue
        key = name_key.get(_raw_name(node.label))
        if key is None:
            continue
        color = STATUS_COLORS.get(key, STATUS_COLORS["untracked"])
        if node.name in _history_diff_backup:
            # keep the mark visible, but let clearing restore the fresh
            # color instead of the pre-refresh one
            use, _old = _history_diff_backup[node.name]
            _history_diff_backup[node.name] = (use, tuple(color))
            continue
        if tuple(node.color) != tuple(color):
            node.use_custom_color = True
            node.color = color
            changed += 1
    return changed


def _fitted_width(label: str) -> float:
    """Node width that fits *label* without truncation (compact style).

    A real font measurement would be exact; here a generous per-character
    estimate keeps the code headless-safe and deterministic.  The clamps
    match the SVG poster's so both share the exact same widths.
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


def _cluster_key(name: str) -> str:
    for sep in (" - ", " / ", "|"):
        if sep in name:
            return name.split(sep, 1)[0].strip() or name
    return name.split(" ", 1)[0] if " " in name else name


def collect_cluster_model(include_externals: bool = True) -> dict:
    """Aggregate tracked names by prefix (one bar per prefix)."""
    names, edges, name_status, externals = live_graph()
    groups: dict[str, list[str]] = {}
    for name in names:
        groups.setdefault(_cluster_key(name), []).append(name)
    rank = {
        "conflict": 0, "blend_modified": 1, "json_modified": 2,
        "json_missing": 3, "orphan": 4, "untracked": 5, "synced": 6,
    }
    cluster_names = sorted(groups)
    labels = {c: f"{c} ({len(groups[c])})" for c in cluster_names}
    status_keys = {}
    for cluster, members in groups.items():
        keys = [name_status.get(m).value if name_status.get(m) is not None
                else "untracked" for m in members]
        status_keys[cluster] = min(keys, key=lambda k: rank.get(k, 5))
    cluster_edges = set()
    for a, b in edges:
        ca, cb = _cluster_key(a), _cluster_key(b)
        if ca != cb:
            cluster_edges.add((ca, cb))
    # Keep labels as the public names; the renderer adds the count via labels.
    return {
        "names": cluster_names,
        "edges": sorted(cluster_edges),
        "status_keys": status_keys,
        "externals": [],
        "focus": "",
        "labels": labels,
    }


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
    if mode == "CLUSTERS":
        model = collect_cluster_model(include_externals)
    else:
        model = collect_board_model(mode, focus, hops, include_externals)
    kept_names = model["names"]
    kept_edges = model["edges"]
    status_keys = model["status_keys"]
    focus = model["focus"]
    extra_externals = model["externals"]
    cluster_labels = model.get("labels", {})

    indegree = {n: 0 for n in kept_names}
    for _a, b in kept_edges:
        if b in indegree:
            indegree[b] += 1
    ext_set = set(extra_externals)
    labels = {}
    for name in kept_names:
        key = status_keys.get(name, "untracked")
        prefix = STATUS_PREFIXES.get(key, "")
        if name in cluster_labels:
            label = cluster_labels[name]
        else:
            label = f"{prefix}{name} (untracked)" if name in ext_set \
                else f"{prefix}{name}"
        labels[name] = ("* " + label) if name == focus else label
    widths = {n: _fitted_width(labels[n]) for n in kept_names}
    heights = {n: _bar_height(1 + min(indegree.get(n, 0), MAX_PORTS))
               for n in kept_names}
    routes: dict = {}
    layout = layered_layout(kept_names, kept_edges, widths=widths,
                            heights=heights, gap=BAR_GAP,
                            routes_out=routes, route_row=REROUTE_ROW)

    graph = bpy.data.node_groups.new(GRAPH_TREE_NAME, "GeometryNodeTree")
    graph.use_fake_user = False

    nodes = {}
    for i, name in enumerate(kept_names):
        node = graph.nodes.new("GeometryNodeGroup")
        node.name = f"n{i:04d}"
        node.label = labels[name]
        node.node_tree = _port_tree(indegree.get(name, 0))
        x, y = layout.get(name, (0.0, 0.0))
        node.location = (x - widths[name] / 2.0,
                         y + heights[name] / 2.0)
        node.hide = True
        node.width = widths[name]
        key = status_keys.get(name, "untracked")
        node.use_custom_color = True
        node.color = STATUS_COLORS.get(key, STATUS_COLORS["untracked"])
        nodes[name] = node

    clipped = 0
    reroutes = 0
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
            tail = src.outputs[0]
            for wx, wy in routes.get((a, b), ()):
                rout = graph.nodes.new("NodeReroute")
                rout.name = f"r{reroutes:04d}"
                rout.width = REROUTE_W
                rout.height = REROUTE_H
                rout.use_custom_color = True
                rout.color = (0.18, 0.18, 0.18)
                # Anchor the location AT the waypoint (+5 south) with the
                # height pinned: the Reroute draws its dot at `location`
                # when the height is 30, but 50 px lower while it keeps
                # the 100 px default — with either behaviour the dot stays
                # within ~15 px of the waypoint, inside the 16-21 px of
                # lane clearance the layout leaves around each routing row.
                rout.location = (wx - REROUTE_W / 2.0,
                                 wy + REROUTE_W / 2.0)
                graph.links.new(tail, rout.inputs[0])
                tail = rout.outputs[0]
                reroutes += 1
            graph.links.new(tail, dst.inputs[slot])
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

    # Unused sockets stay hidden by default — the state Ctrl+H toggles
    # ("Toggle Hidden Node Sockets"): only the sockets that carry a link
    # are drawn, so the compact bars stay clean.
    for node in graph.nodes:
        for sock in list(node.inputs) + list(node.outputs):
            if hasattr(sock, "hide"):
                sock.hide = not sock.is_linked

    global last_report
    last_report = {
        "tree": graph.name,
        "nodes": len(kept_names),
        "edges": len(kept_edges),
        "links": len(graph.links),
        "reroutes": reroutes,
        "clipped": clipped,
        "externals": len(extra_externals),
        "focus": focus if mode == "NEIGHBORHOOD" else "",
        "mode": mode,
        "clusters": len(kept_names) if mode == "CLUSTERS" else 0,
    }
    return last_report


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
