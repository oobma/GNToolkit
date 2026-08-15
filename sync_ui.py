# -*- coding: utf-8 -*-
"""
gn_toolkit.sync_ui — Sidebar panels for DNA/RNA sync (N-panel).

Two panels in the Node Editor sidebar:
  GN_PT_SyncPanel (bl_order=2):  Batch operations and summary
  GN_PT_IssuesPanel (bl_order=3): Scrollable list of all sync issues
"""

from __future__ import annotations

import os

import bpy

from .geometry_validator import (
    IssueType,
    IssueSeverity,
    ValidationIssue,
    format_issue_report,
)
from .importer import restore_zone_area
from .sync_manager import sync_manager, SyncStatus
from .sync_metadata import find_uuid_for_tree, is_ignored, resolve_json_path
from .sync_operators import _get_active_tree


STATUS_ICONS = {
    SyncStatus.SYNCED: 'CHECKMARK',
    SyncStatus.BLEND_MODIFIED: 'LIGHT',
    SyncStatus.JSON_MODIFIED: 'FILE_REFRESH',
    SyncStatus.CONFLICT: 'ERROR',
    SyncStatus.ORPHAN: 'OUTLINER_OB_GROUP_INSTANCE',
    SyncStatus.UNTRACKED: 'PLUS',
    SyncStatus.JSON_MISSING: 'FILE',
}

STATUS_LABELS = {
    SyncStatus.SYNCED: "Synced",
    SyncStatus.BLEND_MODIFIED: "Edited Locally",
    SyncStatus.JSON_MODIFIED: "Changed in JSON",
    SyncStatus.CONFLICT: "Conflict",
    SyncStatus.ORPHAN: "Missing in Blend",
    SyncStatus.UNTRACKED: "Untracked",
    SyncStatus.JSON_MISSING: "JSON File Missing",
}

ISSUE_TYPE_ICONS = {
    IssueType.ATTRIBUTE_MISSING: 'ERROR',
    IssueType.GEOMETRY_DEGENERATE: 'ERROR',
    IssueType.OUTPUT_INVALID: 'SORT_DESC',
    IssueType.GROUP_NOT_FOUND: 'OUTLINER_OB_GROUP_INSTANCE',
    IssueType.INPUT_UNLINKED: 'UNLINKED',
    IssueType.TYPE_MISMATCH: 'COLOR',
}

ISSUE_TYPE_LABELS = {
    IssueType.ATTRIBUTE_MISSING: "Attribute not found",
    IssueType.GEOMETRY_DEGENERATE: "Degenerate geometry",
    IssueType.OUTPUT_INVALID: "Invalid output value",
    IssueType.GROUP_NOT_FOUND: "Group not found",
    IssueType.INPUT_UNLINKED: "Unlinked input",
    IssueType.TYPE_MISMATCH: "Type mismatch",
}

ISSUE_SEVERITY_ICONS = {
    IssueSeverity.ERROR: 'ERROR',
    IssueSeverity.WARNING: 'CANCEL',
    IssueSeverity.INFO: 'INFO',
}


# ---------------------------------------------------------------------------
# JSON remote helpers
# ---------------------------------------------------------------------------

def _json_remotes(tracked: dict) -> dict:
    """Map each distinct resolved JSON path to the number of groups using it."""
    remotes = {}
    blend_dir = sync_manager._blend_dir()
    for info in tracked.values():
        jp = info.get("json_path", "")
        if not jp:
            continue
        abs_path = resolve_json_path(jp, blend_dir)
        remotes[abs_path] = remotes.get(abs_path, 0) + 1
    return remotes


def _active_tracked_json(context):
    """Return (tree_name, json_basename, json_path) for the active tracked
    tree, or None when there is no active tracked tree."""
    tree = _get_active_tree(context)
    if tree is None:
        return None
    uid = find_uuid_for_tree(tree, sync_manager.metadata)
    if not uid:
        return None
    info = sync_manager.metadata.get("tracked_groups", {}).get(uid)
    if not info:
        return None
    jp = info.get("json_path", "")
    if not jp:
        return None
    abs_path = resolve_json_path(jp, sync_manager._blend_dir())
    return tree.name, os.path.basename(abs_path), abs_path


