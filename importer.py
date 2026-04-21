# -*- coding: utf-8 -*-
"""
gn_toolkit.importer — Node-tree reconstruction from JSON.

Contains ``import_node_tree_recursive`` and all the helper functions it
needs (zone creation, dynamic socket mapping, switch item helpers).

Every function receives an ``ImportErrorTracker`` instance instead of
mutating a global counter.
"""

from __future__ import annotations

import bpy

from .codec import unclean_value, _is_vector_type, _is_2d_vector_type, _is_color_type, _is_int_type, _is_bool_type, _is_float_type, _is_string_type, _is_non_scalar_type
from .constants import (
    _VOLATILE_TYPES,
    ZONE_INPUTS,
    SOCKET_TYPE_MAP,
    OPTIONAL_SOCKET_PROPS,
    EXPLICITLY_HANDLED_PROPS,
    INTERFACE_SOCKET_TYPE_REMAP,
)
from .error_tracker import ImportErrorTracker
from .socket_utils import (
    normalize_socket_type,
    attempt_create_item,
    find_robust_socket,
)

# Sentinel for "no tracker available" — used by low-level helpers that
# may be called outside the main import flow.
_WARN = object()


# ---------------------------------------------------------------------------
# Collection helpers
# ---------------------------------------------------------------------------

def _reset_collection(coll, tracker: ImportErrorTracker | None = None) -> None:
    """Clear all items from a Blender collection, handling different API versions.

    Some collections have a ``clear()`` method; others must be removed
    one by one via ``remove()``.
    """
    if coll is None:
        return
    if hasattr(coll, 'clear'):
        try:
            coll.clear()
            return
        except (TypeError, AttributeError, ValueError, RuntimeError):
            pass
    # Fallback: remove items one by one from the end
    while len(coll) > 0:
        try:
            coll.remove(coll[-1])
        except (TypeError, AttributeError, ValueError, RuntimeError):
            if tracker is not None:
                tracker.record(f"Could not clear collection item", level="DEBUG")
            break


def _populate_collection(coll, items_data: list[dict], tracker: ImportErrorTracker) -> None:
    """Populate a collection from serialized item data.

    Each item dict should have at least ``"socket_type"`` and ``"name"``.
    """
    for itm in items_data:
        dt = itm.get("socket_type", "FLOAT")
        s_name = itm.get("name", "Item")
        attempt_create_item(coll, dt, s_name)


# ---------------------------------------------------------------------------
# Dynamic socket mapping
# ---------------------------------------------------------------------------

def map_dynamic_sockets(old_data, new_node, remap_dict, node_key):
    """Map old socket identifiers to new ones by sequential name+type match.

    This is the definitive solution for ID collisions on volatile nodes:
    once a node is fully constructed, we match by name and type rather
    than by recycled identifiers.
    """
    if not old_data or not new_node:
        return

    used_in = set()
    remap_dict.setdefault(node_key + "_IN", {})
    for old_s in old_data.get("inputs", []):
        for i, new_s in enumerate(new_node.inputs):
            if i not in used_in and new_s.name == old_s.get("name") and new_s.type == old_s.get("type", new_s.type):
                remap_dict[node_key + "_IN"][old_s["identifier"]] = new_s.identifier
                used_in.add(i)
                break

    used_out = set()
    remap_dict.setdefault(node_key + "_OUT", {})
    for old_s in old_data.get("outputs", []):
        for i, new_s in enumerate(new_node.outputs):
            if i not in used_out and new_s.name == old_s.get("name") and new_s.type == old_s.get("type", new_s.type):
                remap_dict[node_key + "_OUT"][old_s["identifier"]] = new_s.identifier
                used_out.add(i)
                break


# ---------------------------------------------------------------------------
# Zone creation via Blender operator
# ---------------------------------------------------------------------------

def run_add_zone_operator(nt, input_type, output_type, tracker: ImportErrorTracker):
    """Create a zone pair (input+output nodes) using ``bpy.ops.node.add_zone``.

    The operator requires an active Node Editor area with the target tree
    pinned.  This function temporarily overrides the context, runs the
    operator, and restores the original state.
    """
    win = bpy.context.window
    area = next((a for a in bpy.context.screen.areas if a.type == 'NODE_EDITOR'), None)
    if not area:
        for w in bpy.context.window_manager.windows:
            for s in w.screen.areas:
                if s.type == 'NODE_EDITOR':
                    area = s
                    win = w
                    break
            if area:
                break
    if area:
        space = area.spaces[0]
        region = next((r for r in area.regions if r.type == 'WINDOW'), area.regions[-1])
        orig_tree = getattr(space, 'node_tree', None)
        orig_pin = getattr(space, 'pin', False)
        nodes_before = set(nt.nodes)
        space.pin = True
        space.node_tree = nt
        try:
            with bpy.context.temp_override(window=win, area=area, region=region, space_data=space):
                op_args = {
                    'use_transform': False,
                    'input_node_type': input_type,
                    'output_node_type': output_type,
                }
                if input_type == 'GeometryNodeSimulationInput':
                    op_args['add_default_geometry_link'] = True
                bpy.ops.node.add_zone(**op_args)
        except Exception as e:
            tracker.record(f"Failed to create Zone {input_type}: {e}", level="CRITICAL ERROR")

        space.node_tree = orig_tree
        space.pin = orig_pin
        added_nodes = list(set(nt.nodes) - nodes_before)
        n_in = next((n for n in added_nodes if n.bl_idname == input_type), None)
        n_out = next((n for n in added_nodes if n.bl_idname == output_type), None)
        if n_in and n_out:
            return n_in, n_out
        sel = [n for n in nt.nodes if n.select and n not in nodes_before]
        if len(sel) == 2:
            n1, n2 = sel
            if n1.bl_idname == input_type:
                return n1, n2
            elif n2.bl_idname == input_type:
                return n2, n1
    return None, None


# ---------------------------------------------------------------------------
# Switch / menu item helpers
# ---------------------------------------------------------------------------

def ensure_switch_items(node, target_count, switch_type, tracker: ImportErrorTracker):
    """Ensure that *node* has at least *target_count* items for the given switch type."""
    if switch_type == 'index' and hasattr(node, "index_switch_items"):
        coll = node.index_switch_items
        while len(coll) < target_count:
            try:
                coll.new()
            except Exception as e:
                tracker.record(f"Index Switch item creation failed: {e}", level="WARN")
                break
        return

    if switch_type == 'menu' and hasattr(node, 'enum_items'):
        coll = node.enum_items
        while len(coll) < target_count:
            try:
                coll.new(name=f"Item_{len(coll)}")
            except Exception as e:
                tracker.record(f"Menu Switch item creation failed: {e}", level="WARN")
                break
        return


# ---------------------------------------------------------------------------
# Step 1 helper: Interface reconstruction
# ---------------------------------------------------------------------------

