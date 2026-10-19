# -*- coding: utf-8 -*-
"""
gn_toolkit.constants — All constants and immutable sets for the addon.
"""

from enum import Enum

# Canonical addon version — single source of truth.
# Reflected in bl_info (__init__.py), exported JSON files, the sidecar
# metadata file, and UI labels.
ADDON_VERSION = "0.4.1"

# Marker written to unified JSON packages. Written for user inspection
# only; importers never read it.
PACKAGE_EXPORT_METHOD = "GN_TOOLKIT"

# Node-tree types the add-on versions (multi-editor scope A, shader
# first).  ``NodeTree.type`` strings: GEOMETRY is the primary target and
# SHADER the first additional editor; the compositor and embedded trees
# (materials/worlds) are out of scope so far.
TRACKABLE_TREE_TYPES: tuple[str, ...] = ("GEOMETRY", "SHADER")


def is_trackable_tree_type(tree_type) -> bool:
    """True when a ``NodeTree.type`` string is versioned by the add-on."""
    return tree_type in TRACKABLE_TREE_TYPES


def is_group_node_bl_idname(bl_idname) -> bool:
    """True for group nodes of any tree kind.

    GeometryNodeGroup / ShaderNodeGroup / CompositorNodeGroup all follow
    the ``<Kind>NodeGroup`` naming; Group Input/Output nodes do not match
    (they end with GroupInput/GroupOutput).
    """
    return bool(bl_idname) and str(bl_idname).endswith("NodeGroup")


# Native (built-in) group-node bl_idnames.  These reference shared,
# first-class node-tree datablocks and travel as a name-based
# ``node_tree_reference``.  Any OTHER node that owns a ``node_tree`` is a
# third-party custom group class (a ``ShaderNodeCustomGroup`` subclass or
# similar): its inner tree is PER-INSTANCE content (each node owns its
# own), serialized embedded as ``inner_tree`` (decision 2026-10-15) so
# two instances with divergent edits round-trip exactly.
BUILTIN_GROUP_BL_IDNAMES: frozenset[str] = frozenset({
    "GeometryNodeGroup", "ShaderNodeGroup", "CompositorNodeGroup",
})


class SyncStatus(Enum):
    """Sync state of a tracked entry.

    Shared vocabulary (pure module): the add-on and the headless checker
    import it from here so their verdicts cannot drift apart.
    """
    SYNCED = "synced"
    BLEND_MODIFIED = "blend_modified"
    JSON_MODIFIED = "json_modified"
    CONFLICT = "conflict"
    ORPHAN = "orphan"
    UNTRACKED = "untracked"
    JSON_MISSING = "json_missing"


def hashes_differ(current, baseline) -> bool:
    """True when *current* differs from *baseline*.

    A missing baseline (None or empty string) counts as changed: there is
    no stored hash to compare against (first tracking).
    """
    return (current != baseline) if baseline else True


def classify_sync_status(json_hash, last_json_hash, blend_hash=None,
                         last_blend_hash=None) -> SyncStatus:
    """The shared sync-status rule: stored vs current hashes -> status.

    With both blend arguments omitted (None) only the JSON side is judged
    (the headless checker has no .blend): SYNCED or JSON_MODIFIED.
    """
    json_changed = hashes_differ(json_hash, last_json_hash)
    if blend_hash is None and last_blend_hash is None:
        return SyncStatus.JSON_MODIFIED if json_changed else SyncStatus.SYNCED
    blend_changed = hashes_differ(blend_hash, last_blend_hash)
    if not json_changed and not blend_changed:
        return SyncStatus.SYNCED
    if json_changed and not blend_changed:
        return SyncStatus.JSON_MODIFIED
    if not json_changed and blend_changed:
        return SyncStatus.BLEND_MODIFIED
    return SyncStatus.CONFLICT