# ---------------------------------------------------------------------------
# Panel 1: Batch Operations
# ---------------------------------------------------------------------------

class GN_PT_SyncPanel(bpy.types.Panel):
    bl_label = "Sync"
    bl_idname = "GN_PT_SyncPanel"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'GN Tools'
    bl_order = 2

    def draw(self, context):
        layout = self.layout
        metadata = sync_manager.metadata
        tracked = metadata.get("tracked_groups", {})

        if tracked:
            active = _active_tracked_json(context)
            if active:
                tree_name, json_base, json_path = active
                active_row = layout.row(align=True)
                active_row.label(text=f"Active: {tree_name} → {json_base}", icon='NODETREE')
                copy_op = active_row.operator("gn.sync_copy_json_path", text="", icon='COPYDOWN')
                copy_op.json_path = json_path
                layout.separator()

        row = layout.row(align=True)
        row.operator("gn.sync_link", text="Track Group", icon='LINKED')
        row.operator("gn.sync_link_all", text="Track All", icon='FILE_TICK')

        if not tracked:
            layout.separator()
            layout.label(text="No groups tracked", icon='INFO')
            layout.separator()
            layout.operator("gn.sync_initialize", text="Track from Existing JSON", icon='FILE_FOLDER')
            layout.label(text="Reads the JSON as the source of truth — it is NOT modified")
            layout.separator()
            layout.label(text="Or create the JSON from this .blend with the buttons above:")
            return

        layout.separator()
        layout.label(text="Sync Actions:", icon='ACTION')

        batch_row = layout.row(align=True)
        batch_row.operator("gn.sync_export_modified", text="Commit Modified", icon='EXPORT')
        batch_row.operator("gn.sync_import_modified", text="Pull from JSON", icon='IMPORT')

        batch_row2 = layout.row(align=True)
        batch_row2.operator("gn.sync_export_all", text="Commit All", icon='EXPORT')

        review_row = layout.row(align=True)
        review_row.operator("gn.sync_commit_review", text="Commit with Review…", icon='ACTION')

        check_row = layout.row(align=True)
        check_row.operator("gn.sync_check", text="Refresh Status", icon='FILE_REFRESH')
        prefs = context.scene.gnt_sync_prefs
        check_row.prop(prefs, "check_on_load", text="", icon='PLUGIN',
                       toggle=True, expand=True)

        import_row = layout.row(align=True)
        import_row.operator("gn.sync_import_group_file", text="Import Group from JSON…",
                            icon='IMPORT')

        state = getattr(context.scene, "gnt_import_state", None)
        if state and state.open:
            layout.separator()
            picker_box = layout.box()
            picker_box.label(text=state.filepath, icon='FILE')
            from .sync_operators import _get_import_package, _filter_group_names, _import_package_cache
            package = _get_import_package(_import_package_cache, state.filepath)
            srow = picker_box.row(align=True)
            srow.label(text=state.search or "Type to search…", icon='VIEWZOOM')
            if state.search:
                srow.operator("gn.sync_import_group_clear_search", text="", icon='X')
            all_groups = sorted(package.get("groups", {}))
            groups = _filter_group_names(all_groups, state.search)
            if state.search:
                picker_box.label(text=f"{len(all_groups)} groups — {len(groups)} match",
                                 icon='FILTER')
            for gname in groups[:40]:
                r = picker_box.row(align=True)
                op = r.operator("gn.sync_import_group", text=gname, icon='NODETREE')
                op.filepath = state.filepath
                op.group_name = gname
                if bpy.data.node_groups.get(gname) is not None:
                    r.label(text="in blend", icon='CHECKMARK')
            if len(groups) > 40:
                picker_box.label(text=f"... {len(groups) - 40} more — refine the search",
                                 icon='INFO')
            if not groups:
                picker_box.label(text="No groups match the search", icon='INFO')
            picker_box.row(align=True).operator("gn.sync_import_group_close",
                                                text="Close Picker", icon='X')

        remotes = _json_remotes(tracked)
        if remotes:
            layout.separator()
            layout.label(text="JSON remotes:", icon='FILE_FOLDER')
            for abs_path, count in sorted(remotes.items()):
                remote_row = layout.row(align=True)
                icon = 'FILE' if os.path.isfile(abs_path) else 'ERROR'
                remote_row.label(text=abs_path, icon=icon)
                remote_row.label(text=f"{count} groups")
                copy_op = remote_row.operator("gn.sync_copy_json_path", text="", icon='COPYDOWN')
                copy_op.json_path = abs_path
                reveal_op = remote_row.operator("gn.sync_reveal_json_path", text="", icon='FOLDER_REDIRECT')
                reveal_op.json_path = abs_path

        has_cache = bool(sync_manager._status_cache)

        load_report = sync_manager.load_report
        if load_report:
            n_changed = sum(1 for e in load_report.values()
                            if e.get("status") == SyncStatus.JSON_MISSING)
            layout.separator()
            warn_row = layout.row(align=True)
            warn_row.label(text=f"{len(load_report)} group(s) out of sync with JSON",
                           icon='FILE_REFRESH')
            if n_changed:
                warn_row.label(text=f"({n_changed} file(s) missing)", icon='FILE')
            warn_row.operator("gn.sync_check", text="", icon='FILE_REFRESH')

        if has_cache:
            layout.separator()
            summary = sync_manager.get_status_summary()
            n_total = sum(v for k, v in summary.items() if k != "ignored")
            n_synced = summary.get("synced", 0)
            n_issues = n_total - n_synced
            n_ignored = summary.get("ignored", 0)

            summary_row = layout.row()
            summary_row.label(text=f"Total: {n_total}  |  ", icon='NODETREE')
            summary_row.label(text=f"Synced: {n_synced}")

            n_blend = summary.get("blend_modified", 0)
            n_json = summary.get("json_modified", 0)
            n_conflict = summary.get("conflict", 0)
            n_orphan = summary.get("orphan", 0)
            n_missing = summary.get("json_missing", 0)

            if n_blend:
                summary_row.label(text=f"  To commit: {n_blend}", icon='LIGHT')
            if n_json:
                summary_row.label(text=f"  To pull: {n_json}", icon='FILE_REFRESH')
            if n_conflict:
                summary_row.label(text=f"  Conflict: {n_conflict}", icon='ERROR')
            if n_orphan:
                summary_row.label(text=f"  Missing: {n_orphan}", icon='GROUP')
            if n_missing:
                summary_row.label(text=f"  JSON file: {n_missing}", icon='FILE')

            if n_issues:
                info_row = layout.row()
                info_row.label(text=f"{n_issues} issues", icon='ERROR')
                if n_ignored:
                    info_row.label(text=f"({n_ignored} ignored)", icon='HIDE_ON')
            else:
                layout.label(text="All synced", icon='CHECKMARK')

        layout.separator()
        stop_row = layout.row(align=True)
        stop_row.operator("gn.sync_unlink_all", text="Stop Tracking All", icon='X')


