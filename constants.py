# -*- coding: utf-8 -*-
"""
gn_toolkit.constants — All constants and immutable sets for the addon.
"""

# Canonical addon version — single source of truth.
# Reflected in bl_info (__init__.py), exported JSON files, the sidecar
# metadata file, and UI labels.
ADDON_VERSION = "0.3.2"

# Marker written to unified JSON packages. Written for user inspection
# only; importers never read it.
PACKAGE_EXPORT_METHOD = "GN_TOOLKIT"

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

# Zone output node types. Zone pairs must be created together via
# bpy.ops.node.add_zone (the only API that establishes the pairing).
ZONE_OUTPUTS: frozenset[str] = frozenset({
    'GeometryNodeRepeatOutput',
    'GeometryNodeSimulationOutput',
    'GeometryNodeForeachGeometryElementOutput',
    'NodeClosureOutput',
})

# ---------------------------------------------------------------------------
# Declarative exclusion tables (M1, 2026-10-10) — rationale as data.
#
# Every "not serialized" decision lives here with its reason.  The frozensets
# below are DERIVED from these tables: the member set and its justification
# cannot drift apart, and adding an omission is a one-line diff with a
# mandatory rationale (checked by tests/test_exclusions_rationale.py).
# ---------------------------------------------------------------------------

EXCLUSIONS: dict[str, dict[str, str]] = {
    "node": {
        "name": "Identity anchor: nodes are keyed by name in the JSON.",
        "label": "Presentation: editor label.",
        "location": "Presentation: relative editor position; the absolute "
                    "position is serialized via location_absolute.",
        "width": "Presentation: editor width.",
        "type": "bl_idname is serialized under the canonical 'type' key.",
        "inputs": "Handled by the dedicated socket serializer "
                  "(names, identifiers, defaults).",
        "outputs": "Handled by the dedicated socket serializer.",
        "node_tree": "Group references are serialized as name-based "
                     "node_tree_reference.",
        "repeat_items": "Zone item collection handled by its dedicated "
                        "zone dispatcher.",
        "simulation_items": "Zone item collection handled by its dedicated "
                            "zone dispatcher.",
        "state_items": "Zone item collection handled by its dedicated "
                       "zone dispatcher.",
        "input_items": "Zone item collection handled by its dedicated "
                       "zone dispatcher.",
        "main_items": "Zone item collection handled by its dedicated "
                      "zone dispatcher.",
        "generation_items": "Zone item collection handled by its dedicated "
                            "zone dispatcher.",
        "capture_items": "Capture Attribute item collection handled by its "
                         "dedicated dispatcher.",
        "active_item": "Runtime UI state (active item index).",
        "parent": "Frame membership is layout-only (frames are not "
                  "serialized).",
        "menu_items": "MenuSwitch item collection handled by its dedicated "
                      "dispatcher.",
        "enum_items": "MenuSwitch enum collection handled by its dedicated "
                      "dispatcher.",
        "index_switch_items": "IndexSwitch item collection handled by its "
                              "dedicated dispatcher.",
        "list_items": "List node item collection handled by its dedicated "
                      "dispatcher.",
        "asset_data": "Asset-library metadata, not tree content.",
        "is_preview": "Runtime preview state.",
        "rna_type": "RNA introspection marker.",
    },
    "interface": {
        "name": "Structural key written by the interface serializer itself.",
        "item_type": "Structural key (SOCKET/PANEL); written as its own field.",
        "in_out": "Structural key (INPUT/OUTPUT).",
        "socket_type": "Structural key; written as the socket_type field.",
        "bl_socket_idname": "Structural key; written as bl_socket_idname.",
        "identifier": "Volatile identifier (renumbered on rebuild); items "
                      "are matched by name.",
        "parent": "Panel hierarchy is reconstructed from item order/type.",
        "rna_type": "RNA introspection marker.",
        "enum_items": "Menu items are harvested per item by the dedicated "
                      "enum serializer.",
        "is_inspect_output": "Blender 5.0+ derived/UI-specific socket state.",
        "layer_selection_field": "Blender 5.0+ derived/UI-specific socket "
                                 "state.",
        "dimensions": "Vector dimension is conveyed by the socket class/"
                      "remap (2D variants).",
        "select": "Editor selection state.",
    },
    "tree": {
        "name": "Identity anchor: the tree name keys the JSON/package.",
        "type": "Tree class; constant for the supported GeometryNodeTree.",
        "rna_type": "RNA introspection marker.",
        "library": "Datablock linkage, not content.",
        "tag": "Runtime invalidation flag.",
        "is_embedded_data": "Datablock management flag (embedded trees).",
        "is_embedded_id": "Datablock management flag.",
        "users": "Runtime reference count.",
        "parent": "Library parenting, runtime.",
        "nodes": "Serialized by its dedicated section.",
        "links": "Serialized by its dedicated section.",
        "inputs": "Serialized by its dedicated section.",
        "outputs": "Serialized by its dedicated section.",
        "interface": "Serialized by its dedicated section.",
        "asset_data": "Asset-library metadata, not tree content.",
        "is_preview": "Runtime preview state.",
        "annotation": "Viewport-only annotation data (cannot be serialized).",
        "node_tool_idname": "Node tool identifier — Blender 4.3+, not "
                            "portable between versions.",
        "use_fake_user": "File-management flag owned by the .blend; the "
                         "importer forces a fake user so imported groups "
                         "survive save/reload.",
        "use_extra_user": "File-management flag owned by the .blend (same "
                          "reason as use_fake_user).",
    },
}

