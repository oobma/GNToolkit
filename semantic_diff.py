# -*- coding: utf-8 -*-
"""Semantic diff of GNToolkit group JSON data (pure Python, no bpy).

Two uses:
  * ``gnt_check.py --diff A B`` prints a readable report of what changed
    between two versions (folders, files or git revisions).
  * The add-on builds a compact commit-message suggestion from the same
    comparison when committing ("Git Commit…" pre-fills the message).

Layout-only node properties (position, sizes, selection) are ignored, so
the comparison matches the add-on's change semantics.
"""

from __future__ import annotations

import json
import os

from .constants import HASH_EXCLUDE_NODE_PROPS
from .merge import match_entry_nodes

_DIFF_IO = ("NodeGroupInput", "NodeGroupOutput")
_COSMETIC = frozenset(HASH_EXCLUDE_NODE_PROPS) | {"vector_dimensions"}


def _diff_node_type(node: dict) -> str:
    bl_type = node.get("type", "?")
    for prefix in ("GeometryNode", "FunctionNode", "ShaderNode", "Node"):
        if bl_type.startswith(prefix) and len(bl_type) > len(prefix):
            return bl_type[len(prefix):]
    return bl_type


def _diff_short(value) -> str:
    if isinstance(value, float):
        return "%.6g" % value
    if (isinstance(value, list) and value
            and all(isinstance(x, (int, float)) for x in value)):
        return "(" + ", ".join("%.4g" % x for x in value) + ")"
    if isinstance(value, dict) and "name" in value:
        return "'%s'" % value["name"]
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 60 else text[:57] + "..."


def _diff_interface(a: dict, b: dict) -> list:
    def items(data):
        return {i["identifier"]: i for i in data.get("interface_items", [])
                if i.get("item_type") == "SOCKET" and i.get("identifier")}

    ia, ib = items(a), items(b)
    lines = []
    for key in sorted(set(ib) - set(ia), key=lambda k: ib[k].get("name", "")):
        item = ib[key]
        props = item.get("properties", {})
        extra = ""
        if "min_value" in props and "max_value" in props:
            extra = "  %s..%s" % (_diff_short(props["min_value"]),
                                  _diff_short(props["max_value"]))
        lines.append("+ %s '%s' %s, default %s%s" % (
            item.get("in_out", "").lower(), item.get("name", ""),
            item.get("socket_type", "").replace("NodeSocket", ""),
            _diff_short(props.get("default_value")), extra))
    for key in sorted(set(ia) - set(ib), key=lambda k: ia[k].get("name", "")):
        lines.append("- %s '%s'" % (ia[key].get("in_out", "").lower(),
                                    ia[key].get("name", "")))
    for key in sorted(set(ia) & set(ib), key=lambda k: ib[k].get("name", "")):
        x, y = ia[key], ib[key]
        changes = []
        if x.get("name") != y.get("name"):
            changes.append("name '%s' -> '%s'" % (x.get("name"),
                                                  y.get("name")))
        if x.get("socket_type") != y.get("socket_type"):
            changes.append("type %s -> %s" % (x.get("socket_type"),
                                              y.get("socket_type")))
        px, py = x.get("properties", {}), y.get("properties", {})
        for field in ("default_value", "min_value", "max_value",
                      "hide_in_modifier"):
            if px.get(field) != py.get(field):
                changes.append("%s %s -> %s" % (
                    field, _diff_short(px.get(field)),
                    _diff_short(py.get(field))))
        if changes:
            lines.append("~ '%s': %s" % (y.get("name", ""),
                                         "; ".join(changes)))
    return lines