def _rebuild_interface(ng, data: dict, interface_map: dict, tracker: ImportErrorTracker) -> None:
    """Reconstruct the node-tree interface from serialized data.

    Handles both the modern ``interface`` API and the legacy
    ``inputs``/``outputs`` path.
    """
    use_interface_api = hasattr(ng, 'interface')

    if "interface_items" in data and use_interface_api:
        panel_map = {}
        legacy_enums = {
            inp['identifier']: inp.get('enum_items')
            for inp in data.get('inputs', [])
            if inp.get('socket_type') == 'NodeSocketMenu'
        }

        for i_data in data["interface_items"]:
            item_type = i_data.get("item_type")
            new_item = None
            try:
                if item_type == 'PANEL':
                    new_item = ng.interface.new_panel(i_data["name"])
                    panel_map[i_data["name"]] = new_item
                elif item_type == 'SOCKET':
                    raw_socket_type = i_data.get("bl_socket_idname", i_data.get("socket_type", "NodeSocketFloat"))
                    creation_type = INTERFACE_SOCKET_TYPE_REMAP.get(raw_socket_type, raw_socket_type)
                    force_dimensions = None

                    if raw_socket_type in ("NodeSocketVector2D", "NodeSocketVectorTranslation2D"):
                        creation_type = "NodeSocketVector"
                        force_dimensions = 2

                    kwargs = {
                        "name": i_data.get("name", "Socket"),
                        "in_out": i_data.get("in_out", "INPUT"),
                        "socket_type": creation_type,
                    }

                    parent_item = panel_map.get(i_data.get("parent", ""))
                    if parent_item:
                        try:
                            kwargs["parent"] = parent_item
                            new_item = ng.interface.new_socket(**kwargs)
                        except TypeError:
                            del kwargs["parent"]
                            new_item = ng.interface.new_socket(**kwargs)
                            try:
                                new_item.parent = parent_item
                            except (TypeError, AttributeError, ValueError, RuntimeError):
                                pass
                    else:
                        new_item = ng.interface.new_socket(**kwargs)

                    if new_item and force_dimensions is not None and hasattr(new_item, 'dimensions'):
                        try:
                            new_item.dimensions = force_dimensions
                        except (TypeError, AttributeError, ValueError, RuntimeError):
                            tracker.record(
                                f"Could not set dimensions={force_dimensions} on interface socket "
                                f"'{i_data.get('name')}'", level="DEBUG",
                            )

                    interface_map[i_data.get("identifier", "")] = new_item.identifier

                    # Post-creation type verification: Blender may silently
                    # create a socket with a different type than requested
                    # (e.g. NodeSocketInt instead of NodeSocketString).
                    # Log this so we can detect silent type mismatches.
                    #
                    # Compare against the EXPECTED type, not the raw type,
                    # because 2D vectors are created as NodeSocketVector
                    # then converted to NodeSocketVector2D via dimensions=2.
                    if new_item and hasattr(new_item, 'bl_socket_idname'):
                        actual_type = new_item.bl_socket_idname
                        # Compute the expected type after the full creation flow
                        if raw_socket_type in ("NodeSocketVector2D", "NodeSocketVectorTranslation2D"):
                            expected_type = "NodeSocketVector2D"
                        else:
                            expected_type = raw_socket_type
                        if actual_type != expected_type:
                            # This is a known Blender limitation: some socket
                            # types (e.g. NodeSocketMatrix) are silently
                            # replaced by fallback types during
                            # interface.new_socket().  Log at DEBUG level
                            # since this is expected behavior, not an error.
                            tracker.record(
                                f"Interface socket '{i_data.get('name')}': requested type "
                                f"'{raw_socket_type}' (expected '{expected_type}') but Blender "
                                f"created '{actual_type}'",
                                level="DEBUG",
                            )
            except Exception as e:
                tracker.record(f"Failed to create interface item '{i_data.get('name')}': {e}")

            if new_item:
                _apply_interface_item_properties(new_item, i_data, item_type, legacy_enums, tracker)

                # After applying properties (especially enum_items on Menu
                # sockets), Blender may have triggered an interface rebuild
                # that changed the socket's identifier.  Re-read the actual
                # identifier and update the interface_map if necessary.
                if item_type == 'SOCKET':
                    actual_id = getattr(new_item, 'identifier', None)
                    old_id = i_data.get("identifier", "")
                    if actual_id and old_id and actual_id != interface_map.get(old_id):
                        interface_map[old_id] = actual_id

    else:
        # Fallback legacy
        for item_data in data.get("inputs", []):
            old_id = item_data['identifier']
            new_sock = None
            try:
                if use_interface_api:
                    new_sock = ng.interface.new_socket(
                        name=item_data['name'],
                        in_out='INPUT',
                        socket_type=item_data.get('bl_idname', item_data['socket_type']),
                    )
                else:
                    new_sock = ng.inputs.new(item_data['type'], item_data['name'])
            except Exception as e:
                tracker.record(f"Failed legacy input socket '{item_data['name']}': {e}")
            if new_sock:
                interface_map[old_id] = new_sock.identifier

        for item_data in data.get("outputs", []):
            old_id = item_data['identifier']
            new_sock = None
            try:
                if use_interface_api:
                    new_sock = ng.interface.new_socket(
                        name=item_data['name'],
                        in_out='OUTPUT',
                        socket_type=item_data.get('bl_idname', item_data['socket_type']),
                    )
                else:
                    new_sock = ng.outputs.new(item_data['type'], item_data['name'])
            except Exception as e:
                tracker.record(f"Failed legacy output socket '{item_data['name']}': {e}")
            if new_sock:
                interface_map[old_id] = new_sock.identifier


def _apply_interface_item_properties(new_item, i_data: dict, item_type: str,
                                     legacy_enums: dict, tracker: ImportErrorTracker) -> None:
    """Apply properties and default values to a newly created interface item."""
    # For Menu sockets, create enum_items FIRST, then set default_value.
    # The order is critical: Blender rejects default_value if enum_items
    # is empty, and enum_items may be cleared by interface updates.
    menu_default = None
    if item_type == 'SOCKET' and i_data.get("socket_type") == 'NodeSocketMenu':
        items_source = i_data.get("enum_items")
        if not items_source:
            items_source = legacy_enums.get(i_data.get("identifier"))

        props = i_data.get("properties", {})
        menu_default = props.get("default_value")
        if menu_default is None:
            menu_default = i_data.get("default_value")

        if items_source and hasattr(new_item, 'enum_items'):
            _reset_collection(new_item.enum_items, tracker)
            for ei in items_source:
                try:
                    item_id = ei.get("identifier")
                    if not item_id:
                        item_id = ei.get("name")
                    new_item.enum_items.new(
                        name=ei.get("name", "Item"),
                        identifier=item_id,
                        description=ei.get("description", ""),
                    )
                except Exception as e:
                    tracker.record(f"Creando enum item: {e}")

            # Now that enum_items exist, try to set default_value
            # Skip empty-string defaults — they are not valid enum items.
            if menu_default is not None and hasattr(new_item, 'default_value') and str(menu_default) != "":
                try:
                    setattr(new_item, 'default_value', str(menu_default))
                except Exception as menu_exc:
                    # Blender may have cleared enum_items during interface
                    # rebuild — this is a known timing issue.  We'll retry
                    # in _post_sync_interface.
                    tracker.record(
                        f"Interface menu '{i_data.get('name')}' initial set failed: {menu_exc} "
                        f"(enum_items={[e.identifier for e in new_item.enum_items]})",
                        level="DEBUG",
                    )

            # After enum_items are populated, set menu_expanded on Menu
            # sockets.  This property controls whether the menu is shown
            # expanded in the modifier UI and must be set AFTER enum_items
            # exist for Blender to accept it.
            menu_expanded = props.get("menu_expanded")
            if menu_expanded is not None and hasattr(new_item, 'menu_expanded'):
                try:
                    setattr(new_item, 'menu_expanded', menu_expanded)
                except:
                    pass

            # Also set the 'optional' toggle on Menu sockets after enum_items
            optional = props.get("optional")
            if optional is not None and hasattr(new_item, 'optional'):
                try:
                    setattr(new_item, 'optional', optional)
                except:
                    pass

        elif menu_default is not None and hasattr(new_item, 'default_value'):
            # No enum_items data — default_value cannot be set until
            # enum_items exist.  _post_sync_interface will retry after
            # wiring, when Blender populates enum_items from internal
            # connections.  Skip silently to reduce noise.
            pass

    props = i_data.get("properties", {})
    if "subtype" in props and hasattr(new_item, 'subtype'):
        try:
            sub_val = props["subtype"]
            # Empty string is not a valid subtype enum value — skip it
            if sub_val:
                setattr(new_item, 'subtype', sub_val)
        except:
            tracker.record(f"Could not set subtype on '{i_data.get('name')}'", level="DEBUG")

    for p_name in OPTIONAL_SOCKET_PROPS:
        # 'subtype' is handled explicitly above with empty-string guard;
        # skip it here to avoid overwriting the guarded assignment.
        if p_name == 'subtype':
            continue
        # menu_expanded and optional are handled explicitly above for Menu
        # sockets (they must be set after enum_items exist).  Skip them here
        # to avoid overwriting with a potentially stale value, but only for
        # Menu sockets.  For other socket types, let the loop set them.
        if p_name in ('menu_expanded', 'optional', 'optional_label'):
            if i_data.get("socket_type") == 'NodeSocketMenu':
                continue
        if p_name in props and hasattr(new_item, p_name):
            try:
                setattr(new_item, p_name, props[p_name])
            except:
                tracker.record(
                    f"Could not set {p_name} on '{i_data.get('name')}'", level="DEBUG",
                )

    for p_name, p_val in props.items():
        if p_name in EXPLICITLY_HANDLED_PROPS:
            continue
        try:
            ctx = f"Interface item '{i_data.get('name')}' property '{p_name}'"
            setattr(new_item, p_name, unclean_value(p_val, i_data.get("socket_type", "VALUE"), context=ctx))
        except:
            tracker.record(
                f"Could not set property '{p_name}' on interface item '{i_data.get('name')}'",
                level="DEBUG",
            )

    # default_value MUST be set LAST, after all other properties.
    # Setting properties like subtype, default_input, min_value, max_value
    # can cause Blender to reset default_value back to the Blender default
    # (e.g. 0 for int sockets).  By setting default_value after everything
    # else, we ensure the serialized value takes precedence.
    default_val = props.get("default_value")
    if default_val is None:
        default_val = i_data.get("default_value")

    if default_val is not None and hasattr(new_item, 'default_value'):
        try:
            if i_data.get("socket_type", "") == 'NodeSocketMenu':
                if default_val == "":
                    # Empty string is not a valid menu default_value — skip
                    pass
                else:
                    new_item.default_value = str(default_val)
            else:
                type_hint = i_data.get("bl_socket_idname") or i_data.get("socket_type") or "VALUE"
                ctx = f"Interface item '{i_data.get('name')}' default_value"
                result = unclean_value(default_val, type_hint, context=ctx)
                # Interface item default_value ALWAYS requires 3-component
                # vectors, even for 2D sockets (NodeSocketVector2D).
                if (_is_vector_type(type_hint)
                        and isinstance(result, (list, tuple))
                        and len(result) == 2):
                    result = list(result) + [0.0]
                try:
                    new_item.default_value = result
                except (TypeError, AttributeError, ValueError, RuntimeError) as vec_exc:
                    # Some 2D interface sockets genuinely require 2 components.
                    # If 3-component assignment failed with "2 items" message,
                    # retry with just the first 2 components.
                    if (_is_vector_type(type_hint)
                            and isinstance(result, (list, tuple))
                            and len(result) == 3
                            and "2 items" in str(vec_exc)):
                        new_item.default_value = result[:2]
                    else:
                        raise
        except (TypeError, AttributeError, ValueError, RuntimeError) as exc:
            # For Menu sockets: the primary handler already tried and
            # _post_sync_interface will retry — skip secondary coercion
            # to avoid duplicate error messages.
            if i_data.get("socket_type", "") == 'NodeSocketMenu':
                # Silently skip — will be retried in post-sync
                pass
                return
            # Try secondary coercion based on the interface item's socket type
            socket_type = i_data.get("bl_socket_idname") or i_data.get("socket_type") or "VALUE"
            coerced = _coerce_interface_value(default_val, socket_type)
            if coerced is not _SKIP:
                try:
                    new_item.default_value = coerced
                except (TypeError, AttributeError, ValueError, RuntimeError) as exc2:
                    tracker.record(
                        f"Interface item '{i_data.get('name')}' could not set default_value "
                        f"(primary: {exc}; secondary: {exc2})",
                        level="DEBUG",
                    )
                    print(f"[DEFAULT_VALUE] Interface item '{i_data.get('name')}' "
                          f"kept Blender default (assignment failed: {exc2})")
            else:
                tracker.record(
                    f"Interface item '{i_data.get('name')}' could not set default_value ({exc})",
                    level="DEBUG",
                )


