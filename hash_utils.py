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

from .constants import (
    HASH_EXCLUDE_NODE_PROPS,
    HASH_EXCLUDE_TREE_PROPS,
    HASH_EXCLUDE_SOCKET_PROPS,
    HASH_EXCLUDE_INTERFACE_PROPS,
)


# Datablock reference values ({type, name}) cannot round-trip: the .blend
# may not contain the referenced datablock, and the import may resolve or
# re-create it under a different name (fonts, objects).  Hashing them
# produces permanent noise, so they are dropped from the canonical form —
# together with null defaults (unset datablock defaults serialize as null
# on one side and a reference dict on the other).
def _is_datablock_ref(value) -> bool:
    return value is None or (isinstance(value, dict) and "name" in value)


# data_type property -> socket type of the ACTIVE input for nodes whose
# socket layout is data-type-driven (Compare, Random Value).
_SOCKET_ACTIVE_TYPES = {
    "BOOLEAN": "BOOLEAN",
    "INT": "INT",
    "FLOAT": "VALUE",
    "VECTOR": "VECTOR",
    "COLOR": "RGBA",
    "STRING": "STRING",
}


def node_socket_is_active(bl_idname: str, props: dict, sname: str, stype: str, sid: str = "") -> bool:
    """True when a socket record belongs to the node's ACTIVE configuration.

    Blender 5.2 only exposes the sockets of the active data type (plus
    mode/operation-gated sockets) on several nodes, while 5.1 exports
    contain every variant (A_INT/A_VEC3/..., Min/Min_001/...).  The
    canonical hash and the importer apply this same rule on BOTH engines
    so fingerprints stay version-independent.
    """
    if bl_idname == "FunctionNodeCompare":
        want = _SOCKET_ACTIVE_TYPES.get(props.get("data_type"))
        if sname in ("A", "B"):
            return stype == want
        if sname == "C":
            return props.get("mode") == "DOT_PRODUCT"
        if sname == "Angle":
            return props.get("mode") == "DIRECTION"
        if sname == "Epsilon":
            return (props.get("data_type") in ("FLOAT", "VECTOR")
                    and props.get("operation") in ("EQUAL", "NOT_EQUAL"))
        return True
    if bl_idname == "FunctionNodeRandomValue":
        want = _SOCKET_ACTIVE_TYPES.get(props.get("data_type"))
        if sname in ("Min", "Max"):
            return stype == want
        if sname in ("ID", "Seed"):
            return True
        # Probability and the type variants are 5.1-only
        return False
    if bl_idname == "FunctionNodeBooleanMath":
        # The NOT operation has a single input; 5.1 exports two records.
        if props.get("operation") == "NOT":
            return sid != "Boolean_001"
        return True
    if bl_idname == "FunctionNodeValueToString":
        # Base and Padding inputs were added in 5.2.
        return sname not in ("Base", "Padding")
    if bl_idname == "GeometryNodeCaptureAttribute":
        # The Selection input was added in 5.2.
        return sname != "Selection"
    if bl_idname == "GeometryNodeSubdivisionSurface":
        # The Quality input was added in 5.2.
        return sname != "Quality"
    return True


# The importer creates 2D vector interface sockets as NodeSocketVector2D
# (NodeSocketVectorTranslation2D is not accepted by interface.new_socket);
# the two types are functionally identical and must hash the same.
_2D_VECTOR_SOCKETS = frozenset({
    "NodeSocketVector2D",
    "NodeSocketVectorTranslation2D",
})


def _normalize_interface_socket_type(bl_socket_idname: str) -> str:
    if bl_socket_idname in _2D_VECTOR_SOCKETS:
        return "NodeSocketVector2D"
    return bl_socket_idname