# ---------------------------------------------------------------------------
# Panel 2: Issues List
# ---------------------------------------------------------------------------

class GN_PT_IssuesPanel(bpy.types.Panel):
    bl_label = "Sync Issues"
    bl_idname = "GN_PT_IssuesPanel"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'GN Tools'
    bl_order = 3

    @classmethod
    def poll(cls, context):
        metadata = sync_manager.metadata
        tracked = metadata.get("tracked_groups", {})
        return bool(tracked)

    def draw_header(self, context):
        self.layout.label(text="", icon='FILE_REFRESH')

    def draw(self, context):
        layout = self.layout
        prefs = context.scene.gnt_sync_prefs

        metadata = sync_manager.metadata
        tracked = metadata.get("tracked_groups", {})

        if not tracked:
            layout.label(text="No groups tracked", icon='INFO')
            return

        has_cache = bool(sync_manager._status_cache)
        load_report = sync_manager.load_report

        if not has_cache and not load_report:
            layout.label(text=f"{len(tracked)} groups tracked", icon='NODETREE')
            layout.label(text="Click 'Refresh Status' to check", icon='INFO')
            layout.operator("gn.sync_check", text="Refresh Status", icon='FILE_REFRESH')
            return

        summary = sync_manager.get_status_summary()
        n_total = sum(v for k, v in summary.items() if k != "ignored")
        n_synced = summary.get("synced", 0)
        n_issues = n_total - n_synced + len(sync_manager.load_report)
        n_ignored = summary.get("ignored", 0)

        header_row = layout.row()
        header_row.label(text=f"{n_issues} issues", icon='ERROR')
        if n_ignored:
            header_row.label(text=f"({n_ignored} ignored)", icon='HIDE_ON')

        layout.separator()

        filters_row1 = layout.row(align=True)
        filters_row1.prop(prefs, "show_blend_modified", text="Edited", toggle=True, icon='LIGHT')
        filters_row1.prop(prefs, "show_json_modified", text="In JSON", toggle=True, icon='FILE_REFRESH')
        filters_row1.prop(prefs, "show_conflict", text="Conflict", toggle=True, icon='ERROR')

        filters_row2 = layout.row(align=True)
        filters_row2.prop(prefs, "show_orphan", text="Missing", toggle=True, icon='GROUP')
        filters_row2.prop(prefs, "show_json_missing", text="JSON File", toggle=True, icon='FILE')
        filters_row2.prop(prefs, "show_ignored", text="Ignored", toggle=True, icon='RESTRICT_VIEW_OFF')

        layout.separator()

        filtered_items = self._build_filtered_list(prefs)

        if not filtered_items:
            layout.label(text="No issues match current filters", icon='INFO')
            return

        for uid, blend_name, status, ignored in filtered_items:
            box = layout.box()
            icon = STATUS_ICONS.get(status, 'QUESTION')
            label = STATUS_LABELS.get(status, status.value)

            header = box.row()
            header.label(text=blend_name, icon=icon)

            if ignored:
                header.label(text=label, icon='HIDE_ON')
                badge = header.row(align=True)
                badge.enabled = False
                badge.label(text="IGNORED", icon='RESTRICT_VIEW_ON')
            else:
                header.label(text=label)

            actions = box.row(align=True)
            actions.alignment = 'RIGHT'

            if ignored:
                actions.operator("gn.sync_unignore", text="Un-ignore", icon='RESTRICT_VIEW_OFF').sync_uuid = uid
            elif status == SyncStatus.BLEND_MODIFIED:
                actions.operator("gn.sync_export", text="Commit", icon='EXPORT').sync_uuid = uid
                actions.operator("gn.sync_ignore", text="Ignore", icon='HIDE_ON').sync_uuid = uid
            elif status == SyncStatus.JSON_MODIFIED:
                actions.operator("gn.sync_import", text="Pull", icon='IMPORT').sync_uuid = uid
                actions.operator("gn.sync_ignore", text="Ignore", icon='HIDE_ON').sync_uuid = uid
            elif status == SyncStatus.CONFLICT:
                actions.operator("gn.sync_resolve_json", text="Keep JSON", icon='FILE_REFRESH').sync_uuid = uid
                actions.operator("gn.sync_resolve_blend", text="Keep Blend", icon='LIGHT').sync_uuid = uid
                actions.operator("gn.sync_ignore", text="Ignore", icon='HIDE_ON').sync_uuid = uid
            elif status == SyncStatus.ORPHAN:
                actions.operator("gn.sync_import", text="Restore from JSON", icon='IMPORT').sync_uuid = uid
                actions.operator("gn.sync_unlink", text="Stop Tracking", icon='X').sync_uuid = uid
            elif status == SyncStatus.JSON_MISSING:
                actions.operator("gn.sync_export", text="Re-create JSON", icon='EXPORT').sync_uuid = uid
                actions.operator("gn.sync_unlink", text="Stop Tracking", icon='X').sync_uuid = uid

        layout.separator()
        layout.operator("gn.sync_check", text="Refresh Status", icon='FILE_REFRESH')

    def _build_filtered_list(self, prefs):
        items = []
        statuses = dict(sync_manager._status_cache)
        for uid, entry in sync_manager.load_report.items():
            statuses.setdefault(uid, entry.get("status"))
        for uid, status in statuses.items():
            if status == SyncStatus.SYNCED:
                continue

            ignored = is_ignored(sync_manager.metadata, uid)
            if ignored and not prefs.show_ignored:
                continue

            info = sync_manager.metadata.get("tracked_groups", {}).get(uid, {})
            blend_name = info.get("blend_name", "?")

            if status == SyncStatus.BLEND_MODIFIED and not prefs.show_blend_modified:
                continue
            if status == SyncStatus.JSON_MODIFIED and not prefs.show_json_modified:
                continue
            if status == SyncStatus.CONFLICT and not prefs.show_conflict:
                continue
            if status == SyncStatus.ORPHAN and not prefs.show_orphan:
                continue
            if status == SyncStatus.JSON_MISSING and not prefs.show_json_missing:
                continue

            items.append((uid, blend_name, status, ignored))

        items.sort(key=lambda x: (x[3], x[2].value))
        return items


