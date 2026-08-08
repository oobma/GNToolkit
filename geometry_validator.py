# -*- coding: utf-8 -*-
"""
gn_toolkit.geometry_validator — Generic geometry and node-tree validation.

Detects issues in any Geometry Nodes tree without hardcoding specific
attribute names, node names, or group structures.  Issues are reported
with actionable recommendations so the user can resolve them manually.

Never blocks synchronization — only warns.
"""

from __future__ import annotations

from enum import Enum
from dataclasses import dataclass, field
from typing import Optional

import bpy


# ---------------------------------------------------------------------------
# Issue types and severity
# ---------------------------------------------------------------------------

class IssueType(Enum):
    """Generic issue types detectable in any Geometry Nodes tree."""

    ATTRIBUTE_MISSING = "attribute_missing"
    """Named Attribute node references an attribute that doesn't exist on the target mesh."""

    GEOMETRY_DEGENERATE = "geometry_degenerate"
    """A geometry output produces < 2 vertices or otherwise invalid geometry."""

    OUTPUT_INVALID = "output_invalid"
    """A numeric output socket has a value that should be > 0 but is 0 or negative."""

    GROUP_NOT_FOUND = "group_not_found"
    """A Group node references a node tree that doesn't exist in bpy.data.node_groups."""

    INPUT_UNLINKED = "input_unlinked"
    """A socket that typically requires a connection has no link and no meaningful default."""

    TYPE_MISMATCH = "type_mismatch"
    """A socket's data type doesn't match what the connected source provides."""


class IssueSeverity(Enum):
    """How serious an issue is."""

    ERROR = "error"
    """Will likely cause incorrect results or failure."""

    WARNING = "warning"
    """May cause suboptimal results but might be intentional."""

    INFO = "info"
    """Informational — worth noting but unlikely to cause problems."""


# ---------------------------------------------------------------------------
# Issue dataclass
# ---------------------------------------------------------------------------

@dataclass
class ValidationIssue:
    """A single validation issue found in a node tree."""

    issue_type: IssueType
    severity: IssueSeverity
    node_name: str
    node_idname: str
    socket_name: str = ""
    details: str = ""
    recommendation: str = ""
    available_options: list[str] = field(default_factory=list)
    tree_name: str = ""
    """Name of the node tree where the issue was found."""

    def summary(self) -> str:
        """One-line summary for UI display."""
        parts = [self.node_name]
        if self.socket_name:
            parts.append(f"({self.socket_name})")
        parts.append(f"→ {self.details}")
        return " ".join(parts)


# ---------------------------------------------------------------------------
# Socket type helpers
# ---------------------------------------------------------------------------

_SOCKET_TYPE_LABELS: dict[str, str] = {
    "NodeSocketFloat": "Float",
    "NodeSocketInt": "Integer",
    "NodeSocketBool": "Boolean",
    "NodeSocketVector": "Vector",
    "NodeSocketColor": "Color",
    "NodeSocketGeometry": "Geometry",
    "NodeSocketObject": "Object",
    "NodeSocketImage": "Image",
    "NodeSocketMaterial": "Material",
    "NodeSocketCollection": "Collection",
    "NodeSocketString": "String",
    "NodeSocketRotationEuler": "Euler",
    "NodeSocketRotationQuaternion": "Quaternion",
}


def socket_type_label(socket) -> str:
    """Return a human-readable label for a socket's type."""
    bl_id = getattr(socket, "bl_idname", "")
    return _SOCKET_TYPE_LABELS.get(bl_id, bl_id.replace("NodeSocket", ""))


def is_geometry_socket(socket) -> bool:
    """Check if a socket is a geometry type."""
    bl_id = getattr(socket, "bl_idname", "")
    return bl_id == "NodeSocketGeometry"


def is_numeric_socket(socket) -> bool:
    """Check if a socket outputs a numeric value (float/int)."""
    bl_id = getattr(socket, "bl_idname", "")
    return bl_id in ("NodeSocketFloat", "NodeSocketInt")


# ---------------------------------------------------------------------------
# Validation functions — each detects one generic issue type
# ---------------------------------------------------------------------------