# ---------------------------------------------------------------------------
# Step 1 helper: Zone node creation
# ---------------------------------------------------------------------------

def _create_zone_nodes(ng, node_data: dict, data: dict, node_map: dict,
                       processed_zone_nodes: set, zone_socket_remap: dict,
                       tracker: ImportErrorTracker):
    """Create a zone pair (input + output node) for zone-type nodes.

    Returns the new input node if created, or None.
    """
    node_name = node_data.get("name")
    node_type = node_data.get("type")
    in_name = node_name
    in_type = node_type
    out_name = node_data.get("zone_paired_node")
    out_data = next((n for n in data["nodes"] if n["name"] == out_name), None) if out_name else None

    if not out_data:
        return None

    out_type = out_data["type"]
    for n in ng.nodes:
        n.select = False
    n_in, n_out = run_add_zone_operator(ng, in_type, out_type, tracker)

    if not n_in:
        n_in = next(
            (n for n in ng.nodes if n.bl_idname == in_type and n.name not in processed_zone_nodes), None
        )
    if not n_out:
        n_out = next(
            (n for n in ng.nodes if n.bl_idname == out_type and n.name not in processed_zone_nodes), None
        )

    if not (n_in and n_out):
        return None

    n_in.name = in_name
    n_out.name = out_name
    node_map[in_name] = n_in
    node_map[out_name] = n_out
    processed_zone_nodes.add(in_name)
    processed_zone_nodes.add(out_name)

    items_data = out_data.get("zone_items", [])

    # 1. Repeat & Simulation
    if in_type in ('GeometryNodeRepeatInput', 'GeometryNodeSimulationInput'):
        prop_name = (
            "repeat_items"
            if in_type == 'GeometryNodeRepeatInput'
            else ("state_items" if hasattr(n_out, "state_items") else "simulation_items")
        )
        coll = getattr(n_out, prop_name, None)
        if coll is not None and isinstance(items_data, list):
            _reset_collection(coll, tracker)
            _populate_collection(coll, items_data, tracker)

    # 2. For Each Geometry Element
    elif in_type == 'GeometryNodeForeachGeometryElementInput':
        for prop_name in ["main_items", "generation_items", "input_items"]:
            coll = getattr(n_out, prop_name, None)
            saved_list = out_data.get("zone_items", {}).get(prop_name, [])
            if coll is not None:
                _reset_collection(coll, tracker)
                _populate_collection(coll, saved_list, tracker)

    # 3. Closure Zones
    elif in_type == 'NodeClosureInput':
        for prop_name in ["input_items", "output_items"]:
            coll = getattr(n_out, prop_name, None)
            saved_list = out_data.get("zone_items", {}).get(prop_name, [])
            if coll is not None:
                _reset_collection(coll, tracker)
                _populate_collection(coll, saved_list, tracker)

    # POST-CREATION MAPPING
    map_dynamic_sockets(node_data, n_in, zone_socket_remap, n_in.name)
    map_dynamic_sockets(out_data, n_out, zone_socket_remap, n_out.name)
    return n_in


# ---------------------------------------------------------------------------
# Step 1 helper: Node-specific configuration
# ---------------------------------------------------------------------------

def _configure_special_node(new_node, node_data: dict, node_type: str,
                            zone_socket_remap: dict, tracker: ImportErrorTracker) -> None:
    """Apply type-specific configuration (dynamic items, enum rebuilds, etc.)."""

    if node_type == "GeometryNodeIndexSwitch":
        expected_inputs = len(
            [i for i in node_data.get("inputs", []) if i.get('identifier') not in ('Index', '__extend__')]
        )
        if expected_inputs > 2:
            ensure_switch_items(new_node, expected_inputs, 'index', tracker)
        map_dynamic_sockets(node_data, new_node, zone_socket_remap, new_node.name)

    elif node_type == "GeometryNodeMenuSwitch":
        if hasattr(new_node, 'enum_items'):
            menu_names = []
            if node_data.get("menu_items_data"):
                menu_names = [m["name"] for m in node_data["menu_items_data"]]
            else:
                menu_names = [
                    inp["name"]
                    for inp in node_data.get("inputs", [])
                    if inp.get("identifier", "").startswith("Item_")
                ]

            if menu_names:
                _reset_collection(new_node.enum_items, tracker)
                for name_val in menu_names:
                    try:
                        new_node.enum_items.new(name=name_val)
                    except (TypeError, AttributeError, ValueError, RuntimeError) as exc:
                        tracker.record(
                            f"MenuSwitch '{new_node.name}': could not create enum item '{name_val}': {exc}",
                            level="DEBUG",
                        )
        map_dynamic_sockets(node_data, new_node, zone_socket_remap, new_node.name)

    elif node_type == "GeometryNodeCaptureAttribute":
        capture_data = node_data.get("capture_items_data", [])
        if hasattr(new_node, 'capture_items'):
            _reset_collection(new_node.capture_items, tracker)
            for itm in capture_data:
                dt = itm.get("data_type", "FLOAT")
                nm = itm.get("name", "Attribute")
                attempt_create_item(new_node.capture_items, dt, nm)
            map_dynamic_sockets(node_data, new_node, zone_socket_remap, new_node.name)
        elif capture_data:
            try:
                new_node.data_type = capture_data[0].get("data_type", "FLOAT")
            except (TypeError, AttributeError, ValueError, RuntimeError) as exc:
                tracker.record(
                    f"CaptureAttribute '{new_node.name}': could not set data_type: {exc}", level="DEBUG",
                )

    elif node_type == "NodeEvaluateClosure":
        for prop_name in ["input_items", "output_items"]:
            coll = getattr(new_node, prop_name, None)
            saved_list = node_data.get("zone_items", {}).get(prop_name, [])
            if coll is not None:
                _reset_collection(coll, tracker)
                _populate_collection(coll, saved_list, tracker)
        map_dynamic_sockets(node_data, new_node, zone_socket_remap, new_node.name)

    elif node_type in ("NodeCombineBundle", "NodeSeparateBundle", "GeometryNodeViewer"):
        coll = None
        for prop_name in ['bundle_items', 'viewer_items', 'items']:
            if hasattr(new_node, prop_name):
                temp_coll = getattr(new_node, prop_name)
                if not callable(temp_coll):
                    coll = temp_coll
                    break

        if coll is None:
            coll = (
                new_node.inputs
                if node_type in ("NodeCombineBundle", "GeometryNodeViewer")
                else new_node.outputs
            )

        if coll is not None:
            is_native_io = coll == new_node.inputs or coll == new_node.outputs
            if not is_native_io:
                _reset_collection(coll, tracker)

            source_list = (
                node_data.get("inputs", [])
                if node_type in ("NodeCombineBundle", "GeometryNodeViewer")
                else node_data.get("outputs", [])
            )
            for sock_data in source_list:
                sid = sock_data.get("identifier", "")
                nm = sock_data.get("name", "Item")

                if sid in ("__extend__", "Bundle"):
                    continue
                if sid == "Geometry" or nm == "Geometry":
                    if any(s.type == 'GEOMETRY' for s in new_node.inputs):
                        continue

                dt = sock_data.get("bl_idname", sock_data.get("socket_type", "FLOAT"))
                attempt_create_item(coll, dt, nm)

            map_dynamic_sockets(node_data, new_node, zone_socket_remap, new_node.name)


