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


def layered_layout(names, edges, x_step: float = 420.0,
                   y_step: float = 64.0, heights=None, widths=None,
                   gap: float = 40.0, x_gap: float = 140.0,
                   passes: int = 4, align_passes: int = 6,
                   swap_passes: int = 4, swap_limit: int = 150) -> dict:
    """{name: (x, y)} — consumers at the left, dependencies at the right.

    The level of a node is ``1 + max(level of its dependencies)``; cycles
    are broken deterministically (a node revisiting itself contributes 0).
    Within each level the order is refined with the barycenter heuristic
    (stable sorts, no randomness) to reduce edge crossings, and with
    uniform heights the columns are then pulled vertically toward their
    neighbours (priority method: connected nodes drift to the same height,
    order and spacing preserved — the effect that makes graphviz/Node
    Arrange drawings readable).  ``widths`` packs each column as close as
    its widest node allows (``x_gap`` between columns) instead of a fixed
    grid — that is what keeps the link lengths short.  The final grid is
    mirrored so links flow left-to-right.  ``heights`` maps name → node
    height for real-height stacking (no alignment).  The same inputs
    always produce the same layout.
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
    visiting: set = set()

    def depth(n: str) -> int:
        if n in level:
            return level[n]
        if n in visiting:
            return 0
        visiting.add(n)
        d = 0
        for dep in deps[n]:
            d = max(d, depth(dep) + 1)
        visiting.discard(n)
        level[n] = d
        return d

    for n in names:
        depth(n)
    # Tighten (median heuristic, deterministic sweep): pull every node
    # toward the median level of its neighbours within the feasible band
    # (deps at lower levels, consumers at higher) so edges span fewer
    # columns — the compactness trick graphviz/dot uses.
    for _ in range(3):
        changed = False
        for n in names:
            lo = 1 + max((level[m] for m in deps[n]), default=-1)
            hi = min((level[m] - 1 for m in fwd[n]), default=level[n])
            if hi < lo:
                hi = lo
            vals = sorted(level[m] for m in (deps[n] | fwd[n]))
            if not vals:
                continue
            target = min(max(vals[len(vals) // 2], lo), hi)
            if target != level[n]:
                level[n] = target
                changed = True
        if not changed:
            break
    columns: dict = {}
    for n in names:
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
    if swap_passes and len(names) <= swap_limit:
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
                h = float(heights.get(n, 100.0))
                y[n] = -(offset + h / 2.0)
                offset += h + gap
            else:
                y[n] = -offset
                offset += y_step

    if not heights:
        # Priority-method vertical alignment: each column drifts toward the
        # average height of its neighbours, then the two sweeps restore the
        # order and the minimum spacing inside the column.
        step = float(y_step)
        for alignment in range(max(0, int(align_passes))):
            walk = levels if alignment % 2 == 0 else list(reversed(levels))
            for lv in walk:
                col = columns[lv]
                targets = []
                for n in col:
                    neigh = [y[m] for m in (deps[n] | fwd[n]) if m in y]
                    targets.append(sum(neigh) / len(neigh) if neigh
                                   else y[n])
                for i in range(1, len(col)):
                    targets[i] = min(targets[i], targets[i - 1] - step)
                for i in range(len(col) - 2, -1, -1):
                    targets[i] = max(targets[i], targets[i + 1] + step)
                for n, target in zip(col, targets):
                    y[n] = target

    x = {}
    if widths:
        cursor = 0.0
        for lv in levels:
            column_width = max(float(widths.get(n, 200.0))
                               for n in columns[lv])
            cursor += column_width / 2.0
            for n in columns[lv]:
                x[n] = cursor
            cursor += column_width / 2.0 + x_gap
    else:
        for n in names:
            x[n] = level[n] * x_step
    # Mirror so the consumer-side sits at the left and links read L→R.
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


def render_svg(names, edges, status_keys, focus: str = "",
               externals=(), title: str = "") -> str:
    """Deterministic SVG poster for the board data (pure text, no libs).

    *status_keys* maps name → status key (see ``STATUS_COLORS``); names in
    *externals* are drawn dashed.  The layout is the same layered grid the
    node-editor board uses; dependencies sit at the left.  No timestamp is
    embedded, so the same input always renders the same bytes.
    """
    names = sorted(set(names))
    ext = set(externals)

    def box_width(n):
        label = ("* " + n) if n == focus else n
        return min(340.0, max(170.0, 7.4 * len(label) + 30.0))

    widths = {n: box_width(n) for n in names}
    layout = layered_layout(names, edges, widths=widths)

    boxes = {}
    for n in names:
        x, y = layout.get(n, (0.0, 0.0))
        boxes[n] = (x, -y, widths[n], 46.0)  # SVG y grows downward

    margin = 40.0
    title_h = 56.0
    legend_w = 230.0
    min_x = min((b[0] for b in boxes.values()), default=0.0)
    max_x = max((b[0] + b[2] for b in boxes.values()), default=0.0)
    n_rows = max((len([1 for b in boxes.values() if b[0] == x]) for x in
                  {b[0] for b in boxes.values()}), default=1)
    width = (max_x - min_x) + legend_w + 2 * margin
    height = title_h + margin + n_rows * 180.0 + margin
    dx = margin - min_x
    dy = title_h + margin

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
    # left-to-right, matching the mirrored board).
    for a, b in sorted(set(edges)):
        if a not in boxes or b not in boxes:
            continue
        ax, ay, aw, ah = box_of(a)
        bx, by, bw, bh = box_of(b)
        x1, y1 = ax + aw, ay + ah / 2.0
        x2, y2 = bx, by + bh / 2.0
        c = max(30.0, abs(x1 - x2) * 0.35)
        color = "#b9c2cc" if b in ext else "#7a8aa0"
        parts.append(
            f'<path d="M {x1:.1f} {y1:.1f} C {x1 + c:.1f} {y1:.1f}, '
            f'{x2 - c:.1f} {y2:.1f}, {x2:.1f} {y2:.1f}" fill="none" '
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
            f'fill="#222">{svg_escape(shown)}</text></g>')

    # Legend with counts.
    counts = {}
    for k in status_keys.values():
        counts[k] = counts.get(k, 0) + 1
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