# Node types whose socket identifiers are volatile (recycled by Blender at runtime).
_VOLATILE_TYPES: frozenset[str] = frozenset({
    "GeometryNodeRepeatInput", "GeometryNodeRepeatOutput",
    "GeometryNodeSimulationInput", "GeometryNodeSimulationOutput",
    "GeometryNodeForeachGeometryElementInput", "GeometryNodeForeachGeometryElementOutput",
    "NodeClosureInput", "NodeClosureOutput", "NodeEvaluateClosure",
    "GeometryNodeViewer", "NodeCombineBundle", "NodeSeparateBundle",
    "GeometryNodeCombineBundle", "GeometryNodeSeparateBundle",
    "GeometryNodeCaptureAttribute", "GeometryNodeMenuSwitch", "GeometryNodeIndexSwitch",
    # Bake node outputs are generated from its item list in creation order:
    # the serialized Item_N ids are positional and get reassigned on the
    # rebuild, so matching by id silently rewires to a neighbouring socket
    # (real 5.1-splash case: Group ID / Rest Position swapped).
    "GeometryNodeBake",
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
        "bytecode": "Derived compile cache of the Script node (OSL): the "
                    "compiled listing embeds per-session absolute temp "
                    "paths, so hashing it makes identical content differ "
                    "between processes; regenerated on load.",
        "bytecode_hash": "Derived compile cache of the Script node (same "
                         "as bytecode).",
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
    "ImageUser": "Readonly embedded struct of texture nodes; the sequence "
                 "playback settings (frame offset/duration/start, cyclic, "
                 "auto refresh, tile) are semantic; frame_current is "
                 "runtime and skipped by the serializer.",
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
    # Class-level UI text of the tree TYPE: the tooltip/label/icon
    # strings differ between Blender versions (the ShaderNodeTree
    # bl_description changed in 5.x, measured 2026-10-13) and are not
    # content; bl_idname is redundant with tree_type, which carries the
    # identity.  (bl_use_group_interface, despite the prefix, is a real
    # setting: it stays hashed, folding only at its default via
    # CROSS_VERSION_DEFAULTS.)
    'bl_description',
    'bl_label',
    'bl_icon',
    'bl_idname',
    # Per-session flag: Blender reports True for trees created at runtime
    # (e.g. the tree a material/world gets when nodes are first enabled)
    # and False once the datablock was saved to a file — hashing it made
    # every freshly tracked embedded tree report "modified" after the
    # first save (measured 2026-10-14).
    'is_runtime_data',
})

# "Absent ≡ default" normalizations for properties that exist in only
# some Blender versions (or whose default is version-specific), so the
# same group hashes identically whether it was serialized by 4.2 or by
# 5.x.  Only the version's DEFAULT is folded: a non-default value stays a
# difference (it is real content).  Keys are (scope, property); the
# scopes match the canonicalizer sections.  Rationale per entry
# (probed on 5.2, measured on the 24-group shader corpus, 2026-10-13):
#   interface.is_panel_toggle / interface.structure_type: 5.x-only
#     interface properties (absent in 4.2 exports), defaults
#     False / 'AUTO'.
#   node.warning_propagation: 5.x-only node property, default 'ALL'.
#   node.base / node.convention: Normal Map properties added in 5.x,
#     defaults 'DISPLACED' / 'OPENGL'.
#   tree.bl_use_group_interface: 5.x-only tree property, default True.
#   tree.default_group_node_width: 5.x-only tree property, default 140.
#   tree.grease_pencil: annotation pointer removed in 5.x (None in 4.2
#     exports); None equals absent.
CROSS_VERSION_DEFAULTS: dict = {
    ("interface", "is_panel_toggle"): False,
    ("interface", "structure_type"): "AUTO",
    ("node", "warning_propagation"): "ALL",
    ("node", "base"): "DISPLACED",
    ("node", "convention"): "OPENGL",
    ("tree", "bl_use_group_interface"): True,
    ("tree", "default_group_node_width"): 140,
    ("tree", "grease_pencil"): None,
    # Owner settings (materials): shadow_method exists only up to 4.2
    # (EEVEE Next removed it); default 'OPAQUE' (probed 4.2).
    ("owner", "shadow_method"): "OPAQUE",
    # default_input: 'VALUE' is the enum default for the socket types
    # that expose it; legacy files (5.1-splash) stored an invalid value
    # that Blender sanitizes to '' on read, which older exports wrote to
    # JSON.  Both are unrepresentable/equivalent to absent.
    ("interface", "default_input"): ("VALUE", ""),
    # color_tag: same invalid-enum artifact in legacy files ('16' → ''),
    # default 'NONE'.
    ("tree", "color_tag"): ("NONE", ""),
}

