# -*- coding: utf-8 -*-
"""
gn_toolkit.socket_utils — Socket lookup, normalisation, and creation helpers.

Functions that were previously scattered or nested inside
``import_node_tree_recursive`` are now top-level and independently testable.
"""

from __future__ import annotations

import bpy

from .constants import _VOLATILE_TYPES, ZONE_INPUTS, SOCKET_TYPE_MAP
from .error_tracker import ImportErrorTracker


# ---------------------------------------------------------------------------
# Socket type normalisation
# ---------------------------------------------------------------------------

def normalize_socket_type(raw_type: str) -> str:
    """Normalise a raw socket type string to a short canonical form.

    Examples::

        >>> normalize_socket_type("NodeSocketFloat")
        'FLOAT'
        >>> normalize_socket_type("RGBA")
        'RGBA'
    """
    if not raw_type:
        return "FLOAT"
    r = str(raw_type).upper().replace("NODESOCKET", "")
    if "VECTOR" in r:
        return "VECTOR"
    if "COLOR" in r or "RGBA" in r:
        return "RGBA"
    if "FLOAT" in r or "VALUE" in r:
        return "FLOAT"
    if "GEOMETRY" in r:
        return "GEOMETRY"
    if "BOOL" in r:
        return "BOOLEAN"
    if "INT" in r:
        return "INT"
    if "STRING" in r:
        return "STRING"
    if "COLLECTION" in r:
        return "COLLECTION"
    if "OBJECT" in r:
        return "OBJECT"
    if "MATERIAL" in r:
        return "MATERIAL"
    if "TEXTURE" in r:
        return "TEXTURE"
    if "IMAGE" in r:
        return "IMAGE"
    if "CLOSURE" in r:
        return "CLOSURE"
    return r


# ---------------------------------------------------------------------------
# Item creation for dynamic socket collections
# ---------------------------------------------------------------------------

def attempt_create_item(coll, raw_type: str, name: str) -> bool:
    """Try multiple API signatures to create an item in *coll*.

    Blender's collection ``new()`` method has changed across versions.
    This helper tries the known calling conventions so that the import
    works across Blender 4.x releases.
    """
    if not hasattr(coll, 'new'):
        return False
    s_type = normalize_socket_type(raw_type)
    attempts = [(name, s_type), (s_type, name)]

    for n, t in attempts:
        try:
            coll.new(n, t)
            return True
        except (TypeError, AttributeError, ValueError, RuntimeError):
            pass

    try:
        coll.new(name=n, socket_type=s_type)
        return True
    except (TypeError, AttributeError, ValueError, RuntimeError):
        pass

    full_type = raw_type
    if not full_type.startswith("NodeSocket"):
        full_type = SOCKET_TYPE_MAP.get(s_type, f"NodeSocket{s_type.capitalize()}")

    for n, t in attempts:
        try:
            coll.new(n, full_type)
            return True
        except (TypeError, AttributeError, ValueError, RuntimeError):
            pass

    print(f"[ERROR] Failed to inject item '{name}' of type '{raw_type}'")
    return False


# ---------------------------------------------------------------------------
# Robust socket search
# ---------------------------------------------------------------------------

def find_robust_socket(node, sockets, sid, sname, expected_type=None, dynamic_hint=None):
    """Search for a socket safely, evading identifier collisions.

    For volatile nodes (``dynamic_hint is True``), Blender recycles
    identifiers; this function trusts name + type over the identifier.
    """
    # 1. Exact match: identifier + name
    s = next((x for x in sockets if x.identifier == sid and x.name == sname), None)
    if s:
        return s

    # 1.5 Active protection for volatile nodes — ID may be recycled
    if dynamic_hint is True:
        if expected_type:
            s = next((x for x in sockets if x.name == sname and x.type == expected_type), None)
            if s:
                return s
        s = next((x for x in sockets if x.name == sname), None)
        if s:
            return s

    # 2. Exact match: identifier only
    s = next((x for x in sockets if x.identifier == sid), None)
    if s:
        if dynamic_hint is True:
            # Only accept the recycled ID if type matches
            if expected_type and s.type == expected_type:
                return s
        else:
            return s

    # 3. Fallback: name + type
    if expected_type:
        s = next((x for x in sockets if x.name == sname and x.type == expected_type), None)
        if s:
            return s

    # 4. Fallback: name only
    s = next((x for x in sockets if x.name == sname), None)
    if s:
        return s

    return None


# ---------------------------------------------------------------------------
# Dependency scanning
# ---------------------------------------------------------------------------

def get_tree_dependencies(tree, deps=None):
    """Recursively collect all node-trees referenced by *tree*."""
    if deps is None:
        deps = {}
    if tree.name in deps:
        return deps
    deps[tree.name] = tree
    for node in tree.nodes:
        if node.bl_idname == "GeometryNodeGroup" and getattr(node, "node_tree", None):
            get_tree_dependencies(node.node_tree, deps)
    return deps