def _diff_node_lines(nodes: list) -> list:
    by_prefix = {}
    for node in nodes:
        name = node.get("name", "")
        prefix = None
        for sep in ("_", "."):
            if sep in name:
                prefix = name.split(sep)[0] + sep
                break
        by_prefix.setdefault(prefix, []).append(node)
    lines = []
    for prefix, group in sorted(by_prefix.items(),
                                key=lambda kv: (kv[0] is None, kv[0] or "")):
        if (prefix and len(group) >= 3
                and not prefix.rstrip("_.").isdigit()):
            kinds = {}
            for node in group:
                kind = _diff_node_type(node)
                kinds[kind] = kinds.get(kind, 0) + 1
            summary = ", ".join(
                "%d %s" % (count, kind) if count > 1 else kind
                for kind, count in sorted(kinds.items()))
            lines.append("%s* (%d): %s" % (prefix, len(group), summary))
            lines.append("    " + ", ".join(
                sorted(n.get("name", "")[len(prefix):] for n in group)))
        else:
            for node in sorted(group, key=lambda n: n.get("name", "")):
                lines.append("%s [%s]" % (node.get("name", ""),
                                          _diff_node_type(node)))
    return lines


def _diff_group_nodes(a: dict, b: dict, cosmetic=_COSMETIC) -> tuple:
    na = {n.get("name"): n for n in a.get("nodes", [])}
    nb = {n.get("name"): n for n in b.get("nodes", [])}
    added = [nb[k] for k in nb if k not in na]
    removed = [na[k] for k in na if k not in nb]
    changed = []
    moved = 0
    for key in sorted(set(na) & set(nb)):
        x, y = na[key], nb[key]
        changes = []
        if x.get("type") != y.get("type"):
            changes.append("type %s -> %s" % (_diff_node_type(x),
                                              _diff_node_type(y)))
        if (x.get("label") or "") != (y.get("label") or ""):
            changes.append("label '%s' -> '%s'" % (x.get("label"),
                                                   y.get("label")))
        if (x.get("parent") or None) != (y.get("parent") or None):
            changes.append("frame %s -> %s" % (x.get("parent"),
                                               y.get("parent")))
        px, py = x.get("properties", {}), y.get("properties", {})
        for prop in sorted(set(px) | set(py)):
            if prop in cosmetic or prop.startswith("bl_"):
                continue
            if px.get(prop) != py.get(prop):
                changes.append("%s %s -> %s" % (
                    prop, _diff_short(px.get(prop)),
                    _diff_short(py.get(prop))))
        if y.get("type") not in _DIFF_IO:
            ex = {s.get("identifier") or s.get("name"): s
                  for s in x.get("inputs", [])}
            ey = {s.get("identifier") or s.get("name"): s
                  for s in y.get("inputs", [])}
            for sock in sorted(set(ex) & set(ey)):
                va = ex[sock].get("default_value")
                vb = ey[sock].get("default_value")
                if va is None or vb is None:
                    continue
                if va != vb:
                    changes.append("%s = %s -> %s" % (
                        ey[sock].get("name", sock),
                        _diff_short(va), _diff_short(vb)))
            for sock in sorted(set(ey) - set(ex)):
                changes.append("input added %s"
                               % ey[sock].get("name", sock))
            for sock in sorted(set(ex) - set(ey)):
                changes.append("input removed %s"
                               % ex[sock].get("name", sock))
        for field in ("zone_items", "menu_items_data", "zone_paired_node"):
            if x.get(field) != y.get(field):
                changes.append("%s changed" % field)
        if changes:
            changed.append((y, changes))
        elif x.get("location") != y.get("location"):
            moved += 1
    return added, removed, changed, moved