# ---------------------------------------------------------------------------
# JSON path utility operators
# ---------------------------------------------------------------------------

class GN_OT_CopyJSONPath(bpy.types.Operator):
    bl_idname = "gn.sync_copy_json_path"
    bl_label = "Copy JSON Path"
    bl_description = "Copy the full JSON file path to the clipboard"
    bl_options = {'REGISTER'}

    json_path: bpy.props.StringProperty()

    def execute(self, context):
        if not self.json_path:
            self.report({'ERROR'}, "No JSON path to copy")
            return {'CANCELLED'}
        context.window_manager.clipboard = self.json_path
        self.report({'INFO'}, "JSON path copied to clipboard")
        return {'FINISHED'}


class GN_OT_RevealJSONPath(bpy.types.Operator):
    bl_idname = "gn.sync_reveal_json_path"
    bl_label = "Reveal JSON in Explorer"
    bl_description = "Open the JSON file's folder in the system file explorer (Windows)"
    bl_options = {'REGISTER'}

    json_path: bpy.props.StringProperty()

    def execute(self, context):
        if not self.json_path:
            return {'CANCELLED'}
        folder = os.path.dirname(self.json_path)
        if not os.path.isdir(folder):
            self.report({'ERROR'}, f"Folder not found: {folder}")
            return {'CANCELLED'}
        try:
            os.startfile(folder)
        except (OSError, AttributeError):
            self.report({'WARNING'}, "Could not open the system file explorer")
            return {'CANCELLED'}
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Conflict resolution operators
# ---------------------------------------------------------------------------