def _collect_written_attributes(node_tree, _visited=None) -> set:
    """Collect all attribute names that are written/created in this tree
    and its nested groups."""
    if _visited is None:
        _visited = set()

    if node_tree.name in _visited:
        return set()
    _visited.add(node_tree.name)

    written = set()

    # Node types that write attributes
    _WRITER_NODES = {
        "GeometryNodeStoreNamedAttribute": "data_name",
        "GeometryNodeCaptureAttribute": None,  # Uses capture_items
        "GeometryNodeSetMaterial": None,
    }

    for node in node_tree.nodes:
        if node.bl_idname == "GeometryNodeStoreNamedAttribute":
            name_socket = node.inputs.get("Name")
            if name_socket and not name_socket.is_linked:
                attr_name = getattr(name_socket, "default_value", "")
                if attr_name:
                    written.add(attr_name)
        elif node.bl_idname == "GeometryNodeGroup" and node.node_tree:
            written |= _collect_written_attributes(node.node_tree, _visited)

    return written


def _check_attribute_references(node_tree, mesh, issues: list[ValidationIssue], _visited=None) -> None:
    """Detect Named Attribute nodes referencing attributes not on the mesh
    and not created by any node in the tree."""
    if mesh is None:
        return

    if _visited is None:
        _visited = set()

    mesh_attr_names = {attr.name for attr in mesh.attributes}

    # Collect attributes written by this tree and nested groups
    written_attrs = _collect_written_attributes(node_tree, set())

    # All known attributes = mesh attributes + written attributes
    known_attrs = mesh_attr_names | written_attrs

    def _check_tree(tree, prefix=""):
        if tree.name in _visited:
            return
        _visited.add(tree.name)

        for node in tree.nodes:
            node_label = f"{prefix}{node.name}" if prefix else node.name

            if node.bl_idname == "GeometryNodeInputNamedAttribute":
                name_socket = node.inputs.get("Name")
                if name_socket is None:
                    continue

                attr_name = ""
                if name_socket.is_linked:
                    continue  # Can't determine statically
                else:
                    attr_name = getattr(name_socket, "default_value", "")

                if not attr_name:
                    continue

                if attr_name not in known_attrs:
                    available = sorted(mesh_attr_names)
                    suggestion = _find_closest_attribute(attr_name, available)

                    detail_msg = f"Attribute '{attr_name}' does not exist on mesh '{mesh.name}' and is not created by any node in the tree"
                    rec_msg = f"Connect the 'Name' socket to a valid value or use one of the available attributes"

                    if suggestion:
                        rec_msg += f" (suggestion: '{suggestion}')"

                    issues.append(ValidationIssue(
                        issue_type=IssueType.ATTRIBUTE_MISSING,
                        severity=IssueSeverity.ERROR,
                        node_name=node_label,
                        node_idname=node.bl_idname,
                        socket_name="Name",
                        details=detail_msg,
                        recommendation=rec_msg,
                        available_options=available,
                        tree_name=tree.name,
                    ))

            elif node.bl_idname == "GeometryNodeGroup" and node.node_tree:
                _check_tree(node.node_tree, prefix=f"{node.name}/")

    _check_tree(node_tree)


def _find_closest_attribute(target: str, available: list[str]) -> Optional[str]:
    """Find the closest attribute name to the target (case-insensitive)."""
    target_lower = target.lower()

    # Exact case-insensitive match
    for attr in available:
        if attr.lower() == target_lower:
            return attr

    # Contains match
    for attr in available:
        attr_lower = attr.lower()
        if target_lower in attr_lower or attr_lower in target_lower:
            return attr

    # Common attribute name heuristics
    common_names = {
        "trim contour": ["trim", "contour", "trim_contour", "TrimContour"],
        "degree": ["order", "degree", "control_point_degree"],
        "position": ["pos", "coord", "location", "vertices"],
        "normal": ["normals", "face_normal", "vertex_normal"],
        "uv": ["uvmap", "uv_map", "UVMap", "texcoord"],
    }

    for key, aliases in common_names.items():
        if target_lower == key or target_lower in aliases:
            for attr in available:
                attr_lower = attr.lower()
                if any(alias.lower() in attr_lower for alias in aliases):
                    return attr

    return None


def _check_group_references(node_tree, issues: list[ValidationIssue]) -> None:
    """Detect Group nodes referencing node trees that don't exist."""
    existing_trees = {ng.name for ng in bpy.data.node_groups}

    for node in node_tree.nodes:
        if node.bl_idname != "GeometryNodeGroup":
            continue

        if node.node_tree is None:
            # The node_tree reference is None — group is missing
            # Try to find what it was supposed to reference
            issues.append(ValidationIssue(
                issue_type=IssueType.GROUP_NOT_FOUND,
                severity=IssueSeverity.ERROR,
                node_name=node.name,
                node_idname=node.bl_idname,
                details="Referenced node group does not exist in bpy.data.node_groups",
                recommendation=f"Select a valid group from: {sorted(existing_trees)[:5]}...",
                available_options=sorted(existing_trees),
                tree_name=node_tree.name,
            ))


