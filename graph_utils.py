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
    "synced": (0.62, 0.68, 0.75),
    "blend_modified": (0.95, 0.72, 0.30),
    "json_modified": (0.45, 0.68, 0.95),
    "conflict": (0.92, 0.38, 0.36),
    "orphan": (0.80, 0.45, 0.75),
    "json_missing": (0.75, 0.75, 0.45),
    "untracked": (0.85, 0.85, 0.85),
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


def layered_layout(names, edges, x_step: float = 360.0,
                   y_step: float = 180.0) -> dict:
    """{name: (x, y)} — dependencies at the left, consumers at the right.

    The level of a node is ``1 + max(level of its dependencies)``; cycles
    are broken deterministically (a node revisiting itself contributes 0).
    Within a level, nodes are ordered by name, so the same inputs always
    produce the same layout.
    """
    names = sorted(set(names))
    known = set(names)
    deps = {n: set() for n in names}
    for a, b in edges:
        if a in known and b in known:
            deps[a].add(b)
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
    rows: dict = {}
    for n in names:
        rows.setdefault(level[n], []).append(n)
    layout = {}
    for lv in sorted(rows):
        for i, n in enumerate(rows[lv]):
            layout[n] = (lv * x_step, -i * y_step)
    return layout


def edges_within(edges, names) -> list:
    """Edges whose both endpoints are in *names* (stable order)."""
    known = set(names)
    return sorted((a, b) for a, b in edges if a in known and b in known)