# ---------------------------------------------------------------------------
# Step 2 helper: Apply default values
# ---------------------------------------------------------------------------

# Sentinel returned by _coerce_to_socket_type when the assignment should be
# skipped entirely (e.g. Reroute sockets, non-scalar sockets, enum errors).
_SKIP = object()


def _coerce_interface_value(val, socket_type: str):
    """Force-convert *val* to match the interface item's socket type.

    This mirrors ``_coerce_to_socket_type`` but works on interface items
    (which don't have a ``sock.node`` attribute).  Used as a secondary
    coercion path when ``unclean_value`` fails.
    """
    # --- Menu socket (NodeSocketMenu) ---
    # Menu sockets require a string enum identifier as default_value.
    if socket_type == 'NodeSocketMenu':
        if isinstance(val, str) and val:
            return val
        return _SKIP

    # Non-scalar sockets — skip
    if _is_non_scalar_type(socket_type):
        return _SKIP

    # --- Bool socket ---
    if _is_bool_type(socket_type):
        if val is None:
            return _SKIP
        if isinstance(val, bool):
            return val
        if isinstance(val, (int, float)):
            return bool(val)
        if isinstance(val, str):
            low = val.lower()
            return low in ("true", "1", "yes")
        if isinstance(val, list) and len(val) == 1:
            return bool(val[0])
        return _SKIP

    # --- Int socket ---
    if _is_int_type(socket_type):
        if val is None:
            return _SKIP
        if isinstance(val, bool):
            return int(val)
        if isinstance(val, (int, float)):
            return int(val)
        if isinstance(val, str):
            try:
                return int(val)
            except (ValueError, TypeError):
                return _SKIP
        if isinstance(val, list) and len(val) == 1:
            return int(val[0]) if isinstance(val[0], (int, float, bool)) else _SKIP
        return _SKIP

    # --- Float socket ---
    if _is_float_type(socket_type):
        if val is None:
            return _SKIP
        if isinstance(val, bool):
            return float(val)
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, str):
            try:
                return float(val)
            except (ValueError, TypeError):
                return _SKIP
        if isinstance(val, list) and len(val) == 1:
            return float(val[0]) if isinstance(val[0], (int, float, bool)) else _SKIP
        return _SKIP

    # --- Vector/Rotation socket ---
    if _is_vector_type(socket_type):
        # Interface item default_value ALWAYS requires 3 components, even
        # for 2D vector sockets (NodeSocketVector2D).  The 'dimensions'
        # property only affects the UI display, not the default_value
        # storage format.  A 2-component list will always raise
        # "sequences of dimension 0 should contain 3 items, not 2".
        dim = 3  # Always 3 for interface items
        if val is None:
            return [0.0] * dim
        if isinstance(val, (int, float, bool)):
            f = float(val)
            return [f] * dim
        if isinstance(val, (list, tuple)):
            comps = [float(v) if isinstance(v, (int, float, bool)) else 0.0 for v in val]
            if len(comps) >= dim:
                return comps[:dim]
            return comps + [0.0] * (dim - len(comps))
        return [0.0] * dim

    # --- Color/RGBA socket ---
    if _is_color_type(socket_type):
        if val is None:
            return [0.0, 0.0, 0.0, 1.0]
        if isinstance(val, (int, float, bool)):
            f = float(val)
            return [f, f, f, 1.0]
        if isinstance(val, (list, tuple)):
            comps = [float(v) if isinstance(v, (int, float, bool)) else 0.0 for v in val]
            if len(comps) == 3:
                return comps + [1.0]
            if len(comps) >= 4:
                return comps[:4]
            return comps + [0.0] * (4 - len(comps))
        return [0.0, 0.0, 0.0, 1.0]

    # --- String socket ---
    if _is_string_type(socket_type):
        if val is None:
            return _SKIP
        if isinstance(val, str):
            return val
        return str(val)

    return _SKIP


def _coerce_to_socket_type(val, sock, bl_id: str):
    """Force-convert *val* to the type expected by *sock*'s ``bl_idname``.

    This is the secondary coercion path: it is called only when the primary
    ``unclean_value`` conversion failed to produce a type Blender accepts.
    It uses the socket's ACTUAL ``bl_idname`` as ground truth, bypassing
    any mismatch between the serialized type and the recreated socket type.

    Returns the coerced value, or ``_SKIP`` if the assignment should be
    skipped entirely.
    """
    # NodeReroute: sockets are multi-type and have no meaningful default_value
    if sock.node.bl_idname == "NodeReroute":
        return _SKIP

    # --- Menu socket (NodeSocketMenu) ---
    # Menu sockets require a string enum identifier as default_value.
    # Non-string values cannot be coerced to a valid enum identifier,
    # so skip them.  String values are passed through directly.
    if bl_id == 'NodeSocketMenu':
        if isinstance(val, str) and val:
            return val
        return _SKIP

    # Non-scalar sockets (Geometry, Object, Material, etc.) have no
    # meaningful scalar default_value — skip entirely.
    if _is_non_scalar_type(bl_id):
        return _SKIP

    # --- Bool socket ---
    if _is_bool_type(bl_id):
        if val is None:
            return _SKIP  # Keep Blender default
        if isinstance(val, bool):
            return val
        if isinstance(val, (int, float)):
            return bool(val)
        if isinstance(val, str):
            low = val.lower()
            if low in ("true", "1", "yes"):
                return True
            return False
        if isinstance(val, list):
            # Single-element list: unwrap
            if len(val) == 1:
                return bool(val[0]) if isinstance(val[0], (int, float, bool)) else False
            # Multi-element list (e.g. vector→bool): extract first component
            first = val[0] if val else 0
            if isinstance(first, (int, float, bool)):
                return bool(first)
            return False
        return _SKIP

    # --- Int socket ---
    if _is_int_type(bl_id):
        if val is None:
            return _SKIP  # Keep Blender default
        if isinstance(val, bool):
            return int(val)
        if isinstance(val, (int, float)):
            return int(val)
        if isinstance(val, str):
            try:
                return int(val)
            except (ValueError, TypeError):
                return _SKIP
        if isinstance(val, list):
            # Single-element list: unwrap
            if len(val) == 1:
                return int(val[0]) if isinstance(val[0], (int, float, bool)) else _SKIP
            # Multi-element list (e.g. vector→int): extract first component
            first = val[0] if val else 0
            if isinstance(first, (int, float, bool)):
                return int(first)
            return _SKIP
        return _SKIP

    # --- Float socket ---
    if _is_float_type(bl_id):
        if val is None:
            return _SKIP  # Keep Blender default
        if isinstance(val, bool):
            return float(val)
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, str):
            try:
                return float(val)
            except (ValueError, TypeError):
                return _SKIP
        if isinstance(val, list):
            # Single-element list: unwrap
            if len(val) == 1:
                return float(val[0]) if isinstance(val[0], (int, float, bool)) else _SKIP
            # Multi-element list (e.g. vector→float): extract first component
            first = val[0] if val else 0.0
            if isinstance(first, (int, float, bool)):
                return float(first)
            return _SKIP
        return _SKIP

    # --- Vector/Rotation socket ---
    if _is_vector_type(bl_id):
        dim = 2 if _is_2d_vector_type(bl_id) else 3
        if val is None:
            return [0.0] * dim
        if isinstance(val, (int, float, bool)):
            f = float(val)
            return [f] * dim
        if isinstance(val, (list, tuple)):
            comps = [float(v) if isinstance(v, (int, float, bool)) else 0.0 for v in val]
            if len(comps) >= dim:
                return comps[:dim]
            return comps + [0.0] * (dim - len(comps))
        return [0.0] * dim

    # --- Color/RGBA socket ---
    if _is_color_type(bl_id):
        if val is None:
            return [0.0, 0.0, 0.0, 1.0]
        if isinstance(val, (int, float, bool)):
            f = float(val)
            return [f, f, f, 1.0]
        if isinstance(val, (list, tuple)):
            comps = [float(v) if isinstance(v, (int, float, bool)) else 0.0 for v in val]
            if len(comps) == 3:
                return comps + [1.0]
            if len(comps) >= 4:
                return comps[:4]
            # Pad shorter lists
            return comps + [0.0] * (4 - len(comps))
        return [0.0, 0.0, 0.0, 1.0]

    # --- String socket ---
    if _is_string_type(bl_id):
        if val is None:
            return _SKIP  # Can't assign None to string
        if isinstance(val, str):
            return val
        return str(val)

    return _SKIP