# Historical reinstatements (kept for reviewers): 'is_panel_toggle' and
# 'structure_type' were previously skipped from the interface, but they
# change how the modifier UI presents the socket and are restored on
# import; 'menu_expanded' and 'optional_label' must be serialized so the
# Menu socket expansion state and the Optional label toggle survive.
INTERFACE_SKIP_PROPS: frozenset[str] = frozenset(EXCLUSIONS["interface"])

# Node properties to skip during serialization (table above).
NODE_PROPS_TO_SKIP: frozenset[str] = frozenset(EXCLUSIONS["node"])

# Node-tree-level properties to skip during serialization (table above).
TREE_PROPS_TO_SKIP: frozenset[str] = frozenset(EXCLUSIONS["tree"])

# Readonly embedded structs whose interior IS serialized (allowlist), with
# rationale per entry (M1).  Nested structs inside an allowed type
# (ColorRampElement, CurveMap, CurveMapPoint, ...) dump freely.
# `paired_output` is deliberately absent: it points to a Node (not an owned
# struct) and dumping it would serialize a whole nested node.
STRUCT_DUMP_TYPES: dict[str, str] = {
    "ColorRamp": "Readonly embedded struct; stops/colors/interpolation "
                 "are semantic (audit R3b).",
    "CurveMapping": "Readonly embedded struct; tone-curve points are "
                    "semantic.",
    "CurveProfile": "Readonly embedded struct; profile points are semantic.",
}

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
    # Float subtypes → create as NodeSocketFloat, then set subtype via property
    # Blender 5.1 interface.new_socket() does not accept these directly
    "NodeSocketFloatAngle": "NodeSocketFloat",
    "NodeSocketFloatDistance": "NodeSocketFloat",
    "NodeSocketFloatFactor": "NodeSocketFloat",
    "NodeSocketFloatPercentage": "NodeSocketFloat",
    "NodeSocketFloatTime": "NodeSocketFloat",
    "NodeSocketFloatTimeAbsolute": "NodeSocketFloat",
    "NodeSocketFloatFrequency": "NodeSocketFloat",
    "NodeSocketFloatMass": "NodeSocketFloat",
    "NodeSocketFloatPixel": "NodeSocketFloat",
    "NodeSocketFloatColorTemperature": "NodeSocketFloat",
    "NodeSocketFloatWavelength": "NodeSocketFloat",
    "NodeSocketFloatTranslation": "NodeSocketFloat",
    # Int subtypes → create as NodeSocketInt, then set subtype
    "NodeSocketIntPercentage": "NodeSocketInt",
    "NodeSocketIntFactor": "NodeSocketInt",
    "NodeSocketIntPixel": "NodeSocketInt",
    # String subtypes → create as NodeSocketString, then set subtype
    "NodeSocketStringFilePath": "NodeSocketString",
    # Vector subtypes → create as NodeSocketVector, then set subtype
    "NodeSocketVectorAcceleration": "NodeSocketVector",
    "NodeSocketVectorDirection": "NodeSocketVector",
    "NodeSocketVectorEuler": "NodeSocketVector",
    "NodeSocketVectorXYZ": "NodeSocketVector",
    "NodeSocketVectorTranslation": "NodeSocketVector",
    "NodeSocketVectorVelocity": "NodeSocketVector",
}