class GN_OT_SyncResolveBlend(bpy.types.Operator):
    bl_idname = "gn.sync_resolve_blend"
    bl_label = "Resolve: Keep Blend"
    bl_description = "Keep the .blend version and overwrite the JSON"
    bl_options = {'REGISTER', 'UNDO'}

    sync_uuid: bpy.props.StringProperty(name="UUID")

    def execute(self, context):
        sync_manager.resolve_conflict(self.sync_uuid, "blend")
        sync_manager.save()
        restore_zone_area()
        self.report({'INFO'}, "Conflict resolved — kept the .blend version")
        return {'FINISHED'}


class GN_OT_SyncResolveJSON(bpy.types.Operator):
    bl_idname = "gn.sync_resolve_json"
    bl_label = "Resolve: Keep JSON"
    bl_description = "Keep the JSON version and overwrite the .blend"
    bl_options = {'REGISTER', 'UNDO'}

    sync_uuid: bpy.props.StringProperty(name="UUID")

    def execute(self, context):
        sync_manager.resolve_conflict(self.sync_uuid, "json")
        sync_manager.save()
        restore_zone_area()
        self.report({'INFO'}, "Conflict resolved — kept the JSON version")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# Preferences PropertyGroup
# ---------------------------------------------------------------------------

class GN_SyncPrefs(bpy.types.PropertyGroup):
    """Persistent preferences for the issues panel.

    NOTE: properties must use the annotation syntax (``name: bpy.props.X``)
    — Blender 4.x/5.x silently ignores the assignment syntax.
    """

    show_blend_modified: bpy.props.BoolProperty(
        name="Show Edited Locally", default=True,
    )
    show_json_modified: bpy.props.BoolProperty(
        name="Show Changed in JSON", default=True,
    )
    show_conflict: bpy.props.BoolProperty(
        name="Show Conflict", default=True,
    )
    show_orphan: bpy.props.BoolProperty(
        name="Show Missing in Blend", default=True,
    )
    show_json_missing: bpy.props.BoolProperty(
        name="Show JSON File Missing", default=True,
    )
    show_ignored: bpy.props.BoolProperty(
        name="Show Ignored", default=False,
    )
    check_on_load: bpy.props.BoolProperty(
        name="Check JSON on open",
        description="After loading a .blend, compare the JSON hashes in the background "
                    "and show a notice when files changed outside Blender",
        default=True,
    )