def _apply_default_values(data: dict, node_map: dict, zone_socket_remap: dict,
                          tracker: ImportErrorTracker) -> list[tuple]:
    """Set default_value and hide on all sockets for all nodes.

    Returns a list of ``(socket, raw_value, serialized_bl_id, ctx)``
    tuples for deferred string defaults that must be retried after
    wiring (when Blender has propagated the correct socket types from
    the referenced group's interface).
    """
    deferred_string_defaults: list[tuple] = []

    for node_data in data["nodes"]:
        node_name = node_data.get("name")
        if node_name not in node_map:
            continue
        node = node_map[node_name]
        is_dynamic = node.bl_idname in _VOLATILE_TYPES

        # NodeReroute has multi-type sockets whose type depends on
        # connections; at import time no connections exist yet, so the
        # socket type and default_value are meaningless.  Skip entirely.
        if node.bl_idname == "NodeReroute":
            continue

        for inp_data in node_data.get("inputs", []):
            sid = inp_data.get("identifier")
            sname = inp_data.get("name")

            if node.name + "_IN" in zone_socket_remap:
                remapped = zone_socket_remap[node.name + "_IN"].get(sid)
                if remapped:
                    sid = remapped

            sock = find_robust_socket(node, node.inputs, sid, sname, inp_data.get("type"), dynamic_hint=is_dynamic)

            if sock:
                if "hide" in inp_data and hasattr(sock, "hide"):
                    sock.hide = inp_data["hide"]
                if "default_value" in inp_data and hasattr(sock, "default_value"):
                    # Use the ACTUAL socket's bl_idname as primary type hint,
                    # because the interface rebuild may have changed the socket
                    # type (e.g. NodeSocketMatrix → NodeSocketInt in Blender 5.0).
                    bl_id = getattr(sock, 'bl_idname', '') or ''
                    serialized_bl_id = inp_data.get("bl_idname", "")
                    type_hint = bl_id or serialized_bl_id or inp_data.get("type") or "VALUE"
                    raw_val = inp_data["default_value"]
                    ctx = f"Node '{node.name}' input '{sname}'"

                    # When the serialized type is String but the socket
                    # runtime type is NOT String, the socket type is
                    # likely incorrect due to the referenced group not
                    # being fully reconstructed yet.  Try direct string
                    # assignment first; if that fails, defer to post-wiring
                    # rather than coercing to a wrong type.
                    if (isinstance(raw_val, str)
                            and _is_string_type(serialized_bl_id)
                            and not _is_string_type(bl_id)):
                        try:
                            sock.default_value = raw_val
                            continue
                        except (TypeError, AttributeError, ValueError, RuntimeError):
                            # Direct assignment failed — defer for post-wiring
                            deferred_string_defaults.append((sock, raw_val, serialized_bl_id, ctx))
                            continue

                    # When the runtime socket is NodeSocketMenu but the
                    # serialized value is not a string (int/bool/list),
                    # the socket type changed after export.  We can't
                    # resolve the enum identifier from a numeric value,
                    # so skip and let Step 6 handle it after Blender has
                    # fully populated enum_items.
                    if bl_id == 'NodeSocketMenu' and not isinstance(raw_val, str):
                        # Defer to Step 6 (_final_menu_defaults_pass)
                        # which operates on interface-level Menu sockets
                        # after all wiring is complete.
                        continue

                    try:
                        sock.default_value = unclean_value(raw_val, type_hint, context=ctx)
                    except (TypeError, AttributeError, ValueError, RuntimeError) as exc:
                        # Primary conversion failed — try aggressive secondary
                        # coercion based on the socket's ACTUAL bl_idname.
                        exc_str = str(exc)
                        coerced = _coerce_to_socket_type(raw_val, sock, bl_id)
                        if coerced is not _SKIP:
                            try:
                                sock.default_value = coerced
                            except (TypeError, AttributeError, ValueError, RuntimeError) as exc2:
                                tracker.record(
                                    f"Node '{node.name}' socket '{sname}': "
                                    f"could not set default_value (primary: {exc}; "
                                    f"secondary: {exc2})",
                                    level="WARN",
                                )
                                print(f"[DEFAULT_VALUE] Node '{node.name}' socket '{sname}': "
                                      f"kept Blender default (assignment failed: {exc2})")
                        else:
                            # Expected skip — socket type has no meaningful
                            # scalar default_value (e.g. Geometry, Object) or
                            # the value was None/internally non-assignable.
                            # Only log at DEBUG to reduce noise.
                            tracker.record(
                                f"Node '{node.name}' socket '{sname}': "
                                f"skipped default_value assignment ({exc})",
                                level="DEBUG",
                            )
            else:
                # Socket not found — value will remain at Blender's default
                if "default_value" in inp_data:
                    tracker.record(
                        f"Node '{node.name}': input socket '{sname}' (id={sid}) not found, "
                        f"default_value not restored",
                        level="WARN",
                    )
                    print(f"[DEFAULT_VALUE] Node '{node.name}': input socket '{sname}' "
                          f"(id={sid}) not found — default_value kept at Blender default")

        for out_data in node_data.get("outputs", []):
            sid = out_data.get("identifier")
            sname = out_data.get("name")

            if node.name + "_OUT" in zone_socket_remap:
                remapped = zone_socket_remap[node.name + "_OUT"].get(sid)
                if remapped:
                    sid = remapped

            sock = find_robust_socket(node, node.outputs, sid, sname, out_data.get("type"), dynamic_hint=is_dynamic)
            if sock:
                if "hide" in out_data and hasattr(sock, "hide"):
                    sock.hide = out_data["hide"]
            else:
                if "default_value" in out_data:
                    tracker.record(
                        f"Node '{node.name}': output socket '{sname}' (id={sid}) not found",
                        level="WARN",
                    )
                    print(f"[DEFAULT_VALUE] Node '{node.name}': output socket '{sname}' "
                          f"(id={sid}) not found")

    return deferred_string_defaults


# ---------------------------------------------------------------------------
# Step 3 helper: Wire links
# ---------------------------------------------------------------------------

