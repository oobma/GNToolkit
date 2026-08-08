# -*- coding: utf-8 -*-
"""
gn_toolkit.hash_utils — Canonical hashing for DNA/RNA sync.

Produces deterministic SHA-256 hashes from serialized node tree data,
enabling change detection without false positives from volatile properties
or ordering differences.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from .constants import HASH_EXCLUDE_NODE_PROPS, HASH_EXCLUDE_TREE_PROPS


def canonicalize_node_tree_data(data: dict) -> dict:
    """Return a deep copy of *data* with all lists and dicts sorted
    deterministically so that two functionally identical node trees
    produce the same hash regardless of creation order.

    Does NOT modify the original *data*.
    """
    out = {}

    # Tree-level fields --------------------------------------------------
    out["name"] = data.get("name", "")

    # Interface items: sort by identifier
    if "interface_items" in data:
        items = [dict(item) for item in data["interface_items"]]
        for item in items:
            if "properties" in item and isinstance(item["properties"], dict):
                item["properties"] = dict(sorted(item["properties"].items()))
        out["interface_items"] = sorted(items, key=lambda x: x.get("identifier", x.get("name", "")))
    elif "inputs" in data or "outputs" in data:
        # Legacy path
        inp = [dict(i) for i in data.get("inputs", [])]
        otp = [dict(o) for o in data.get("outputs", [])]
        for lst in (inp, otp):
            for item in lst:
                if "properties" in item and isinstance(item["properties"], dict):
                    item["properties"] = dict(sorted(item["properties"].items()))
        out["inputs"] = sorted(inp, key=lambda x: x.get("identifier", x.get("name", "")))
        out["outputs"] = sorted(otp, key=lambda x: x.get("identifier", x.get("name", "")))

    # Node list: sort by name
    nodes = []
    for node_data in data.get("nodes", []):
        nd = dict(node_data)
        # Remove volatile properties from hash computation
        if "properties" in nd and isinstance(nd["properties"], dict):
            nd["properties"] = {k: v for k, v in nd["properties"].items()
                                if k not in HASH_EXCLUDE_NODE_PROPS}
            nd["properties"] = dict(sorted(nd["properties"].items()))
        # Sort inputs/outputs by identifier
        if "inputs" in nd:
            nd["inputs"] = sorted(
                [dict(s) for s in nd["inputs"]],
                key=lambda x: x.get("identifier", x.get("name", "")),
            )
        if "outputs" in nd:
            nd["outputs"] = sorted(
                [dict(s) for s in nd["outputs"]],
                key=lambda x: x.get("identifier", x.get("name", "")),
            )
        # Remove location (visual position only)
        nd.pop("location", None)
        # Sort zone_items if present
        if "zone_items" in nd and isinstance(nd["zone_items"], list):
            nd["zone_items"] = sorted(
                [dict(z) for z in nd["zone_items"]],
                key=lambda x: x.get("identifier", x.get("name", "")),
            )
        # Sort menu_items_data if present
        if "menu_items_data" in nd and isinstance(nd["menu_items_data"], list):
            nd["menu_items_data"] = sorted(
                [dict(m) for m in nd["menu_items_data"]],
                key=lambda x: x.get("identifier", x.get("name", "")),
            )
        # Sort capture_items_data if present
        if "capture_items_data" in nd and isinstance(nd["capture_items_data"], list):
            nd["capture_items_data"] = sorted(
                [dict(c) for c in nd["capture_items_data"]],
                key=lambda x: x.get("identifier", x.get("name", "")),
            )
        nodes.append(nd)
    out["nodes"] = sorted(nodes, key=lambda x: x.get("name", ""))

    # Links: sort by (from_node, from_socket_id, to_node, to_socket_id)
    links = [dict(lk) for lk in data.get("links", [])]
    out["links"] = sorted(
        links,
        key=lambda x: (
            x.get("from_node", ""),
            x.get("from_socket_id", ""),
            x.get("to_node", ""),
            x.get("to_socket_id", ""),
        ),
    )

    # Tree properties: exclude volatile ones
    tree_props = {}
    for k, v in data.get("tree_properties", {}).items():
        if k not in HASH_EXCLUDE_TREE_PROPS:
            tree_props[k] = v
    out["tree_properties"] = dict(sorted(tree_props.items()))

    return out


def canonical_hash_from_tree(tree) -> str:
    """Compute a deterministic SHA-256 hash for a Blender node tree.

    Uses the existing serializer to produce the data dict, then
    canonicalizes it before hashing.
    """
    from .serializer import serialize_node_tree
    raw_data = serialize_node_tree(tree)
    canonical = canonicalize_node_tree_data(raw_data)
    canonical_json = json.dumps(canonical, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()


def canonical_hash_from_json_data(data_dict: dict) -> str:
    """Compute a deterministic SHA-256 hash from a JSON-compatible dict.

    The dict is canonicalized (sorted lists) before hashing so that
    two JSON files with the same content but different key ordering
    produce the same hash.
    """
    canonical = canonicalize_node_tree_data(data_dict)
    canonical_json = json.dumps(canonical, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()


def _load_json_file(filepath: str) -> dict | None:
    """Load a JSON file and return the parsed data, or None on failure."""
    import os
    if not os.path.isfile(filepath):
        return None
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def canonical_hash_from_json_path(filepath: str) -> str | None:
    """Compute SHA-256 from a JSON file on disk.

    For unified packages (GN_UNIFIED_PACKAGE), hashes all node groups
    together.  For per-group hashing, use canonical_hash_from_json_group()
    instead.

    Returns None if the file does not exist or cannot be parsed.
    """
    data = _load_json_file(filepath)
    if data is None:
        return None

    if isinstance(data, dict) and data.get("type") == "GN_UNIFIED_PACKAGE":
        groups = data.get("node_groups", {})
        canonical_json = json.dumps(groups, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()

    return canonical_hash_from_json_data(data)


def canonical_hash_from_json_group(filepath: str, group_name: str) -> str | None:
    """Compute a deterministic SHA-256 hash for a single node group
    within a JSON file.

    Supports both unified packages (GN_UNIFIED_PACKAGE) and standalone
    group files.  For unified packages, extracts only the named group
    and hashes it individually.  This enables per-group change detection
    when multiple groups share a single JSON file.

    Returns None if the file or group is not found.
    """
    data = _load_json_file(filepath)
    if data is None:
        return None

    if isinstance(data, dict) and data.get("type") == "GN_UNIFIED_PACKAGE":
        groups = data.get("node_groups", {})
        group_data = groups.get(group_name)
        if group_data is None:
            return None
        return canonical_hash_from_json_data(group_data)

    if isinstance(data, dict) and "nodes" in data:
        # Standalone group file — hash the whole thing
        # (group_name is ignored; the file IS the group)
        return canonical_hash_from_json_data(data)

    return None


def list_groups_in_json(filepath: str) -> list[str]:
    """Return a list of node group names found in a JSON file.

    For unified packages, returns all group names in the
    ``node_groups`` dictionary.  For standalone group files, returns
    a single-element list with the group's ``name`` field.

    Returns an empty list if the file cannot be read or has no groups.
    """
    data = _load_json_file(filepath)
    if data is None:
        return []

    if isinstance(data, dict) and data.get("type") == "GN_UNIFIED_PACKAGE":
        return list(data.get("node_groups", {}).keys())

    if isinstance(data, dict) and "name" in data:
        return [data["name"]]

    return []


def get_json_mtime(filepath: str) -> float | None:
    """Return the modification time of a file, or None if not found."""
    import os
    if not os.path.isfile(filepath):
        return None
    return os.path.getmtime(filepath)