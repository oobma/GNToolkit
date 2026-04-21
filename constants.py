# -*- coding: utf-8 -*-
"""
gn_toolkit.constants — All constants and immutable sets for the addon.
"""

ADDON_VERSION = "0.1.4"

# Node types whose socket identifiers are volatile (recycled by Blender at runtime).
_VOLATILE_TYPES: frozenset[str] = frozenset({
    "GeometryNodeRepeatInput", "GeometryNodeRepeatOutput",
    "GeometryNodeSimulationInput", "GeometryNodeSimulationOutput",
    "GeometryNodeForeachGeometryElementInput", "GeometryNodeForeachGeometryElementOutput",
    "NodeClosureInput", "NodeClosureOutput", "NodeEvaluateClosure",
    "GeometryNodeViewer", "NodeCombineBundle", "NodeSeparateBundle",
    "GeometryNodeCombineBundle", "GeometryNodeSeparateBundle",
    "GeometryNodeCaptureAttribute", "GeometryNodeMenuSwitch", "GeometryNodeIndexSwitch",
})

# Zone input node types (each one pairs with a corresponding output).
ZONE_INPUTS: frozenset[str] = frozenset({
    'GeometryNodeRepeatInput',
    'GeometryNodeSimulationInput',
    'GeometryNodeForeachGeometryElementInput',
    'NodeClosureInput',
})

# Node properties to skip during serialization (handled separately or volatile).
NODE_PROPS_TO_SKIP: frozenset[str] = frozenset({
    'name', 'label', 'location', 'type', 'inputs', 'outputs', 'node_tree',
    'repeat_items', 'simulation_items', 'state_items', 'input_items', 'main_items',
    'generation_items', 'capture_items', 'active_item', 'parent', 'menu_items',
    'enum_items', 'index_switch_items',
    # Read-only / non-serializable properties
    'asset_data', 'is_preview',
})

# Node-tree-level properties to skip during serialization.
TREE_PROPS_TO_SKIP: frozenset[str] = frozenset({
    'name', 'type', 'rna_type', 'library', 'tag', 'is_embedded_data',
    'is_embedded_id', 'users', 'parent', 'nodes', 'links', 'inputs', 'outputs',
    'interface',
    # Read-only / non-serializable properties
    'asset_data', 'is_preview',
    # Annotation data-blocks are viewport-only, cannot be serialized
    'annotation',
    # Node tool identifier — Blender 4.3+, not portable between versions
    'node_tool_idname',
})

# Interface item properties to skip when serializing bl_rna properties.
INTERFACE_SKIP_PROPS: frozenset[str] = frozenset({
    'name', 'item_type', 'in_out', 'socket_type', 'bl_socket_idname',
    'identifier', 'parent', 'rna_type', 'enum_items',
    # Vector/Rotation socket-specific properties (Blender 5.0+)
    'is_inspect_output', 'is_panel_toggle', 'layer_selection_field',
    'structure_type', 'dimensions',
    # NOTE: 'menu_expanded' and 'optional_label' were previously skipped,
    # but they must be serialized so that Menu socket expansion state
    # and the Optional label toggle are preserved across export/import.
    # UI-only state — not meaningful to serialize
    'select',
})

# Mapping from short socket type names to full Blender socket class names.
SOCKET_TYPE_MAP: dict[str, str] = {
    "FLOAT": "NodeSocketFloat",
    "INT": "NodeSocketInt",
    "BOOLEAN": "NodeSocketBool",
    "VECTOR": "NodeSocketVector",
    "RGBA": "NodeSocketColor",
    "GEOMETRY": "NodeSocketGeometry",
}

# Remap table for interface.new_socket(socket_type=...).
# Some bl_socket_idname values are not accepted directly by
# ng.interface.new_socket() and must be mapped to the canonical
# identifier.  In Blender 4.x/5.x, new_socket silently creates a
# fallback type (usually Int or Float) instead of raising an error
# when given an unrecognised socket_type.
#
# NOTE: NodeSocketMatrix IS supported by new_socket() in Blender
# 4.3+.  Do NOT remap it to NodeSocketFloat — that causes cascading
# type mismatches for all Matrix sockets.  Similarly, the 2D vector
# variants are handled explicitly in _rebuild_interface (create as
# NodeSocketVector then set dimensions=2), so they don't need to be
# here unless new_socket() rejects them outright.
INTERFACE_SOCKET_TYPE_REMAP: dict[str, str] = {
    # 2D vector variants → create as standard Vector, then set dimensions=2
    # (These are also handled explicitly in _rebuild_interface, so the
    # remap is a safety net in case the explicit check is bypassed.)
    "NodeSocketVector2D": "NodeSocketVector",
    "NodeSocketVectorTranslation2D": "NodeSocketVector",
}

# Optional socket properties that may exist on interface items.
OPTIONAL_SOCKET_PROPS: tuple[str, ...] = (
    'min_value', 'max_value', 'description', 'subtype', 'hide_value',
    'hide_in_modifier', 'default_attribute_name', 'attribute_domain',
    'default_input', 'force_non_field',
    # Blender 5.0+ interface socket properties:
    'menu_expanded',
    # The 'optional' toggle ("Optional" label in the UI) is exposed as
    # 'optional' in the Blender 5.0+ Python API.  We also keep
    # 'optional_label' as a fallback in case the name differs across
    # versions.
    'optional', 'optional_label',
)

# Properties that are already handled explicitly and should be skipped in the
# generic property-application loop during import.
#
# NOTE: menu_expanded and optional are NOT in this set because they may only
# exist in i_data["properties"] (serialized via bl_rna.properties) and not
# through the OPTIONAL_SOCKET_PROPS path.  The generic loop needs to be able
# to set them when they're present.  The OPTIONAL_SOCKET_PROPS loop runs
# first and will set them if found in props; the generic loop then skips
# them because they've already been set (the same value is in props).
# However, if they're ONLY in the generic props dict, the generic loop
# must not skip them.
EXPLICITLY_HANDLED_PROPS: frozenset[str] = frozenset({
    "subtype", "min_value", "max_value", "default_attribute_name",
    "attribute_domain", "default_input", "description", "hide_value",
    "hide_in_modifier", "force_non_field", "default_value",
    # Vector/Rotation socket-specific properties (Blender 5.0+)
    "is_inspect_output", "is_panel_toggle", "layer_selection_field",
    "structure_type", "dimensions",
    # Boolean metadata properties — must NOT be passed through unclean_value
    # with the socket's data type (e.g. menu_expanded=False on a Color socket
    # would be coerced to [0,0,0,1]).  They are handled by the OPTIONAL_SOCKET_PROPS
    # loop (direct setattr) and must be skipped in the generic unclean_value loop.
    "menu_expanded", "optional", "optional_label", "select",
})