def _check_unlinked_inputs(node_tree, issues: list[ValidationIssue]) -> None:
    """Detect sockets that typically need connections but aren't linked."""
    # Node types where certain inputs are critical.
    # Only includes built-in operation nodes, NOT group nodes.
    # Group node inputs are designed to be connected externally and
    # checking them produces false positives.
    _CRITICAL_INPUTS: dict[str, list[str]] = {
        "GeometryNodeSetPosition": ["Geometry"],
        "GeometryNodeTransform": ["Geometry"],
        "GeometryNodeSeparateGeometry": ["Geometry", "Selection"],
        "GeometryNodeDuplicateElements": ["Geometry"],
        "GeometryNodeDeleteGeometry": ["Geometry", "Selection"],
        "GeometryNodeSubdivideMesh": ["Geometry"],
        "GeometryNodeExtrudeMesh": ["Geometry"],
        "GeometryNodeMergeByDistance": ["Geometry"],
        "GeometryNodeSplitToInstances": ["Geometry"],
        "GeometryNodeCurveToMesh": ["Curve", "Fill Caps"],
        "GeometryNodeCurveToPoints": ["Curve"],
        "GeometryNodeMeshToPoints": ["Mesh"],
        "GeometryNodePointsToCurves": ["Points"],
        "GeometryNodePointsToVertices": ["Points"],
        "GeometryNodeInstanceOnPoints": ["Points", "Instance"],
        "GeometryNodeInstanceRealizeInstances": ["Geometry"],
        "GeometryNodeJoinGeometry": ["Geometry"],
        "GeometryNodeScaleElements": ["Geometry"],
        "GeometryNodeTranslateInstances": ["Geometry"],
        "GeometryNodeRotateInstances": ["Geometry"],
    }

    for node in node_tree.nodes:
        # Skip Group Input nodes — their inputs are the group's interface
        # sockets, which are by definition unlinked inside the tree.
        # They receive data from outside when the group is used.
        if node.bl_idname == "NodeGroupInput":
            continue

        critical = _CRITICAL_INPUTS.get(node.bl_idname, [])
        for inp in node.inputs:
            if inp.name in critical and not inp.is_linked:
                # Check if it has a meaningful default
                has_default = False
                try:
                    default = inp.default_value
                    # Geometry sockets have None as default, which is meaningless
                    if default is not None:
                        has_default = True
                    elif is_geometry_socket(inp):
                        has_default = False
                except (AttributeError, TypeError):
                    has_default = False

                if not has_default:
                    issues.append(ValidationIssue(
                        issue_type=IssueType.INPUT_UNLINKED,
                        severity=IssueSeverity.WARNING,
                        node_name=node.name,
                        node_idname=node.bl_idname,
                        socket_name=inp.name,
                        details=f"Input '{inp.name}' is not connected and has no default value",
                        recommendation=f"Connect to a {socket_type_label(inp)} source",
                        tree_name=node_tree.name,
                    ))


def _check_numeric_outputs(node_tree, issues: list[ValidationIssue]) -> None:
    """Detect interface outputs with invalid numeric values (0 when should be > 0).

    NOTE: This check is intentionally minimal. Interface output default values
    are meaningless placeholders — the actual values come from connected nodes
    at runtime.  A default of 0 on a Count output is normal and expected.

    This function is kept as a stub for future enhancement but currently
    does not report any issues to avoid false positives.
    """
    pass


# ---------------------------------------------------------------------------
# Main validation entry point
# ---------------------------------------------------------------------------

def get_mesh_from_node_tree(node_tree) -> Optional[bpy.types.Mesh]:
    """Find the mesh used by objects that have this node tree as a modifier.

    Returns the first mesh found, or None if no object uses this tree.
    """
    for obj in bpy.data.objects:
        for mod in obj.modifiers:
            if mod.type == 'NODES' and mod.node_group == node_tree:
                if obj.type == 'MESH' and obj.data:
                    return obj.data
    return None