def _diff_group_links(a: dict, b: dict) -> tuple:
    labels = {n.get("name"): (n.get("label") or n.get("name"))
              for n in list(a.get("nodes", [])) + list(b.get("nodes", []))}
    old_names = {n.get("name") for n in a.get("nodes", [])}

    def key(link):
        return (link.get("from_node"), link.get("from_socket_id"),
                link.get("to_node"), link.get("to_socket_id"))

    socket_names = {}
    for link in list(a.get("links", [])) + list(b.get("links", [])):
        socket_names[(link.get("from_node"), link.get("from_socket_id"))] = \
            link.get("from_socket_name") or link.get("from_socket_id")
        socket_names[(link.get("to_node"), link.get("to_socket_id"))] = \
            link.get("to_socket_name") or link.get("to_socket_id")
    la = {key(link) for link in a.get("links", [])}
    lb = {key(link) for link in b.get("links", [])}
    added, removed = lb - la, la - lb
    by_dest = {}
    for k in removed:
        by_dest.setdefault((k[2], k[3]), []).append(k)
    relinked, used_added, used_removed = [], set(), set()
    for k in sorted(added):
        candidates = by_dest.get((k[2], k[3]))
        if candidates:
            old = candidates.pop(0)
            relinked.append("%s.%s: %s.%s -> %s.%s" % (
                labels.get(k[2], k[2]), socket_names.get((k[2], k[3]), k[3]),
                labels.get(old[0], old[0]),
                socket_names.get((old[0], old[1]), old[1]),
                labels.get(k[0], k[0]),
                socket_names.get((k[0], k[1]), k[1])))
            used_added.add(k)
            used_removed.add(old)

    def text(k):
        return "%s.%s -> %s.%s" % (
            labels.get(k[0], k[0]), socket_names.get((k[0], k[1]), k[1]),
            labels.get(k[2], k[2]), socket_names.get((k[2], k[3]), k[3]))

    rest = added - used_added
    internal = {k for k in rest
                if k[0] not in old_names and k[2] not in old_names}
    return (sorted(text(k) for k in rest - internal),
            sorted(text(k) for k in removed - used_removed),
            relinked, len(internal))


def _bypass_reroutes(data: dict) -> dict:
    """Copy *data* with NodeReroute pass-throughs removed.

    Reroute nodes are cosmetic routing helpers: their input is reconnected
    to every consumer of their output. A change that only adds, removes or
    moves a reroute therefore becomes a no-op.
    """
    nodes = data.get("nodes")
    links = data.get("links")
    if not isinstance(nodes, list) or not isinstance(links, list):
        return data
    reroutes = {n.get("name") for n in nodes
                if n.get("type") == "NodeReroute"}
    if not reroutes:
        return data

    incoming = {}
    outgoing = {}
    for link in links:
        if link.get("to_node") in reroutes:
            incoming[link.get("to_node")] = link
        if link.get("from_node") in reroutes:
            outgoing.setdefault(link.get("from_node"), []).append(link)

    new_links = [l for l in links
                 if l.get("from_node") not in reroutes
                 and l.get("to_node") not in reroutes]
    for reroute, outs in outgoing.items():
        source = incoming.get(reroute)
        guard = set()
        while (source is not None
               and source.get("from_node") in reroutes
               and source.get("from_node") not in guard):
            guard.add(source.get("from_node"))
            source = incoming.get(source.get("from_node"))
        if source is None:
            continue
        for out in outs:
            if out.get("to_node") in reroutes:
                continue
            copied = dict(out)
            copied["from_node"] = source.get("from_node")
            copied["from_socket_id"] = source.get("from_socket_id")
            copied["from_socket_name"] = source.get("from_socket_name")
            new_links.append(copied)

    out = dict(data)
    out["nodes"] = [n for n in nodes if n.get("name") not in reroutes]
    out["links"] = new_links
    return out


def canonical_hash_ignoring_reroutes(data: dict) -> str:
    """Canonical hash with reroute nodes bypassed (history/diff only)."""
    from .hash_utils import canonical_hash_from_json_data
    if not isinstance(data, dict):
        return canonical_hash_from_json_data(data)
    return canonical_hash_from_json_data(_bypass_reroutes(data))


