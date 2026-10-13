# -*- coding: utf-8 -*-
"""Pure graph helpers for the add-on's graph view (and the SVG poster).

No ``bpy`` here: callers feed plain data (names, edges, status values), so
the neighbourhood filter, the layered layout and the status colours are
unit-testable and shared by the node-editor board and the poster export.

The ``gnt_check --render`` repository command keeps its own copy of the
render-adjacent helpers on purpose: it must stay a single self-contained
file that runs without the add-on.
"""

from __future__ import annotations

import heapq

STATUS_COLORS: dict = {
    "synced": (0.1963, 0.2621, 0.3608),
    "blend_modified": (0.95, 0.72, 0.30),
    "json_modified": (0.2307, 0.3291, 0.95),
    "conflict": (0.92, 0.38, 0.36),
    "orphan": (0.80, 0.45, 0.75),
    "json_missing": (0.70, 0.72, 0.40),
    "untracked": (0.5253, 0.5046, 0.4562),
}

STATUS_LABELS: dict = {
    "synced": "Synced",
    "blend_modified": "Edited locally",
    "json_modified": "Changed in JSON",
    "conflict": "Conflict",
    "orphan": "Missing in blend",
    "json_missing": "JSON file missing",
    "untracked": "Untracked",
}


def neighborhood(edges, focus: str, hops: int) -> set:
    """Names within *hops* dependency hops of *focus* (both directions).

    Edges are (consumer, dependency) pairs; the walk follows both.  The
    focus itself is always kept.
    """
    fwd, rev = {}, {}
    for a, b in edges:
        fwd.setdefault(a, set()).add(b)
        rev.setdefault(b, set()).add(a)
    keep = {focus}
    frontier = {focus}
    for _ in range(max(0, int(hops))):
        nxt = set()
        for n in frontier:
            nxt |= fwd.get(n, set()) | rev.get(n, set())
        nxt -= keep
        if not nxt:
            break
        keep |= nxt
        frontier = nxt
    return keep


def _isotonic(values) -> list:
    """Non-decreasing least-squares fit (PAVA), deterministic.

    Pool-adjacent-violators: the closest monotone sequence to *values*.
    Used to fit a column to its neighbour targets while keeping the row
    order and the minimum spacing, without cascade growth.
    """
    blocks = []
    for v in values:
        total, count = float(v), 1
        while blocks and blocks[-1][0] / blocks[-1][1] > total / count:
            prev_total, prev_count = blocks.pop()
            total += prev_total
            count += prev_count
        blocks.append((total, count))
    fit = []
    for total, count in blocks:
        fit.extend([total / count] * count)
    return fit


def crossings_of_layout(layout: dict, edges) -> int:
    """Crossings between adjacent columns of a finished layout (metric)."""
    order = {}
    columns = {}
    for n, (x, y) in layout.items():
        columns.setdefault(round(x, 6), []).append(n)
    xs = sorted(columns)
    for x in xs:
        members = sorted(columns[x], key=lambda n: (-layout[n][1], n))
        for i, n in enumerate(members):
            order[n] = i
    total = 0
    for low, high in zip(xs, xs[1:]):
        segs = []
        for a, b in edges:
            if a not in order or b not in order:
                continue
            xa = round(layout[a][0], 6)
            xb = round(layout[b][0], 6)
            if (xa, xb) == (low, high):
                segs.append((order[a], order[b]))
            elif (xb, xa) == (low, high):
                segs.append((order[b], order[a]))
        for i in range(len(segs)):
            a1, b1 = segs[i]
            for j in range(i + 1, len(segs)):
                a2, b2 = segs[j]
                if (a1 - a2) * (b1 - b2) < 0:
                    total += 1
    return total


def _cycle_free_deps(names, deps) -> dict:
    """Copy of *deps* without the back edges that close cycles.

    Three-colour DFS in name order (deterministic): an edge whose
    dependency is still on the stack would close a cycle and is dropped —
    ranking needs a DAG.
    """
    state: dict = {}
    out = {n: set() for n in names}
    for root in names:
        if state.get(root):
            continue
        state[root] = 1
        stack = [(root, iter(sorted(deps.get(root, ()))))]
        while stack:
            node, it = stack[-1]
            went = False
            for dep in it:
                st = state.get(dep, 0)
                if st == 1:
                    continue
                out[node].add(dep)
                if st == 0:
                    state[dep] = 1
                    stack.append((dep, iter(sorted(deps.get(dep, ())))))
                    went = True
                    break
            if not went:
                state[node] = 2
                stack.pop()
    return out