# ---------------------------------------------------------------------------
# Embedded trees (owner-aware tracking: materials, worlds)
# ---------------------------------------------------------------------------

# kind -> section name in packages and subfolder name in folder exports.
EMBEDDED_KINDS: dict[str, str] = {
    "material": "materials",
    "world": "worlds",
}

# Curated owner settings versioned next to the tree (approved 2026-10-14).
# Allowlist: only these properties are serialized; non-default values are
# content, and version-only properties fold to absent at their default
# (see CROSS_VERSION_DEFAULTS, scope "owner").
MATERIAL_SETTINGS_PROPS: tuple[str, ...] = (
    # Mixing / transparency
    "blend_method", "surface_render_method", "shadow_method",
    "alpha_threshold", "use_transparency_overlap", "use_transparent_shadow",
    "show_transparent_back",
    # Culling / shadows
    "use_backface_culling", "use_backface_culling_shadow",
    "use_backface_culling_lightprobe_volume",
    # Refraction / SSS / volume
    "use_screen_refraction", "use_raytrace_refraction", "refraction_depth",
    "use_sss_translucency", "use_thickness_from_shadow",
    "volume_intersection_method",
    # Displacement / thickness
    "displacement_method", "max_vertex_displacement", "thickness_mode",
    # Viewport display
    "diffuse_color", "metallic", "roughness", "specular_color",
    "specular_intensity",
    # Freestyle / render
    "line_color", "line_priority", "pass_index",
)

WORLD_SETTINGS_PROPS: tuple[str, ...] = (
    "color", "lightgroup", "use_eevee_finite_volume",
    "sun_angle", "sun_threshold", "use_sun_shadow",
    "sun_shadow_filter_radius", "sun_shadow_maximum_resolution",
    "sun_shadow_jitter_overblur", "use_sun_shadow_jitter",
)

# Version of the canonical hash algorithm. Bump it when the hash
# normalization changes: stored baselines from older algorithms become
# meaningless, and SyncManager._ensure_hash_version() silently re-stamps
# them (preserving any real divergence) instead of reporting a spurious
# "everything changed".
# 17: the tree type (``tree_type``, absent = GeometryNodeTree) is part
# of the canonical content — identity is segmented by tree type like
# Blender segments node-tree datablocks.
# 18: ImageUser playback settings (frame offset/duration/start, cyclic,
# auto refresh) of texture nodes are versioned content.
# 19: version-gated properties fold to absent when they carry their
# default (CROSS_VERSION_DEFAULTS), so 4.2 and 5.x sessions hash the
# same group identically.
# 20: default_input folds both 'VALUE' (its enum default) and the ''
# sanitized from invalid legacy values — the last false-change class
# found on the real 5.1-splash project.
# 21: color_tag folds ('NONE'/''), and homonymous interface INPUT/OUTPUT
# items sort by (name, direction) because the roundtrip may recreate
# them swapped (Blender inserts a same-named output before its input).
# 22: custom group nodes embed their per-instance inner tree
# (``inner_tree``) instead of a name reference, and referenced Text
# datablocks (OSL scripts) are versioned under ``texts``; the Script
# node's compiled ``bytecode``/``bytecode_hash`` are no longer stored.
# 23: interface menu ``enum_items`` are excluded from the hash: they are
# derived from the wiring (a Menu Switch behind the Group Input) and the
# serializer captured them only opportunistically, so source and rebuilt
# trees disagreed without any content change (external library, 2026-10-19).
HASH_VERSION: int = 23

# Sidecar file settings
SIDECAR_EXTENSION = ".gntsync"
SIDECAR_TEXT_BLOCK_NAME = "__gnt_sync_metadata__.json"

# Lock file settings for preventing concurrent JSON writes
LOCK_TIMEOUT_SECONDS = 5.0
# Interactive reads retry for less time than writers wait (UI responsiveness)
READ_TIMEOUT_SECONDS = 2.0