def _node_renames(a: dict, b: dict) -> dict:
    """Map base node names to their side names when the matcher pairs them.

    UIDs are authoritative; nodes without them fall back to the same
    name+type, signature and topology rules used by the merge.  A rename
    into the name of a base node that stays put is skipped: the diff
    keeps it as delete + add instead of merging two different nodes.
    """
    matched = match_entry_nodes(a, b, use_uids=True)
    a_names = {n.get("name", "") for n in a.get("nodes", [])}
    moved = {old for old, new in matched["matches"].items() if old != new}
    renames = {}
    for old, new in matched["matches"].items():
        if old == new or not old or not new:
            continue
        if new in a_names and new not in moved:
            continue
        renames[old] = new
    return renames


def _apply_node_renames(data: dict, renames: dict) -> dict:
    """Copy *data* with node names (and name references) remapped."""
    if not renames:
        return data
    out = dict(data)
    nodes = []
    for node in data.get("nodes", []):
        node = dict(node)
        node["name"] = renames.get(node.get("name"), node.get("name"))
        if node.get("parent") in renames:
            node["parent"] = renames[node["parent"]]
        if node.get("zone_paired_node") in renames:
            node["zone_paired_node"] = renames[node["zone_paired_node"]]
        nodes.append(node)
    out["nodes"] = nodes
    links = []
    for link in data.get("links", []):
        link = dict(link)
        link["from_node"] = renames.get(link.get("from_node"),
                                        link.get("from_node"))
        link["to_node"] = renames.get(link.get("to_node"),
                                      link.get("to_node"))
        links.append(link)
    out["links"] = links
    return out


def compare_groups(a: dict, b: dict) -> dict | None:
    """Structured semantic difference between two group versions.

    Returns None when nothing changed (layout-only moves and reroutes do
    not count).
    """
    a = _bypass_reroutes(a)
    b = _bypass_reroutes(b)
    renames = _node_renames(a, b)
    renamed = sorted(renames.items())
    a = _apply_node_renames(a, renames)
    interface = _diff_interface(a, b)
    added, removed, changed, moved = _diff_group_nodes(a, b)
    new_links, gone_links, relinked, internal = _diff_group_links(a, b)
    if not (interface or renamed or added or removed or changed or new_links
            or gone_links or relinked or internal or moved):
        return None
    return {
        "interface": interface,
        "renamed": renamed,
        "added": added,
        "removed": removed,
        "changed": changed,
        "new_links": new_links,
        "gone_links": gone_links,
        "relinked": relinked,
        "internal": internal,
        "moved": moved,
        "nodes_a": len(a.get("nodes", [])),
        "nodes_b": len(b.get("nodes", [])),
        "links_a": len(a.get("links", [])),
        "links_b": len(b.get("links", [])),
    }


def format_group_report(name: str, result: dict) -> list:
    """Readable report lines for one changed group (gnt_check --diff)."""
    lines = ["== %s  (%d -> %d nodes, %d -> %d links)" % (
        name, result["nodes_a"], result["nodes_b"],
        result["links_a"], result["links_b"])]
    if result["interface"]:
        lines.append("  Interface:")
        lines += ["    " + x for x in result["interface"]]
    if result.get("renamed"):
        lines.append("  Nodes renamed (%d):" % len(result["renamed"]))
        lines += ["    %s -> %s" % pair for pair in result["renamed"]]
    if result["added"]:
        lines.append("  Nodes added (%d):" % len(result["added"]))
        lines += ["    " + x for x in _diff_node_lines(result["added"])]
    if result["removed"]:
        lines.append("  Nodes removed (%d):" % len(result["removed"]))
        lines += ["    " + x for x in _diff_node_lines(result["removed"])]
    if result["changed"]:
        lines.append("  Nodes changed (%d):" % len(result["changed"]))
        for node, changes in result["changed"]:
            lines.append("    %s: %s" % (node.get("name", ""),
                                         "; ".join(changes)))
    if result["relinked"]:
        lines.append("  Links re-linked (%d):" % len(result["relinked"]))
        lines += ["    " + x for x in result["relinked"]]
    if result["new_links"]:
        lines.append("  Links added (%d):" % len(result["new_links"]))
        lines += ["    " + x for x in result["new_links"]]
    if result["internal"]:
        lines.append("  (+%d internal links between new nodes)"
                     % result["internal"])
    if result["gone_links"]:
        lines.append("  Links removed (%d):" % len(result["gone_links"]))
        lines += ["    " + x for x in result["gone_links"]]
    if result["moved"]:
        lines.append("  (%d nodes only moved)" % result["moved"])
    return lines