def canonicalize_node_tree_data(data: dict) -> dict:
    """Return a deep copy of *data* with all lists and dicts sorted
    deterministically so that two functionally identical node trees
    produce the same hash regardless of creation order.

    Does NOT modify the original *data*.
    """
    out = {}

    # Tree-level fields --------------------------------------------------
    out["name"] = data.get("name", "")

    # Connected sockets: the importer skips restoring default_value on
    # sockets that are linked (the link overrides them at runtime), so
    # those defaults are not roundtrip-stable and must not be hashed.
    # Keyed by socket IDENTIFIER (unique per node) — socket names are
    # frequently shared (e.g. the three "Value" inputs of a Math node).
    connected_inputs = set()
    connected_outputs = set()
    for lk in data.get("links", []):
        connected_outputs.add((lk.get("from_node", ""),
                               lk.get("from_socket_id", lk.get("from_socket_name", ""))))
        connected_inputs.add((lk.get("to_node", ""),
                              lk.get("to_socket_id", lk.get("to_socket_name", ""))))

    # Interface items: sort by NAME. Interface socket identifiers are
    # reordered by the roundtrip (cosmetic), while names are stable —
    # hashing by identifier made every reimported group look changed.
    if "interface_items" in data:
        items = []
        for item in data["interface_items"]:
            it = dict(item)
            it.pop("identifier", None)
            it.pop("parent", None)
            if "bl_socket_idname" in it:
                it["bl_socket_idname"] = _normalize_interface_socket_type(it["bl_socket_idname"])
            if "properties" in it and isinstance(it["properties"], dict):
                it["properties"] = {
                    k: v for k, v in it["properties"].items()
                    if k not in HASH_EXCLUDE_INTERFACE_PROPS
                    and not (k == "default_value" and _is_datablock_ref(v))
                }
                it["properties"] = dict(sorted(it["properties"].items()))
            if "enum_items" in it and isinstance(it["enum_items"], list):
                cleaned = []
                for e in it["enum_items"]:
                    ed = dict(e)
                    ed.pop("description", None)
                    cleaned.append(ed)
                it["enum_items"] = sorted(cleaned, key=lambda x: x.get("name", ""))
            items.append(it)
        out["interface_items"] = sorted(items, key=lambda x: x.get("name", ""))
    elif "inputs" in data or "outputs" in data:
        # Legacy path
        def _clean_socket_list(lst):
            cleaned = []
            for item in lst:
                it = dict(item)
                it.pop("identifier", None)
                if "bl_socket_idname" in it:
                    it["bl_socket_idname"] = _normalize_interface_socket_type(it["bl_socket_idname"])
                if "properties" in it and isinstance(it["properties"], dict):
                    it["properties"] = {
                        k: v for k, v in it["properties"].items()
                        if k not in HASH_EXCLUDE_INTERFACE_PROPS
                        and not (k == "default_value" and _is_datablock_ref(v))
                    }
                    it["properties"] = dict(sorted(it["properties"].items()))
                cleaned.append(it)
            return sorted(cleaned, key=lambda x: x.get("name", ""))
        out["inputs"] = _clean_socket_list(data.get("inputs", []))
        out["outputs"] = _clean_socket_list(data.get("outputs", []))

    # Node list: sort by name
    nodes = []
    for node_data in data.get("nodes", []):
        nd = dict(node_data)
        node_name = nd.get("name", "")
        # Remove volatile properties from hash computation.  The bl_*
        # UI-template limits (bl_width_max, bl_height_max, ...) differ
        # between Blender versions (e.g. 30.0 vs FLT_MAX) and are not
        # content, so they are excluded wholesale.  vector_dimensions
        # (Vector input node) was added in 5.2 and cannot exist in 5.1
        # exports.
        if "properties" in nd and isinstance(nd["properties"], dict):
            nd["properties"] = {k: v for k, v in nd["properties"].items()
                                if k not in HASH_EXCLUDE_NODE_PROPS
                                and not k.startswith("bl_")
                                and k != "vector_dimensions"}
            nd["properties"] = dict(sorted(nd["properties"].items()))
        # Node sockets: canonical form is by NAME. Socket identifiers are
        # reordered by the roundtrip (like interface identifiers), so
        # sorting by identifier yields different orders per side.
        node_bl_idname = nd.get("type", "")
        node_props = nd.get("properties", {}) if isinstance(nd.get("properties"), dict) else {}

        def _clean_node_sockets(lst, connected_set):
            cleaned = []
            for s in lst:
                sd = dict(s)
                sock_id = sd.get("identifier", sd.get("name", ""))
                if (node_name, sock_id) in connected_set:
                    sd.pop("default_value", None)
                if "default_value" in sd and _is_datablock_ref(sd.get("default_value")):
                    sd.pop("default_value", None)
                # Compare / Random Value: only the active-type sockets
                # (plus mode/operation-gated ones) are real; the 5.1
                # exports carry every type variant, the 5.2 nodes only the
                # active ones — drop the rest so both sides hash equal.
                if not node_socket_is_active(node_bl_idname, node_props,
                                             sd.get("name", ""), sd.get("type", ""),
                                             sd.get("identifier", "")):
                    continue
                for k in HASH_EXCLUDE_SOCKET_PROPS:
                    sd.pop(k, None)
                cleaned.append(sd)
            return sorted(cleaned, key=lambda x: x.get("name", ""))
        if "inputs" in nd:
            nd["inputs"] = _clean_node_sockets(nd["inputs"], connected_inputs)
        if "outputs" in nd:
            nd["outputs"] = _clean_node_sockets(nd["outputs"], connected_outputs)
        # Remove location (visual position only)
        nd.pop("location", None)
        # Item collections: same name-based normalization
        def _clean_items(lst):
            cleaned = []
            for item in lst:
                it = dict(item)
                it.pop("identifier", None)
                it.pop("description", None)
                cleaned.append(it)
            return sorted(cleaned, key=lambda x: x.get("name", ""))
        for coll in ("zone_items", "menu_items_data", "capture_items_data"):
            if coll in nd and isinstance(nd[coll], list):
                nd[coll] = _clean_items(nd[coll])
        nodes.append(nd)
    out["nodes"] = sorted(nodes, key=lambda x: x.get("name", ""))

    # Links: canonical socket references are the NAMES (identifiers are
    # reordered by the roundtrip); fall back to the id when a name is
    # missing (defensive, old data).  Identical canonical tuples are the
    # same link (a socket accepts one link) — 5.1 exports can carry the
    # same link twice under the type-variant sockets (e.g. Compare A and
    # A_INT both named "A"), so duplicates are collapsed.
    links = []
    seen = set()
    for lk in data.get("links", []):
        ld = dict(lk)
        ld["from_socket_name"] = ld.get("from_socket_name", ld.get("from_socket_id", ""))
        ld["to_socket_name"] = ld.get("to_socket_name", ld.get("to_socket_id", ""))
        ld.pop("from_socket_id", None)
        ld.pop("to_socket_id", None)
        key = (ld.get("from_node", ""), ld["from_socket_name"],
               ld.get("to_node", ""), ld["to_socket_name"])
        if key in seen:
            continue
        seen.add(key)
        links.append(ld)
    out["links"] = sorted(
        links,
        key=lambda x: (
            x.get("from_node", ""),
            x.get("from_socket_name", ""),
            x.get("to_node", ""),
            x.get("to_socket_name", ""),
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