def _simplex_ranks(names, dag_deps) -> dict:
    """Minimum total-span ranks — network simplex (Gansner et al., 1993).

    With unit weights the sum of all edge spans telescopes to a linear
    cost in the ranks, so a network simplex over the feasible tight tree
    reaches the global optimum: ranks satisfy ``rank(consumer) >=
    rank(dependency) + 1`` and minimise the total span — the compactness
    property graphviz/dot derives from this same algorithm.  *dag_deps*
    must be acyclic (see ``_cycle_free_deps``); everything deterministic.
    """
    names = sorted(names)
    arcs = [(u, v) for v in names for u in sorted(dag_deps[v])]
    out_arcs = {n: [] for n in names}
    in_arcs = {n: [] for n in names}
    for i, (u, v) in enumerate(arcs):
        out_arcs[u].append(i)
        in_arcs[v].append(i)

    rank = {n: 0 for n in names}
    missing = {n: len(in_arcs[n]) for n in names}
    heap = [n for n in names if not in_arcs[n]]
    heapq.heapify(heap)
    while heap:
        u = heapq.heappop(heap)
        for ai in out_arcs[u]:
            v = arcs[ai][1]
            if rank[u] + 1 > rank[v]:
                rank[v] = rank[u] + 1
            missing[v] -= 1
            if missing[v] == 0:
                heapq.heappush(heap, v)

    adj = {n: set() for n in names}
    for u, v in arcs:
        adj[u].add(v)
        adj[v].add(u)
    seen = set()
    components = []
    for root in names:
        if root in seen:
            continue
        seen.add(root)
        comp = [root]
        stack = [root]
        while stack:
            n = stack.pop()
            for m in sorted(adj[n]):
                if m not in seen:
                    seen.add(m)
                    comp.append(m)
                    stack.append(m)
        components.append(sorted(comp))

    for comp in components:
        if len(comp) < 2:
            continue
        cset = set(comp)
        comp_arcs = [i for i, (u, _v) in enumerate(arcs) if u in cset]
        weight = {n: len(out_arcs[n]) - len(in_arcs[n]) for n in comp}

        def slack(ai):
            u, v = arcs[ai]
            return rank[v] - rank[u] - 1

        root = comp[0]
        tree = set()
        t_adj = {n: [] for n in comp}
        inside = {root}

        def add_tree(ai):
            u, v = arcs[ai]
            tree.add(ai)
            t_adj[u].append(ai)
            t_adj[v].append(ai)

        while len(inside) < len(comp):
            grown = True
            while grown:
                grown = False
                for ai in comp_arcs:
                    if ai in tree:
                        continue
                    u, v = arcs[ai]
                    uin, vin = u in inside, v in inside
                    if uin != vin and slack(ai) == 0:
                        add_tree(ai)
                        inside.add(v if uin else u)
                        grown = True
            if len(inside) == len(comp):
                break
            best = None
            for ai in comp_arcs:
                u, v = arcs[ai]
                uin, vin = u in inside, v in inside
                if uin == vin:
                    continue
                key = (slack(ai), ai)
                if best is None or key < best[0]:
                    best = (key, ai, uin)
            _, ai, tail_in = best
            delta = slack(ai) if tail_in else -slack(ai)
            for n in inside:
                rank[n] += delta
            u, v = arcs[ai]
            add_tree(ai)
            inside.add(v if tail_in else u)

        limit = 50 * int(len(comp) ** 0.5) + 50
        for _ in range(limit):
            parent = {root: None}
            par_arc = {root: None}
            children = {n: [] for n in comp}
            visited = {root}
            order = []
            stack = [root]
            while stack:
                n = stack.pop()
                order.append(n)
                for ai in t_adj[n]:
                    u, v = arcs[ai]
                    m = v if u == n else u
                    if m in visited:
                        continue
                    visited.add(m)
                    parent[m] = n
                    par_arc[m] = ai
                    children[n].append(m)
                    stack.append(m)

            sub = dict(weight)
            for n in reversed(order):
                p = parent[n]
                if p is not None:
                    sub[p] += sub[n]
            tin = {}
            tout = {}
            counter = 0
            tin[root] = counter
            counter += 1
            stack = [(root, iter(children[root]))]
            while stack:
                n, it = stack[-1]
                for c in it:
                    tin[c] = counter
                    counter += 1
                    stack.append((c, iter(children[c])))
                    break
                else:
                    tout[n] = counter - 1
                    stack.pop()

            best_leave = None
            for c in order:
                ai = par_arc[c]
                if ai is None:
                    continue
                u, _v = arcs[ai]
                cut = sub[c] if tin[c] <= tin[u] <= tout[c] else -sub[c]
                if cut < 0:
                    key = (cut, ai)
                    if best_leave is None or key < best_leave[0]:
                        best_leave = (key, c)
            if best_leave is None:
                break
            _, c = best_leave
            ai = par_arc[c]
            _u, v = arcs[ai]
            in_sub = (tin[c] <= tin[v] <= tout[c])

            def in_side(n):
                s = tin[c] <= tin[n] <= tout[c]
                return s if in_sub else not s

            best_enter = None
            for aj in comp_arcs:
                if aj in tree:
                    continue
                x, y = arcs[aj]
                if in_side(x) and not in_side(y):
                    key = (slack(aj), aj)
                    if best_enter is None or key < best_enter[0]:
                        best_enter = (key, aj)
            if best_enter is None:
                break
            _, aj = best_enter
            delta = slack(aj)
            for n in comp:
                if in_side(n):
                    rank[n] += delta
            u2, v2 = arcs[ai]
            t_adj[u2].remove(ai)
            t_adj[v2].remove(ai)
            tree.discard(ai)
            add_tree(aj)

    # The span cost is invariant to a global offset: normalise every
    # weakly connected component so its shallowest node sits at level 0.
    for comp in components:
        base = min(rank[n] for n in comp)
        if base:
            for n in comp:
                rank[n] -= base

    return rank