def _wire_links(ng, data: dict, node_map: dict, interface_map: dict,
                group_interface_maps: dict | None, zone_socket_remap: dict,
                tracker: ImportErrorTracker) -> None:
    """Create all links between nodes in the tree."""
    for link_data in data.get("links", []):
        from_node = node_map.get(link_data["from_node"])
        to_node = node_map.get(link_data["to_node"])

        if not (from_node and to_node):
            continue

        from_id = link_data["from_socket_id"]
        from_name = link_data.get("from_socket_name", from_id)
        to_id = link_data["to_socket_id"]
        to_name = link_data.get("to_socket_name", to_id)

        from_expected_type = next(
            (s.get("type") for nd in data["nodes"] if nd["name"] == link_data["from_node"]
             for s in nd.get("outputs", []) if s.get("identifier") == from_id),
            None,
        )
        to_expected_type = next(
            (s.get("type") for nd in data["nodes"] if nd["name"] == link_data["to_node"]
             for s in nd.get("inputs", []) if s.get("identifier") == to_id),
            None,
        )

        # 1. Group Input/Output Remap
        if from_node.type == 'GROUP_INPUT':
            from_id = interface_map.get(from_id, from_id)
        elif from_node.bl_idname == 'GeometryNodeGroup' and getattr(from_node, "node_tree", None) and group_interface_maps is not None:
            ref_name = from_node.node_tree.name
            if ref_name in group_interface_maps:
                from_id = group_interface_maps[ref_name].get(from_id, from_id)

        if to_node.type == 'GROUP_OUTPUT':
            to_id = interface_map.get(to_id, to_id)
        elif to_node.bl_idname == 'GeometryNodeGroup' and getattr(to_node, "node_tree", None) and group_interface_maps is not None:
            ref_name = to_node.node_tree.name
            if ref_name in group_interface_maps:
                to_id = group_interface_maps[ref_name].get(to_id, to_id)

        # 2. Dynamic Status
        from_is_dynamic = from_node.bl_idname in _VOLATILE_TYPES
        to_is_dynamic = to_node.bl_idname in _VOLATILE_TYPES

        # 3. Post-Creation Sequential Remap
        if from_node.name + "_OUT" in zone_socket_remap:
            remapped = zone_socket_remap[from_node.name + "_OUT"].get(from_id)
            if remapped:
                from_id = remapped

        if to_node.name + "_IN" in zone_socket_remap:
            remapped = zone_socket_remap[to_node.name + "_IN"].get(to_id)
            if remapped:
                to_id = remapped

        from_sock = find_robust_socket(
            from_node, from_node.outputs, from_id, from_name,
            from_expected_type, dynamic_hint=from_is_dynamic,
        )

        if to_node.bl_idname == "GeometryNodeViewer" and to_name == "Geometry":
            to_sock = next(
                (s for s in to_node.inputs if s.name == "Geometry" and s.type == 'GEOMETRY'), None
            )
        else:
            to_sock = find_robust_socket(
                to_node, to_node.inputs, to_id, to_name,
                to_expected_type, dynamic_hint=to_is_dynamic,
            )

        if from_sock and to_sock:
            try:
                ng.links.new(from_sock, to_sock)
            except Exception as e:
                tracker.record(
                    f"Failed to link {from_node.name}.{from_name} -> {to_node.name}.{to_name}: {e}",
                    level="WARN",
                )
        else:
            tracker.record(
                f"Socket not found: {from_node.name}.{from_name} -> {to_node.name}.{to_name}",
                level="WARN",
            )


# ---------------------------------------------------------------------------
# Step 4 helper: Populate enum_items from internal nodes
# ---------------------------------------------------------------------------

def _populate_enum_items_from_internal(ng, interface_item, interface_identifier: str,
                                       tracker: ImportErrorTracker) -> bool:
    """Populate enum_items on an interface Menu socket from internal nodes.

    When the JSON has no enum_items data for a Menu socket (because the
    serializer couldn't capture them), this function searches the node
    tree for a MenuSwitch node connected to the Group Input's socket
    that corresponds to this interface item.

    After wiring (Step 3), Blender populates enum_items on MenuSwitch
    nodes, so we can copy them to the interface item.

    Returns True if enum_items were successfully populated.
    """
    group_input = next((n for n in ng.nodes if n.bl_idname == 'NodeGroupInput'), None)
    if not group_input:
        return False

    # Find the Group Input socket corresponding to this interface item
    gi_socket = None
    for sock in group_input.outputs:
        if getattr(sock, 'identifier', '') == interface_identifier:
            gi_socket = sock
            break

    if gi_socket is None:
        # Fallback: match by name
        item_name = getattr(interface_item, 'name', '')
        for sock in group_input.outputs:
            if sock.name == item_name and hasattr(sock, 'bl_idname') and 'Menu' in sock.bl_idname:
                gi_socket = sock
                break

    if gi_socket is None:
        return False

    # Follow links from Group Input to find connected MenuSwitch nodes
    for link in ng.links:
        if link.from_socket != gi_socket:
            continue
        target_node = link.to_node

        # Direct connection to MenuSwitch
        if target_node.bl_idname == "GeometryNodeMenuSwitch" and hasattr(target_node, 'enum_items'):
            if len(target_node.enum_items) > 0:
                for ei in target_node.enum_items:
                    try:
                        interface_item.enum_items.new(
                            name=ei.name,
                            identifier=getattr(ei, 'identifier', ei.name),
                            description=getattr(ei, 'description', ''),
                        )
                    except Exception:
                        pass
                return len(interface_item.enum_items) > 0

        # Connection to a Group node — look at the referenced tree's
        # interface for Menu sockets with enum_items
        if target_node.bl_idname == "GeometryNodeGroup" and hasattr(target_node, 'node_tree') and target_node.node_tree:
            ref_tree = target_node.node_tree
            if hasattr(ref_tree, 'interface'):
                # Find the Group node's input socket that received the link
                target_sock = link.to_socket
                target_sock_name = getattr(target_sock, 'name', '')
                for ref_item in ref_tree.interface.items_tree:
                    if (ref_item.item_type == 'SOCKET'
                            and getattr(ref_item, 'socket_type', '') == 'NodeSocketMenu'
                            and hasattr(ref_item, 'enum_items')
                            and len(ref_item.enum_items) > 0
                            and getattr(ref_item, 'name', '') == target_sock_name):
                        for ei in ref_item.enum_items:
                            try:
                                interface_item.enum_items.new(
                                    name=ei.name,
                                    identifier=getattr(ei, 'identifier', ei.name),
                                    description=getattr(ei, 'description', ''),
                                )
                            except Exception:
                                pass
                        return len(interface_item.enum_items) > 0

    return False


# ---------------------------------------------------------------------------
# Step 4 helper: Post-sync interface defaults
# ---------------------------------------------------------------------------

def _post_sync_interface(ng, data: dict, interface_map: dict,
                         tracker: ImportErrorTracker) -> None:
    """Re-apply default values and UI properties to interface items after wiring.

    After connecting cables, Blender resets interface menus due to a
    latency bug.  This step re-applies the values using translated IDs.

    For Menu sockets, enum_items may have been cleared by Blender's
    interface update — this function re-creates them before setting
    the default_value.

    Also re-applies menu_expanded and optional properties that Blender
    may have reset during the wiring phase.
    """
    use_interface_api = hasattr(ng, 'interface')
    if not (use_interface_api and "interface_items" in data):
        return

    # Build legacy_enums for Menu sockets (same logic as _rebuild_interface)
    legacy_enums = {
        inp['identifier']: inp.get('enum_items')
        for inp in data.get('inputs', [])
        if inp.get('socket_type') == 'NodeSocketMenu'
    }

    for i_data in data["interface_items"]:
        if i_data.get("item_type") != 'SOCKET':
            continue

        old_item_id = i_data.get("identifier", "")
        new_item_id = interface_map.get(old_item_id, old_item_id)

        props = i_data.get("properties", {})
        default_val = props.get("default_value")
        if default_val is None:
            default_val = i_data.get("default_value")

        if not new_item_id:
            continue

        try:
            item = next(
                (x for x in ng.interface.items_tree if getattr(x, 'identifier', '') == new_item_id),
                None,
            )
            if not item:
                continue

            is_menu = i_data.get("socket_type", "") == 'NodeSocketMenu'

            # --- Menu socket handling ---
            if is_menu:
                # Re-create enum_items if they were cleared by Blender
                if hasattr(item, 'enum_items') and len(item.enum_items) == 0:
                    items_source = i_data.get("enum_items")
                    if not items_source:
                        items_source = legacy_enums.get(old_item_id)
                    if items_source:
                        _reset_collection(item.enum_items, tracker)
                        for ei in items_source:
                            try:
                                item_id = ei.get("identifier") or ei.get("name")
                                item.enum_items.new(
                                    name=ei.get("name", "Item"),
                                    identifier=item_id,
                                    description=ei.get("description", ""),
                                )
                            except Exception as e:
                                tracker.record(f"Post-sync enum item: {e}")

                    # Fallback: if enum_items are still empty (no JSON data),
                    # try to find them from internal MenuSwitch nodes in the
                    # tree that are connected to this interface socket via
                    # the Group Input node.  After wiring, these nodes have
                    # their enum_items populated by Blender.
                    if len(item.enum_items) == 0:
                        _populate_enum_items_from_internal(
                            ng, item, new_item_id, tracker,
                        )

                # Re-apply default_value for Menu sockets
                if default_val is not None and hasattr(item, 'default_value') and str(default_val) != "":
                    try:
                        item.default_value = str(default_val)
                    except Exception as menu_exc:
                        tracker.record(
                            f"Post-sync: could not set default_value on menu item "
                            f"'{i_data.get('name')}': {menu_exc} "
                            f"(enum_items={[e.identifier for e in item.enum_items] if hasattr(item,'enum_items') else 'N/A'})",
                            level="DEBUG",
                        )

                # Re-apply menu_expanded after enum_items are populated
                menu_expanded = props.get("menu_expanded")
                if menu_expanded is not None and hasattr(item, 'menu_expanded'):
                    try:
                        item.menu_expanded = menu_expanded
                    except:
                        pass

                # Re-apply optional toggle
                optional = props.get("optional")
                if optional is not None and hasattr(item, 'optional'):
                    try:
                        item.optional = optional
                    except:
                        pass

            # --- Non-menu socket: apply default_value with coercion ---
            elif default_val is not None and hasattr(item, 'default_value'):
                type_hint = i_data.get("bl_socket_idname") or i_data.get("socket_type") or "VALUE"
                ctx = f"Post-sync interface '{i_data.get('name')}' default_value"
                try:
                    result = unclean_value(default_val, type_hint, context=ctx)
                    # Interface item default_value ALWAYS requires 3-component
                    # vectors, even for 2D sockets (NodeSocketVector2D).
                    if (_is_vector_type(type_hint)
                            and isinstance(result, (list, tuple))
                            and len(result) == 2):
                        result = list(result) + [0.0]
                    try:
                        item.default_value = result
                    except (TypeError, AttributeError, ValueError, RuntimeError) as vec_exc:
                        # Some 2D interface sockets genuinely require 2 components.
                        if (_is_vector_type(type_hint)
                                and isinstance(result, (list, tuple))
                                and len(result) == 3
                                and "2 items" in str(vec_exc)):
                            item.default_value = result[:2]
                        else:
                            raise
                except (TypeError, AttributeError, ValueError, RuntimeError) as exc:
                    # Try secondary coercion
                    socket_type = i_data.get("bl_socket_idname") or i_data.get("socket_type") or "VALUE"
                    coerced = _coerce_interface_value(default_val, socket_type)
                    if coerced is not _SKIP:
                        try:
                            item.default_value = coerced
                        except:
                            tracker.record(
                                f"Post-sync: could not set default_value on interface item "
                                f"'{i_data.get('name')}'",
                                level="DEBUG",
                            )
                    else:
                        tracker.record(
                            f"Post-sync: skipped default_value on interface item "
                            f"'{i_data.get('name')}' ({exc})",
                            level="DEBUG",
                        )

            # --- Also re-apply optional for non-Menu sockets ---
            if not is_menu:
                optional = props.get("optional")
                if optional is not None and hasattr(item, 'optional'):
                    try:
                        item.optional = optional
                    except:
                        pass

        except:
            tracker.record(
                f"Post-sync: error processing interface item '{i_data.get('name')}'",
                level="DEBUG",
            )


