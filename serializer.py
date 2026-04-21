# -*- coding: utf-8 -*-
"""
gn_toolkit.serializer — Node-tree and node serialization (export).

Serializes Blender node trees into JSON-safe dictionaries that can be
re-imported by ``gn_toolkit.importer``.
"""

from __future__ import annotations

from .codec import clean_value
from .constants import (
    NODE_PROPS_TO_SKIP,
    TREE_PROPS_TO_SKIP,
    INTERFACE_SKIP_PROPS,
    OPTIONAL_SOCKET_PROPS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


# Dispatch table: bl_idname → handler(node, data)
_NODE_SERIALIZERS: dict[str, callable] = {
    "GeometryNodeMenuSwitch": _handle_menu_switch,
    "GeometryNodeSimulationOutput": _handle_simulation_output,
    "GeometryNodeRepeatOutput": _handle_repeat_output,
    "GeometryNodeForeachGeometryElementOutput": _handle_foreach_output,
    "GeometryNodeCaptureAttribute": _handle_capture_attribute,
    "GeometryNodeCombineBundle": _handle_bundle_node,
    "GeometryNodeSeparateBundle": _handle_bundle_node,
    "NodeCombineBundle": _handle_bundle_node,
    "NodeSeparateBundle": _handle_bundle_node,
}

# Closure types share the same handler
_NODE_SERIALIZERS["NodeClosureOutput"] = _handle_closure_output
_NODE_SERIALIZERS["NodeEvaluateClosure"] = _handle_closure_output


# ---------------------------------------------------------------------------
# Main serialization functions
# ---------------------------------------------------------------------------

# Socket types whose default_value cannot be meaningfully serialized/restored.
# Attempting to assign a scalar to these sockets will always fail on import.
_NON_SCALAR_SOCKET_TYPES = frozenset({
    'GEOMETRY', 'OBJECT', 'COLLECTION', 'MATERIAL', 'TEXTURE', 'IMAGE',
    'MATRIX', 'CLOSURE', 'MENU',
})


def serialize_node(node):
    """Serialize a single Blender node into a JSON-safe dictionary."""
    data = {
        "name": node.name,
        "type": node.bl_idname,
        "label": node.label,
        "location": list(node.location),
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
        # - Non-scalar types (Geometry, Object, etc.): can't assign scalar
        if (node.bl_idname != "NodeReroute"
                and hasattr(inp, 'default_value')
                and inp.type not in _NON_SCALAR_SOCKET_TYPES):
            inp_data["default_value"] = clean_value(inp.default_value)
        data["inputs"].append(inp_data)
    for out in node.outputs:
        data["outputs"].append({
            "name": out.name,
            "identifier": out.identifier,
            "type": out.type,
            "bl_idname": out.bl_idname,
            "hide": getattr(out, 'hide', False),
        })

    for prop in node.bl_rna.properties:
        if prop.identifier in NODE_PROPS_TO_SKIP or prop.is_readonly:
            continue
        try:
            val = getattr(node, prop.identifier)
            data["properties"][prop.identifier] = clean_value(val)
        except (TypeError, AttributeError, ValueError, RuntimeError):
            pass

    # Dispatch to type-specific serializer
    handler = _NODE_SERIALIZERS.get(node.bl_idname)
    if handler:
        handler(node, data)

    # Paired zone node detection
    if hasattr(node, 'paired_output') and node.paired_output:
        data["zone_paired_node"] = node.paired_output.name
    elif hasattr(node, 'paired_input') and node.paired_input:
        data["zone_paired_node"] = node.paired_input.name
    if node.bl_idname == "GeometryNodeGroup":
        data["node_tree_reference"] = node.node_tree.name if node.node_tree else None

    return data


def serialize_node_tree(tree):
    """Serialize an entire Blender node tree into a JSON-safe dictionary."""
    data = {"name": tree.name, "inputs": [], "outputs": [], "nodes": [], "links": []}

    group_input_node = next((n for n in tree.nodes if n.bl_idname == 'NodeGroupInput'), None)
    input_connections = {}
    if group_input_node:
        for link in tree.links:
            if link.from_node == group_input_node:
                input_connections[link.from_socket.identifier] = link.to_node

    if hasattr(tree, 'interface'):
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
                        if connected_node.bl_idname == "GeometryNodeGroup" and hasattr(connected_node, 'node_tree') and connected_node.node_tree:
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
                try:
                    val = getattr(item, prop.identifier)
                    i_data["properties"][prop.identifier] = clean_value(val)
                except:
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
                    try:
                        val = getattr(item, opt_prop)
                        s_data[opt_prop] = clean_value(val)
                    except:
                        pass
                if "enum_items" in i_data:
                    s_data["enum_items"] = i_data["enum_items"]
                data["inputs" if s_data["in_out"] == 'INPUT' else "outputs"].append(s_data)
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

    for node in tree.nodes:
        data["nodes"].append(serialize_node(node))
    for link in tree.links:
        data["links"].append({
            "from_node": link.from_node.name,
            "from_socket_id": link.from_socket.identifier,
            "from_socket_name": link.from_socket.name,
            "to_node": link.to_node.name,
            "to_socket_id": link.to_socket.identifier,
            "to_socket_name": link.to_socket.name,
        })

    data["tree_properties"] = {}
    for prop in tree.bl_rna.properties:
        if prop.identifier in TREE_PROPS_TO_SKIP or prop.is_readonly:
            continue
        try:
            val = getattr(tree, prop.identifier)
            data["tree_properties"][prop.identifier] = clean_value(val)
        except (TypeError, AttributeError, ValueError, RuntimeError):
            pass

    return data