def layered_layout(names, edges, x_step: float = 420.0,
                   y_step: float = 44.0, heights=None, widths=None,
                   gap: float = 40.0, x_gap: float = 40.0,
                   passes: int = 4, align_passes: int = 8,
                   align_damping: float = 0.0,
                   swap_passes: int = 4, swap_limit: int = 150,
                   routes_out=None, route_row: float = 10.0) -> dict:
    """{name: (x, y)} — consumers at the left, dependencies at the right.

    The level of a node comes from a **network simplex** ranking of the
    dependency DAG (Gansner et al., 1993, the ranking dot uses): ranks
    minimise the total edge span, so the drawing is structurally compact;
    cycles are broken deterministically (see ``_cycle_free_deps``).
    Within each level the order is refined with the barycenter heuristic
    (stable sorts, no randomness) to reduce edge crossings, and with
    uniform heights the columns are then pulled vertically toward their
    neighbours (priority method: connected nodes drift to the same height,
    order and spacing preserved — the effect that makes classic layered
    drawings readable) with the most compact order-preserving fit,
    so conflicting targets cannot inflate a column.  ``widths`` packs each column as close as its widest node
    allows (``x_gap`` between columns) instead of a fixed grid — that is
    what keeps the link lengths short.  The x of every node is the
    column CENTER (callers position boxes with ``x - width / 2``).  The
    final grid is mirrored so links flow left-to-right.  ``heights`` maps
    name → node height for real-height stacking (collapsed node bars grow
    with their socket count), packed with the same isotonic alignment.
    The same inputs always produce the same layout.

    When ``routes_out`` is a dict, every edge that would jump over
    intermediate columns gets one routing point per column in between
    (they take part in the ordering and the vertical alignment exactly
    like nodes), and ``routes_out[(consumer, dependency)]`` receives the
    list of waypoint positions in edge order — the node editor turns them
    into Reroute nodes and the poster draws them as a polyline.  Each
    routing row is ``route_row`` tall inside the column stack, so callers
    can size it for the drawn footprint of their marker.
    """
    names = sorted(set(names))
    known = set(names)
    deps = {n: set() for n in names}
    fwd = {n: set() for n in names}
    for a, b in edges:
        if a in known and b in known:
            deps[a].add(b)
            fwd[b].add(a)
    level: dict = {}
    # Minimum total-span ranks over the acyclic part of the dependency
    # graph (network simplex): long cables shrink structurally first, so
    # only the remaining ones need routing points between columns.
    level = _simplex_ranks(names, _cycle_free_deps(names, deps))
    # Routing points: one per intermediate column for every edge that
    # would jump over columns.  They join the ordering and the vertical
    # alignment like ordinary nodes, which is what turns long cables into
    # tidy vertical lanes instead of lines sweeping across the drawing.
    dummy_set: set = set()
    route_chains: dict = {}
    if routes_out is not None:
        for a, b in sorted(set(edges)):
            if a not in known or b not in known or a == b:
                continue
            if abs(level[b] - level[a]) <= 1:
                continue
            step_lv = 1 if level[b] > level[a] else -1
            cur = a
            chain = []
            for lv in range(level[a] + step_lv, level[b], step_lv):
                d = "\x00routed%04d" % len(dummy_set)
                dummy_set.add(d)
                deps[d] = {cur}
                fwd.setdefault(cur, set()).add(d)
                fwd.setdefault(d, set())
                level[d] = lv
                cur = d
                chain.append(d)
            deps[b].add(cur)
            fwd.setdefault(cur, set()).add(b)
            route_chains[(a, b)] = chain
    working = names + sorted(dummy_set)

    columns: dict = {}
    for n in working:
        columns.setdefault(level[n], []).append(n)
    levels = sorted(columns)
    index = {lv: {n: i for i, n in enumerate(columns[lv])} for lv in levels}
    lev_pos = {lv: i for i, lv in enumerate(levels)}

    def _reorder(lev, ref_lev) -> None:
        def bary(n):
            vals = [index[ref_lev][m] for m in (deps[n] | fwd[n])
                    if m in index[ref_lev]]
            return (sum(vals) / len(vals), n) if vals else (-1.0, n)
        columns[lev] = sorted(columns[lev], key=bary)
        index[lev] = {n: i for i, n in enumerate(columns[lev])}

    for _ in range(max(0, int(passes))):
        for lv in levels[1:]:
            prev = levels[lev_pos[lv] - 1]
            _reorder(lv, prev)
        for lv in reversed(levels[:-1]):
            nxt = levels[lev_pos[lv] + 1]
            _reorder(lv, nxt)

    # Deterministic adjacent-swap refinement (2-opt): try swapping each
    # neighbouring pair inside a column and keep it only when the adjacent
    # boundaries lose crossings.  Skipped on very large graphs (cost).
    if swap_passes and len(working) <= swap_limit:
        boundary_cache = {}

        def _boundary_segs(lv):
            segs = boundary_cache.get(lv)
            if segs is None:
                segs = []
                if lv in index and (lv + 1) in index:
                    for u in columns[lv + 1]:
                        for d in deps[u]:
                            if d in index[lv]:
                                segs.append((index[lv][d],
                                             index[lv + 1][u]))
                boundary_cache[lv] = segs
            return segs

        def _boundary_crossings(segs):
            total = 0
            for i in range(len(segs)):
                a1, b1 = segs[i]
                for j in range(i + 1, len(segs)):
                    a2, b2 = segs[j]
                    if (a1 - a2) * (b1 - b2) < 0:
                        total += 1
            return total

        for _pass in range(max(0, int(swap_passes))):
            improved = False
            for lv in levels:
                col = columns[lv]
                for i in range(len(col) - 1):
                    affected = (lv - 1, lv)
                    before = sum(_boundary_crossings(_boundary_segs(b))
                                 for b in affected)
                    col[i], col[i + 1] = col[i + 1], col[i]
                    index[lv] = {n: k for k, n in enumerate(col)}
                    for b in affected:
                        boundary_cache.pop(b, None)
                    after = sum(_boundary_crossings(_boundary_segs(b))
                                for b in affected)
                    if after < before:
                        improved = True
                    else:
                        col[i], col[i + 1] = col[i + 1], col[i]
                        index[lv] = {n: k for k, n in enumerate(col)}
                        for b in affected:
                            boundary_cache.pop(b, None)
            if not improved:
                break

    y = {}
    for lv in levels:
        offset = 0.0
        for n in columns[lv]:
            if heights:
                h = float(heights.get(n, route_row if n in dummy_set else 100.0))
                y[n] = -(offset + h / 2.0)
                offset += h + gap
            else:
                y[n] = -offset
                offset += y_step

    # Vertical alignment (both stacking modes): every node targets the
    # average height of its neighbours; each column is then fitted to the
    # closest order-and-spacing preserving stack with isotonic regression
    # (PAVA) — conflicting targets pool instead of cascading, so columns
    # and routing lanes stay compact and never inflate.  With real heights
    # the minimum spacing follows each pair of boxes; with uniform heights
    # it degenerates to one row per node.
    if heights:
        def _item_h(n):
            return float(heights.get(n, route_row if n in dummy_set else 100.0))
        col_gap = float(gap)
    else:
        def _item_h(n):
            return float(y_step)
        col_gap = 0.0
    cap = float(align_damping) * float(y_step) if align_damping else 0.0
    for alignment in range(max(0, int(align_passes))):
        walk = levels if alignment % 2 == 0 else list(reversed(levels))
        for lv in walk:
            col = columns[lv]
            targets = []
            for n in col:
                neigh = [y[m] for m in (deps[n] | fwd[n]) if m in y]
                targets.append(sum(neigh) / len(neigh) if neigh
                               else y[n])
            if cap:
                targets = [y[n] + max(-cap, min(cap, t - y[n]))
                           for n, t in zip(col, targets)]
            offsets = []
            acc = 0.0
            for n in col:
                offsets.append(acc + _item_h(n) / 2.0)
                acc += _item_h(n) + col_gap
            data = [-(t + o) for t, o in zip(targets, offsets)]
            fit = _isotonic(data)
            for n, o, f in zip(col, offsets, fit):
                y[n] = -(f + o)

    x = {}
    if widths:
        cursor = 0.0
        for lv in levels:
            column_width = max(float(widths.get(
                n, 0.0 if n in dummy_set else 200.0)) for n in columns[lv])
            cursor += column_width / 2.0
            for n in columns[lv]:
                x[n] = cursor
            cursor += column_width / 2.0 + x_gap
    else:
        for n in working:
            x[n] = level[n] * x_step
    # Mirror so the consumer-side sits at the left and links read L→R.
    if routes_out is not None:
        for (a, b), chain in route_chains.items():
            routes_out[(a, b)] = [(-x[d], y[d]) for d in chain]
    return {n: (-x[n], y[n]) for n in names}