# Interface socket variants (Blender 5.x).  ``bl_socket_idname`` values are
# the interface socket class names (NodeSocketVectorFactor2D,
# NodeSocketIntVector2D, ...).  interface.new_socket() accepts only the 18
# base types, so the importer decomposes every serialized name into
# (base type, dimensions, subtype) and applies them in that order.
#
# Complete matrix as of Blender 5.2 (verified against bpy.types and the
# official API docs; see docs/port-5.2.md):
#   * Vector: 9 subtypes x {3D (implicit), 2D, 4D} + plain 2D/4D
#   * Int: Factor / Percentage / Pixel / Unsigned, plus the integer-vector
#     family (Vector[Sub]{2D,3D})
#   * Float: 12 subtypes; String: FilePath; every other base: no variants
#
# Unsigned (Float/Int) and the integer-vector family exist as classes but
# CANNOT be recreated through the Python API: the subtype enum has no
# UNSIGNED value, the Int item has no ``dimensions`` property, and the only
# re-typing path (writing ``bl_socket_idname``) corrupts the item and
# crashes Blender on further use (EXCEPTION_ACCESS_VIOLATION, verified on
# 5.2.0).  They fall back to their base type with an import warning, and
# the canonical hash maps them to that fallback so sync does not report
# phantom divergence.
INTERFACE_BASE_TYPES: tuple[str, ...] = (
    "NodeSocketFloat", "NodeSocketInt", "NodeSocketBool", "NodeSocketVector",
    "NodeSocketColor", "NodeSocketRotation", "NodeSocketMatrix",
    "NodeSocketString", "NodeSocketMenu", "NodeSocketGeometry",
    "NodeSocketObject", "NodeSocketCollection", "NodeSocketImage",
    "NodeSocketMaterial", "NodeSocketFont", "NodeSocketSound",
    "NodeSocketBundle", "NodeSocketClosure",
)

# Only valid for other node-tree kinds (shader/texture/compositor/mask
# editors); never present in a GeometryNodeTree interface.
FOREIGN_SOCKET_TYPES: tuple[str, ...] = (
    "NodeSocketShader", "NodeSocketTexture", "NodeSocketMask",
    "NodeSocketScene", "NodeSocketText",
)

_FLOAT_SUBTYPE_MAP: dict[str, str | None] = {
    "Angle": "ANGLE",
    "ColorTemperature": "COLOR_TEMPERATURE",
    "Distance": "DISTANCE",
    "Factor": "FACTOR",
    "Frequency": "FREQUENCY",
    "Mass": "MASS",
    "Percentage": "PERCENTAGE",
    "Pixel": "PIXEL",
    "Time": "TIME",
    "TimeAbsolute": "TIME_ABSOLUTE",
    "Wavelength": "WAVELENGTH",
    # Exists as a class; no UNSIGNED value in the subtype enum.
    "Unsigned": None,
}

_INT_SUBTYPE_MAP: dict[str, str | None] = {
    "Factor": "FACTOR",
    "Percentage": "PERCENTAGE",
    "Pixel": "PIXEL",
    # Exists as a class; no UNSIGNED value in the subtype enum.
    "Unsigned": None,
}

_STRING_SUBTYPE_MAP: dict[str, str] = {"FilePath": "FILE_PATH"}

VECTOR_SUBTYPE_MAP: dict[str, str] = {
    "Acceleration": "ACCELERATION",
    "Direction": "DIRECTION",
    "Euler": "EULER",
    "Factor": "FACTOR",
    "Percentage": "PERCENTAGE",
    "Pixel": "PIXEL",
    "Translation": "TRANSLATION",
    "Velocity": "VELOCITY",
    "XYZ": "XYZ",
}

NON_RECREATABLE_FALLBACKS: dict[str, str] = {
    "NodeSocketFloatUnsigned": "NodeSocketFloat",
    "NodeSocketIntUnsigned": "NodeSocketInt",
    "NodeSocketIntVector2D": "NodeSocketInt",
    "NodeSocketIntVector3D": "NodeSocketInt",
    "NodeSocketIntVectorFactor2D": "NodeSocketInt",
    "NodeSocketIntVectorFactor3D": "NodeSocketInt",
    "NodeSocketIntVectorPercentage2D": "NodeSocketInt",
    "NodeSocketIntVectorPercentage3D": "NodeSocketInt",
    "NodeSocketIntVectorPixel2D": "NodeSocketInt",
    "NodeSocketIntVectorPixel3D": "NodeSocketInt",
    "NodeSocketIntVectorUnsigned2D": "NodeSocketInt",
    "NodeSocketIntVectorUnsigned3D": "NodeSocketInt",
}