def summarize_group(name: str, result: dict) -> str:
    """One-line summary for commit messages (counts only)."""
    added, removed = len(result["added"]), len(result["removed"])
    links_added = (len(result["new_links"]) + len(result["relinked"])
                   + result["internal"])
    links_removed = len(result["gone_links"]) + len(result["relinked"])
    parts = []
    if added or removed:
        parts.append("+%d/-%d nodes" % (added, removed))
    if result.get("renamed"):
        parts.append("%d node(s) renamed" % len(result["renamed"]))
    if links_added or links_removed:
        parts.append("+%d/-%d links" % (links_added, links_removed))
    if result["interface"]:
        parts.append("%d interface change(s)" % len(result["interface"]))
    if result["changed"]:
        parts.append("%d node(s) changed" % len(result["changed"]))
    if not parts:
        parts.append("%d node(s) moved" % result["moved"])
    return "%s: %s" % (name, ", ".join(parts))


def suggest_message(changed: list, new_groups: list,
                    limit: int = 5, max_len: int = 260) -> str:
    """Compact commit message from per-group summaries and new groups."""
    parts = list(changed[:limit])
    parts += ["add %s (%d nodes)" % (name, count)
              for name, count in new_groups[:limit]]
    total = len(changed) + len(new_groups)
    if total > len(parts):
        parts.append("and %d more group(s)" % (total - len(parts)))
    message = "; ".join(parts)
    if len(message) > max_len:
        message = message[:max_len - 4].rstrip(" ;,") + " ..."
    return message


def summarize_commit_groups(old_groups: dict, new_groups: dict,
                            limit: int = 5, max_len: int = 260) -> str:
    """Compact message for the group changes a commit writes (HEAD -> work).

    ``old_groups`` and ``new_groups`` are ``{name: entry}`` maps (the same
    shape produced by :func:`add_group_data`).  The direction is old -> new,
    so the message describes what the commit actually changes, unlike the
    save-time suggestion cached from the live .blend.
    """
    changed = []
    new = []
    for name in sorted(set(old_groups) | set(new_groups)):
        old = old_groups.get(name)
        current = new_groups.get(name)
        if old is not None and not isinstance(old, dict):
            continue
        if current is not None and not isinstance(current, dict):
            continue
        if old is None and current is not None:
            new.append((name, len(current.get("nodes", []) or [])))
        elif current is None and old is not None:
            changed.append("%s: removed" % name)
        elif old is not None and current is not None:
            result = compare_groups(old, current)
            if result is not None:
                changed.append(summarize_group(name, result))
    return suggest_message(changed, new, limit=limit, max_len=max_len)


def add_group_data(groups: dict, data, fallback_name: str) -> None:
    """Merge one JSON document (standalone or package) into *groups*."""
    if not isinstance(data, dict):
        return
    node_groups = data.get("node_groups")
    if isinstance(node_groups, dict):
        for name, group in node_groups.items():
            if isinstance(group, dict):
                groups[name] = group
    elif "nodes" in data:
        groups[data.get("name", fallback_name)] = data


def load_groups(path: str) -> dict:
    """{group name: data} from a folder (walks it) or a single JSON file."""
    files = []
    if os.path.isdir(path):
        for root, _dirs, names in os.walk(path):
            for name in sorted(names):
                if name.endswith(".json"):
                    files.append(os.path.join(root, name))
    else:
        files = [path]
    groups = {}
    for file_path in files:
        try:
            with open(file_path, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        add_group_data(groups, data, os.path.basename(file_path)[:-5])
    return groups