# ---------------------------------------------------------------------------
# Step 5 helper: Re-apply Group node defaults
# ---------------------------------------------------------------------------

def _reapply_group_node_defaults(data: dict, node_map: dict,
                                  tracker: ImportErrorTracker) -> None:
    """Re-apply default_value on Group node sockets after all other steps.

    After wiring (Step 3) and interface synchronisation (Step 4), Blender
    may have reset Group node socket default_values back to the interface
    defaults of the referenced subgroup.  This step re-applies the
    node-level defaults from the JSON data, ensuring that per-instance
    overrides are preserved.

    This is a targeted pass that ONLY processes GeometryNodeGroup nodes,
    and ONLY sets default_value (no other properties).
    """
    for node_data in data["nodes"]:
        node_name = node_data.get("name")
        if node_name not in node_map:
            continue
        node = node_map[node_name]

        # Only process Group nodes
        if node.bl_idname != "GeometryNodeGroup":
            continue
        # Skip nodes without a referenced tree
        if not getattr(node, 'node_tree', None):
            continue

        for inp_data in node_data.get("inputs", []):
            raw_val = inp_data.get("default_value")
            if raw_val is None:
                continue
            sname = inp_data.get("name", "")
            # Find the socket by name (most reliable for Group nodes
            # whose identifiers may have changed after node_tree assignment)
            sock = None
            for s in node.inputs:
                if s.name == sname:
                    sock = s
                    break
            if sock is None:
                continue
            if not hasattr(sock, 'default_value'):
                continue

            bl_id = getattr(sock, 'bl_idname', '') or ''
            ctx = f"Node '{node.name}' input '{sname}' (re-apply)"

            # Skip non-scalar sockets (Geometry, Object, etc.)
            if _is_non_scalar_type(bl_id):
                continue

            # Use the secondary coercion path which is safer and uses
            # the socket's ACTUAL bl_idname as ground truth.
            coerced = _coerce_to_socket_type(raw_val, sock, bl_id)
            if coerced is not _SKIP:
                try:
                    sock.default_value = coerced
                except (TypeError, AttributeError, ValueError, RuntimeError) as exc:
                    tracker.record(
                        f"{ctx}: could not re-apply default_value: {exc}",
                        level="DEBUG",
                    )


# ---------------------------------------------------------------------------
# Step 6 helper: Final Menu defaults verification
# ---------------------------------------------------------------------------

def _final_menu_defaults_pass(ng, data: dict, interface_map: dict,
                              tracker: ImportErrorTracker) -> None:
    """Final verification and repair pass for Menu socket default_values.

    After all other steps (including _post_sync_interface and
    _reapply_group_node_defaults), this pass ensures that every
    NodeSocketMenu interface item has its correct default_value set.
    This is a safety net for timing issues where Blender's interface
    updates prevented earlier attempts from succeeding.

    The pass also handles node-level Menu sockets (inputs/outputs on
    Group nodes) whose default_value might not have been restored
    by the earlier steps.
    """
    if not (hasattr(ng, 'interface') and "interface_items" in data):
        return

    for i_data in data["interface_items"]:
        if i_data.get("item_type") != 'SOCKET':
            continue
        if i_data.get("socket_type", "") != 'NodeSocketMenu':
            continue

        props = i_data.get("properties", {})
        default_val = props.get("default_value")
        if default_val is None:
            default_val = i_data.get("default_value")
        if default_val is None or str(default_val) == "":
            continue

        old_item_id = i_data.get("identifier", "")
        new_item_id = interface_map.get(old_item_id, old_item_id)
        if not new_item_id:
            continue

        item = next(
            (x for x in ng.interface.items_tree
             if getattr(x, 'identifier', '') == new_item_id),
            None,
        )
        if not item or not hasattr(item, 'default_value'):
            continue

        # Check if default_value is already correct
        current_val = getattr(item, 'default_value', None)
        if str(current_val) == str(default_val):
            continue

        # If enum_items are empty, try to populate them from internal
        # nodes (MenuSwitch or Group nodes connected to Group Input)
        if hasattr(item, 'enum_items') and len(item.enum_items) == 0:
            _populate_enum_items_from_internal(ng, item, new_item_id, tracker)

        # Now try to set default_value
        if hasattr(item, 'enum_items') and len(item.enum_items) > 0:
            try:
                item.default_value = str(default_val)
            except Exception as e:
                tracker.record(
                    f"Final pass: could not set default_value on Menu "
                    f"'{i_data.get('name')}' (value={default_val!r}): {e} "
                    f"(enum_items={[ei.identifier for ei in item.enum_items]})",
                    level="DEBUG",
                )

    # --- Also verify node-level Menu socket default_values ---
    # Group node inputs of type NodeSocketMenu may not have been
    # handled by _reapply_group_node_defaults because that function
    # uses _coerce_to_socket_type which returns _SKIP for Menu types.
    for node_data in data["nodes"]:
        node_name = node_data.get("name")
        if node_name is None:
            continue
        # Find the node in the tree by name
        node = ng.nodes.get(node_name)
        if node is None:
            continue

        for inp_data in node_data.get("inputs", []):
            raw_val = inp_data.get("default_value")
            if raw_val is None:
                continue
            sname = inp_data.get("name", "")
            sock = None
            for s in node.inputs:
                if s.name == sname:
                    sock = s
                    break
            if sock is None:
                continue
            bl_id = getattr(sock, 'bl_idname', '') or ''
            if bl_id != 'NodeSocketMenu':
                continue
            if not hasattr(sock, 'default_value'):
                continue

            # Try to set the string enum value on the Menu socket
            try:
                sock.default_value = str(raw_val)
            except (TypeError, AttributeError, ValueError, RuntimeError):
                # Socket might not have enum_items yet — skip silently
                pass


# ---------------------------------------------------------------------------
# Main import logic
# ---------------------------------------------------------------------------

