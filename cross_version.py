# -*- coding: utf-8 -*-
"""Cross-version comparison of GNToolkit JSON exports (pure Python, no bpy).

Compares two exports of the same project made with different Blender
versions.  Nodes and sockets are matched by name so a new socket inserted
by the newer Blender is not confused with a renamed/reordered one:

  * additive: sockets added by the new version (reported with type and
    default value; their behaviour depends on that default);
  * destructive: removed nodes/sockets, type/value changes on sockets
    present in both, node properties, links, interface items or group
    structure changed, groups added/removed;
  * identical: canonical hash equal.

The command is intentionally export-based: GNToolkit does not launch
Blender binaries.  Export the same project with each Blender and compare
the two folders/files here.

Exit codes: 0 no destructive change (additive-only is fine),
1 destructive change (or any change with ``strict``), 2 unreadable input.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy

from .hash_utils import (
    canonical_hash_from_json_data,
    canonicalize_node_tree_data,
)

IGNORED_NODE_KEYS = {"location", "location_absolute", "parent", "width",
                     "height", "select", "hide"}
IGNORED_SOCKET_KEYS = {"name", "identifier", "hide"}
GROUP_KEYS = {"name", "tree_type", "nodes", "links", "interface_items",
              "inputs", "outputs"}


def load_groups(path: str):
    """Return ({group: entry}, [unreadable paths]) from a folder or file."""
    files = []
    if os.path.isdir(path):
        for root, _dirs, names in os.walk(path):
            for name in sorted(names):
                if name.endswith(".json"):
                    files.append(os.path.join(root, name))
    else:
        files = [path]
    groups = {}
    unreadable = []
    for file_path in files:
        try:
            with open(file_path, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            unreadable.append({"path": file_path, "error": str(exc)})
            continue
        if not isinstance(data, dict):
            unreadable.append({"path": file_path, "error": "not a JSON object"})
            continue
        node_groups = data.get("node_groups")
        if isinstance(node_groups, dict):
            groups.update(node_groups)
        elif "nodes" in data:
            groups[data.get("name", os.path.basename(file_path)[:-5])] = data
    return groups, unreadable


def _canon(entry):
    return canonicalize_node_tree_data(deepcopy(entry))


def _node_map(entry):
    return {str(n.get("name", "")): n for n in entry.get("nodes", [])}


def _socket_map(sockets):
    return {str(s.get("name", "")): s for s in sockets or []}


def _socket_changes(old_sockets, new_sockets):
    old, new = _socket_map(old_sockets), _socket_map(new_sockets)
    added = [new[name] for name in sorted(set(new) - set(old))]
    removed = [old[name] for name in sorted(set(old) - set(new))]
    changed = []
    for name in sorted(set(old) & set(new)):
        for field in sorted(set(old[name]) | set(new[name])):
            if field in IGNORED_SOCKET_KEYS:
                continue
            if old[name].get(field) != new[name].get(field):
                changed.append({
                    "socket": name,
                    "field": field,
                    "old": old[name].get(field),
                    "new": new[name].get(field),
                })
    return added, removed, changed


def _diff_value(old, new, path, out, limit=50):
    if len(out) >= limit:
        return
    if type(old) is not type(new):
        out.append({"path": path, "old": old, "new": new})
        return
    if isinstance(old, dict):
        for key in sorted(set(old) | set(new), key=str):
            if key not in old:
                out.append({"path": f"{path}/{key}", "old": "<absent>",
                            "new": new[key]})
            elif key not in new:
                out.append({"path": f"{path}/{key}", "old": old[key],
                            "new": "<absent>"})
            else:
                _diff_value(old[key], new[key], f"{path}/{key}", out, limit)
    elif isinstance(old, list):
        if len(old) != len(new):
            out.append({"path": path + "/len", "old": len(old),
                        "new": len(new)})
        for index in range(min(len(old), len(new))):
            _diff_value(old[index], new[index], f"{path}/{index}", out, limit)
    elif old != new:
        out.append({"path": path, "old": old, "new": new})


def _compare_nodes(old_nodes, new_nodes):
    added = sorted(set(new_nodes) - set(old_nodes))
    removed = sorted(set(old_nodes) - set(new_nodes))
    type_changes = []
    socket_added, socket_removed, socket_changed = [], [], []
    rest_changes = []
    for name in sorted(set(old_nodes) & set(new_nodes)):
        old, new = old_nodes[name], new_nodes[name]
        if old.get("type") != new.get("type"):
            type_changes.append({"node": name, "old": old.get("type"),
                                 "new": new.get("type")})
            continue
        for direction in ("inputs", "outputs"):
            a, r, c = _socket_changes(old.get(direction), new.get(direction))
            for item in a:
                socket_added.append({"node": name, "node_type": old.get("type"),
                                     "direction": direction, "socket": dict(item)})
            for item in r:
                socket_removed.append({"node": name, "node_type": old.get("type"),
                                       "direction": direction,
                                       "socket": dict(item)})
            for item in c:
                socket_changed.append({"node": name,
                                       "node_type": old.get("type"),
                                       "direction": direction, **item})
        rest_old = {k: v for k, v in old.items()
                    if k not in ("name", "type", "inputs", "outputs")
                    and k not in IGNORED_NODE_KEYS}
        rest_new = {k: v for k, v in new.items()
                    if k not in ("name", "type", "inputs", "outputs")
                    and k not in IGNORED_NODE_KEYS}
        _diff_value(rest_old, rest_new, f"node:{name}", rest_changes)
    return {
        "nodes_added": added,
        "nodes_removed": removed,
        "node_type_changes": type_changes,
        "sockets_added": socket_added,
        "sockets_removed": socket_removed,
        "sockets_changed": socket_changed,
        "node_changes": rest_changes,
    }


def _interface_items(entry):
    items = entry.get("interface_items")
    if isinstance(items, list):
        return items
    legacy = []
    for direction in ("inputs", "outputs"):
        for item in entry.get(direction, []) or []:
            copy = dict(item)
            copy["in_out"] = direction
            legacy.append(copy)
    return legacy


def _compare_interface(old_entry, new_entry):
    old_items = {(str(i.get("name", "")), str(i.get("in_out", ""))): i
                 for i in _interface_items(old_entry)}
    new_items = {(str(i.get("name", "")), str(i.get("in_out", ""))): i
                 for i in _interface_items(new_entry)}
    added = [dict(new_items[key]) for key in sorted(set(new_items) - set(old_items))]
    removed = [dict(old_items[key]) for key in sorted(set(old_items) - set(new_items))]
    changed = []
    for key in sorted(set(old_items) & set(new_items)):
        old = {k: v for k, v in old_items[key].items()
               if k not in ("identifier",)}
        new = {k: v for k, v in new_items[key].items()
               if k not in ("identifier",)}
        diffs = []
        _diff_value(old, new, "", diffs, limit=20)
        if diffs:
            changed.append({"item": list(key), "changes": diffs})
    return {"interface_added": added, "interface_removed": removed,
            "interface_changed": changed}


def compare_group(old, new):
    old_hash = canonical_hash_from_json_data(old)
    new_hash = canonical_hash_from_json_data(new)
    if old_hash == new_hash:
        return {"status": "identical", "additive": False, "reasons": []}
    old, new = _canon(old), _canon(new)
    if old.get("tree_type", "GeometryNodeTree") != \
            new.get("tree_type", "GeometryNodeTree"):
        return {"status": "changed", "additive": False,
                "reasons": ["tree type changed"]}
    detail = _compare_nodes(_node_map(old), _node_map(new))
    detail.update(_compare_interface(old, new))
    links_old = old.get("links", [])
    links_new = new.get("links", [])
    if links_old != links_new:
        detail["links"] = [{"path": "links", "old": len(links_old),
                            "new": len(links_new)}]
    else:
        detail["links"] = []
    rest_old = {k: v for k, v in old.items() if k not in GROUP_KEYS}
    rest_new = {k: v for k, v in new.items() if k not in GROUP_KEYS}
    rest = []
    _diff_value(rest_old, rest_new, "group", rest, limit=30)
    detail["group_changes"] = rest

    destructive = any((
        detail["nodes_added"], detail["nodes_removed"],
        detail["node_type_changes"], detail["sockets_removed"],
        detail["sockets_changed"], detail["node_changes"],
        detail["interface_added"], detail["interface_removed"],
        detail["interface_changed"], detail["links"], detail["group_changes"],
    ))
    if not destructive and detail["sockets_added"]:
        status = "changed"
        additive = True
    elif not destructive:
        status = "unexplained"
        additive = False
    else:
        status = "changed"
        additive = False
    return {"status": status, "additive": additive,
            "reasons": [], "detail": detail}


def compare_groups(old_groups: dict, new_groups: dict) -> dict:
    old_names, new_names = set(old_groups), set(new_groups)
    common = sorted(old_names & new_names)
    only_old = sorted(old_names - new_names)
    only_new = sorted(new_names - old_names)
    groups = {}
    identical = changed = additive = 0
    destructive = []
    added_agg = {}
    removed_agg = {}
    shared_agg = {}
    for name in common:
        result = compare_group(old_groups[name], new_groups[name])
        groups[name] = result
        if result["status"] == "identical":
            identical += 1
            continue
        changed += 1
        if result.get("additive"):
            additive += 1
        else:
            destructive.append(name)
        detail = result.get("detail") or {}
        for item in detail.get("sockets_added", []):
            key = (item["node_type"], item["direction"],
                   item["socket"].get("name", ""))
            entry = added_agg.setdefault(key, {
                "node_type": item["node_type"],
                "direction": item["direction"],
                "socket": item["socket"].get("name", ""),
                "type": item["socket"].get("type"),
                "bl_idname": item["socket"].get("bl_idname"),
                "default_value": item["socket"].get("default_value"),
                "count": 0, "groups": [],
            })
            entry["count"] += 1
            entry["groups"].append(name)
        for item in detail.get("sockets_removed", []):
            key = (item["node_type"], item["direction"],
                   item["socket"].get("name", ""))
            entry = removed_agg.setdefault(key, {
                "node_type": item["node_type"],
                "direction": item["direction"],
                "socket": item["socket"].get("name", ""),
                "type": item["socket"].get("type"),
                "count": 0, "groups": [],
            })
            entry["count"] += 1
            entry["groups"].append(name)
        for item in detail.get("sockets_changed", []):
            key = (item["node_type"], item["direction"], item["socket"],
                   item["field"])
            entry = shared_agg.setdefault(key, {
                "node_type": item["node_type"],
                "direction": item["direction"],
                "socket": item["socket"],
                "field": item["field"],
                "old": item.get("old"),
                "new": item.get("new"),
                "count": 0, "groups": [],
            })
            entry["count"] += 1
            entry["groups"].append(name)
    return {
        "counts": {
            "old": len(old_groups), "new": len(new_groups),
            "identical": identical, "changed": changed,
            "additive": additive, "destructive": len(destructive),
            "only_old": len(only_old), "only_new": len(only_new),
        },
        "only_old": only_old,
        "only_new": only_new,
        "destructive": destructive,
        "added_sockets": sorted(added_agg.values(),
                                key=lambda x: (-x["count"],
                                               x["node_type"],
                                               x["socket"])),
        "removed_sockets": sorted(removed_agg.values(),
                                  key=lambda x: (-x["count"],
                                                 x["node_type"],
                                                 x["socket"])),
        "shared_changes": sorted(shared_agg.values(),
                                 key=lambda x: (-x["count"],
                                                x["node_type"],
                                                x["socket"])),
        "groups": {name: result for name, result in groups.items()
                   if result["status"] != "identical"},
    }


def exit_code(report: dict, strict: bool = False) -> int:
    if report.get("unreadable_old") or report.get("unreadable_new"):
        return 2
    counts = report["counts"]
    if counts["destructive"] or counts["only_old"] or counts["only_new"]:
        return 1
    if strict and counts["changed"]:
        return 1
    return 0


def format_report(report: dict, as_json: bool = False) -> str:
    if as_json:
        return json.dumps(report)
    counts = report["counts"]
    lines = []
    lines.append(
        "cross-version: %d old, %d new | %d identical, %d changed "
        "(%d additive, %d destructive), %d only-old, %d only-new"
        % (counts["old"], counts["new"], counts["identical"],
           counts["changed"], counts["additive"], counts["destructive"],
           counts["only_old"], counts["only_new"]))
    if report.get("unreadable_old") or report.get("unreadable_new"):
        for side in ("old", "new"):
            for item in report.get(f"unreadable_{side}", []):
                lines.append("[UNREADABLE] %s (%s)" % (item["path"],
                                                       item["error"]))
    if report.get("added_sockets"):
        lines.append("added sockets (new controls; check their defaults):")
        for item in report["added_sockets"]:
            lines.append(
                "  %3dx %s %s '%s' (%s, default %r)"
                % (item["count"], item["node_type"], item["direction"],
                   item["socket"], item.get("type"), item.get("default_value")))
    if report.get("removed_sockets"):
        lines.append("removed sockets:")
        for item in report["removed_sockets"]:
            lines.append("  %3dx %s %s '%s' (%s)"
                         % (item["count"], item["node_type"],
                            item["direction"], item["socket"],
                            item.get("type")))
    if report.get("shared_changes"):
        lines.append("changes on sockets present in both:")
        for item in report["shared_changes"]:
            lines.append("  %3dx %s %s '%s' %s: %r -> %r"
                         % (item["count"], item["node_type"],
                            item["direction"], item["socket"],
                            item["field"], item.get("old"),
                            item.get("new")))
    if report["destructive"]:
        lines.append("destructive groups:")
        for name in report["destructive"]:
            result = report["groups"].get(name, {})
            detail = result.get("detail") or {}
            reasons = []
            for key in ("nodes_added", "nodes_removed",
                        "node_type_changes", "sockets_removed",
                        "sockets_changed", "node_changes",
                        "interface_added", "interface_removed",
                        "interface_changed", "links", "group_changes"):
                if detail.get(key):
                    reasons.append(key)
            if result.get("status") == "unexplained":
                reasons.append("unexplained")
            lines.append("  %s (%s)" % (name, ", ".join(reasons) or "changed"))
    else:
        lines.append("destructive changes: none")
    if report.get("only_old"):
        lines.append("only in old: %s" % ", ".join(report["only_old"][:20]))
    if report.get("only_new"):
        lines.append("only in new: %s" % ", ".join(report["only_new"][:20]))
    return "\n".join(lines)