def validate_node_tree(node_tree, mesh=None, _visited=None, _is_top_level=True) -> list[ValidationIssue]:
    """Run all generic validators on a node tree and its nested groups.

    Args:
        node_tree: The Geometry Nodes tree to validate.
        mesh: Optional mesh to check attribute references against.
              If None, tries to find mesh from objects using this tree.
        _visited: Internal set to avoid infinite recursion.
        _is_top_level: If True, run all checks. If False (nested group),
                      skip checks that produce false positives on nested groups.

    Returns:
        List of ValidationIssue objects.
    """
    if _visited is None:
        _visited = set()

    if node_tree.name in _visited:
        return []
    _visited.add(node_tree.name)

    issues: list[ValidationIssue] = []

    if mesh is None:
        mesh = get_mesh_from_node_tree(node_tree)

    # Check attribute references (smart: accounts for attributes created by nodes)
    _check_attribute_references(node_tree, mesh, issues, _visited.copy())

    _check_group_references(node_tree, issues)
    _check_unlinked_inputs(node_tree, issues)

    # Only check numeric outputs on the top-level tree.
    # Nested group output defaults are meaningless placeholders
    # that get overridden by actual connections at runtime.
    if _is_top_level:
        _check_numeric_outputs(node_tree, issues)

    # Recursively validate nested groups
    for node in node_tree.nodes:
        if node.bl_idname == 'GeometryNodeGroup' and node.node_tree:
            nested_issues = validate_node_tree(node.node_tree, mesh, _visited, _is_top_level=False)
            # Prefix node names with parent group for clarity
            for issue in nested_issues:
                issue.node_name = f"{node.name}/{issue.node_name}"
            issues.extend(nested_issues)

    return issues


def validate_all_tracked_trees() -> dict[str, list[ValidationIssue]]:
    """Validate all tracked node trees via the sync manager.

    Returns:
        Dict mapping UUID to list of issues found.
    """
    from .sync_manager import sync_manager

    all_issues: dict[str, list[ValidationIssue]] = {}

    for uuid_str, info in sync_manager.metadata.get("tracked_groups", {}).items():
        blend_name = info.get("blend_name", "")
        tree = bpy.data.node_groups.get(blend_name)
        if tree is None:
            # Try to find by UUID property
            for ng in bpy.data.node_groups:
                if ng.get("gnt_sync_id") == uuid_str:
                    tree = ng
                    break

        if tree is None:
            continue

        mesh = get_mesh_from_node_tree(tree)
        issues = validate_node_tree(tree, mesh)

        if issues:
            all_issues[uuid_str] = issues

    return all_issues


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def format_issue_report(issues: list[ValidationIssue]) -> str:
    """Format issues as a human-readable report."""
    if not issues:
        return "No issues found."

    lines = []
    lines.append("=" * 65)
    lines.append(f"VALIDATION COMPLETED WITH WARNINGS ({len(issues)} issues)")
    lines.append("=" * 65)
    lines.append("")

    for i, issue in enumerate(issues, 1):
        icon = "ERROR" if issue.severity == IssueSeverity.ERROR else "WARN"
        lines.append(f"[{i}] [{icon}] {issue.issue_type.value.upper()}")
        lines.append(f"    Group: {issue.tree_name}")
        lines.append(f"    Node: {issue.node_name}")
        if issue.socket_name:
            lines.append(f"    Socket: {issue.socket_name}")
        lines.append(f"    Problem: {issue.details}")
        if issue.recommendation:
            lines.append(f"    Recommendation: {issue.recommendation}")
        if issue.available_options:
            opts = ", ".join(issue.available_options[:5])
            if len(issue.available_options) > 5:
                opts += f" ... (+{len(issue.available_options) - 5} more)"
            lines.append(f"    Available options: {opts}")
        lines.append("")

    lines.append("=" * 65)
    return "\n".join(lines)


def format_issue_summary(issues: list[ValidationIssue]) -> str:
    """Short one-line summary of issues."""
    if not issues:
        return "No issues"

    errors = sum(1 for i in issues if i.severity == IssueSeverity.ERROR)
    warnings = sum(1 for i in issues if i.severity == IssueSeverity.WARNING)
    infos = sum(1 for i in issues if i.severity == IssueSeverity.INFO)

    parts = []
    if errors:
        parts.append(f"{errors} error" + ("es" if errors != 1 else ""))
    if warnings:
        parts.append(f"{warnings} warning" + ("s" if warnings != 1 else ""))
    if infos:
        parts.append(f"{infos} info")

    return ", ".join(parts)