def parse_interface_socket_variant(bl_socket_idname: str):
    """Decompose an interface socket name into creation parameters.

    Returns ``(base_type, dimensions, subtype)`` — ``dimensions`` is 2, 3,
    4 or None, ``subtype`` is an interface subtype string or None — or
    None when the name does not belong to a Geometry-Nodes base type.

    Examples::

        NodeSocketVector         -> ("NodeSocketVector", None, None)
        NodeSocketVector2D       -> ("NodeSocketVector", 2, None)
        NodeSocketVectorFactor2D -> ("NodeSocketVector", 2, "FACTOR")
        NodeSocketFloatAngle     -> ("NodeSocketFloat", None, "ANGLE")
        NodeSocketIntVector2D    -> ("NodeSocketInt", 2, None)
    """
    if not isinstance(bl_socket_idname, str):
        return None
    if bl_socket_idname in FOREIGN_SOCKET_TYPES:
        return None
    base = None
    for candidate in INTERFACE_BASE_TYPES:
        if bl_socket_idname.startswith(candidate):
            base = candidate
            break
    if base is None:
        return None

    suffix = bl_socket_idname[len(base):]
    dimensions = None
    if suffix.endswith("2D"):
        dimensions, suffix = 2, suffix[:-2]
    elif suffix.endswith("3D"):
        dimensions, suffix = 3, suffix[:-2]
    elif suffix.endswith("4D"):
        dimensions, suffix = 4, suffix[:-2]

    if base == "NodeSocketInt" and suffix.startswith("Vector"):
        suffix = suffix[len("Vector"):]
    if not suffix:
        return (base, dimensions, None)

    if base == "NodeSocketFloat":
        subtype = _FLOAT_SUBTYPE_MAP.get(suffix)
    elif base == "NodeSocketInt":
        subtype = _INT_SUBTYPE_MAP.get(suffix)
    elif base == "NodeSocketString":
        subtype = _STRING_SUBTYPE_MAP.get(suffix)
    elif base == "NodeSocketVector":
        subtype = VECTOR_SUBTYPE_MAP.get(suffix)
    else:
        subtype = None
    if subtype is None and suffix not in ("Unsigned",):
        return None
    return (base, dimensions, subtype)

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
    # Panel-toggle / list-field presentation (Blender 5.0+); set directly
    # (never coerced) after the socket is nested in its panel.
    'is_panel_toggle', 'structure_type',
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

# ---------------------------------------------------------------------------
# DNA/RNA Sync — Constants
# ---------------------------------------------------------------------------

# Node properties to exclude from canonical hash computation.
# These are volatile/visual properties that do not affect node functionality.
HASH_EXCLUDE_NODE_PROPS: frozenset[str] = frozenset({
    'location',
    'location_absolute',
    'width',
    # Frame height: layout-only and auto-resized by Blender during
    # rebuilds (5.2 resizes differently), so it cannot round-trip.
    'height',
    'select',
    # Derived from the node's actual sockets; not restored by the roundtrip
    'socket_idname',
})

# Socket fields excluded from the canonical hash: identifiers reorder
# during the roundtrip (cosmetic), bl_idname subtypes are lost on rebuild
# (Float vs FloatAngle, Vector vs VectorXYZ), and 'hide' is UI-only state.
HASH_EXCLUDE_SOCKET_PROPS: frozenset[str] = frozenset({
    'identifier',
    'bl_idname',
    'hide',
})

# Interface item properties excluded from the canonical hash: UI-only
# state that the roundtrip may not restore (kept in serialization, but
# not meaningful for change detection).
HASH_EXCLUDE_INTERFACE_PROPS: frozenset[str] = frozenset({
    'optional_label',
    'menu_expanded',
})

# Node-tree-level properties to exclude from canonical hash computation.
# use_extra_user / use_fake_user (fake user) are file-management flags that
# the roundtrip does not control (Blender may set them on referenced trees
# during operations); they are not content.
HASH_EXCLUDE_TREE_PROPS: frozenset[str] = frozenset({
    'annotation',
    'use_extra_user',
    'use_fake_user',
})

# Version of the canonical hash algorithm. Bump it when the hash
# normalization changes: stored baselines from older algorithms become
# meaningless, and SyncManager._ensure_hash_version() silently re-stamps
# them (preserving any real divergence) instead of reporting a spurious
# "everything changed".
HASH_VERSION: int = 13

# Sidecar file settings
SIDECAR_EXTENSION = ".gntsync"
SIDECAR_TEXT_BLOCK_NAME = "__gnt_sync_metadata__.json"

# Lock file settings for preventing concurrent JSON writes
LOCK_TIMEOUT_SECONDS = 5.0
# Interactive reads retry for less time than writers wait (UI responsiveness)
READ_TIMEOUT_SECONDS = 2.0
