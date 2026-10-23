# -*- coding: utf-8 -*-
"""
gn_toolkit.serializer — Node-tree and node serialization (export).

Serializes Blender node trees into JSON-safe dictionaries that can be
re-imported by ``gn_toolkit.importer``.
"""

from __future__ import annotations

import json
import uuid

import bpy

from .codec import clean_value
from .constants import (
    BUILTIN_GROUP_BL_IDNAMES,
    MATERIAL_SETTINGS_PROPS,
    NODE_PROPS_TO_SKIP,
    NODE_UID_PROP,
    TREE_PROPS_TO_SKIP,
    INTERFACE_SKIP_PROPS,
    OPTIONAL_SOCKET_PROPS,
    STRUCT_DUMP_TYPES as _STRUCT_DUMP_TYPES,
    WORLD_SETTINGS_PROPS,
    is_group_node_bl_idname,
    parse_interface_socket_variant,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def tree_has_animation(tree) -> bool:
    """True when the tree carries animation content (action/drivers/NLA).

    Animation is a declared boundary of the add-on (values animated by
    keyframes or drivers are NOT versioned); this detection only raises
    the informational flag and the UI/checker warning.
    """
    ad = getattr(tree, "animation_data", None)
    if ad is None:
        return False
    if ad.action is not None:
        return True
    if len(ad.drivers) > 0:
        return True
    return any(bool(tr.strips) for tr in ad.nla_tracks)


def tree_compositor_scenes(tree) -> list[str]:
    """Names of the scenes that use *tree* as their compositor (5.0+).

    Informational owner reference (like ``has_animation``): it travels in
    the JSON so a fresh import can assign the scene, but the canonical
    hash ignores it.  Blender 4.2 has no ``Scene.compositing_node_group``
    and returns no owners.
    """
    if getattr(tree, "bl_idname", "") != "CompositorNodeTree":
        return []
    scenes = []
    for scene in bpy.data.scenes:
        try:
            if getattr(scene, "compositing_node_group", None) == tree:
                scenes.append(scene.name)
        except Exception:
            continue
    return sorted(scenes)


def _serialize_enum_items(collection) -> list[dict]:
    """Serialize a Blender enum_items collection into a list of dicts.

    Each dict contains ``identifier``, ``name`` and ``description``.
    Used both for node-level enum_items (MenuSwitch) and for
    interface-level enum_items (NodeSocketMenu).
    """
    result = []
    for ei in collection:
        ei_id = getattr(ei, 'identifier', '')
        if not ei_id:
            ei_id = getattr(ei, 'name', '')
        result.append({
            "identifier": ei_id,
            "name": getattr(ei, 'name', ''),
            "description": getattr(ei, 'description', ''),
        })
    return result


def _serialize_zone_item(item, type_attr: str = "socket_type", type_default: str = "GEOMETRY") -> dict:
    """Serialize a single zone/loop item into a dict."""
    return {
        "name": item.name,
        "identifier": getattr(item, 'identifier', item.name),
        type_attr: getattr(item, type_attr, type_default),
    }


# ---------------------------------------------------------------------------
# Node-type dispatchers for serialize_node()
# ---------------------------------------------------------------------------

def _handle_menu_switch(node, data: dict) -> None:
    if hasattr(node, 'enum_items'):
        data["menu_items_data"] = _serialize_enum_items(node.enum_items)


def _handle_simulation_output(node, data: dict) -> None:
    sim_items = getattr(node, 'state_items', getattr(node, 'simulation_items', []))
    for item in sim_items:
        data["zone_items"].append(_serialize_zone_item(item))


def _handle_repeat_output(node, data: dict) -> None:
    if hasattr(node, 'repeat_items'):
        for item in node.repeat_items:
            data["zone_items"].append(_serialize_zone_item(item))


def _handle_foreach_output(node, data: dict) -> None:
    data["zone_items"] = {"main_items": [], "generation_items": [], "input_items": []}
    for prop_name in ["main_items", "generation_items", "input_items"]:
        if hasattr(node, prop_name):
            for item in getattr(node, prop_name):
                data["zone_items"][prop_name].append(_serialize_zone_item(item))


def _handle_closure_output(node, data: dict) -> None:
    data["zone_items"] = {"input_items": [], "output_items": []}
    if hasattr(node, 'input_items'):
        for item in node.input_items:
            data["zone_items"]["input_items"].append(_serialize_zone_item(item))
    if hasattr(node, 'output_items'):
        for item in node.output_items:
            data["zone_items"]["output_items"].append(_serialize_zone_item(item))


def _handle_capture_attribute(node, data: dict) -> None:
    data["capture_items_data"] = [
        {
            "name": item.name,
            "identifier": getattr(item, 'identifier', item.name),
            "data_type": getattr(item, "data_type", "FLOAT"),
        }
        for item in node.capture_items
    ]


def _handle_bundle_node(node, data: dict) -> None:
    for prop_name in ['bundle_items', 'items']:
        if hasattr(node, prop_name):
            collection = getattr(node, prop_name)
            if callable(collection):
                continue
            data["bundle_items_data"] = [
                {
                    "name": getattr(item, 'name', ''),
                    "identifier": getattr(item, 'identifier', item.name),
                    "socket_type": getattr(item, 'socket_type', getattr(item, 'data_type', 'FLOAT')),
                }
                for item in collection
            ]
            break


def _handle_list_items(node, data: dict) -> None:
    """Serialize the list_items collection of the 5.2 list nodes
    (Field to List, Closure to List). The output socket of these nodes is
    named after the active list item, so the item set must round-trip."""
    if hasattr(node, 'list_items'):
        data["list_items_data"] = [
            {
                "name": getattr(item, 'name', 'Value'),
                "socket_type": getattr(item, 'socket_type', 'FLOAT'),
            }
            for item in node.list_items
        ]


def _handle_bake(node, data: dict) -> None:
    """Bake node item list (Blender 5.x): names, sockets, attribute flags.

    The item color is read-only (derived from the socket type) and is not
    serialized; active_index/show_options are UI-only.  The list keeps its
    creation order (semantic, like sockets).
    """
    items = getattr(node, "bake_items", None)
    if items is None:
        return
    data["bake_items_data"] = [
        {
            "name": getattr(item, "name", "Value"),
            "socket_type": getattr(item, "socket_type", "FLOAT"),
            "is_attribute": bool(getattr(item, "is_attribute", False)),
            "attribute_domain": getattr(item, "attribute_domain", "POINT"),
        }
        for item in items
    ]


# Dispatch table: bl_idname → handler(node, data)
_NODE_SERIALIZERS: dict[str, callable] = {
    "GeometryNodeMenuSwitch": _handle_menu_switch,
    "GeometryNodeBake": _handle_bake,
    "GeometryNodeSimulationOutput": _handle_simulation_output,
    "GeometryNodeRepeatOutput": _handle_repeat_output,
    "GeometryNodeForeachGeometryElementOutput": _handle_foreach_output,
    "GeometryNodeCaptureAttribute": _handle_capture_attribute,
    "GeometryNodeCombineBundle": _handle_bundle_node,
    "GeometryNodeSeparateBundle": _handle_bundle_node,
    "NodeCombineBundle": _handle_bundle_node,
    "NodeSeparateBundle": _handle_bundle_node,
    "GeometryNodeFieldToList": _handle_list_items,
    "GeometryNodeClosureToList": _handle_list_items,
}

# Closure types share the same handler
_NODE_SERIALIZERS["NodeClosureOutput"] = _handle_closure_output
_NODE_SERIALIZERS["NodeEvaluateClosure"] = _handle_closure_output


# ---------------------------------------------------------------------------
# Main serialization functions
# ---------------------------------------------------------------------------

# Socket types whose default_value cannot be meaningfully serialized/restored.
# Attempting to assign a scalar to these sockets will always fail on import.
# MENU is NOT listed: menu sockets store a string enum identifier that the
# importer restores (after enum_items exist), and per-node overrides on Group
# nodes are lost if omitted.
_NON_SCALAR_SOCKET_TYPES = frozenset({
    'GEOMETRY', 'OBJECT', 'COLLECTION', 'MATERIAL', 'TEXTURE', 'IMAGE',
    'MATRIX', 'CLOSURE',
})

# Struct properties skipped in the embedded-struct dump (see
# serialize_struct_value): RNA plumbing, UI-only selection, and
# session-derived state (ImageUser.frame_current is the image system's
# playback cursor, recomputed at evaluation time; rationale in
# constants.STRUCT_DUMP_TYPES['ImageUser']).
_STRUCT_SKIP_PROPS = frozenset({"rna_type", "select", "frame_current"})

# Struct dump allowlist: constants.STRUCT_DUMP_TYPES (declarative table with
# rationale, M1), imported above as _STRUCT_DUMP_TYPES.  It keeps the dump
# away from object-like structs: `paired_output` on zone nodes points to a
# Node, and dumping it would serialize a whole nested node.


def serialize_struct_value(val, depth: int = 0):
    """Dump an embedded RNA struct (ColorRamp, CurveMapping, ...) to JSON.

    Read-only pointer properties (the ColorRamp of a Color Ramp node, the
    CurveMapping of the Float/Vector Curve nodes) cannot be replaced, but
    their CONTENT is real semantics.  Without this dump they were skipped
    silently: editing the ramp did not move the canonical hash (a false
    "Synced") and the importer rebuilt the node with its default content.

    Returns None for anything that is not a nested struct (data-block IDs,
    scalars) or whose top-level type is not in ``_STRUCT_DUMP_TYPES``.
    Collections and nested structs are walked recursively; the result is
    plain, deterministic JSON (same RNA order, cleaned floats) and is
    restored by ``importer._restore_struct_prop``.
    """
    if depth > 4 or val is None:
        return None
    if not isinstance(val, bpy.types.bpy_struct):
        return None
    if isinstance(val, bpy.types.ID):
        return None
    struct_name = val.bl_rna.identifier
    if depth == 0 and struct_name not in _STRUCT_DUMP_TYPES:
        return None
    out = {"__struct__": struct_name}
    for prop in val.bl_rna.properties:
        if prop.identifier in _STRUCT_SKIP_PROPS:
            continue
        try:
            sub_val = getattr(val, prop.identifier)
        except (TypeError, AttributeError, ValueError, RuntimeError):
            continue
        if prop.type == 'COLLECTION':
            items = []
            for item in sub_val:
                sub = serialize_struct_value(item, depth + 1)
                if sub is not None:
                    items.append(sub)
            out[prop.identifier] = items
        elif prop.type == 'POINTER':
            sub = serialize_struct_value(sub_val, depth + 1)
            if sub is not None:
                out[prop.identifier] = sub
        else:
            out[prop.identifier] = clean_value(sub_val)
    return out


def serialize_node(node, skip_output_defaults: bool = False):
    """Serialize a single Blender node into a JSON-safe dictionary.

    When *skip_output_defaults* is True, output socket default_value is not
    serialized. Output defaults are meaningless in Geometry Nodes (outputs
    are always computed), and importing them costs an O(tree size) write per
    socket on Blender 5.1+, so dropping them makes imports much faster for
    large groups. Old JSON files that still contain output defaults import
    unchanged.
    """
    data = {
        "name": node.name,
        "type": node.bl_idname,
        "label": node.label,
        "location": list(node.location),
        "parent": node.parent.name if node.parent else None,
        "inputs": [],
        "outputs": [],
        "properties": {},
        "zone_items": [],
        "zone_paired_node": None,
        "menu_items_data": [],
    }

    for inp in node.inputs:
        inp_data = {
            "name": inp.name,
            "identifier": inp.identifier,
            "type": inp.type,
            "bl_idname": inp.bl_idname,
            "hide": getattr(inp, 'hide', False),
        }
        # Skip default_value for sockets that can't be meaningfully restored:
        # - NodeReroute: multi-type sockets, type depends on connections
        # - Connected inputs: the link drives the value (and the importer
        #   ignores connected defaults by design, so re-exports would show
        #   phantom differences)
        # - Non-scalar types (Geometry, Object, etc.): can't assign scalar
        if (node.bl_idname != "NodeReroute"
                and hasattr(inp, 'default_value')
                and not getattr(inp, 'is_linked', False)
                and inp.type not in _NON_SCALAR_SOCKET_TYPES
                and not (inp.type == 'MENU' and not inp.default_value)):
            inp_data["default_value"] = clean_value(inp.default_value)
        data["inputs"].append(inp_data)
    for out in node.outputs:
        out_data = {
            "name": out.name,
            "identifier": out.identifier,
            "type": out.type,
            "bl_idname": out.bl_idname,
            "hide": getattr(out, 'hide', False),
        }
        # Serialize default_value for output sockets (required for ShaderNodeValue, etc.)
        if (node.bl_idname != "NodeReroute"
                and not skip_output_defaults
                and hasattr(out, 'default_value')
                and not getattr(out, 'is_linked', False)
                and out.type not in _NON_SCALAR_SOCKET_TYPES
                and not (out.type == 'MENU' and not out.default_value)):
            out_data["default_value"] = clean_value(out.default_value)
        data["outputs"].append(out_data)

    for prop in node.bl_rna.properties:
        if prop.identifier in NODE_PROPS_TO_SKIP:
            continue
        try:
            val = getattr(node, prop.identifier)
        except (TypeError, AttributeError, ValueError, RuntimeError):
            continue
        if prop.is_readonly:
            # Embedded read-only structs (ColorRamp, CurveMapping, ...):
            # their content is real semantics — dump it instead of
            # silently skipping the property.
            dump = serialize_struct_value(val)
            if dump is not None:
                data["properties"][prop.identifier] = dump
            continue
        data["properties"][prop.identifier] = clean_value(val)

    custom = _custom_properties(node)
    if custom:
        data["custom_properties"] = custom

    # Dispatch to type-specific serializer
    handler = _NODE_SERIALIZERS.get(node.bl_idname)
    if handler:
        handler(node, data)

    # Deterministic output ordering for the per-node item collections
    # (name-based, matching the canonical hash).
    for coll_name in ("zone_items", "menu_items_data", "capture_items_data",
                      "list_items_data", "bundle_items_data"):
        coll = data.get(coll_name)
        if isinstance(coll, dict):
            data[coll_name] = {
                k: sorted(v, key=lambda x: x.get("name", ""))
                for k, v in coll.items()
            }
        elif coll:
            data[coll_name] = sorted(coll, key=lambda x: x.get("name", ""))

    # Paired zone node detection
    if hasattr(node, 'paired_output') and node.paired_output:
        data["zone_paired_node"] = node.paired_output.name
    elif hasattr(node, 'paired_input') and node.paired_input:
        data["zone_paired_node"] = node.paired_input.name
    inner_tree = getattr(node, "node_tree", None)
    if node.bl_idname in BUILTIN_GROUP_BL_IDNAMES:
        # Native group node: shared first-class tree, referenced by name.
        data["node_tree_reference"] = inner_tree.name if inner_tree else None
    elif inner_tree is not None:
        # Third-party custom group class (any naming): the inner tree is
        # per-instance content — embed it so two instances with divergent
        # edits round-trip exactly, with no global name needed.
        data["inner_tree"] = _serialize_node_tree_core(inner_tree)

    return data


def _custom_properties(id_block) -> dict:
    """Custom (ID) properties of a node or node tree, JSON-cleaned.

    The add-on's own bookkeeping keys (``gnt_*``, e.g. the sync uuid and
    the degraded-socket map) are excluded: they are metadata, not content.
    """
    props = {}
    try:
        keys = list(id_block.keys())
    except (AttributeError, TypeError, RuntimeError):
        return props
    for key in keys:
        if key.startswith("gnt_"):
            continue
        try:
            props[key] = clean_value(id_block[key])
        except (TypeError, AttributeError, ValueError, RuntimeError):
            continue
    return props


def _collect_tree_texts(nodes_data: list) -> dict:
    """Referenced Text datablocks of a serialized node list, name → content.

    Script nodes (OSL) reference a Text datablock; Blender persists and
    carries that content on append/save, so it is versioned content of
    the entry (decision 2026-10-15).  Only LOCAL texts referenced from
    serialized properties are collected — library-linked ones belong to
    another .blend and are out of scope, like other linked datablocks.
    """
    names: set[str] = set()

    def _scan(value):
        if isinstance(value, dict):
            if value.get("type") == "Text" and isinstance(value.get("name"), str):
                names.add(value["name"])
            for item in value.values():
                _scan(item)
        elif isinstance(value, list):
            for item in value:
                _scan(item)

    for node_data in nodes_data:
        _scan(node_data.get("properties"))
        _scan(node_data.get("custom_properties"))

    texts = {}
    for name in sorted(names):
        text = bpy.data.texts.get(name)
        if text is None or getattr(text, "library", None) is not None:
            continue
        try:
            texts[name] = text.as_string()
        except Exception:
            continue
    return texts


def _serialize_node_tree_core(tree):
    """Serialize a tree WITHOUT derived-dependency embedding.

    Public callers go through ``serialize_node_tree``, which can embed
    the local untracked dependency trees; derived copies are serialized
    with this core (the map is flat, one level).
    """
    data = {"name": tree.name}
    # Tree type (class name): written ONLY when it is not the default
    # GeometryNodeTree, so existing Geometry Nodes exports stay
    # byte-identical (absent = GeometryNodeTree).
    tree_blid = getattr(tree, "bl_idname", "GeometryNodeTree")
    if tree_blid != "GeometryNodeTree":
        data["tree_type"] = tree_blid
    # Animation boundary (D3b): informational flag (the canonical hash
    # only builds known keys, so it never moves the hash) that drives the
    # UI and gnt_check warnings.
    if tree_has_animation(tree):
        data["has_animation"] = True
    # Scene owner references (compositor only, 5.0+): informational, not
    # part of the canonical hash.  A fresh import uses them to assign the
    # rebuilt group to the scenes that used it in the source file.
    compositor_scenes = tree_compositor_scenes(tree)
    if compositor_scenes:
        data["compositor_scenes"] = compositor_scenes
    data.update({"inputs": [], "outputs": [], "nodes": [], "links": []})

    group_input_node = next((n for n in tree.nodes if n.bl_idname == 'NodeGroupInput'), None)
    input_connections = {}
    if group_input_node:
        for link in tree.links:
            if link.from_node == group_input_node:
                input_connections[link.from_socket.identifier] = link.to_node

    if hasattr(tree, 'interface'):
        # Interface sockets that exist as classes but cannot be built by
        # the Python API (see constants.NON_RECREATABLE_FALLBACKS) are
        # recreated as their base type; the importer remembers the original
        # name here so re-exports keep it instead of silently writing the
        # degraded type.
        degraded = {}
        try:
            stored = tree.get("gnt_degraded_sockets")
            if stored is not None:
                degraded = {k: stored[k] for k in stored.keys()}
        except Exception:
            degraded = {}

        data["interface_items"] = []
        for item in tree.interface.items_tree:
            i_data = {
                "name": getattr(item, 'name', ''),
                "item_type": item.item_type,
                "properties": {},
            }
            if hasattr(item, 'identifier'):
                i_data['identifier'] = item.identifier
            if hasattr(item, 'parent') and item.parent:
                i_data['parent'] = item.parent.name

            if item.item_type == 'SOCKET':
                i_data['in_out'] = getattr(item, 'in_out', 'INPUT')
                i_data['socket_type'] = getattr(item, 'socket_type', 'FLOAT')
                i_data['bl_socket_idname'] = getattr(item, 'bl_socket_idname', i_data['socket_type'])
                degraded_name = degraded.get(getattr(item, 'identifier', ''))
                if degraded_name:
                    i_data['bl_socket_idname'] = degraded_name
                    variant = parse_interface_socket_variant(degraded_name)
                    if variant is not None:
                        i_data['socket_type'] = variant[0]

                if i_data['socket_type'] == 'NodeSocketMenu':
                    items_found = []
                    # Primary: enum_items on the interface item itself
                    if hasattr(item, 'enum_items') and len(item.enum_items) > 0:
                        items_found = _serialize_enum_items(item.enum_items)

                    # Fallback 1: read enum_items from the directly
                    # connected MenuSwitch node (via Group Input)
                    if not items_found and item.identifier in input_connections:
                        connected_node = input_connections[item.identifier]
                        if connected_node.bl_idname == "GeometryNodeMenuSwitch" and hasattr(connected_node, 'enum_items'):
                            items_found = _serialize_enum_items(connected_node.enum_items)

                    # Fallback 2: search for MenuSwitch nodes connected to
                    # the Group Input via links (handles cases where
                    # input_connections didn't capture the connection)
                    if not items_found:
                        group_input = next(
                            (n for n in tree.nodes if n.bl_idname == 'NodeGroupInput'),
                            None,
                        )
                        if group_input:
                            for sock in group_input.outputs:
                                if getattr(sock, 'identifier', '') == item.identifier:
                                    for link in tree.links:
                                        if link.from_socket == sock:
                                            target = link.to_node
                                            if target.bl_idname == "GeometryNodeMenuSwitch" and hasattr(target, 'enum_items') and len(target.enum_items) > 0:
                                                items_found = _serialize_enum_items(target.enum_items)
                                                break
                                    break

                    # Fallback 3: for Menu sockets connected to a Group node,
                    # look at the referenced tree's interface for Menu sockets
                    # with enum_items
                    if not items_found and item.identifier in input_connections:
                        connected_node = input_connections[item.identifier]
                        if is_group_node_bl_idname(connected_node.bl_idname) and hasattr(connected_node, 'node_tree') and connected_node.node_tree:
                            ref_tree = connected_node.node_tree
                            if hasattr(ref_tree, 'interface'):
                                for ref_item in ref_tree.interface.items_tree:
                                    if (ref_item.item_type == 'SOCKET'
                                            and getattr(ref_item, 'socket_type', '') == 'NodeSocketMenu'
                                            and hasattr(ref_item, 'enum_items')
                                            and len(ref_item.enum_items) > 0):
                                        items_found = _serialize_enum_items(ref_item.enum_items)
                                        break

                    if items_found:
                        i_data["enum_items"] = items_found

            for prop in item.bl_rna.properties:
                if prop.identifier in INTERFACE_SKIP_PROPS or prop.is_readonly:
                    continue
                # Reading 'subtype' on an Int interface socket with an
                # invalid value triggers slow C-level RNA warnings.
                # NOTE: do not validate against prop.enum_items — on
                # Blender 5.2 it reports only ["DEFAULT"] even though the
                # full subtype enum is accepted by the setter.
                if prop.identifier == 'subtype' and prop.type == 'ENUM':
                    try:
                        current = getattr(item, prop.identifier, None)
                        if current and current != 'NONE':
                            i_data["properties"][prop.identifier] = current
                    except Exception:
                        pass
                    continue
                if prop.identifier == 'dimensions':
                    # Encoded variants (NodeSocketVector2D, ...2D) convey the
                    # dimension in the socket class name; a dimension edited
                    # on a BASE class (plain NodeSocketVector set to 2) does
                    # not, so it is serialized explicitly and restored by the
                    # importer's property pass.
                    try:
                        dims = getattr(item, 'dimensions', None)
                        if dims is not None:
                            variant = parse_interface_socket_variant(
                                i_data.get('bl_socket_idname') or '')
                            variant_dims = None
                            if variant and len(variant) > 1 and variant[1] is not None:
                                variant_dims = int(variant[1])
                            if int(dims) != 3 and variant_dims != int(dims):
                                i_data["properties"][prop.identifier] = int(dims)
                    except Exception:
                        pass
                    continue
                try:
                    val = getattr(item, prop.identifier)
                    i_data["properties"][prop.identifier] = clean_value(val)
                except Exception:
                    pass

            data["interface_items"].append(i_data)

            if item.item_type == 'SOCKET':
                s_data = {
                    "name": getattr(item, 'name', ''),
                    "identifier": getattr(item, 'identifier', ''),
                    "socket_type": getattr(item, 'socket_type', 'FLOAT'),
                    "bl_idname": getattr(item, 'bl_socket_idname', getattr(item, 'socket_type', 'FLOAT')),
                    "in_out": getattr(item, 'in_out', 'INPUT'),
                    "default_value": clean_value(getattr(item, 'default_value', None)),
                }
                for opt_prop in OPTIONAL_SOCKET_PROPS:
                    if opt_prop == 'subtype':
                        # Reading subtype via getattr on Int sockets with
                        # invalid values triggers RNA warnings.  Do NOT
                        # validate against rna_prop.enum_items — Blender
                        # 5.2 reports only ["DEFAULT"] there.
                        try:
                            current = getattr(item, 'subtype', None)
                            if current and current != 'NONE':
                                s_data['subtype'] = current
                        except Exception:
                            pass
                        continue
                    try:
                        val = getattr(item, opt_prop)
                        s_data[opt_prop] = clean_value(val)
                    except Exception:
                        pass
                if "enum_items" in i_data:
                    s_data["enum_items"] = i_data["enum_items"]
                data["inputs" if s_data["in_out"] == 'INPUT' else "outputs"].append(s_data)

        # Interface items keep Blender's items_tree order: the order of
        # the OUTPUT sockets is runtime semantics (a NODES modifier
        # requires the FIRST output to be geometry, and modifier input
        # keys are positional).  The importer creates items in this same
        # order, so a roundtrip is byte-identical without sorting.
        for it in data["interface_items"]:
            if it.get("enum_items"):
                it["enum_items"] = sorted(it["enum_items"], key=lambda x: x.get("name", ""))
    else:
        for inp in tree.inputs:
            data["inputs"].append({
                "name": inp.name,
                "identifier": inp.identifier,
                "type": inp.type,
                "bl_idname": inp.bl_idname,
                "default_value": clean_value(inp.default_value) if hasattr(inp, 'default_value') else None,
            })
        for out in tree.outputs:
            data["outputs"].append({
                "name": out.name,
                "identifier": out.identifier,
                "type": out.type,
                "bl_idname": out.bl_idname,
            })
        data["inputs"].sort(key=lambda s: s.get("name", ""))
        data["outputs"].sort(key=lambda s: s.get("name", ""))

    for node in tree.nodes:
        data["nodes"].append(serialize_node(node, skip_output_defaults=tree.type == 'GEOMETRY'))
    for link in tree.links:
        data["links"].append({
            "from_node": link.from_node.name,
            "from_socket_id": link.from_socket.identifier,
            "from_socket_name": link.from_socket.name,
            "to_node": link.to_node.name,
            "to_socket_id": link.to_socket.identifier,
            "to_socket_name": link.to_socket.name,
        })

    # Deterministic output ordering for the node list (name-based,
    # mirroring the canonical hash) so that re-exporting an unchanged
    # group produces a stable git diff and a rename only moves the
    # renamed block.  Links and interface items keep their creation
    # order: some volatile nodes change their socket layout while links
    # are being made, so link order is functional, and a NODES modifier
    # requires the first interface output to be geometry.
    data["nodes"].sort(key=lambda n: n.get("name", ""))

    data["tree_properties"] = {}
    for prop in tree.bl_rna.properties:
        if prop.identifier in TREE_PROPS_TO_SKIP or prop.is_readonly:
            continue
        try:
            val = getattr(tree, prop.identifier)
            data["tree_properties"][prop.identifier] = clean_value(val)
        except (TypeError, AttributeError, ValueError, RuntimeError):
            pass

    custom = _custom_properties(tree)
    if custom:
        data["custom_properties"] = custom

    texts = _collect_tree_texts(data.get("nodes", []))
    if texts:
        data["texts"] = texts

    return data


def tracked_blend_names() -> set:
    """Names of the currently tracked groups (ambient tracking context)."""
    try:
        from .sync_manager import sync_manager
        tracked = sync_manager.metadata.get("tracked_groups", {})
    except Exception:
        return set()
    return {info.get("blend_name", "") for info in tracked.values()}


def collect_derived_untracked(tree) -> dict:
    """Local, untracked, reachable dependency trees of *tree*.

    Library-linked trees are excluded by design (they belong to another
    .blend and are out of the versioning scope).  The root tree itself is
    never part of its own derived map.
    """
    from .socket_utils import get_tree_dependencies
    tracked = tracked_blend_names()
    derived = {}
    for name, dep in get_tree_dependencies(tree).items():
        if not name or name == tree.name or name in tracked:
            continue
        if getattr(dep, "library", None) is not None:
            continue
        derived[name] = dep
    return derived


def node_uid_map(tree) -> dict:
    """Current name -> stable UID map of a tree (only nodes that have one)."""
    return {node.name: node.get(NODE_UID_PROP, "")
            for node in sorted(tree.nodes, key=lambda n: n.name)
            if isinstance(node.get(NODE_UID_PROP, ""), str)
            and node.get(NODE_UID_PROP, "")}


def _deterministic_node_uid(tree, node, ordinal=0) -> str:
    """UUIDv5 fallback for a node without a persisted identity.

    The seed includes the serialized content (minus layout) so two
    branches that independently create a node with the same auto name but
    different content do not collide; identical creations may share the
    identity and merge as one.
    """
    try:
        payload = serialize_node(node)
        payload.pop("location", None)
        payload.pop("parent", None)
        for direction in ("inputs", "outputs"):
            for sock in payload.get(direction, []):
                if isinstance(sock, dict):
                    sock.pop("identifier", None)
                    sock.pop("hide", None)
        fingerprint = json.dumps(payload, sort_keys=True, default=str)
    except Exception:
        fingerprint = "%s:%s" % (getattr(node, "name", ""),
                                 getattr(node, "bl_idname", ""))
    seed = "gntoolkit:node:%s:%s" % (getattr(tree, "name", ""), fingerprint)
    if ordinal:
        seed += ":dup:%d" % ordinal
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def ensure_node_uids(tree) -> dict:
    """Assign a UID to every node that lacks one and deduplicate collisions.

    Existing properties are preserved (a rename keeps the identity).  A
    node without a property gets a deterministic UUIDv5 derived from the
    tree/node/type, so exporting the same unsaved .blend in two processes
    stays byte-identical; once the .blend is saved, the property is the
    stable identity.  Duplicates receive the next deterministic ordinal.
    """
    seen = set()
    for node in sorted(tree.nodes, key=lambda n: n.name):
        uid = node.get(NODE_UID_PROP, "")
        ordinal = 0
        if not isinstance(uid, str) or not uid or uid in seen:
            uid = _deterministic_node_uid(tree, node)
            while uid in seen:
                ordinal += 1
                uid = _deterministic_node_uid(tree, node, ordinal)
            node[NODE_UID_PROP] = uid
        seen.add(uid)
    return node_uid_map(tree)


def serialize_node_tree(tree, derived=None, assign_uids=False):
    """Serialize an entire Blender node tree into a JSON-safe dictionary.

    Embedded dependencies: by default (``derived=None``) a *tracked* tree
    embeds its local untracked dependency trees under
    ``derived_node_groups`` (a flat name→tree map).  Editing an untracked
    nested group then moves the parent's hash (no more false Synced) and
    the entry stays self-contained for cloning.  When the dependency
    becomes tracked, its copy is dropped from the parent on the next
    export.  ``derived=False`` disables the embedding (manual exports
    that already ship every dependency as its own entry); a dict gives
    the exact name→tree set (used when a group is being tracked and its
    full dependency closure must be recorded).

    ``assign_uids`` assigns stable node identities before serializing
    (export/track/merge paths).  Read-only hash/status paths keep it
    False and only report the identities already present.
    """
    if assign_uids:
        ensure_node_uids(tree)
    data = _serialize_node_tree_core(tree)
    uids = node_uid_map(tree)
    if uids:
        data["node_uids"] = uids
    if derived is None:
        if tree.name in tracked_blend_names():
            derived = collect_derived_untracked(tree)
    if derived:
        data["derived_node_groups"] = {
            name: _serialize_node_tree_core(dep)
            for name, dep in sorted(derived.items())
        }
    return data


def serialize_owner_settings(owner, kind: str) -> dict:
    """Curated datablock settings of an embedded-tree owner (allowlist).

    Only the properties in ``constants.MATERIAL_SETTINGS_PROPS`` /
    ``WORLD_SETTINGS_PROPS`` are versioned (plus the owner's custom
    properties, minus the add-on's own gnt_* bookkeeping).  Everything
    else of the datablock (node_tree, users, preview, asset data, ...)
    is identity/UI/runtime state, not content.
    """
    names = (MATERIAL_SETTINGS_PROPS if kind == "material"
             else WORLD_SETTINGS_PROPS)
    props = {}
    for pid in names:
        try:
            val = getattr(owner, pid)
        except Exception:
            continue
        props[pid] = clean_value(val)
    custom = {}
    try:
        for key in owner.keys():
            if isinstance(key, str) and not key.startswith("gnt_"):
                custom[key] = clean_value(owner[key])
    except Exception:
        pass
    out = {"properties": props}
    if custom:
        out["custom_properties"] = custom
    return out


def serialize_embedded_entry(owner, kind: str, derived=None,
                             assign_uids=False) -> dict:
    """Serialize an owner's node tree as one package entry.

    The entry is the tree dump plus an ``owner_settings`` section; its
    ``name`` is the OWNER name (the identity of an embedded tree — the
    tree datablock's own name is cosmetic and can be renamed by Blender
    when it collides with a group).  ``derived`` follows
    ``serialize_node_tree``; a tracked embedded entry embeds its local
    untracked group dependencies like a tracked group does.
    ``assign_uids`` follows ``serialize_node_tree``.
    """
    tree = owner.node_tree
    if derived is None:
        derived = collect_derived_untracked(tree)
    data = serialize_node_tree(tree, derived=derived,
                               assign_uids=assign_uids)
    data["name"] = owner.name
    data["owner_settings"] = serialize_owner_settings(owner, kind)
    return data


def canonical_hash_from_embedded(owner, kind: str) -> str:
    """Canonical hash of an embedded entry (tree + owner settings)."""
    from .hash_utils import canonical_hash_from_json_data
    return canonical_hash_from_json_data(serialize_embedded_entry(owner, kind))