def edges_within(edges, names) -> list:
    """Edges whose both endpoints are in *names* (stable order)."""
    known = set(names)
    return sorted((a, b) for a, b in edges if a in known and b in known)


def svg_escape(text) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _hex_color(rgb) -> str:
    return "#%02x%02x%02x" % tuple(
        max(0, min(255, int(round(c * 255)))) for c in rgb)


def _text_on(fill_hex: str) -> str:
    """Readable label colour for a box fill (light text over dark fills).

    The node editor draws labels light on its dark theme; the poster has a
    white canvas, so the label must follow the fill: dark statuses (synced
    blue, changed-in-JSON blue) get light text, the rest keeps #222.
    """
    s = fill_hex.lstrip("#")
    r = int(s[0:2], 16) / 255.0
    g = int(s[2:4], 16) / 255.0
    b = int(s[4:6], 16) / 255.0
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "#f0f0f0" if lum < 0.4 else "#222"


def render_svg(names, edges, status_keys, focus: str = "",
               externals=(), title: str = "", positions=None,
               routes=None) -> str:
    """Deterministic SVG poster for the board data (pure text, no libs).

    *status_keys* maps name → status key (see ``STATUS_COLORS``); names in
    *externals* are drawn dashed.  The layout is the same layered grid the
    node-editor board uses; dependencies sit at the left.  *positions*
    (name → (x_center, y_top)) overrides the computed layout so an
    externally arranged board can be exported as it is displayed; *routes*
    then supplies the waypoints per edge.  No timestamp is embedded, so
    the same input always renders the same bytes.
    """
    names = sorted(set(names))
    ext = set(externals)

    def box_width(n):
        label = ("* " + n) if n == focus else n
        return min(340.0, max(170.0, 7.4 * len(label) + 30.0))

    widths = {n: box_width(n) for n in names}
    if positions is None:
        routes = {} if routes is None else routes
        layout = layered_layout(names, edges, widths=widths, y_step=64.0,
                                routes_out=routes)
    else:
        layout = dict(positions)
        routes = {} if routes is None else routes

    boxes = {}
    for n in names:
        x, y = layout.get(n, (0.0, 0.0))
        boxes[n] = (x - widths[n] / 2.0, -y, widths[n], 46.0)  # y grows down

    counts = {}
    for k in status_keys.values():
        counts[k] = counts.get(k, 0) + 1
    legend_rows = sum(1 for key in STATUS_COLORS if counts.get(key, 0))

    margin = 40.0
    title_h = 56.0
    legend_w = 230.0
    min_x = min((b[0] for b in boxes.values()), default=0.0)
    max_x = max((b[0] + b[2] for b in boxes.values()), default=0.0)
    min_y = min((b[1] for b in boxes.values()), default=0.0)
    max_y = max((b[1] + b[3] for b in boxes.values()), default=0.0)
    width = (max_x - min_x) + legend_w + 2 * margin
    dx = margin - min_x
    dy = title_h + margin - min_y
    legend_bottom = dy + 10 + 24 + max(0, legend_rows - 1) * 26 + 18
    height = max(max_y + dy, legend_bottom) + margin

    def box_of(n):
        x, y, w, h = boxes[n]
        return x + dx, y + dy, w, h

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" '
        f'height="{height:.0f}" viewBox="0 0 {width:.0f} {height:.0f}" '
        'font-family="Segoe UI, sans-serif">',
        '<defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#7a8aa0"/></marker></defs>',
        f'<rect x="0" y="0" width="{width:.0f}" height="{height:.0f}" '
        'fill="white"/>',
        f'<text x="{margin:.0f}" y="34" font-size="18" fill="#333">'
        f'{svg_escape(title)}</text>',
    ]

    # Edges first (under the boxes).  Consumers sit at the left: draw from
    # the consumer's right edge to the dependency's left edge (links read
    # left-to-right, matching the mirrored board).  Long edges follow their
    # routing waypoints so they travel down the columns instead of sweeping
    # across the drawing.
    for a, b in sorted(set(edges)):
        if a not in boxes or b not in boxes:
            continue
        ax, ay, aw, ah = box_of(a)
        bx, by, bw, bh = box_of(b)
        points = [(ax + aw, ay + ah / 2.0)]
        for wx, wy in routes.get((a, b), ()):
            points.append((wx + dx, -wy + dy))
        points.append((bx, by + bh / 2.0))
        segments = [f"M {points[0][0]:.1f} {points[0][1]:.1f}"]
        for (x1, y1), (x2, y2) in zip(points, points[1:]):
            c = max(30.0, abs(x1 - x2) * 0.35)
            segments.append(f"C {x1 + c:.1f} {y1:.1f}, "
                            f"{x2 - c:.1f} {y2:.1f}, {x2:.1f} {y2:.1f}")
        color = "#b9c2cc" if b in ext else "#7a8aa0"
        parts.append(
            f'<path d="{" ".join(segments)}" fill="none" '
            f'stroke="{color}" stroke-width="1.4" marker-end="url(#arr)"/>')

    # Boxes.
    for n in names:
        x, y, w, h = box_of(n)
        key = status_keys.get(n, "untracked")
        fill = _hex_color(STATUS_COLORS.get(key, STATUS_COLORS["untracked"]))
        dashed = n in ext
        stroke = "#666" if dashed else "#4a5a6a"
        extra = ' stroke-dasharray="6 4"' if dashed else ""
        if n == focus:
            stroke = "#1b3a5c"
            extra += ' stroke-width="3"'
        else:
            extra += ' stroke-width="1.2"'
        parts.append(
            f'<g><title>{svg_escape(n)}</title>'
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" '
            f'rx="7" fill="{fill}" stroke="{stroke}"{extra}/>')
        label = ("* " + n) if n == focus else n
        max_chars = int((w - 16) / 7.0)
        shown = label if len(label) <= max_chars else label[:max_chars - 1] + "…"
        parts.append(
            f'<text x="{x + 8:.1f}" y="{y + h / 2 + 4:.1f}" font-size="12" '
            f'fill="{_text_on(fill)}">{svg_escape(shown)}</text></g>')

    # Legend with counts.
    lx = width - margin - legend_w + 10
    ly = dy + 10
    parts.append(f'<text x="{lx:.0f}" y="{ly:.0f}" font-size="13" '
                 'fill="#333">Legend</text>')
    row = 0
    for key in STATUS_COLORS:
        total = counts.get(key, 0)
        if not total:
            continue
        y = ly + 24 + row * 26
        parts.append(
            f'<rect x="{lx:.0f}" y="{y - 12:.0f}" width="18" height="14" '
            f'rx="3" fill="{_hex_color(STATUS_COLORS[key])}" stroke="#4a5a6a" '
            'stroke-width="1"/>')
        parts.append(
            f'<text x="{lx + 26:.0f}" y="{y:.0f}" font-size="12" '
            f'fill="#333">{svg_escape(STATUS_LABELS[key])} ({total})</text>')
        row += 1

    parts.append("</svg>")
    parts.append("")
    return "\n".join(parts)