def import_node_tree_recursive(
    data: dict,
    json_cache: dict,
    group_interface_maps: dict | None = None,
    context=None,
    tracker: ImportErrorTracker | None = None,
):
    """Reconstruct a node tree from its JSON representation.

    Parameters
    ----------
    data : dict
        Serialized node tree data (as produced by ``serialize_node_tree``).
    json_cache : dict
        Cache of all serialized trees (name → data) for dependency resolution.
    group_interface_maps : dict, optional
        Maps group names to their identifier remapping dicts.
    context : Blender context, optional
        Used for status bar updates.
    tracker : ImportErrorTracker, optional
        Error tracker for this import operation.  If *None*, a fresh one
        is created internally.
    """
    if tracker is None:
        tracker = ImportErrorTracker()
    if group_interface_maps is None:
        group_interface_maps = {}

    name = data["name"]

    if context:
        context.workspace.status_text_set(f"Rebuilding: {name}...")

    # Resolve dependencies
    for node_data in data["nodes"]:
        if node_data["type"] == "GeometryNodeGroup":
            ref_name = node_data.get("node_tree_reference")
            if ref_name and ref_name in json_cache and not bpy.data.node_groups.get(ref_name):
                import_node_tree_recursive(
                    json_cache[ref_name], json_cache, group_interface_maps, context, tracker
                )

    # Get or create the node tree
    ng = bpy.data.node_groups.get(name)
    if ng:
        for node in ng.nodes:
            ng.nodes.remove(node)
        if hasattr(ng, 'interface'):
            for item in ng.interface.items_tree:
                ng.interface.remove(item)
        else:
            ng.inputs.clear()
            ng.outputs.clear()
    else:
        ng = bpy.data.node_groups.new(name, 'GeometryNodeTree')

    # Apply tree-level properties
    for prop_name, prop_val in data.get("tree_properties", {}).items():
        try:
            ctx = f"Tree '{name}' property '{prop_name}'"
            setattr(ng, prop_name, unclean_value(prop_val, context=ctx))
        except (TypeError, AttributeError, ValueError, RuntimeError) as exc:
            tracker.record(f"Tree '{name}': could not set property '{prop_name}': {exc}", level="DEBUG")
            print(f"[DEFAULT_VALUE] Tree '{name}' property '{prop_name}': "
                  f"kept Blender default (assignment failed: {exc})")

    # --- Interface ---
    interface_map = {}
    _rebuild_interface(ng, data, interface_map, tracker)

    if group_interface_maps is not None:
        group_interface_maps[name] = interface_map

    # --- Step 1: Node creation ---
    node_map = {}
    processed_zone_nodes = set()
    zone_socket_remap = {}

    for node_data in data["nodes"]:
        node_name = node_data.get("name")
        node_type = node_data.get("type")
        new_node = None

        if node_type in ZONE_INPUTS and node_name not in processed_zone_nodes:
            new_node = _create_zone_nodes(
                ng, node_data, data, node_map, processed_zone_nodes, zone_socket_remap, tracker
            )

        elif node_name in node_map:
            new_node = node_map[node_name]
        else:
            try:
                new_node = ng.nodes.new(node_type)
            except RuntimeError as e:
                tracker.record(f"Failed to create node '{node_name}' (Type: {node_type}): {e}")
                continue
            except Exception as e:
                tracker.record(f"Unexpected error creating node '{node_name}': {e}", level="CRITICAL")
                continue
            new_node.name = node_name
            node_map[node_name] = new_node

        if new_node is None:
            continue

        # Type-specific configuration
        _configure_special_node(new_node, node_data, node_type, zone_socket_remap, tracker)

        # Location, label, and generic properties
        if "location" in node_data:
            new_node.location = node_data["location"]
        if "label" in node_data:
            new_node.label = node_data["label"]

        for prop_name, prop_val in node_data.get("properties", {}).items():
            try:
                ctx = f"Node '{new_node.name}' property '{prop_name}'"
                final_val = unclean_value(prop_val, context=ctx)
                if prop_name == 'color' and isinstance(final_val, (list, tuple)) and len(final_val) == 4:
                    final_val = list(final_val[:3])
                setattr(new_node, prop_name, final_val)
            except (TypeError, AttributeError, ValueError, RuntimeError) as exc:
                # If the value is a dict (serialized data-block), try to
                # resolve it directly via bpy.data and assign the result.
                if isinstance(prop_val, dict) and "name" in prop_val:
                    resolved = unclean_value(prop_val, context=ctx)
                    if resolved is not None and not isinstance(resolved, dict):
                        try:
                            setattr(new_node, prop_name, resolved)
                            continue
                        except (TypeError, AttributeError, ValueError, RuntimeError):
                            pass
                tracker.record(
                    f"Node '{new_node.name}': could not set property '{prop_name}': {exc}",
                    level="DEBUG",
                )

        if node_type == "GeometryNodeGroup":
            ref_name = node_data.get("node_tree_reference")
            if ref_name:
                ref_tree = bpy.data.node_groups.get(ref_name)
                if ref_tree:
                    new_node.node_tree = ref_tree

    # --- Step 2: Default values ---
    deferred_string_defaults = _apply_default_values(data, node_map, zone_socket_remap, tracker)

    # --- Step 3: Wiring ---
    _wire_links(ng, data, node_map, interface_map, group_interface_maps, zone_socket_remap, tracker)

    # --- Step 3.5: Retry deferred string defaults ---
    # After wiring, the referenced group's interface is fully built and
    # Blender may have propagated the correct socket types to Group nodes.
    # We retry the string defaults by looking up the socket FRESH from the
    # node (not using the old socket reference, which may have been
    # invalidated by interface updates).
    #
    # IMPORTANT: We do NOT unset/re-set node_tree to force a refresh,
    # because node.node_tree = None destroys all sockets and invalidates
    # any connected links, which can cause Blender to crash (segfault)
    # when the C-level link pointers become dangling.
    for sock, raw_val, serialized_bl_id, ctx in deferred_string_defaults:
        # The saved 'sock' reference may be stale after wiring (Blender
        # may have rebuilt the node's sockets).  Re-lookup by node+name.
        node = getattr(sock, 'node', None) if sock is not None else None
        if node is None:
            continue

        # Find the socket fresh from the node's current inputs
        sock_name = getattr(sock, 'name', '')
        fresh_sock = None
        for s in node.inputs:
            if s.name == sock_name:
                fresh_sock = s
                break
        if fresh_sock is None:
            tracker.record(
                f"{ctx}: deferred string default skipped (socket no longer exists)",
                level="DEBUG",
            )
            continue

        current_bl_id = getattr(fresh_sock, 'bl_idname', '') or ''

        if _is_string_type(current_bl_id):
            # Socket type has been corrected — assign the string value
            try:
                fresh_sock.default_value = raw_val
            except (TypeError, AttributeError, ValueError, RuntimeError) as exc:
                tracker.record(
                    f"{ctx}: deferred string default could not be set "
                    f"(runtime type={current_bl_id}): {exc}",
                    level="DEBUG",
                )
        else:
            # Socket type is still NOT String.  The serialized type was
            # String but Blender gave us a different type (e.g.
            # NodeSocketFloatFactor, NodeSocketInt, NodeSocketBool).
            # Direct string assignment would crash (ACCESS_VIOLATION),
            # but we can try to COERCE the value to the runtime type.
            # This handles cases like serialized string "1" that should
            # become int 1 or float 1.0 on the actual socket.
            coerced = _coerce_to_socket_type(raw_val, fresh_sock, current_bl_id)
            if coerced is not _SKIP:
                try:
                    fresh_sock.default_value = coerced
                    tracker.record(
                        f"{ctx}: deferred string default coerced from "
                        f"string to {type(coerced).__name__} for runtime "
                        f"type {current_bl_id}",
                        level="DEBUG",
                    )
                except (TypeError, AttributeError, ValueError, RuntimeError) as exc:
                    tracker.record(
                        f"{ctx}: deferred string default could not be set "
                        f"(coerced to {type(coerced).__name__}, "
                        f"runtime type={current_bl_id}): {exc}",
                        level="DEBUG",
                    )
            else:
                # Value is genuinely non-coercible (e.g. "Knot" → int)
                tracker.record(
                    f"{ctx}: deferred string default skipped — "
                    f"socket type is {current_bl_id}, value cannot be coerced "
                    f"from string",
                    level="DEBUG",
                )

    # --- Step 4: Post-synchronisation of interface ---
    _post_sync_interface(ng, data, interface_map, tracker)

    # --- Step 5: Re-apply Group node default values ---
    # After wiring and interface synchronisation, Blender may have reset
    # Group node socket default_values back to the interface defaults of
    # the referenced subgroup.  This step re-applies the node-level
    # defaults from the JSON data, ensuring that per-instance overrides
    # (like Self Side = 1 instead of the interface default 0) are
    # preserved.
    _reapply_group_node_defaults(data, node_map, tracker)

    # --- Step 6: Final Menu defaults verification ---
    # Safety net for NodeSocketMenu default_values that weren't set by
    # earlier steps due to Blender's interface timing issues.  This pass
    # verifies all Menu interface items and node-level Menu sockets,
    # and attempts to set any remaining default_values.
    _final_menu_defaults_pass(ng, data, interface_map, tracker)

    print(f"[OK] Reconstruction of node '{name}' completed.")
    return ng