# ---------------------------------------------------------------------------
# Panel 3: Geometry Validation Issues
# ---------------------------------------------------------------------------

class GN_PT_GeometryIssuesPanel(bpy.types.Panel):
    """Panel showing geometry validation issues for tracked groups."""

    bl_label = "Geometry Validation"
    bl_idname = "GN_PT_GeometryIssuesPanel"
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'GN Tools'
    bl_order = 4
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        metadata = sync_manager.metadata
        tracked = metadata.get("tracked_groups", {})
        return bool(tracked)

    def draw_header(self, context):
        count = sync_manager.get_geometry_issue_count()
        if count:
            self.layout.label(text=f"({count})", icon='ERROR')
        else:
            self.layout.label(text="", icon='CHECKMARK')

    def draw(self, context):
        layout = self.layout
        issues = sync_manager.get_geometry_issues()

        # Button to run validation
        row = layout.row()
        row.operator("gn.validate_geometry", text="Validate", icon='VIEWZOOM')

        if not issues:
            has_cache = sync_manager.has_geometry_issues()
            if has_cache:
                layout.label(text="No geometry issues found", icon='CHECKMARK')
            else:
                layout.label(text="Click 'Validate' to check", icon='INFO')
            return

        layout.separator()
        layout.label(text=f"{len(issues)} issue(s) found", icon='ERROR')
        layout.separator()

        for issue in issues:
            box = layout.box()

            # Header: severity icon + type label
            header = box.row()
            sev_icon = ISSUE_SEVERITY_ICONS.get(issue.severity, 'INFO')
            type_icon = ISSUE_TYPE_ICONS.get(issue.issue_type, 'QUESTION')
            header.label(text="", icon=sev_icon)
            header.label(text=ISSUE_TYPE_LABELS.get(issue.issue_type, issue.issue_type.value))

            # Details
            detail_row = box.row()
            detail_row.label(text=f"Group: {issue.tree_name}")
            detail_row = box.row()
            detail_row.label(text=f"Node: {issue.node_name}")
            if issue.socket_name:
                detail_row = box.row()
                detail_row.label(text=f"Socket: {issue.socket_name}")

            # Problem description
            box.label(text=f"Problem: {issue.details}")

            # Recommendation
            rec_row = box.row()
            rec_row.label(text=f"Recommendation: {issue.recommendation}", icon='INFO')

            # Available options (if any)
            if issue.available_options:
                opts = ", ".join(issue.available_options[:5])
                if len(issue.available_options) > 5:
                    opts += f" ... (+{len(issue.available_options) - 5})"
                opt_row = box.row()
                opt_row.label(text=f"Options: {opts}")


# ---------------------------------------------------------------------------
# Geometry validation operator
# ---------------------------------------------------------------------------

class GN_OT_ValidateGeometry(bpy.types.Operator):
    """Run geometry validation on all tracked groups."""

    bl_idname = "gn.validate_geometry"
    bl_label = "Validate Geometry"
    bl_description = "Check all tracked groups for geometry issues"
    bl_options = {'REGISTER'}

    def execute(self, context):
        sync_manager.invalidate_geometry_cache()
        issues_by_uuid = sync_manager.validate_geometry()

        total = sum(len(issues) for issues in issues_by_uuid.values())
        if total:
            # Build report
            all_issues = []
            for issues in issues_by_uuid.values():
                all_issues.extend(issues)
            report = format_issue_report(all_issues)
            self.report({'WARNING'}, f"Validation found {total} issue(s). See console for details.")
            print(report)
        else:
            self.report({'INFO'}, "No geometry issues found")

        return {'FINISHED'}


classes = (
    GN_PT_SyncPanel,
    GN_PT_IssuesPanel,
    GN_PT_GeometryIssuesPanel,
    GN_OT_SyncResolveBlend,
    GN_OT_SyncResolveJSON,
    GN_OT_CopyJSONPath,
    GN_OT_RevealJSONPath,
    GN_OT_ValidateGeometry,
    GN_SyncPrefs,
)
